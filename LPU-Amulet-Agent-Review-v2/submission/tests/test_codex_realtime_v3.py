"""Offline V3 safety tests. Synthetic children/RTC only; no model network calls."""
import importlib.util
import asyncio
import json
import os
from pathlib import Path
import signal
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def probe_module():
    path = ROOT / "scripts/probe_codex_realtime_v3.py"
    assert path.is_file(), "V3-specific managed-OAuth probe is not implemented"
    spec = importlib.util.spec_from_file_location("probe_codex_realtime_v3", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_explicit_product_selection_and_clean_process_environment(tmp_path):
    probe = probe_module()
    home = tmp_path / "selected-codex"
    home.mkdir()
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"inference_provider": "codex", "codex_home": str(home)}))
    assert probe.selected_home(runtime, str(home)) == home
    env = probe.minimal_env({
        "HOME": "/synthetic/home", "PATH": "/bin", "TMPDIR": "/untrusted/tmp",
        "OPENAI_API_KEY": "synthetic-secret", "BEEP_OPENAI_API_KEY": "synthetic-secret",
        "OPENAI_CUSTOM_HEADERS": "synthetic-secret", "CODEX_HOME": "/wrong/store",
        "HTTP_PROXY": "https://wrong.invalid", "NODE_OPTIONS": "--require=untrusted.js",
        "DYLD_INSERT_LIBRARIES": "/wrong/lib", "BEEP_CONFIG_FILE": "/wrong/config",
    }, home, tmp_path)
    assert env == {
        "HOME": "/synthetic/home", "PATH": "/bin", "LANG": "en_US.UTF-8",
        "RUST_LOG": "off", "NO_COLOR": "1", "CODEX_HOME": str(home),
        "TMPDIR": str(tmp_path),
    }
    for request in ("relative", "", str(tmp_path / "wrong-home")):
        with pytest.raises(probe.ProbeFailure, match="explicit_selection_mismatch"):
            probe.selected_home(runtime, request)
    runtime.write_text(json.dumps({"inference_provider": "openai", "codex_home": str(home)}))
    with pytest.raises(probe.ProbeFailure, match="explicit_selection_mismatch"):
        probe.selected_home(runtime, str(home))


def test_process_only_isolation_disables_inherited_extensions(tmp_path):
    probe = probe_module()
    config = tmp_path / "config.toml"
    config.write_text('[mcp_servers.first]\ncommand="never-run"\n'
                      '[mcp_servers.second]\nurl="https://never.invalid"\n'
                      '[plugins."example@catalog"]\nenabled=true\n')
    original = config.read_bytes()
    settings = probe.config_safety_overrides(tmp_path)
    assert settings == ['mcp_servers.first.enabled=false', 'mcp_servers.second.enabled=false',
                        'plugins={"example@catalog"={enabled=false}}']
    cmd = probe.app_server_command("/installed/codex", tmp_path, settings)
    assert cmd[:3] == ["/installed/codex", "app-server", "--stdio"]
    for entry in ['forced_login_method="chatgpt"', 'model_provider="openai"',
                  'features.hooks=false', 'features.shell_tool=false',
                  'features.plugins=false', 'features.apps=false', 'features.memories=false',
                  'features.skip_host_skill_discovery=true', 'features.unified_exec=false',
                  'history.persistence="none"', 'realtime.version="v3"',
                  'shell_environment_policy.inherit="none"', 'notify=[]', *settings]:
        assert entry in cmd
    assert config.read_bytes() == original
    for text in ['experimental_realtime_ws_base_url="https://override.invalid"',
                 'profile="unreviewed"', '[model_providers.other]\nname="Other"']:
        config.write_text(text)
        with pytest.raises(probe.ProbeFailure, match="custom_configuration_requires_review"):
            probe.config_safety_overrides(tmp_path)


