"""Unpaid diagnostics probes; all events/IO are explicit synthetic fixtures."""

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
CANARY = "canary-NOT-A-CREDENTIAL-https://private.invalid/prompt?token=secret"


def load_harness():
    spec = importlib.util.spec_from_file_location(
        "native_diagnostics_harness", ROOT / "scripts" / "verify_live_session.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_execute_failure_keeps_safe_exception_and_separate_artifact(tmp_path):
    harness = load_harness()
    calls = []

    class Boundary:
        directory = tmp_path
        owns_directory = True
        gate = SimpleNamespace(attempts=1)

        async def prepare(self):
            calls.append("prepare")

        async def voice(self):
            calls.append("voice")
            raise ValueError(CANARY)

        async def cleanup(self):
            calls.append("cleanup")

        async def export(self):
            calls.append("export")
            return {"live_attempts": 1, "measurements": {}, "artifacts": {}}

    result = await harness.execute_session(Boundary())
    assert calls == ["prepare", "voice", "cleanup", "export"]
    assert not result["passed"] and result["category"] == "voice_failed"
    assert "native_diagnostics" in result, "execute_session discarded the exact failure"
    path = Path(result["artifacts"]["native_diagnostics"])
    payload = json.loads(path.read_text())
    assert path.name == "native-diagnostics.json"
    assert payload["purpose"] == "native_diagnostics_not_business_evidence"
    failure = payload["failures"][0]
    assert failure["stage"] == "voice" and failure["type"] == "ValueError"
    assert any(
        frame["source"] == "scripts/verify_live_session.py" and frame["line"] > 0
        for frame in failure["frames"]
    )
    assert "voice_socket_close_seconds" not in result["measurements"]
    assert CANARY not in json.dumps(result) + path.read_text()
    assert "native_diagnostics" not in harness.REQUIRED_ARTIFACTS


def test_provider_metadata_allowlists_and_correlates_without_content():
    from beep_agent.native_diagnostics import NativeDiagnostics

    diagnostics = NativeDiagnostics()
    assert hasattr(diagnostics, "server_event"), "raw SDK metadata observer is missing"
    request = {
        "type": "conversation.item.create",
        "event_id": CANARY + "-event",
        "previous_item_id": CANARY + "-prior",
        "item": {
            "id": CANARY + "-item",
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_image", "image_url": CANARY},
                {"type": "input_text", "text": CANARY},
            ],
        },
    }
    diagnostics.client_event(request)
    diagnostics.server_event(
        {
            "type": "error",
            "event_id": CANARY + "-ack",
            "error": {
                "event_id": CANARY + "-event",
                "code": "invalid_value",
                "type": "invalid_request_error",
                "param": "item.content[0].type",
                "message": CANARY,
                "cause": {"body": CANARY},
                "headers": {"Authorization": CANARY},
                "args": [CANARY],
            },
        }
    )
    diagnostics.server_event(
        {
            "type": "conversation.item.added",
            "event_id": "ack-2",
            "item": request["item"],
            "previous_item_id": CANARY + "-prior",
        }
    )
    diagnostics.server_event(
        {
            "type": "response.done",
            "response": {
                "id": CANARY,
                "status": "failed",
                "output": [{"content": CANARY}],
                "status_details": {
                    "type": "failed",
                    "reason": CANARY,
                    "error": {
                        "code": "server_error",
                        "type": CANARY,
                        "param": CANARY,
                        "message": CANARY,
                    },
                },
            },
        }
    )
    diagnostics.server_event({"type": CANARY, "error": {"code": "server_error", "message": CANARY}})
    payload = diagnostics.snapshot()
    encoded = json.dumps(payload)
    assert CANARY not in encoded and "private.invalid" not in encoded
    create, added, unknown = payload["events"]
    rejected, failed = payload["errors"]
    assert create["item"] == added["item"] == "item-1"
    assert create["previous_item"] == added["previous_item"] == "item-2"
    assert rejected["error"]["event"] == create["event"] == "event-1"
    assert rejected["error"] == {
        "type": "invalid_request_error",
        "code": "invalid_value",
        "param": "item.content[0].type",
        "event": "event-1",
    }
    assert failed["status"] == "failed"
    assert failed["error"]["code"] == "server_error"
    assert failed["error"]["type"] == failed["error"]["param"] == "other"
    assert unknown["type"] == "other" and "error" not in unknown
    assert request["item"]["content"][0]["image_url"] == CANARY  # not mutated


