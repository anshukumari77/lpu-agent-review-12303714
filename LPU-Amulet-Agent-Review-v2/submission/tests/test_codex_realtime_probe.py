"""Offline safety tests; these never start Codex or contact a provider."""
import importlib.util
from pathlib import Path


def probe_module():
    path = Path(__file__).parents[1] / "scripts/probe_codex_realtime.py"
    assert path.is_file(), "bounded realtime probe helper not implemented"
    spec = importlib.util.spec_from_file_location("probe_codex_realtime", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_classification_does_not_confuse_api_key_route_with_bad_oauth():
    probe = probe_module()
    assert probe.classify_error("realtime conversation requires API key auth") == "transport_requires_api_key"
    assert probe.classify_error("HTTP 401 Unauthorized") == "failed_authentication"
    assert probe.classify_error('HTTP 403 Forbidden {"code":"not_supported"}') == "unsupported_account_or_access_denied"
    assert probe.classify_error("WebSocket connection failed") == "technical_failure"
    assert probe.classify_error('{"code":"invalid_quicksilver_alpha_header"}') == "protocol_incompatibility"


def test_process_boundary_and_start_payload_never_inherit_keys():
    probe = probe_module()
    env = probe.minimal_env({"HOME": "/tmp/home", "PATH": "/bin", "OPENAI_API_KEY": "secret", "CODEX_HOME": "/other", "ANTHROPIC_API_KEY": "secret"})
    assert env == {"HOME": "/tmp/home", "PATH": "/bin", "LANG": "en_US.UTF-8", "RUST_LOG": "off"}
    params = probe.thread_params("/tmp/isolated")
    assert params["ephemeral"] is True
    assert params["environments"] == params["dynamicTools"] == []
    assert params["sandbox"] == "read-only"
    start = probe.realtime_params("thread-test", {"type": "websocket"})
    assert start["clientManagedHandoffs"] is True
    assert start["includeStartupContext"] is False
    assert start["flushTranscriptTailOnSessionEnd"] is False
    assert start["outputModality"] == "audio"
    assert start["voice"] == "cove"  # 0.153.2 builtin v1 default; marin is v2-only
    assert start["realtimeStartInstructions"] == start["realtimeEndInstructions"] == ""


def test_error_allowlist_preserves_codes_but_not_secrets_or_identity():
    probe = probe_module()
    result = probe.safe_error('HTTP 403 Forbidden {"code":"unsupported_plan", "email":"private@example.com", "access_token":"secret-value"}', -32600)
    assert result["http_status"] == 403
    assert result["rpc_code"] == -32600
    assert result["provider_code"] == "unsupported_plan"
    assert "secret-value" not in str(result) and "private@example.com" not in str(result)


def test_evidence_requires_real_audio_not_start_ack_and_ignores_user_echo():
    probe = probe_module()
    evidence = probe.Evidence()
    assert evidence.outcome() == "technical_failure"  # no session was connected
    evidence.observe({"method": "thread/realtime/started", "params": {"version": "v1"}})
    evidence.observe({"method": "thread/realtime/transcript/done", "params": {"role": "user", "text": "Voice probe complete."}})
    assert evidence.outcome() == "no_audio_response"
    assert evidence.summary()["phrase_verified"] is False
    evidence.observe({"method": "thread/realtime/outputAudio/delta", "params": {"audio": {"data": "AQACAAMA", "sampleRate": 24000, "numChannels": 1}}})
    assert evidence.outcome() == "audio_received_transcript_unverified"
    evidence.observe({"method": "thread/realtime/transcript/done", "params": {"role": "assistant", "text": "Voice probe complete."}})
    assert evidence.outcome() == "success"


def test_command_disables_side_effects_and_uses_managed_oauth_only():
    probe = probe_module()
    command = probe.app_server_command("/bin/codex", "/tmp/probe")
    assert command[:3] == ["/bin/codex", "app-server", "--stdio"]
    for entry in ['forced_login_method="chatgpt"', 'mcp_servers={}', 'features.hooks=false', 'features.memories=false', 'features.shell_tool=false', 'features.skip_host_skill_discovery=true', 'history.persistence="none"']:
        assert entry in command
    assert probe.MAX_ATTEMPTS == 2 and probe.INFERENCE_BUDGET_SECONDS < 90


def test_mcp_servers_are_disabled_individually_not_by_empty_table(tmp_path):
    probe = probe_module()
    config = tmp_path / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text('[mcp_servers.example]\ncommand="do-not-execute"\n[mcp_servers.second]\nurl="https://example.invalid"\n')
    assert probe.config_safety_overrides(tmp_path) == ['mcp_servers.example.enabled=false', 'mcp_servers.second.enabled=false']