def test_v3_contract_uses_v1_catalog_default_and_no_agent_turn(tmp_path):
    probe = probe_module()
    catalog = {"defaultV1": "cove", "v1": ["cove", "ember"],
               "defaultV2": "marin", "v2": ["marin", "cedar"]}
    voice = probe.select_voice(catalog)
    assert voice == "cove"
    params = probe.realtime_params("synthetic-thread", "browser-generated-sdp", voice)
    assert params["version"] == "v3"
    assert params["model"] == "gpt-live-1-codex"
    assert params["outputModality"] == "audio"
    assert params["voice"] == "cove"
    assert params["transport"] == {"type": "webrtc", "sdp": "browser-generated-sdp"}
    assert params["includeStartupContext"] is False
    assert params["clientManagedHandoffs"] is True
    assert params["initialItems"] == []
    assert params["delegationAckFiller"] is False
    assert params["flushTranscriptTailOnSessionEnd"] is False
    assert params["realtimeStartInstructions"] == params["realtimeEndInstructions"] == ""
    assert params["codexResponsesAsItems"] is False
    thread = probe.thread_params(str(tmp_path))
    for key in ("environments", "dynamicTools", "runtimeWorkspaceRoots", "selectedCapabilityRoots"):
        assert thread[key] == []
    assert thread["ephemeral"] is True
    assert thread["sandbox"] == "read-only"
    assert "turn/start" not in probe.ALLOWED_RPC
    assert "account/login/start" not in probe.ALLOWED_RPC
    assert "account/logout" not in probe.ALLOWED_RPC
    with pytest.raises(probe.ProbeFailure, match="voice_catalog_mismatch"):
        probe.select_voice({**catalog, "defaultV1": "marin"})
    for schema_name, payload in [("ThreadStartParams.json", thread),
                                 ("ThreadRealtimeStartParams.json", params)]:
        probe.validate_payload(ROOT / ".local/codex-protocol/v2" / schema_name, payload)


def test_v3_requires_connection_decoded_samples_and_real_assistant_transcript():
    probe = probe_module()
    evidence = probe.Evidence()
    evidence.observe({"method": "thread/realtime/started", "params": {"version": "v3"}})
    assert evidence.outcome() == "not_connected"
    evidence.observe_wire({"type": "session.started"})
    evidence.observe_wire({"type": "input_transcript.added", "item": {"text": probe.PHRASE}})
    evidence.observe_wire({"type": "turn.done", "turn": {"role": "user", "transcript": probe.PHRASE}})
    evidence.observe_rtc({"connectionState": "connected", "dataChannelState": "open",
                          "decodedSamples": 24000, "nonzeroSamples": 0, "energy": 0})
    assert evidence.outcome() == "connected_no_audio"
    assert not evidence.summary()["phrase_verified"]
    evidence.observe_wire({"type": "output_transcript.added", "item": {"text": "Voice probe "}})
    evidence.observe_wire({"type": "output_transcript.added", "item": {"text": "complete."}})
    assert evidence.outcome() == "connected_no_audio"
    evidence.observe_rtc({"connectionState": "connected", "dataChannelState": "open",
                          "decodedSamples": 48000, "nonzeroSamples": 12000, "energy": 0.2})
    assert evidence.outcome() == "audio_received_transcript_pending"
    evidence.observe_wire({"type": "turn.done", "turn": {"role": "assistant", "transcript": probe.PHRASE}})
    assert evidence.summary()["assistant_transcript"] == probe.PHRASE  # not delta+done duplicated
    assert evidence.outcome() == "success"
    evidence.observe({"method": "turn/started", "params": {}})
    assert evidence.outcome() == "safety_stop_unexpected_task"


def test_v3_error_codes_are_precise_but_never_return_raw_errors():
    probe = probe_module()
    error = probe.safe_error('HTTP 400 {"code":"invalid_quicksilver_alpha_header", '
                             '"access_token":"secret-value", "email":"private@example.com"}', -32600)
    assert error == {"classification": "protocol_incompatibility", "http_status": 400,
                     "rpc_code": -32600, "provider_code": "invalid_quicksilver_alpha_header"}
    assert probe.safe_error("realtime conversation requires API key auth")["classification"] == "transport_requires_api_key"
    for code in ("secret-value", {"private": "secret-value"}, ["secret-value"]):
        safe = probe.safe_error(json.dumps({"error": {"code": code, "token": "secret-value"}}))
        assert "secret-value" not in str(safe)
    assert probe.safe_error("HTTP 403 Forbidden")["classification"] == "access_denied"
    assert probe.safe_error("HTTP 401 Unauthorized")["classification"] == "failed_authentication"
    evidence = probe.Evidence()
    evidence.observe_wire({"type": "error", "error": {"code": "invalid_quicksilver_alpha_header"}})
    assert evidence.outcome() == "protocol_incompatibility"
    evidence.observe_wire({"type": "delegation.created", "item": {"type": "delegation"}})
    assert evidence.outcome() == "safety_stop_unexpected_task"