@pytest.mark.parametrize("outcome", ["return", "failure", "cancel"])
async def test_drive_voice_attaches_passive_observer_and_always_detaches(monkeypatch, outcome):
    from livekit.rtc import EventEmitter

    harness = load_harness()
    native = EventEmitter()
    backend = harness.RealSession.__new__(harness.RealSession)
    backend.settings = SimpleNamespace()
    backend.store = SimpleNamespace(list_events=lambda *a: [])
    backend.state = {"id": "synthetic-no-rtc"}
    backend.rooms = [None, None, None]
    backend.gate = harness.SingleAttempt(lambda *a, **kw: pytest.fail("extra assembly"))
    backend.args = SimpleNamespace(seconds=1)
    backend.tasks, backend.events, backend.measurements = [], [], {}

    async def no_evidence(*args):
        await asyncio.Future()

    backend.call = no_evidence

    async def bounded(runtime, seconds, **options):
        runtime.assembly = SimpleNamespace(agent=SimpleNamespace(realtime_llm_session=native))
        for _ in range(30):
            if native._events.get("openai_server_event_received"):
                break
            await asyncio.sleep(0.005)
        assert native._events.get("openai_server_event_received"), "no SDK observer attached"
        assert native._events.get("openai_client_event_queued")
        native.emit("openai_server_event_received", {"type": "session.updated", "session": CANARY})
        if outcome == "failure":
            raise ValueError(CANARY)
        if outcome == "cancel":
            raise asyncio.CancelledError(CANARY)
        return {"voice_socket_close_seconds": 0.01, "voice_input_window_seconds": 0.005}

    monkeypatch.setattr(harness, "bounded_voice", bounded)
    try:
        if outcome == "return":
            await backend._drive_voice()
        else:
            with pytest.raises(ValueError if outcome == "failure" else asyncio.CancelledError):
                await backend._drive_voice()
        await asyncio.sleep(0)
        assert not any(native._events.values())
        assert backend.gate.attempts == 0
        assert backend.native_diagnostics.snapshot()["counts"]["server"]["session.updated"] == 1
        assert not [
            task
            for task in asyncio.all_tasks()
            if task.get_name() == "acceptance.native-diagnostics"
        ]
        if outcome != "return":
            assert "voice_socket_close_seconds" not in backend.measurements
    finally:
        for task in backend.tasks:
            task.cancel()
        await asyncio.gather(*backend.tasks, return_exceptions=True)


async def test_observer_retains_native_task_error_hidden_by_bounded_voice(tmp_path):
    from beep_agent.native_diagnostics import NativeDiagnostics, NativeObserver
    from livekit.rtc import EventEmitter

    harness = load_harness()
    diagnostics = NativeDiagnostics()
    observer = NativeObserver(diagnostics)
    trigger = asyncio.Event()

    async def fail():
        await trigger.wait()
        raise TypeError(CANARY)

    task = asyncio.create_task(fail())
    runtime = SimpleNamespace(
        model_task=task,
        assembly=SimpleNamespace(agent=SimpleNamespace(realtime_llm_session=EventEmitter())),
    )
    observer.start(runtime, 1)
    try:
        await asyncio.sleep(0.01)
        trigger.set()
        await asyncio.gather(task, return_exceptions=True)
        # This is the wrapper error returned by unchanged bounded_voice.
        diagnostics.failure("voice", harness.Rejected("voice_runtime_failed"))
    finally:
        observer.close()
        await asyncio.gather(observer.task, return_exceptions=True)
    failures = diagnostics.snapshot()["failures"]
    assert {f["type"] for f in failures} == {"TypeError", "Rejected"}, "native task error was lost"
    assert [f["stage"] for f in failures] == ["native_task", "voice"]
    assert CANARY not in json.dumps(diagnostics.snapshot())


@pytest.mark.parametrize("stage", ["voice", "cleanup", "export"])
async def test_failure_export_summary_survives_cancellation_and_business_export_failure(
    tmp_path, stage
):
    harness = load_harness()
    recovery = tmp_path / "recovery.json"
    recovery.write_text('{"schema_retained":true}')

    class Boundary:
        directory = tmp_path
        owns_directory = True
        gate = SimpleNamespace(attempts=1)

        async def prepare(self):
            pass

        async def voice(self):
            if stage == "voice":
                raise asyncio.CancelledError(CANARY)
            raise ValueError(CANARY)

        async def cleanup(self):
            if stage == "cleanup":
                raise asyncio.CancelledError(CANARY)

        async def export(self):
            if stage == "export":
                raise asyncio.CancelledError(CANARY)
            raise RuntimeError(CANARY)

    result = await harness.execute_session(Boundary())
    assert result["category"] == "artifact_export_failed" and result["live_attempts"] == 1
    assert result["artifacts"]["recovery"] == str(recovery)
    assert not result["passed"]
    summary = result["native_diagnostics"]
    assert "failures" in summary, "failure path has no useful summary"
    assert any(f["stage"] == stage and f["type"] == "CancelledError" for f in summary["failures"])
    assert (
        json.loads(Path(result["artifacts"]["native_diagnostics"]).read_text())["failures"]
        == summary["failures"]
    )
    assert CANARY not in json.dumps(result)


