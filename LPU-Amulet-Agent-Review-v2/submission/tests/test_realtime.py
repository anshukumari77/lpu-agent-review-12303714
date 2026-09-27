"""SDK construction and application state tests, not live provider claims."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import psycopg


def state(**changes):
    result = dict(
        id="s",
        tenant_id="t",
        status="active",
        consent_epoch=2,
        started_at=datetime.now(timezone.utc).isoformat(),
        max_seconds=5400,
        client_consent={"ai": True, "recording": True},
        facilitator_consent={"ai": True, "recording": True},
        event_seq=4,
        recording_status="recording",
    )
    result.update(changes)
    return result


def settings():
    from beep_agent.config import Settings

    return Settings(
        database_url="postgresql:///synthetic",
        admin_token="synthetic-admin",
        signing_secret="synthetic-signing-secret-at-least-32-chars",
        livekit_api_key="devkey",
        livekit_api_secret="secret",
        openai_api_key="synthetic-not-real",
        s3_bucket="synthetic",
        s3_access_key="synthetic",
        s3_secret_key="synthetic",
    )


@pytest.mark.asyncio
async def test_native_sdk_assembly_is_gated_and_explicit():
    from beep_agent.realtime import assemble_native, NativeAssembly
    from livekit.agents import AgentSession, Agent
    from livekit.plugins.openai import realtime

    cfg = settings()
    with pytest.raises(PermissionError):
        assemble_native(cfg, state(status="introduction"))
    with pytest.raises(PermissionError):
        assemble_native(cfg, state(client_consent={"ai": False, "recording": True}))
    with pytest.raises(Exception):
        assemble_native(
            cfg.model_copy(update={"openai_api_key": cfg.admin_token.__class__("")}), state()
        )
    assembly = assemble_native(cfg, state())
    assert isinstance(assembly, NativeAssembly)
    assert isinstance(assembly.session, AgentSession)
    assert isinstance(assembly.agent, Agent)
    assert isinstance(assembly.model, realtime.RealtimeModel)
    assert assembly.options.participant_identity == "client:s"
    assert assembly.options.video_input is False
    assert assembly.options.text_input is False
    assert assembly.options.close_on_disconnect is False
    assert assembly.model.model == cfg.realtime_model
    assert assembly.session.vad is None
    assert assembly.session.stt is None and assembly.session.tts is None
    assert assembly.model._opts.conn_options.max_retry == 0
    assert assembly.model._opts.max_session_duration is None  # app owns rollover
    await assembly.model.aclose()


def test_probe_fencing_requires_epoch_revision_sequence_and_active_state():
    from beep_agent.realtime import probe_is_current, RuntimeFence

    current = state()
    probe = dict(id="p", text="Which approval?", based_on_revision=3, consent_epoch=2)
    snapshot = dict(revision=3, consent_epoch=2, last_event_seq=4, probe=probe)
    assert probe_is_current(snapshot, current)
    assert not probe_is_current(snapshot, state(event_seq=5))
    assert not probe_is_current(snapshot, state(consent_epoch=3))
    assert not probe_is_current(snapshot, state(status="paused"))
    fence = RuntimeFence()
    fence.update(current, 10)
    ticket = fence.ticket
    assert fence.valid(ticket, 10.5)
    assert not fence.valid(ticket, 12)  # stale control-plane poll fails closed
    fence.update(state(status="paused", consent_epoch=3), 12)
    assert not fence.valid(ticket, 12)


@pytest.mark.asyncio
async def test_epoch_shutdown_clears_both_buffers_and_joins_owned_tasks():
    import asyncio
    from beep_agent.realtime import close_native

    # SDK transport lifecycle seam: instrument boundary effects, not inference.
    actions = []

    class Input:
        def set_audio_enabled(self, value):
            actions.append(("audio", value))

        def set_video_enabled(self, value):
            actions.append(("video", value))

    class Output:
        audio = SimpleNamespace(clear_buffer=lambda: actions.append("playback.clear"))

    class Session:
        input, output = Input(), Output()

        def clear_user_turn(self):
            actions.append("input.clear")

        async def interrupt(self, **kwargs):
            actions.append("interrupt")

        async def aclose(self):
            actions.append("session.close")

    class Model:
        async def aclose(self):
            actions.append("model.close")

    cancelled = asyncio.Event()

    async def slow():
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    task = asyncio.create_task(slow())
    await asyncio.sleep(0)
    await close_native(SimpleNamespace(session=Session(), model=Model()), [task])
    assert task.done() and cancelled.is_set()
    assert actions[:2] == [("audio", False), ("video", False)]
    assert "playback.clear" in actions and "input.clear" in actions
    assert actions[-2:] == ["session.close", "model.close"]


def test_transcript_boundary_preserves_partial_delivered_text_and_provenance():
    from livekit.agents import llm
    from beep_agent.realtime import transcript_evidence

    client = llm.ChatMessage(id="item-client", role="user", content=["I approve the quote."])
    event = transcript_evidence(client, state(), 1200)
    assert event["actor"] == "client" and event["id"] == "transcript:item-client"
    agent = llm.ChatMessage(
        id="item-agent", role="assistant", content=["Who approves"], interrupted=True
    )
    delivered = transcript_evidence(agent, state(), 1300)
    assert delivered["text"] == "Who approves"
    assert "interrupted=true" in delivered["source_ref"]
    assert "livekit-agents@1.8.1" in delivered["source_ref"]
    assert transcript_evidence(llm.ChatMessage(role="assistant", content=[]), state(), 1400) is None
    assert transcript_evidence(client, state(status="paused"), 1400) is None


def test_telemetry_keeps_measured_usage_not_fake_cost_and_redacts_sdk_logs():
    import logging
    from livekit.agents.metrics import RealtimeModelMetrics
    from beep_agent.telemetry import usage_record, SDKPrivacyFilter

    metric = RealtimeModelMetrics(
        request_id="req-test",
        timestamp=1,
        input_tokens=10,
        output_tokens=2,
        total_tokens=12,
        input_token_details={},
        output_token_details={},
    )
    usage = usage_record(metric, model="gpt-realtime")
    assert usage["measurement"] == "measured"
    assert usage["input_tokens"] == 10
    assert "cost_aud" not in usage
    assert usage["versions"]["livekit-agents"] == "1.8.1"
    record = logging.LogRecord(
        "livekit.agents", 40, "x", 1, "transcript=private key=sk-sensitive", (), None
    )
    SDKPrivacyFilter().filter(record)
    assert "private" not in record.getMessage() and "sk-sensitive" not in record.getMessage()
    from livekit.agents.metrics.usage import AgentSessionUsage, LLMModelUsage
    from beep_agent.telemetry import SessionUsageDelta

    collector = SessionUsageDelta()
    first = AgentSessionUsage(
        model_usage=[LLMModelUsage(provider="openai", model="gpt-realtime", input_tokens=10)]
    )
    second = AgentSessionUsage(
        model_usage=[LLMModelUsage(provider="openai", model="gpt-realtime", input_tokens=25)]
    )
    assert collector.collect(first)[0]["input_tokens"] == 10
    assert collector.collect(second)[0]["input_tokens"] == 15
    assert collector.collect(second) == []


async def test_runtime_intro_client_identity_gate_and_server_assembly(database):
    from beep_agent.realtime import InterviewRuntime, create_server
    from beep_agent.store import Store
    from livekit.agents import AgentServer

    store = Store(database)
    store.initialize()
    created = store.create_session("tenant", title="Synthetic", pack_id="general", offer="paid")
    sid = created["id"]
    for role in ("client", "facilitator"):
        store.set_consent("tenant", sid, role, ai=True, recording=True)
    calls = []

    def forbidden_model(*args, **kwargs):
        calls.append("model")
        raise AssertionError("No paid client should be constructed")

    room = SimpleNamespace(
        remote_participants={
            "facilitator:" + sid: SimpleNamespace(
                identity="facilitator:" + sid, track_publications={}
            )
        },
        name=created["room_name"],
        isconnected=lambda: True,
    )
    runtime = InterviewRuntime(
        settings(), store, "tenant", sid, room, assembly_factory=forbidden_model
    )
    await runtime.poll()
    assert runtime.assembly is None and not calls
    store.control("tenant", sid, "client", "pause")
    await runtime.poll()
    assert runtime.assembly is None and not calls
    await runtime.aclose()
    assert runtime.model_task is None
    server = create_server(settings())
    assert isinstance(server, AgentServer)


# Reuse the real PostgreSQL schema fixture, not a persistence stub.
from test_worker import database as worker_database  # noqa: E402


@pytest.fixture
def database():
    yield from worker_database.__wrapped__()


async def test_runtime_rollover_pause_and_overall_cap_join_generation(database):
    import asyncio
    import time
    from beep_agent.realtime import InterviewRuntime
    from beep_agent.store import Store
    from test_worker import active_session

    store = Store(database)
    store.initialize()
    state_now = active_session(store)
    sid = state_now["id"]
    store.set_recording("tenant", sid, "recording", "EG-test")
    room = SimpleNamespace(
        remote_participants={
            "client:" + sid: SimpleNamespace(identity="client:" + sid, track_publications={})
        },
        name=state_now["room_name"],
        isconnected=lambda: True,
    )
    calls = []

    class TransportBoundaryRuntime(InterviewRuntime):
        async def _generation(self, ticket):
            calls.append("start")
            try:
                await asyncio.Event().wait()
            finally:
                calls.append("closed")

    runtime = TransportBoundaryRuntime(settings(), store, "tenant", sid, room)
    await runtime.poll()
    await asyncio.sleep(0)
    assert calls == ["start"]
    runtime.model_started = time.monotonic() - 3301
    await runtime.poll()
    await asyncio.sleep(0)
    assert calls == ["start", "closed", "start"]
    store.control("tenant", sid, "client", "pause")
    await runtime.poll()
    assert calls[-1] == "closed" and runtime.model_task is None
    with psycopg.connect(database) as conn:
        conn.execute(
            "UPDATE beep_sessions SET data=jsonb_set(data,'{started_at}',to_jsonb((now()-interval '91 minutes')::text)) WHERE id=%s",
            (sid,),
        )
    await runtime.poll()
    assert runtime.finished
    await runtime.aclose()


def test_silent_probe_and_pre_cap_readback_are_clock_fenced():
    import time
    from datetime import timedelta
    from beep_agent.realtime import DirectivePolicy

    now = time.monotonic()
    policy = DirectivePolicy(last_activity=now - 30, last_spoken=now - 30)
    session = state()
    snapshot = dict(
        revision=1,
        consent_epoch=2,
        last_event_seq=4,
        claims=[],
        steps=[],
        unknowns=["Approval threshold"],
        probe=dict(
            id="p-silent", text="Show the approval threshold.", consent_epoch=2, based_on_revision=1
        ),
    )
    assert policy.choose(snapshot, session, now, idle=False) is None
    directive = policy.choose(snapshot, session, now, idle=True)
    assert "approval threshold" in directive.lower()
    assert policy.choose(snapshot, session, now + 30, idle=True) is None
    ending = state(started_at=(datetime.now(timezone.utc) - timedelta(seconds=5290)).isoformat())
    readback = policy.choose(snapshot, ending, now + 40, idle=True)
    assert "read back" in readback.lower() and "correction" in readback.lower()
    assert policy.choose(snapshot, ending, now + 80, idle=True) is None
    assert policy.choose(snapshot, state(status="paused"), now + 100, idle=True) is None


async def test_generation_remains_owned_until_shutdown_finishes():
    import asyncio
    from beep_agent.realtime import InterviewRuntime

    runtime = InterviewRuntime(
        settings(), None, "tenant", "session", SimpleNamespace(remote_participants={})
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def generation():
        try:
            await asyncio.Event().wait()
        finally:
            entered.set()
            await release.wait()

    task = asyncio.create_task(generation())
    runtime.model_task = task
    await asyncio.sleep(0)
    stop = asyncio.create_task(runtime._stop_generation())
    await entered.wait()
    assert runtime.model_task is task  # no replacement model before old cleanup completes
    release.set()
    await stop
    assert runtime.model_task is None and task.done()