@pytest.mark.asyncio
async def test_rpc_denies_server_requests_and_forbids_unsafe_client_methods():
    probe = probe_module()
    assert hasattr(probe, "AppServer"), "safe private RPC transport not implemented"
    code = '''
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "initialize":
        print(json.dumps({"id": message["id"], "result": {}}), flush=True)
    elif message.get("method") == "thread/start":
        print(json.dumps({"id": 987, "method": "item/tool/call", "params": {"secret": "do-not-expose"}}), flush=True)
    elif message.get("id") == 987:
        assert "error" in message
        print(json.dumps({"id": 2, "result": {}}), flush=True)
'''
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    server = probe.AppServer(child)
    try:
        await server.request("initialize", {})
        for method in ("turn/start", "account/login/start", "account/logout", "config/value/write"):
            with pytest.raises(probe.ProbeFailure, match="rpc_method_not_authorized"):
                await server.request(method, {})
        with pytest.raises(probe.ProbeFailure, match="safety_stop_unexpected_task"):
            await server.request("thread/start", {})
        assert server.denied_requests == 1
        assert server.evidence.unexpected_task
        assert "do-not-expose" not in json.dumps(server.evidence.summary())
    finally:
        await server.close()
    assert child.returncode is not None
    assert server.pump.done()


@pytest.mark.asyncio
async def test_repeated_cancellation_and_concurrent_close_still_kill_and_reap():
    probe = probe_module()
    assert hasattr(probe, "ManagedChild"), "cancellation-safe child cleanup not implemented"
    code = 'import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); print("ready",flush=True); time.sleep(60)'
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
    await asyncio.wait_for(child.stdout.readline(), 3)
    managed = probe.ManagedChild(child, grace=.1, term_grace=.1)
    closer = asyncio.create_task(managed.close())
    concurrent = asyncio.create_task(managed.close())
    try:
        await asyncio.sleep(.02)
        closer.cancel()
        await asyncio.sleep(.02)
        closer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closer, 3)
        await asyncio.wait_for(concurrent, 3)
        assert child.returncode == -signal.SIGKILL
        with pytest.raises(ProcessLookupError):
            os.kill(child.pid, 0)
        assert managed.cleanup_done
    finally:
        if child.returncode is None:
            os.killpg(child.pid, signal.SIGKILL)
            await child.wait()


@pytest.mark.asyncio
async def test_real_chromium_synthetic_loopback_decodes_samples_and_v3_events(tmp_path):
    probe = probe_module()
    assert hasattr(probe, "BrowserBridge"), "isolated V3 Chromium bridge not implemented"
    # A local peer in the same blank page supplies the explicitly synthetic test
    # audio/events. No external provider, microphone, camera or screen is used.
    fixture = r'''
      else if(request.op === 'syntheticPeer') result = await page.evaluate(async()=>{
        const peer = window.fixturePeer = new RTCPeerConnection({iceServers:[]});
        const oscillator = window.context.createOscillator();
        const gain = window.context.createGain(); gain.gain.value = 0.1;
        const target = window.context.createMediaStreamDestination();
        oscillator.connect(gain); gain.connect(target); oscillator.start();
        peer.addTrack(target.stream.getAudioTracks()[0], target.stream);
        peer.ondatachannel = e => { e.channel.onopen = () => {
          for(const event of [{type:'session.started'},
            {type:'output_transcript.added',item:{text:'Voice probe complete.'}},
            {type:'turn.done',turn:{role:'assistant',transcript:'Voice probe complete.'}}])
            e.channel.send(JSON.stringify(event));
        }; };
        await peer.setRemoteDescription(window.pc.localDescription);
        await peer.setLocalDescription(await peer.createAnswer());
        await window.pc.setRemoteDescription(peer.localDescription);
        return {synthetic:true};
      });
'''
    script = probe.BROWSER_BRIDGE.replace("// TEST_PEER_INSERTION", fixture)
    env = probe.minimal_env(os.environ, tmp_path, tmp_path)
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        # Hermetic test runner supplies only a browser executable cache, never a
        # profile. Keep this synthetic test override out of production auth env.
        env["PLAYWRIGHT_BROWSERS_PATH"] = os.environ["PLAYWRIGHT_BROWSERS_PATH"]
    browser = await probe.BrowserBridge.launch(env, tmp_path, script=script)
    try:
        offer = await browser.call({"op": "offer"})
        assert offer["audioSection"] and offer["dataSection"]
        assert offer["microphoneCalls"] == offer["screenCalls"] == 0
        assert hasattr(probe, "synthesize_input"), "bounded offline speech input not implemented"
        wave_bytes = probe.synthesize_input(tmp_path, env)
        import base64
        injected = await browser.call({"op": "inputAudio", "wavBase64": base64.b64encode(wave_bytes).decode()})
        assert 0 < injected["durationSeconds"] < 10
        assert injected["syntheticInputStarted"] is True
        await browser.call({"op": "syntheticPeer"})
        evidence = probe.Evidence()
        evidence.observe({"method": "thread/realtime/started", "params": {"version": "v3"}})
        try:
            async with asyncio.timeout(15):
                while evidence.outcome() != "success":
                    evidence.observe_rtc(await browser.call({"op": "stats"}))
                    await asyncio.sleep(.1)
        except TimeoutError:
            pytest.fail(json.dumps(evidence.summary()))
        assert evidence.summary()["assistant_transcript"] == probe.PHRASE
        assert evidence.rtc["nonzeroSamples"] > 0
        assert evidence.rtc.get("decodedEnergy", 0) > 0
        excerpt = await browser.call({"op": "excerpt"})
        import base64
        audio = base64.b64decode(excerpt["pcm16Base64"], validate=True)
        assert any(audio) and 0 < len(audio) <= 192000
    finally:
        await browser.close()
    assert browser.closed_ack
    assert browser.cleanup_done and browser.process.returncode == 0