def test_caps_unknown_values_and_audio_counters_never_retain_payloads(tmp_path):
    from beep_agent import native_diagnostics as nd

    d = nd.NativeDiagnostics()
    for i in range(nd.MAX_COUNTER + 2):
        d.server_event(
            {
                "type": "response.output_audio.delta",
                "delta": CANARY,
                "event_id": CANARY + str(i),
                "item_id": CANARY + str(i),
            }
        )
        d.client_event({"type": "input_audio_buffer.append", "audio": CANARY})
    assert not any(d.snapshot()["id_handles"].values())
    assert not d.snapshot()["events"]
    for i in range(100):
        d.server_event(
            {
                "type": "conversation.item.added",
                "event_id": str(i),
                "item": {"id": "item" + str(i), "type": CANARY, "role": CANARY, "content": CANARY},
                "previous_item_id": "previous" + str(i),
            }
        )
        d.failure(CANARY, RuntimeError(CANARY))
    for i in range(100):
        d.server_event({"type": CANARY + str(i), "delta": CANARY})
    for i in range(30):
        d.server_event(
            {
                "type": "error",
                "error": {
                    "event_id": "new" + str(i),
                    "code": CANARY,
                    "param": {"arbitrary": CANARY},
                    "type": [CANARY],
                    "message": CANARY,
                    "nested": {"response": {"error": {"body": CANARY}}},
                },
            }
        )
    payload = d.snapshot()
    assert payload["counts"]["server"]["response.output_audio.delta"] == nd.MAX_COUNTER
    assert payload["counts"]["client"]["input_audio_buffer.append"] == nd.MAX_COUNTER
    assert len(payload["events"]) == nd.MAX_EVENTS and len(payload["errors"]) == nd.MAX_ERRORS
    assert len(payload["failures"]) == nd.MAX_FAILURES and payload["failures_dropped"] > 0
    assert all(n <= nd.MAX_IDS for n in payload["id_handles"].values())
    assert set(payload["counts"]["server"]) <= nd.SERVER_TYPES | {"other"}
    assert payload["errors"][-1]["error"]["event"] == "other"
    assert payload["errors"][0]["error"]["code"] == "other"
    assert payload["failures"][0]["stage"] == "other"
    result = {"artifacts": {}, "measurements": {}}
    d.export(SimpleNamespace(directory=tmp_path, owns_directory=True), result)
    path = Path(result["artifacts"]["native_diagnostics"])
    assert 0 < path.stat().st_size <= nd.MAX_ARTIFACT_BYTES
    assert path.stat().st_mode & 0o777 == 0o600
    assert CANARY not in path.read_text() + json.dumps(result)
    assert result["measurements"] == {}


def test_exception_values_causes_frames_and_custom_type_names_are_not_formatted():
    from beep_agent import native_diagnostics as nd

    accesses = []

    class Toxic(Exception):
        def __str__(self):
            accesses.append("str")
            raise AssertionError(CANARY)

        def __repr__(self):
            accesses.append("repr")
            raise AssertionError(CANARY)

        def __getattribute__(self, name):
            if name in {"args", "__cause__", "__context__", "__traceback__", "message", "body"}:
                accesses.append(name)
                raise AssertionError(CANARY)
            return super().__getattribute__(name)

    Toxic.__name__ = CANARY
    Toxic.__module__ = CANARY
    exc = Toxic(CANARY, {"nested": CANARY})
    exc.__cause__ = ValueError(CANARY)
    exc.__context__ = RuntimeError(CANARY)
    d = nd.NativeDiagnostics()
    # Synthetic code objects: only trusted source path aliases survive; no code/local/function text.
    namespace = {"exc": exc}
    exec(compile("def fail():\n    raise exc\n", CANARY, "exec"), namespace)
    try:
        namespace["fail"]()
    except Toxic as caught:
        d.failure("voice", caught)
    failure = d.snapshot()["failures"][0]
    assert failure == {"stage": "voice", "type": "other", "frames": []}
    assert not accesses
    assert CANARY not in json.dumps(d.snapshot())
    # A genuine installed SDK property exception gives a useful line reference, not its message.
    from livekit.agents import Agent

    try:
        Agent(instructions="explicit synthetic fixture").realtime_llm_session
    except RuntimeError as caught:
        d.failure("voice", caught)
    assert any(
        f["source"] == "livekit/agents/voice/agent.py"
        for f in d.snapshot()["failures"][-1]["frames"]
    )


