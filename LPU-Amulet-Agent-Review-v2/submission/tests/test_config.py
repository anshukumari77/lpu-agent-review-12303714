"""Configuration tests use synthetic values, never product credentials."""
import importlib.util


def test_missing_credentials_are_reported_without_secret_values():
    assert importlib.util.find_spec("beep_agent.config") is not None, "Settings not implemented"
    from beep_agent.config import Settings
    settings = Settings(_env_file=None)
    readiness = settings.readiness()
    assert readiness["ready"] is False
    assert "openai_api_key" in readiness["blockers"]
    assert "admin_token" in readiness["blockers"]


def test_require_runtime_fails_closed_and_does_not_echo_credentials():
    import pytest
    from beep_agent.config import Settings
    settings = Settings(admin_token="synthetic-admin-value", _env_file=None)
    assert hasattr(settings, "require_runtime"), "Runtime admission guard not implemented"
    with pytest.raises(RuntimeError) as result:
        settings.require_runtime()
    assert "synthetic-admin-value" not in str(result.value)
    assert "openai_api_key" in str(result.value)


def test_remote_origin_cannot_use_insecure_cookies():
    import pytest
    from pydantic import ValidationError
    from beep_agent.config import Settings
    with pytest.raises(ValidationError):
        Settings(public_origin="https://review.example.com", secure_cookies=False)


def test_explicit_product_config_file_and_environment_precedence(tmp_path, monkeypatch):
    import json
    from beep_agent.config import Settings
    config = tmp_path / "synthetic.json"
    config.write_text(json.dumps({"admin_token": "synthetic-admin-only", "voice": "cedar"}))
    monkeypatch.setenv("BEEP_CONFIG_FILE", str(config))
    monkeypatch.setenv("BEEP_VOICE", "marin")
    settings = Settings()
    assert settings.admin_token.get_secret_value() == "synthetic-admin-only"
    assert settings.voice == "marin"
    assert "synthetic-admin-only" not in repr(settings)


def test_reasoning_provider_defaults_to_openai_with_explicit_codex_opt_in(tmp_path):
    from beep_agent.config import Settings
    default = Settings(openai_api_key="", codex_home=None)
    assert getattr(default, "inference_provider", None) == "openai"
    assert default.inference_model == default.planner_model
    assert default.codex_home is None
    assert default.codex_executable == "codex"
    assert default.codex_model == "gpt-5.5"
    selected = Settings(inference_provider="codex", codex_home=tmp_path,
                        codex_model="synthetic-codex-model", planner_model="synthetic-api-model")
    assert selected.inference_model == "synthetic-codex-model"


def test_reasoning_admission_checks_only_the_selected_provider(tmp_path):
    import pytest
    from beep_agent.config import Settings
    selected = Settings(inference_provider="codex", codex_home=tmp_path, openai_api_key="")
    assert hasattr(selected, "require_inference"), "Separate reasoning admission is missing"
    selected.require_inference()  # Configuration only; no account access is allowed here.
    Settings(openai_api_key="synthetic-key", codex_home=None).require_inference()
    with pytest.raises(RuntimeError, match="codex_home"):
        Settings(inference_provider="codex", codex_home=None,
                 openai_api_key="synthetic-key-no-fallback").require_inference()
    with pytest.raises(RuntimeError, match="openai_api_key"):
        Settings(inference_provider="openai", openai_api_key="",
                 codex_home=tmp_path).require_inference()


def test_runtime_keeps_voice_and_infrastructure_gates_separate_from_oauth(tmp_path):
    import pytest
    from beep_agent.config import Settings
    selected = Settings(inference_provider="codex", codex_home=None, openai_api_key="")
    assert "codex_home" in selected.readiness()["blockers"]
    selected.codex_home = tmp_path
    status = selected.readiness()
    assert status["configuration_only"] is True
    assert status["configured"]["codex_home"] is True
    assert status["configured"]["openai_api_key"] is False
    assert set(status["blockers"]) >= {"openai_api_key", "database_url", "admin_token",
                                      "signing_secret", "livekit_api_key", "livekit_api_secret",
                                      "s3_bucket", "s3_access_key", "s3_secret_key"}
    assert "authenticated" not in status and "verified" not in status
    with pytest.raises(RuntimeError, match="openai_api_key"):
        selected.require_runtime()
    complete = Settings(inference_provider="codex", codex_home=tmp_path,
                        database_url="synthetic-dsn", admin_token="synthetic", signing_secret="synthetic",
                        livekit_api_key="synthetic", livekit_api_secret="synthetic", openai_api_key="synthetic",
                        s3_bucket="synthetic", s3_access_key="synthetic", s3_secret_key="synthetic")
    complete.require_runtime()
    complete.recording_enabled = False
    with pytest.raises(RuntimeError, match="recording_enabled"):
        complete.require_runtime()