def test_effective_config_is_checked_before_start_and_raw_values_not_reported():
    probe = probe_module()
    assert hasattr(probe, "check_effective_config"), "effective isolation readback not implemented"
    config = {"forced_login_method": "chatgpt", "model_provider": "openai",
              "mcp_servers": {"one": {"enabled": False, "env": {"SECRET": "private-value"}}},
              "plugins": {"example": {"enabled": False}},
              "features": {"hooks": False, "plugins": False, "apps": False, "shell_tool": False,
                           "unified_exec": False, "memories": False},
              "realtime": {"version": "v3", "type": "conversational"}}
    readback = probe.check_effective_config(config)
    assert readback["mcp_servers_disabled"] == 1
    assert "private-value" not in json.dumps(readback)
    assert probe.check_effective_config({**config, "chatgpt_base_url": "https://chatgpt.com/backend-api/"}) == readback
    for broken in ({**config, "forced_login_method": "api"},
                   {**config, "mcp_servers": {"one": {"enabled": True}}},
                   {**config, "openai_base_url": "https://not-authorized.invalid"}):
        with pytest.raises(probe.ProbeFailure, match="effective_isolation_mismatch"):
            probe.check_effective_config(broken)


@pytest.mark.asyncio
async def test_start_ack_then_provider_block_stops_once_without_speech_or_fallback():
    probe = probe_module()
    assert hasattr(probe, "probe_session"), "bounded V3 session orchestration not implemented"

    class Server:
        def __init__(self):
            self.evidence = probe.Evidence()
            self.calls = []
            self.changed = asyncio.Event()

        async def request(self, method, params, timeout=15):
            self.calls.append((method, params))
            if method == "thread/realtime/start":
                self.evidence.observe({"method": "thread/realtime/error", "params": {
                    "message": 'remote error {"code":"invalid_quicksilver_alpha_header","token":"never-print"}'}})
            elif method == "thread/realtime/stop":
                self.evidence.observe({"method": "thread/realtime/closed", "params": {}})
            return {}

    class Browser:
        async def call(self, message, timeout=15):
            assert message["op"] in {"offer", "stats"}
            if message["op"] == "offer":
                return {"sdp": "synthetic-private-sdp", "audioSection": True, "dataSection": True,
                        "microphoneCalls": 0, "screenCalls": 0}
            return {"connectionState": "new", "dataChannelState": "connecting",
                    "decodedSamples": 0, "nonzeroSamples": 0}

    server = Server()
    result = await probe.probe_session(server, Browser(), "fake-thread", "cove")
    assert result["rpc_start_accepted"] is True
    assert result["outcome"] == "protocol_incompatibility"
    assert result["connected_session"] is False
    assert result["speech_requested"] is False
    assert result["closed_notification"] and result["stop_acknowledged"]
    assert [c[0] for c in server.calls] == ["thread/realtime/start", "thread/realtime/stop"]
    assert server.calls[0][1]["version"] == "v3"
    assert server.calls[0][1]["transport"]["type"] == "webrtc"
    assert "never-print" not in json.dumps(result)
    assert "synthetic-private-sdp" not in json.dumps(result)