@pytest.mark.parametrize("exception", [TypeError, RuntimeError, asyncio.CancelledError])
async def test_formatter_errors_cannot_raise_or_log_into_sdk_flow(monkeypatch, caplog, exception):
    from beep_agent import native_diagnostics as nd
    from livekit.rtc import EventEmitter

    d = nd.NativeDiagnostics()
    native = EventEmitter()
    observer = nd.NativeObserver(d)
    runtime = SimpleNamespace(
        assembly=SimpleNamespace(agent=SimpleNamespace(realtime_llm_session=native))
    )
    observer.start(runtime, 1)
    await asyncio.sleep(0.01)

    def broken(*args):
        raise exception(CANARY)

    monkeypatch.setattr(d, "_record_event", broken)
    for name in nd.CALLBACKS:
        native.emit(name, {"type": "error", "message": CANARY})
    assert d.snapshot()["formatter_errors"] == 2
    assert not caplog.records
    observer.close()
    await asyncio.gather(observer.task, return_exceptions=True)
    assert not any(native._events.values())


@pytest.mark.parametrize("point", ["before_start", "polling", "attached", "deadline", "partial_on"])
async def test_observer_cancelled_attach_deadline_and_detach_are_bounded(point):
    from beep_agent import native_diagnostics as nd
    from livekit.rtc import EventEmitter

    class Partial(EventEmitter):
        def on(self, name, callback):
            super().on(name, callback)
            if point == "partial_on" and name == nd.CALLBACKS[1]:
                raise asyncio.CancelledError(CANARY)

    d = nd.NativeDiagnostics()
    native = Partial()
    observer = nd.NativeObserver(d)
    runtime = SimpleNamespace(assembly=None)
    if point not in {"before_start", "polling"}:
        runtime.assembly = SimpleNamespace(agent=SimpleNamespace(realtime_llm_session=native))
    observer.start(runtime, 0.03)
    original_task = observer.task
    observer.start(runtime, 60)  # duplicate start is inert
    assert observer.task is original_task
    if point == "before_start":
        observer.close()
    elif point in {"polling", "attached"}:
        await asyncio.sleep(0.01)
        original_task.cancel()
    await asyncio.wait_for(asyncio.gather(original_task, return_exceptions=True), 0.3)
    await asyncio.sleep(0)
    observer.close()
    assert not any(native._events.values())
    assert d.snapshot()["observer"]["stopped"] is True
    assert d.snapshot()["observer"]["polls"] <= nd.MAX_POLLS
    before = json.dumps(d.snapshot())
    native.emit(nd.CALLBACKS[0], {"type": "error", "error": {"message": CANARY}})
    assert before == json.dumps(d.snapshot())


def test_export_failure_never_deletes_unowned_temporary_or_loses_safe_summary(tmp_path):
    from beep_agent.native_diagnostics import NativeDiagnostics

    temporary = tmp_path / "native-diagnostics.json.tmp"
    temporary.write_text("another writer's explicitly synthetic sentinel")
    d = NativeDiagnostics()
    d.failure("voice", ValueError(CANARY))
    result = {"artifacts": {}}
    d.export(SimpleNamespace(directory=tmp_path, owns_directory=True), result)
    assert temporary.is_file(), "diagnostics deleted a temporary it did not create"
    assert result["native_diagnostics"]["export_status"] == "write_failed"
    assert result["native_diagnostics"]["failures"][0]["type"] == "ValueError"
    assert not result["artifacts"]
    assert CANARY not in json.dumps(result)


async def test_cancelled_observer_start_closes_unstarted_coroutine(monkeypatch):
    from beep_agent import native_diagnostics as nd
    import inspect

    captured = []

    def cancelled(coro, **kwargs):
        captured.append(coro)
        raise asyncio.CancelledError(CANARY)

    monkeypatch.setattr(asyncio, "create_task", cancelled)
    d = nd.NativeDiagnostics()
    observer = nd.NativeObserver(d)
    observer.start(SimpleNamespace(assembly=None), 1)
    try:
        assert len(captured) == 1
        assert inspect.getcoroutinestate(captured[0]) == inspect.CORO_CLOSED
        assert d.snapshot()["observer"]["stopped"]
    finally:
        for coro in captured:
            coro.close()  # fixture owns failure case; never leave a runtime warning


