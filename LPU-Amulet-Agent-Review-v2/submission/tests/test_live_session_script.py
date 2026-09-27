"""Unpaid safety tests. Provider doubles below are never acceptance evidence."""
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("seconds", ["0", "61", "nan", "-1", "60.1"])
def test_invalid_voice_cap_refuses_before_configuration(monkeypatch, seconds):
    script = load_script()
    monkeypatch.setattr(script, "load_settings", lambda: pytest.fail("configuration touched"))
    output = io.StringIO()
    assert script.main(["--allow-live", "--seconds", seconds], output=output) == 2
    assert json.loads(output.getvalue())["category"] == "arguments_invalid"


def test_missing_voice_key_has_no_network_or_filesystem_mutation(monkeypatch):
    script = load_script()
    from beep_agent.config import Settings
    from pydantic import SecretStr
    import socket
    import os
    config = Settings(openai_api_key="").model_copy(update={"openai_api_key": SecretStr("")})
    monkeypatch.setattr(script, "load_settings", lambda: config)
    def forbidden(*args, **kwargs):
        pytest.fail("missing credential reached a mutation or network operation")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(os, "mkdir", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(script, "run_live", forbidden)
    output = io.StringIO()
    assert script.main(["--allow-live"], output=output) == 2
    assert json.loads(output.getvalue())["category"] == "voice_credential_missing"


def test_unscoped_openai_key_is_not_borrowed(monkeypatch):
    script = load_script()
    monkeypatch.setenv("BEEP_CONFIG_FILE", "")
    monkeypatch.delenv("BEEP_OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-product-credential")
    assert not script.load_settings().openai_api_key.get_secret_value()
    monkeypatch.setenv("BEEP_OPENAI_API_KEY", "explicit-product-test-value")
    assert script.load_settings().openai_api_key.get_secret_value() == "explicit-product-test-value"


def test_live_gate_permits_one_native_assembly_even_after_failure():
    script = load_script()
    calls = []
    def construct(*a, **kw):
        calls.append(1)
        raise RuntimeError("synthetic rejected transport")
    gate = script.SingleAttempt(construct)
    with pytest.raises(RuntimeError):
        gate(None, {})
    with pytest.raises(script.Rejected) as error:
        gate(None, {})
    assert error.value.category == "attempt_limit"
    assert len(calls) == gate.attempts == 1


async def test_voice_deadline_mutes_before_joining_cleanup():
    import asyncio
    script = load_script()
    calls = []
    class Runtime:
        failures = 0
        model_task = None
        def __init__(self):
            self.stop = asyncio.Event()
        async def run(self):
            try:
                await self.stop.wait()
            finally:
                calls.append("runtime_joined")
        def cut(self):
            calls.append("muted")
            self.stop.set()
        async def aclose(self):
            calls.append("closed")
    runtime = Runtime()
    await script.bounded_voice(runtime, 0.03, cut=runtime.cut, complete=lambda: False)
    assert calls[0] == "muted"
    assert "runtime_joined" in calls and "closed" in calls


@pytest.mark.parametrize("failure", ["prepare", "voice", "finish", "cleanup", None])
async def test_lifecycle_always_cleans_up_and_never_accepts_empty_evidence(failure):
    script = load_script()
    calls = []
    class Boundary:
        async def prepare(self):
            calls.append("prepare")
            if failure == "prepare":
                raise RuntimeError("sk-secret provider payload")
        async def voice(self):
            calls.append("voice")
            if failure == "voice":
                raise RuntimeError("sk-secret provider payload")
        async def finish(self):
            calls.append("finish")
            if failure == "finish":
                raise RuntimeError("sk-secret provider payload")
        async def cleanup(self):
            calls.append("cleanup")
            if failure == "cleanup":
                raise RuntimeError("sk-secret provider payload")
        async def export(self):
            calls.append("export")
            return {"live_attempts": 1, "measurements": {}, "artifacts": {}, "passed": True}
    result = await script.execute_session(Boundary())
    assert "cleanup" in calls and calls[-1] == "export"
    assert result["passed"] is False
    assert "sk-secret" not in json.dumps(result)
    assert result["category"] in {
        "prepare_failed", "voice_failed", "finish_failed", "cleanup_unresolved", "evidence_incomplete"
    }


async def test_prerequisites_only_does_not_construct_a_session(monkeypatch):
    script = load_script()
    async def preflight(config):
        return {"local_postgres": True, "local_livekit": True, "local_s3": True}
    monkeypatch.setattr(script, "preflight", preflight)
    monkeypatch.setattr(script, "RealSession", lambda *a: pytest.fail("session created"))
    result = await script.run_live(None, SimpleNamespace(prerequisites_only=True, seconds=60))
    assert result["passed"] is False
    assert result["live_attempts"] == 0
    assert result["category"] == "prerequisites_verified_not_live"


@pytest.mark.parametrize("field,value", [
    ("livekit_url", "wss://remote.invalid"), ("s3_endpoint", "https://remote.invalid"),
    ("database_url", "postgresql://user@remote.invalid/beep"),
])
async def test_preflight_rejects_nonlocal_service_targets_before_io(monkeypatch, field, value):
    script = load_script()
    from beep_agent.config import Settings
    import socket
    settings = Settings(database_url="postgresql://user@127.0.0.1/beep",
                        admin_token="test", signing_secret="test", openai_api_key="test",
                        livekit_api_key="test", livekit_api_secret="test", s3_bucket="test",
                        s3_access_key="test", s3_secret_key="test",
                        s3_endpoint="http://127.0.0.1:8334")
    settings = settings.model_copy(update={field: value})
    monkeypatch.setattr(socket.socket, "connect", lambda *a: pytest.fail("network reached"))
    with pytest.raises(script.Rejected) as error:
        await script.preflight(settings)
    assert error.value.category == "local_services_required"


def test_actual_synthetic_fixture_is_speech_and_pixels_not_capture(tmp_path):
    import shutil
    import wave
    if sys.platform != "darwin" or not shutil.which("say") or not shutil.which("ffmpeg"):
        pytest.skip("local macOS synthesis tools required")
    script = load_script()
    result = script.synthetic_fixture(tmp_path)
    with wave.open(str(result["speech"]), "rb") as audio:
        assert audio.getframerate() == 48000
        assert audio.getnchannels() == 1 and audio.getsampwidth() == 2
        assert 0 < audio.getnframes() <= 20 * 48000
        assert any(audio.readframes(audio.getnframes()))
    from PIL import Image
    image = Image.open(result["screen"])
    assert image.size == (1280, 720)
    assert result["synthetic_input"] is True


async def test_real_postgres_fixture_preserves_consent_and_recording_fences(tmp_path):
    import os
    import psycopg
    from beep_agent.config import Settings
    from beep_agent.realtime import recording_is_fresh, model_allowed
    from beep_agent.store import StoreError
    dsn = os.environ.get("BEEP_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("BEEP_TEST_DATABASE_URL required for isolated real PostgreSQL")
    script = load_script()
    backend = script.RealSession(Settings(database_url=dsn, openai_api_key=""),
                                 SimpleNamespace(seconds=60), directory=tmp_path)
    try:
        await backend.prepare_database()
        sid = backend.state["id"]
        store = backend.store
        assert backend.state["status"] == "awaiting_consent"
        with pytest.raises(StoreError):
            store.control(backend.tenant, sid, "client", "handover")
        for role in ("client", "facilitator"):
            store.set_consent(backend.tenant, sid, role, ai=True, recording=True)
        with pytest.raises(StoreError):
            store.control(backend.tenant, sid, "client", "handover")
        await backend.elapsed_intro_fixture()
        state = store.control(backend.tenant, sid, "client", "handover")
        assert model_allowed(state)
        assert not recording_is_fresh(state)
        assert state["max_seconds"] == 960 and state["intro_seconds"] == 900
        with pytest.raises(StoreError):
            store.create_session(backend.tenant, title="Second must fail", pack_id="quotation", offer="paid")
        with psycopg.connect(backend.settings.database_url) as conn:
            assert conn.execute("SELECT current_schema()").fetchone()[0] == backend.schema
            assert conn.execute("SELECT count(*) FROM beep_sessions").fetchone()[0] == 1
            assert conn.execute("SELECT count(*) FROM beep_recording_reservations").fetchone()[0] == 0
        assert backend.gate.attempts == 0
        exported = await backend.export()
        assert json.loads(Path(exported["artifacts"]["events"]).read_text()) == []
        assert "report" not in exported["artifacts"]
        assert not script.acceptance_complete(exported)
    finally:
        if getattr(backend, "worker", None):
            await backend.worker.__aexit__(None, None, None)
        # This DB-only test never invokes Egress. No remote resource can exist.
        if getattr(backend, "schema_created", False):
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(
                    psycopg.sql.Identifier(backend.schema)))


async def test_ambiguous_recorder_failure_retains_real_pg_cleanup_without_provider_calls(tmp_path, monkeypatch):
    import os
    import psycopg
    from beep_agent.config import Settings
    from beep_agent.recording import RecordingError
    dsn = os.environ.get("BEEP_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("BEEP_TEST_DATABASE_URL required for isolated real PostgreSQL")
    script = load_script()
    backend = script.RealSession(Settings(database_url=dsn, openai_api_key=""),
                                 SimpleNamespace(seconds=60), directory=tmp_path / "run")
    calls = []
    class RecorderBoundary:
        async def start(self, state):
            calls.append("start")
            raise RecordingError("synthetic ambiguous boundary", "EG-unit-test-only")
        async def reconcile_stop(self, state, reservation):
            calls.append("reconcile")
            raise RecordingError("synthetic unresolved boundary")
    original = backend.prepare_database
    async def database():
        await original()
        backend.worker.recording = RecorderBoundary()
        monkeypatch.setattr(backend.worker, "_ensure_provider", lambda: pytest.fail("paid provider reached"))
    async def no_rtc():
        pass
    monkeypatch.setattr(backend, "prepare_database", database)
    monkeypatch.setattr(backend, "connect_participants", no_rtc)
    monkeypatch.setattr(backend, "start_media", no_rtc)
    try:
        result = await script.execute_session(backend)
        assert result["passed"] is False
        assert calls == ["start", "reconcile"]
        assert backend.gate.attempts == 0
        state = backend.store.get_session(backend.tenant, backend.state["id"])
        assert state["recording_cleanup_pending"] is True
        assert state["egress_id"] == "EG-unit-test-only"
        with psycopg.connect(backend.settings.database_url) as conn:
            assert conn.execute("SELECT count(*) FROM beep_recording_reservations WHERE NOT settled").fetchone()[0] == 1
            assert conn.execute("SELECT max(attempts) FROM beep_jobs").fetchone()[0] == 1
        recovery = json.loads((backend.directory / "recovery.json").read_text())
        assert recovery["schema_retained"] is True
        assert recovery["egress_id"] == "EG-unit-test-only"
        assert "signing_key" not in json.dumps(recovery)
    finally:
        # Explicit unit IO boundary: no Egress request was ever sent.
        if backend.worker:
            await backend.worker.__aexit__(None, None, None)
        if backend.schema_created:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute(psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(
                    psycopg.sql.Identifier(backend.schema)))


async def test_reasoning_budget_counts_before_request_and_never_retries():
    script = load_script()
    calls = []
    class ProviderBoundary:
        async def extract(self, *a, **kw):
            calls.append("extract")
            raise RuntimeError("synthetic provider failure")
        async def synthesize_report(self, *a, **kw):
            calls.append("report")
            return None
    metrics = {}
    provider = script.CappedReasoning(ProviderBoundary(), metrics)
    with pytest.raises(RuntimeError):
        await provider.extract(None, None)
    with pytest.raises(script.Rejected):
        await provider.extract(None, None)
    with pytest.raises(script.Rejected):
        await provider.synthesize_report(None, "sid")
    assert calls == ["extract"]
    assert metrics == {"discovery_requests": 1}


async def test_voice_runs_real_runtime_with_one_attempt_factory_but_no_io(monkeypatch, tmp_path):
    script = load_script()
    from beep_agent.config import Settings
    from beep_agent.realtime import InterviewRuntime
    backend = script.RealSession(Settings(openai_api_key=""), SimpleNamespace(seconds=60), directory=tmp_path)
    backend.state = {"id": "synthetic-offline-test"}
    backend.store = SimpleNamespace(list_events=lambda *a: [])
    backend.rooms = [None, None, SimpleNamespace()]
    async def bounded(runtime, seconds, **options):
        assert type(runtime) is InterviewRuntime
        assert seconds == 60
        assert runtime.assembly_factory is backend.gate
        # Standalone harness has no LiveKit JobContext; it must own the SDK HTTP session.
        assert backend.gate.factory.keywords["http_session"].closed is False
        assert runtime.settings is backend.settings
        assert runtime.observer_factory == backend.observer_factory
        assert runtime.assembly is None
        assert options["complete"]() is False
        backend.events = [
            {"kind": "transcript", "actor": "client", "seq": 1, "text": "Untrusted selected screen DATA",
             "source_ref": "livekit-agents@unit:conversation_item_added:context"},
            {"kind": "transcript", "actor": "agent", "seq": 2, "text": "What starts the work?",
             "source_ref": "livekit-agents@unit:conversation_item_added:agent"},
            {"kind": "screen_observation", "actor": "observer", "seq": 3, "text": "unit screen",
             "source_ref": "livekit:screen@0:sha256:unit"},
        ]
        backend.measurements["received_audio_peak"] = 100
        backend.screen_publication = SimpleNamespace(sid="screen")
        backend.observed_source = "livekit:screen@0:sha256:unit"
        assert options["complete"]() is False  # must not cut short on injected image context
        options["cut"]()
        assert backend.media_stop.is_set()
        assert runtime.stop.is_set()
        assert options["transport_closed"]() is False
        await options["close_transport"]()
        assert options["transport_closed"]() is True
        return {"voice_input_window_seconds": 0, "voice_socket_close_seconds": 0}
    monkeypatch.setattr(script, "bounded_voice", bounded)
    await backend.voice()
    await backend.cancel_media()
    assert backend.gate.attempts == 0


async def test_finalisation_refuses_paid_reasoning_when_capture_is_missing(tmp_path):
    script = load_script()
    from beep_agent.config import Settings
    backend = script.RealSession(Settings(openai_api_key=""), SimpleNamespace(seconds=60), directory=tmp_path)
    backend.state = {"id": "synthetic"}
    backend.store = SimpleNamespace(list_events=lambda *a: [])
    backend.worker = SimpleNamespace(_ensure_provider=lambda: pytest.fail("reasoning attempted without capture"))
    with pytest.raises(script.Rejected) as error:
        await backend.finish()
    assert error.value.category == "evidence_incomplete"


def test_acceptance_requires_every_measured_gate_and_real_paths(tmp_path):
    script = load_script()
    paths = {}
    for key in ("synthetic_speech", "synthetic_screen", "received_voice", "observed_screen",
                "events", "snapshot", "report", "usage", "recording", "recovery"):
        path = tmp_path / key
        path.write_text("unpaid unit gate fixture, NOT provider output")
        paths[key] = str(path)
    measured = {key: True for key in script.REQUIRED_CHECKS}
    measured.update(sessions_created=1, voice_input_window_seconds=1.0,
                    voice_socket_close_seconds=1.1, received_audio_frames=1,
                    received_audio_peak=100, recording_bytes=100, client_transcripts=1,
                    agent_transcripts=2, screen_observations=1, checkpoint_count=1,
                    discovery_requests=1, report_requests=1, screen_requests=1)
    result = {"measurements": measured, "artifacts": paths, "live_attempts": 1,
              "voice_cap_seconds": 60}
    assert script.acceptance_complete(result)
    for key in script.REQUIRED_CHECKS:
        assert not script.acceptance_complete({**result, "measurements": {**measured, key: False}}), key
    for overrides in ({"live_attempts": 0}, {"live_attempts": 2}, {"artifacts": {}},
                      {"measurements": {**measured, "received_audio_peak": 0}},
                      {"measurements": {**measured, "voice_socket_close_seconds": 60.01}},
                      {"measurements": {**measured, "voice_socket_close_seconds": float("nan")}}):
        assert not script.acceptance_complete({**result, **overrides})
    (tmp_path / "recording").unlink()
    assert not script.acceptance_complete(result)


def test_cli_returns_json_and_withholds_raw_sdk_output(monkeypatch, capfd):
    script = load_script()
    from pydantic import SecretStr
    import os
    import logging
    monkeypatch.setattr(script, "load_settings", lambda: SimpleNamespace(openai_api_key=SecretStr("unit-only")))
    async def run(config, args):
        logging.warning("sk-secret untrusted SDK detail")
        print("raw provider payload")
        os.write(2, b"sk-secret C extension log")
        return {"passed": False, "category": "voice_failed", "live_attempts": 1}
    monkeypatch.setattr(script, "run_live", run)
    output = io.StringIO()
    assert script.main(["--allow-live"], output=output) == 1
    assert json.loads(output.getvalue())["live_attempts"] == 1
    captured = capfd.readouterr()
    assert captured.err == captured.out == ""
    assert "sk-secret" not in output.getvalue()


def test_import_and_missing_key_are_audited_zero_mutations(tmp_path):
    import os
    import subprocess
    child = r'''
import os, sys, runpy
violations = []
def audit(event, args):
    bad = event in {"socket.connect", "socket.getaddrinfo", "os.mkdir", "os.remove", "os.rename",
                    "os.rmdir", "subprocess.Popen", "os.system", "os.truncate", "os.chmod"}
    if event == "open":
        flags = args[2]
        bad = bool(flags & (os.O_CREAT | os.O_WRONLY | os.O_RDWR | os.O_TRUNC | os.O_APPEND))
    if bad:
        violations.append(event)
        raise RuntimeError("mutation forbidden by unpaid acceptance audit")
sys.addaudithook(audit)
module = runpy.run_path(sys.argv[1], run_name="imported_acceptance")
assert not violations
assert module["main"]([]) == 2
assert not violations
assert module["main"](["--allow-live"]) == 2
assert not violations
'''
    env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(tmp_path),
           "OPENAI_API_KEY": "not-a-product-credential"}
    result = subprocess.run([sys.executable, "-B", "-c", child, str(SCRIPT)],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    values = [json.loads(line) for line in result.stdout.splitlines()]
    assert [v["category"] for v in values] == ["live_opt_in_required", "voice_credential_missing"]


async def test_export_failure_keeps_actual_attempt_count_and_recovery_path(tmp_path):
    script = load_script()
    recovery = tmp_path / "recovery.json"
    recovery.write_text('{"schema_retained": true}')
    class Boundary:
        gate = SimpleNamespace(attempts=1)
        directory = tmp_path
        async def prepare(self):
            raise RuntimeError("synthetic failure")
        async def cleanup(self):
            pass
        async def export(self):
            raise RuntimeError("secret raw database error")
    result = await script.execute_session(Boundary())
    assert result["passed"] is False
    assert result["live_attempts"] == 1
    assert result["artifacts"]["recovery"] == str(recovery)
    assert "secret raw" not in json.dumps(result)


async def test_run_writes_a_machine_readable_manifest_without_claiming_success(monkeypatch, tmp_path):
    script = load_script()
    class Boundary:
        directory = tmp_path
        owns_directory = True
        async def prepare(self):
            pass
        async def voice(self):
            pass
        async def finish(self):
            pass
        async def cleanup(self):
            pass
        async def export(self):
            return {"live_attempts": 0, "measurements": {}, "artifacts": {}}
    async def preflight(config):
        return {}
    monkeypatch.setattr(script, "preflight", preflight)
    monkeypatch.setattr(script, "RealSession", lambda *a: Boundary())
    result = await script.run_live(None, SimpleNamespace(prerequisites_only=False))
    assert result["passed"] is False
    assert json.loads((tmp_path / "verification.json").read_text()) == result
    assert result["artifacts"]["verification"] == str(tmp_path / "verification.json")


def test_context_injection_is_not_mistaken_for_spoken_client_transcription(tmp_path):
    script = load_script()
    from beep_agent.config import Settings
    backend = script.RealSession(Settings(openai_api_key=""), SimpleNamespace(seconds=60), directory=tmp_path)
    backend.events = [{"kind": "transcript", "actor": "client", "seq": 1,
                       "text": "Untrusted selected screen DATA at 900001ms",
                       "source_ref": "livekit-agents@unit:conversation_item_added:context"}]
    backend.capture_measurements()
    assert backend.measurements["synthetic_speech_transcribed"] is False
    backend.events[0]["text"] = "The owner approves the quotation."
    backend.measurements["synthetic_speech_samples_sent"] = 48000
    backend.capture_measurements()
    assert backend.measurements["synthetic_speech_transcribed"] is True


def test_unknown_error_category_cannot_echo_untrusted_provider_text():
    script = load_script()
    assert script.Rejected("sk-secret raw provider error").category == "operation_failed"


async def test_voice_cancellation_joins_owned_task_cleanup():
    import asyncio
    script = load_script()
    entered = asyncio.Event()
    calls = []
    class Runtime:
        failures = 0
        model_task = None
        async def run(self):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                calls.append("joined")
        async def aclose(self):
            calls.append("closed")
    running = asyncio.create_task(script.bounded_voice(Runtime(), 60,
                                                        cut=lambda: calls.append("muted")))
    await entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert calls == ["muted", "closed", "joined"]


async def test_missing_selected_oauth_store_refuses_before_service_io(monkeypatch, tmp_path):
    script = load_script()
    from beep_agent.config import Settings
    import psycopg
    cfg = Settings(database_url="postgresql://user@127.0.0.1/beep", admin_token="unit",
                   signing_secret="unit", openai_api_key="unit", livekit_api_key="unit",
                   livekit_api_secret="unit", s3_bucket="unit", s3_access_key="unit",
                   s3_secret_key="unit", s3_endpoint="http://127.0.0.1:8334",
                   inference_provider="codex", codex_home=tmp_path / "absent", codex_executable=sys.executable)
    monkeypatch.setattr(psycopg, "connect", lambda *a, **kw: pytest.fail("missing credential reached PostgreSQL"))
    with pytest.raises(script.Rejected) as error:
        await script.preflight(cfg)
    assert error.value.category == "reasoning_prerequisite_missing"


async def test_cleanup_attempts_every_owned_source_on_close_error(tmp_path):
    script = load_script()
    from beep_agent.config import Settings
    backend = script.RealSession(Settings(openai_api_key=""), SimpleNamespace(seconds=60), directory=tmp_path)
    calls = []
    class Source:
        def __init__(self, fail):
            self.fail = fail
        async def aclose(self):
            calls.append(self.fail)
            if self.fail:
                raise RuntimeError("synthetic close failure")
    backend.sources = [Source(True), Source(False)]
    with pytest.raises(script.Rejected):
        await backend.cancel_media()
    assert calls == [True, False]
    assert backend.media_stop.is_set()


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_live_session.py"


def load_script():
    assert SCRIPT.exists(), "Opt-in acceptance harness is missing"
    spec = importlib.util.spec_from_file_location("verify_live_session", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_no_flag_refuses_before_loading_settings_or_any_io(monkeypatch):
    script = load_script()
    calls = []
    monkeypatch.setattr(script, "load_settings", lambda: calls.append("settings"))
    monkeypatch.setattr(script, "run_live", lambda *a: calls.append("live"))
    output = io.StringIO()
    assert script.main([], output=output) == 2
    assert calls == []
    assert json.loads(output.getvalue()) == {
        "passed": False, "category": "live_opt_in_required", "live_attempts": 0
    }