@pytest.mark.asyncio
async def test_resource_cleanup_is_joined_on_repeated_cancellation():
    probe = probe_module()
    assert hasattr(probe, "cleanup_resources"), "joined session cleanup not implemented"
    record = {}

    class Server:
        denied_requests = 0
        process = type("Process", (), {"returncode": None})()

        async def request(self, method, params, timeout=15):
            if method == "thread/unsubscribe":
                await asyncio.sleep(.05)
                return {"status": "unsubscribed"}
            assert method == "thread/loaded/list"
            return {"data": [], "nextCursor": None}

        async def close(self):
            await asyncio.sleep(.05)
            self.process.returncode = 0

    class Browser:
        process = type("Process", (), {"returncode": None})()
        closed_ack = False

        async def close(self):
            await asyncio.sleep(.05)
            self.process.returncode = 0
            self.closed_ack = True

    task = asyncio.create_task(probe.cleanup_resources(Server(), Browser(), "fake-thread", record))
    await asyncio.sleep(.01)
    task.cancel()
    await asyncio.sleep(.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert record["browser_closed_ack"]
    assert record["ephemeral_thread_unloaded_readback"]
    assert record["app_server_exit_code"] == record["browser_exit_code"] == 0


@pytest.mark.asyncio
async def test_runner_rejects_non_chatgpt_account_before_browser_or_thread(tmp_path, monkeypatch):
    probe = probe_module()
    assert hasattr(probe, "run_probe"), "opt-in V3 runner not implemented"
    home = tmp_path / "codex"
    home.mkdir()
    runtime = tmp_path / "runtime.json"
    runtime.write_text(json.dumps({"inference_provider": "codex", "codex_home": str(home)}))
    calls = []
    config = {"forced_login_method": "chatgpt", "model_provider": "openai", "mcp_servers": {}, "plugins": {},
              "features": {name: False for name in ["hooks", "plugins", "apps", "shell_tool", "unified_exec", "memories"]},
              "realtime": {"version": "v3", "type": "conversational"}}

    class Server:
        denied_requests = 0
        process = type("Process", (), {"returncode": None})()

        async def send(self, message):
            assert message["method"] == "initialized"

        async def request(self, method, params, timeout=15):
            calls.append(method)
            if method == "initialize":
                return {}
            if method == "config/read":
                return {"config": config}
            assert method == "account/read"
            assert params == {"refreshToken": False}
            return {"account": {"type": "apiKey", "email": "never-print@example.com"}}

        async def close(self):
            self.process.returncode = 0

    async def launch(codex, scratch, extra, env):
        assert "OPENAI_API_KEY" not in env and env["CODEX_HOME"] == str(home)
        return Server()

    monkeypatch.setattr(probe, "launch_app_server", launch)
    monkeypatch.setattr(probe, "prepare_installed", lambda *args: (
        ROOT / ".local/codex-protocol/v2", {"codex_version": "codex-cli 0.153.2"}))
    result = await probe.run_probe(str(home), runtime_path=runtime)
    assert result["outcome"] == "selected_account_not_chatgpt"
    assert result["attempts"] == []
    assert result["app_server_exit_code"] == 0
    assert result["isolated_scratch_removed"] is True
    assert "thread/start" not in calls
    assert "never-print@example.com" not in json.dumps(result)


def test_cli_requires_opt_in_and_preserves_existing_report(tmp_path, monkeypatch, capsys):
    probe = probe_module()
    assert hasattr(probe, "main"), "explicit CLI and report guard not implemented"
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    assert probe.main([]) == 0
    assert not (tmp_path / "docs").exists()
    result = {"outcome": "protocol_incompatibility", "attempts": [], "native_voice_qualified": False}
    probe.save_report(result)
    before = (tmp_path / "docs/codex-realtime-v3.json").read_bytes()
    assert probe.main(["--run", "--codex-home", "/explicit/codex"]) == 2
    assert (tmp_path / "docs/codex-realtime-v3.json").read_bytes() == before
    assert "existing_result_preserved" in capsys.readouterr().out
    with pytest.raises(FileExistsError):
        probe.save_report(result)


def test_followup_is_only_for_connected_phrase_mismatch_not_provider_block():
    probe = probe_module()
    assert hasattr(probe, "allow_synthetic_followup"), "narrow followup admission not implemented"
    prior = {"attempts": [{"connected_session": True, "unexpected_task": False, "errors": [],
                            "outcome": "audio_received_transcript_unverified", "closed_notification": True,
                            "stop_acknowledged": True}], "app_server_exit_code": 0, "browser_exit_code": 0,
             "matching_owned_processes_after_cleanup": 0, "attempt_limit": 1}
    assert probe.allow_synthetic_followup(prior)
    assert not probe.allow_synthetic_followup({**prior, "synthetic_input_followup": {}})
    for key, value in [("errors", [{"classification": "access_denied"}]), ("unexpected_task", True),
                       ("connected_session", False), ("closed_notification", False)]:
        changed = {**prior, "attempts": [{**prior["attempts"][0], key: value}]}
        assert not probe.allow_synthetic_followup(changed)


def test_input_transcripts_are_distinct_from_returned_assistant_output():
    probe = probe_module()
    evidence = probe.Evidence()
    evidence.observe_wire({"type": "turn.done", "turn": {"role": "user", "transcript": "Please say voice probe complete."}})
    assert evidence.summary().get("input_transcript") == "Please say voice probe complete."
    assert not evidence.summary()["phrase_verified"]


@pytest.mark.asyncio
async def test_repeated_cancel_during_audio_excerpt_still_stops_realtime():
    probe = probe_module()
    started = asyncio.Event()
    excerpt_entered = asyncio.Event()

    class Server:
        evidence = probe.Evidence()
        stopped = False

        async def request(self, method, params, timeout=15):
            if method == "thread/realtime/start":
                started.set()
                await asyncio.sleep(60)
            elif method == "thread/realtime/stop":
                self.stopped = True
                self.evidence.closed = True
            return {}

    class Browser:
        async def call(self, message, timeout=15):
            if message["op"] == "offer":
                return {"sdp": "fake", "audioSection": True, "dataSection": True,
                        "microphoneCalls": 0, "screenCalls": 0}
            if message["op"] == "stats":
                return {"decodedSamples": 10, "nonzeroSamples": 10}
            assert message["op"] == "excerpt"
            excerpt_entered.set()
            await asyncio.sleep(.1)
            return {}

    server = Server()
    task = asyncio.create_task(probe.probe_session(server, Browser(), "fake-thread", "cove"))
    await started.wait()
    task.cancel()
    await excerpt_entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert server.stopped, "cancellation skipped realtime stop while capturing evidence"


def test_explicit_synthetic_followup_preserves_prior_and_is_single_use(tmp_path, monkeypatch):
    probe = probe_module()
    monkeypatch.setattr(probe, "ROOT", tmp_path)
    docs = tmp_path / "docs"
    docs.mkdir()
    prior = {"attempts": [{"connected_session": True, "unexpected_task": False, "errors": [],
                            "outcome": "audio_received_transcript_unverified", "closed_notification": True,
                            "stop_acknowledged": True}], "app_server_exit_code": 0, "browser_exit_code": 0,
             "matching_owned_processes_after_cleanup": 0, "outcome": "audio_received_transcript_unverified"}
    (docs / "codex-realtime-v3.json").write_text(json.dumps(prior))
    (docs / "codex-realtime-v3.md").write_text("Initial record\n")
    runs = []

    async def run(home, *, synthetic_input=False):
        assert home == "/explicit/selected" and synthetic_input is True
        runs.append(True)
        return {"outcome": "success", "native_voice_qualified": True}

    monkeypatch.setattr(probe, "run_probe", run)
    args = ["--run", "--codex-home", "/explicit/selected", "--verify-synthetic-input"]
    assert probe.main(args) == 0
    final = json.loads((docs / "codex-realtime-v3.json").read_text())
    assert final["attempts"] == prior["attempts"]
    assert final["synthetic_input_followup"]["outcome"] == "success"
    assert probe.main(args) == 2
    assert len(runs) == 1


def test_parallel_wire_and_appserver_transcripts_do_not_double_text():
    probe = probe_module()
    evidence = probe.Evidence()
    for text in ("Voice probe", " complete."):
        evidence.observe({"method": "thread/realtime/transcript/delta", "params": {"role": "assistant", "delta": text}})
        evidence.observe_wire({"type": "output_transcript.added", "item": {"text": text}})
    assert evidence.summary()["assistant_transcript"] == "Voice probe complete."
    assert evidence.summary()["assistant_turn_done"] is False
    evidence.observe_wire({"type": "turn.done", "turn": {"role": "assistant", "transcript": "Voice probe complete."}})
    assert evidence.summary()["assistant_turn_done"] is True
    assert evidence.summary()["assistant_transcript_source"] == "webrtc"