async def test_real_installed_sdk_raw_callbacks_no_extra_attempt_or_request(monkeypatch, tmp_path):
    """Actual SDK serializer/receiver callbacks, explicit synthetic in-memory socket.

    No provider/RTC transport exists. Agent activity below is the sole fixture seam
    for exposing the actual installed RealtimeSession through the real property.
    """
    import socket
    from contextlib import suppress
    import aiohttp
    from livekit.agents import Agent, APIConnectOptions
    from livekit.plugins.openai import realtime
    from beep_agent.native_diagnostics import NativeDiagnostics, NativeObserver, CALLBACKS

    def forbidden(*args, **kwargs):
        pytest.fail("diagnostics attempted a network or reply operation")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(realtime.RealtimeSession, "generate_reply", forbidden)
    harness = load_harness()
    observations = []
    for enabled in (False, True):
        go = asyncio.Event()
        sent = []
        received = asyncio.Queue()
        opens = []
        peers = []
        d = NativeDiagnostics()
        observer = NativeObserver(d)

        class SyntheticSocket:
            closed = False

            async def send_str(self, raw):
                # Fixture inspects only event type; no headers/URL/raw body is retained.
                sent.append(json.loads(raw)["type"])

            async def receive(self):
                return await received.get()

            async def close(self):
                self.closed = True
                received.put_nowait(SimpleNamespace(type=aiohttp.WSMsgType.CLOSE))

        ws = SyntheticSocket()

        class SyntheticHTTP:
            async def ws_connect(self, **kwargs):
                opens.append(1)
                await go.wait()
                return ws

        def construct():
            return realtime.RealtimeModel(
                api_key="explicit-synthetic-no-provider-key",
                base_url="http://127.0.0.1/not-requested",
                http_session=SyntheticHTTP(),
                max_session_duration=None,
                conn_options=APIConnectOptions(max_retry=0, timeout=1),
            )

        gate = harness.SingleAttempt(construct)
        model = gate()
        native = model.session()  # one fixture-owned session; observer must never create one
        monkeypatch.setattr(model, "session", forbidden)
        agent = Agent(instructions="synthetic offline callback fixture")
        agent._activity = SimpleNamespace(realtime_llm_session=native)
        runtime = SimpleNamespace(assembly=SimpleNamespace(agent=agent), model_task=None)
        native.on(CALLBACKS[0], lambda event: peers.append(event.get("type")))
        existing = set(native._events[CALLBACKS[0]])
        try:
            with harness.quiet_sdk_output():
                if enabled:
                    observer.start(runtime, 1)
                    for _ in range(40):
                        if d.snapshot()["observer"]["attached"]:
                            break
                        await asyncio.sleep(0.005)
                    assert d.snapshot()["observer"]["attached"]
                go.set()
                native.send_event(
                    {
                        "type": "conversation.item.create",
                        "event_id": "fixture-event",
                        "item": {
                            "id": "fixture-item",
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": CANARY}],
                        },
                    }
                )
                received.put_nowait(
                    SimpleNamespace(
                        type=aiohttp.WSMsgType.TEXT,
                        data=json.dumps(
                            {"type": "session.created", "session": {"instructions": CANARY}}
                        ),
                    )
                )
                received.put_nowait(
                    SimpleNamespace(
                        type=aiohttp.WSMsgType.TEXT,
                        data=json.dumps(
                            {
                                "type": "error",
                                "event_id": "fixture-error",
                                "error": {
                                    "event_id": "fixture-event",
                                    "type": "invalid_request_error",
                                    "code": "invalid_value",
                                    "param": "item.content[0].type",
                                    "message": CANARY,
                                },
                            }
                        ),
                    )
                )
                for _ in range(100):
                    if len(sent) == 2 and len(peers) == 2:
                        break
                    await asyncio.sleep(0.005)
                assert sent == ["session.update", "conversation.item.create"]
                assert peers == ["session.created", "error"]
                assert len(opens) == gate.attempts == 1
                if enabled:
                    assert d.snapshot()["counts"]["client"] == {
                        "session.update": 1,
                        "conversation.item.create": 1,
                    }
                    request = next(
                        e for e in d.snapshot()["events"] if e["type"] == "conversation.item.create"
                    )
                    assert d.snapshot()["errors"][0]["error"]["event"] == request["event"]
                    result = {"artifacts": {}}
                    d.export(SimpleNamespace(directory=tmp_path, owns_directory=True), result)
                    assert CANARY not in Path(result["artifacts"]["native_diagnostics"]).read_text()
                observations.append((len(opens), gate.attempts, sent, peers))
        finally:
            observer.close()
            if observer.task:
                await asyncio.gather(observer.task, return_exceptions=True)
            assert native._events[CALLBACKS[0]] == existing
            assert not native._events.get(CALLBACKS[1])
            with harness.quiet_sdk_output(), suppress(Exception, asyncio.CancelledError):
                await native.aclose()
            await model.aclose()
            assert ws.closed
    assert observations[0] == observations[1]


def test_unknown_identifier_and_value_objects_are_other_without_conversion():
    from beep_agent.native_diagnostics import NativeDiagnostics

    class Toxic:
        def __str__(self):
            raise AssertionError(CANARY)

        def __hash__(self):
            raise AssertionError(CANARY)

    d = NativeDiagnostics()
    for identifier in ("\ud800", CANARY * 100, Toxic(), 42):
        d.server_event(
            {
                "type": "error",
                "event_id": identifier,
                "error": {
                    "event_id": identifier,
                    "code": Toxic(),
                    "type": Toxic(),
                    "param": Toxic(),
                },
            }
        )
    payload = d.snapshot()
    assert len(payload["errors"]) == 4, "invalid ID caused the entire metadata record to be lost"
    assert payload["formatter_errors"] == 0
    assert all(
        e["event"] == e["error"]["event"] == e["error"]["code"] == "other"
        for e in payload["errors"]
    )


async def test_off_failure_attempts_both_listeners_and_makes_late_callbacks_inert():
    from beep_agent import native_diagnostics as nd
    from livekit.rtc import EventEmitter

    attempted = []

    class BrokenOff(EventEmitter):
        def off(self, name, callback):
            attempted.append(name)
            if name == nd.CALLBACKS[0]:
                raise asyncio.CancelledError(CANARY)
            return super().off(name, callback)

    native = BrokenOff()
    d = nd.NativeDiagnostics()
    observer = nd.NativeObserver(d)
    observer.start(
        SimpleNamespace(
            assembly=SimpleNamespace(agent=SimpleNamespace(realtime_llm_session=native))
        ),
        1,
    )
    await asyncio.sleep(0.01)
    observer.close()
    await asyncio.gather(observer.task, return_exceptions=True)
    assert attempted == list(nd.CALLBACKS)
    assert d.snapshot()["observer"]["detach_failed"]
    snapshot = json.dumps(d.snapshot())
    native.emit(nd.CALLBACKS[0], {"type": "error"})
    assert snapshot == json.dumps(d.snapshot())
    # Only the fixture emitter is deliberately defective; the actual SDK off is tested above.
    for callback in tuple(native._events[nd.CALLBACKS[0]]):
        EventEmitter.off(native, nd.CALLBACKS[0], callback)


@pytest.mark.parametrize("seconds", [0, -1, float("nan"), float("inf"), None, "60"])
async def test_invalid_observer_budgets_do_not_poll_or_attach(seconds):
    from beep_agent.native_diagnostics import NativeDiagnostics, NativeObserver

    d = NativeDiagnostics()
    observer = NativeObserver(d)
    observer.start(SimpleNamespace(assembly=None), seconds)
    await asyncio.wait_for(asyncio.gather(observer.task, return_exceptions=True), 0.1)
    assert d.snapshot()["observer"]["polls"] == 0
    assert d.snapshot()["observer"]["stopped"]


def test_tracebacks_and_largest_records_stay_inside_artifact_cap(tmp_path):
    from beep_agent import native_diagnostics as nd

    d = nd.NativeDiagnostics()
    # Synthetic recursion uses a known path but does not execute/modify project source.
    namespace = {}
    exec(
        compile(
            "def fail(n):\n    if n: return fail(n-1)\n    raise RuntimeError('synthetic')\n",
            str(ROOT / "scripts/verify_live_session.py"),
            "exec",
        ),
        namespace,
    )
    for _ in range(nd.MAX_FAILURES + 1):
        try:
            namespace["fail"](100)
        except RuntimeError as exc:
            d.failure("voice", exc)
    for i in range(nd.MAX_EVENTS):
        d.server_event(
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "event_id": str(i),
                "item_id": "i" + str(i),
                "previous_item_id": "p" + str(i),
                "response_id": "r" + str(i),
                "item": {"type": "function_call_output", "role": "assistant"},
                "transcript": CANARY,
            }
        )
    for i in range(nd.MAX_ERRORS):
        d.server_event(
            {
                "type": "response.done",
                "event_id": str(i),
                "item_id": "i" + str(i),
                "previous_item_id": "p" + str(i),
                "item": {"type": "function_call_output", "role": "assistant"},
                "response": {
                    "id": "r" + str(i),
                    "status": "in_progress",
                    "status_details": {
                        "error": {
                            "code": max(nd.ERROR_CODES, key=len),
                            "type": max(nd.ERROR_TYPES, key=len),
                            "param": max(nd.ERROR_PARAMS, key=len),
                            "event_id": str(i),
                            "message": CANARY,
                        }
                    },
                },
            }
        )
    for event in nd.SERVER_TYPES:
        d.server_event({"type": event})
    for event in nd.CLIENT_TYPES:
        d.client_event({"type": event})
    assert all(len(f["frames"]) <= nd.MAX_FRAMES for f in d.snapshot()["failures"])
    result = {}
    d.export(SimpleNamespace(directory=tmp_path, owns_directory=True), result)
    assert result["native_diagnostics"]["exported"]
    assert Path(result["artifacts"]["native_diagnostics"]).stat().st_size <= nd.MAX_ARTIFACT_BYTES
    assert CANARY not in Path(result["artifacts"]["native_diagnostics"]).read_text()


def test_unowned_or_unwritable_directory_never_makes_acceptance_evidence(tmp_path, monkeypatch):
    from beep_agent import native_diagnostics as nd

    d = nd.NativeDiagnostics()
    result = {}
    d.export(SimpleNamespace(directory=tmp_path, owns_directory=False), result)
    assert result["native_diagnostics"]["export_status"] == "directory_unavailable"
    assert not list(tmp_path.iterdir())

    def failed(*args):
        raise OSError(CANARY)

    monkeypatch.setattr(nd.os, "replace", failed)
    result = {}
    d.export(SimpleNamespace(directory=tmp_path, owns_directory=True), result)
    assert result["native_diagnostics"]["export_status"] == "write_failed"
    assert not list(tmp_path.iterdir())
    assert CANARY not in json.dumps(result)


def test_frozen_acceptance_and_lifecycle_blocks_remain_byte_unchanged():
    import ast
    import hashlib

    expected = {
        "Rejected": "dceb011de17b43496a8283c47884617eb49ff0128b41fc1290ee160847a96dd7",
        "Rejected.__init__": "b774bbc3d74f45433ff94a5c3c55bedab94009fb728c5ee6692f6e07639c3218",
        "SafeParser": "3b36be226d04f6b0578802be95cd774ae8c0c7f058199f6f29e61e6c2b61e46b",
        "SafeParser.error": "a63e2947ed1bd3632505ed070b597fda0e9e30fd57a006f1e1efee5326e58b89",
        "load_settings": "b356d7b6b2d0b5d6d36fee53b9abfced499b720684ca284030666ef703ffe930",
        "SingleAttempt": "c283cacf8fa093b020716757865f47e0db7abb4ef8a993e3b75d9a9c9c7a7d0a",
        "SingleAttempt.__init__": "5bd8327f7f3f0a2a11c3f3ddaa21cddf501068ddf907de635300390554ce8358",
        "SingleAttempt.__call__": "748f848d255ddde87d42e77c627c8076eebcd87126130ffbca9819416e33d638",
        "bounded_voice": "fd03fff2ff7de9a40a56f5f94313380b1ee13e07a565bc462a04df1d0648ffa0",
        "acceptance_complete": "acd645e845b49a412cb0a039d8f93234ce70b2175f2e742417b4cda27fcbf567",
        "preflight": "2b9e628dccbc8f89d8715931ab833b3fa3088e5054e6caa83a658ed5329e9e0d",
        "local_command": "e21b88031bd716c3352e14bc238ed0ee53824a0fc301c356efab578807db81fa",
        "synthetic_fixture": "2c86b2e370ae35fe7757242207263834e76b816de5c3d40f1ec68d384ecc7a25",
        "CappedReasoning": "6b269347533b2238b7f39b5d7f7c1ab2c7e513406125737bce22190c8e10980c",
        "CappedReasoning.__init__": "70e6836df61d1aed1da3a82dc8bc71eb00a569c74561ae0892c0e660221e8ce3",
        "CappedReasoning._request": "ebdbbce29f9cd6c19b932fcb3d1bdffc39ef912700a2a20dc1315ee291cd0184",
        "CappedReasoning.extract": "a065db1ddbd6bfba1be432be54d990a57b91755bf21209a7941b5bfb7f36e6db",
        "CappedReasoning.synthesize_report": "156094f4c66521170020305afe48d656651d3ca553337f6cc9819fb2b55b4c5a",
        "RealSession.__init__": "65bed1762f8db9d61903a6380b70d6ab4a9eb9e69175da79b52b71c51181fab0",
        "RealSession.prepare": "ec72a8c9b7a2c788d54964e8359572a0e178203dc1f3e23cf22a0e356df5d967",
        "RealSession.connect_participants": "da3cda1facf88d281bd46dc2fc9af3a8598be9474ac3951839e54feb1a9f402f",
        "RealSession.receive_audio": "1242437f1d170b61533b20883053013261557a148715f6a85c3ef1bb90115af7",
        "RealSession.start_media": "ddf52e6262c129c4093d9e1e8088498f12762f6e412e8c87b98e6646d3b07124",
        "RealSession.publish_media": "68d50727f5471303f816a71940daba31a6954ad731ec2c65394eeb37b462dfbf",
        "RealSession.observer_factory": "6b38891d10b12841f3d0f27d40498c546f98e5d14f795fb6c81120c8962c97a3",
        "RealSession.voice": "6f7e51fbcbf583fd54cf521ca54e62755823f15691cfaff90c73d5810d59678f",
        "RealSession.capture_measurements": "251812bbab92393a014ad97e295a47f18fb13e5ffec301ec62acb693d153bf75",
        "RealSession.finish": "f51a388dd859c08e7b0382e8c3d53dc367ebd89e8f8f0eb8c81af279b78c2a01",
        "RealSession.download_recording": "1dbc9b4a8766d258d81809601cc91998128d413e9bb71c0cac213074b6814c74",
        "RealSession.cancel_media": "98ed7f1e37c98098e191a71358edd89b088dee5a6ed76bf61e27b5208a08e432",
        "RealSession.audit": "f30ab8392db2710c3b81dee7ef70c00edbfd37d0d82488dc7778d9a6b5bb8ecb",
        "RealSession.cleanup": "5673f6a4e7ec050af8d8cfbd7e74e5fb1c2db59d0129307f0f0b3b1db497d87e",
        "RealSession.export": "ff7722d49a1c6d3af8b2fd4b9fae62aac85fd1ffa232b5fd470ddf8950876e47",
        "RealSession.call": "ee0949b7b7b6255efa1f4a1c6416b94cc04b5ac8155b60fee25fe6db5e52a86b",
        "RealSession.journal": "dc32c9033a199fac5219761166a0ad2f2690837e267c62ab349386047abf8c7e",
        "RealSession.prepare_database": "830d4277365749d29a95697ce0368e6eafde0847d6463b018aa53ebe1756e442",
        "RealSession.elapsed_intro_fixture": "5b333447c6da16137270d3b193eded044c271cafedc6c45e6e1d87c74f027adf",
        "write_json": "f7e27c5390559d56408a72182164354a412a070e044002c5223ce91d854e2908",
        "run_live": "fd9193af8b0999ab1983d190b12d2181e782686d6f7348e887d0809c0520fe77",
        "quiet_sdk_output": "4fe11da7edffa76a821e992da5ac50441cddf8276c9f6a521d37beedd155d976",
        "main": "08c29ea0f75664c83ae213c5ff050c32d21a13638bd6c350f07c8a15d6567499",
    }
    raw = (ROOT / "scripts/verify_live_session.py").read_bytes()
    lines = raw.splitlines(keepends=True)
    actual = {}

    def walk(nodes, prefix=""):
        for node in nodes:
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + node.name
                if name in expected:
                    actual[name] = hashlib.sha256(
                        b"".join(lines[node.lineno - 1 : node.end_lineno])
                    ).hexdigest()
                if isinstance(node, ast.ClassDef):
                    walk(node.body, name + ".")

    walk(ast.parse(raw).body)
    assert actual == expected


async def test_real_bounded_voice_failure_exports_no_invented_close_measurement(
    monkeypatch, tmp_path
):
    import socket
    import beep_agent.realtime as runtime_module

    harness = load_harness()
    closed = []
    owners = []

    class Runtime:
        assembly = model_task = None
        failures = 0

        def __init__(self, *args, **kwargs):
            self.stop = asyncio.Event()
            self.fence = SimpleNamespace(invalidate=lambda: None)
            owners.append(kwargs["assembly_factory"].factory.keywords["http_session"])

        async def run(self):
            await asyncio.sleep(0.005)
            raise ValueError(CANARY)

        async def aclose(self):
            closed.append(1)

    monkeypatch.setattr(runtime_module, "InterviewRuntime", Runtime)
    monkeypatch.setattr(socket.socket, "connect", lambda *a: pytest.fail("network forbidden"))
    backend = harness.RealSession.__new__(harness.RealSession)
    backend.settings = SimpleNamespace()
    backend.store = SimpleNamespace(list_events=lambda *a: [])
    backend.state = {"id": "explicit-synthetic"}
    backend.rooms = [None, None, None]
    backend.gate = harness.SingleAttempt(lambda *a, **k: pytest.fail("unexpected assembly"))
    backend.args = SimpleNamespace(seconds=1)
    backend.tasks, backend.events, backend.measurements = [], [], {}
    backend.directory, backend.owns_directory = tmp_path, True
    backend.media_stop = asyncio.Event()

    async def prepare():
        pass

    async def read_events(*args):
        return []

    async def cleanup():
        for task in backend.tasks:
            task.cancel()
        await asyncio.gather(*backend.tasks, return_exceptions=True)

    async def export():
        return {
            "artifacts": {},
            "measurements": backend.measurements,
            "live_attempts": backend.gate.attempts,
        }

    backend.prepare, backend.call, backend.cleanup, backend.export = (
        prepare,
        read_events,
        cleanup,
        export,
    )
    result = await harness.execute_session(backend)
    assert result["category"] == "voice_failed" and not result["passed"]
    assert closed == [1] and len(owners) == 1 and owners[0].closed
    assert (
        backend.gate.attempts == 0
        and result["measurements"]["native_http_requests_dispatched"] == 0
    )
    assert "voice_socket_close_seconds" not in result["measurements"]
    assert "voice_input_window_seconds" not in result["measurements"]
    payload = json.loads(Path(result["artifacts"]["native_diagnostics"]).read_text())
    assert payload["native_close_elapsed"] == "not_measured_by_diagnostics"
    assert payload["failures"][0]["type"] == "ValueError"
    assert not [t for t in asyncio.all_tasks() if t.get_name() == "acceptance.native-diagnostics"]
    assert CANARY not in json.dumps(result) + json.dumps(payload)
