"""Real PostgreSQL + LangGraph + OpenAI SDK; only HTTP provider is synthetic."""

import json
import os
import uuid

import httpx
import psycopg
import pytest
from psycopg.conninfo import make_conninfo


def settings():
    from test_realtime import settings as make_settings

    return make_settings()


@pytest.fixture
def database():
    dsn = os.environ.get("BEEP_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("BEEP_TEST_DATABASE_URL required for actual PostgreSQL integration")
    schema = "worker_test_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(psycopg.sql.SQL("CREATE SCHEMA {}").format(psycopg.sql.Identifier(schema)))
    isolated = make_conninfo(dsn, options=f"-c search_path={schema}")
    try:
        yield isolated
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                psycopg.sql.SQL("DROP SCHEMA {} CASCADE").format(psycopg.sql.Identifier(schema))
            )


def active_session(store, *, recording=False):
    session = store.create_session(
        "tenant", title="Synthetic quote review", pack_id="quotation", offer="paid"
    )
    sid = session["id"]
    for role in ("client", "facilitator"):
        store.set_consent("tenant", sid, role, ai=True, recording=True)
    # Deterministic elapsed intro fixture; production control still checks server time.
    with psycopg.connect(store.database_url) as conn:
        conn.execute(
            "UPDATE beep_sessions SET data=jsonb_set(data,'{started_at}',to_jsonb((now()-interval '16 minutes')::text)) WHERE id=%s",
            (sid,),
        )
        # Discovery tracer excludes the independent recording service boundary.
        conn.execute("DELETE FROM beep_jobs WHERE session_id=%s AND kind='recording_start'", (sid,))
    active = store.control("tenant", sid, "client", "handover")
    if recording:
        # Explicit synthetic ACTIVE readback, not a real recorder claim.
        active = store.set_recording('tenant', sid, 'recording', 'EG-synthetic-fixture')
    return active


def response(request):
    data = json.loads(request.content)
    content = json.loads(data["input"][0]["content"][0]["text"])["untrusted_data"]
    event = content["event"]
    if event is None:
        extraction = {
            "summary_claim_ids": [c["id"] for c in content["snapshot"]["claims"][:1]],
            "opportunity_claim_ids": [],
            "priority_unknowns": [],
        }
    else:
        extraction = {
            "claims": [
                {
                    "id": "claim-" + event["id"],
                    "text": event["text"],
                    "status": "reported",
                    "evidence_ids": [event["id"]],
                }
            ],
            "steps": [],
            "edges": [],
            "coverage": [],
            "unknowns": [],
            "superseded_claim_ids": [],
        }
    return httpx.Response(
        200,
        json={
            "id": "resp-synthetic",
            "object": "response",
            "created_at": 1,
            "status": "completed",
            "model": "gpt-5.4-mini",
            "output": [
                {
                    "id": "msg-synthetic",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": json.dumps(extraction), "annotations": []}
                    ],
                }
            ],
            "usage": {"input_tokens": 100, "output_tokens": 30, "total_tokens": 130},
        },
    )


async def test_durable_worker_processes_actual_graph_and_saver_idempotently(database):
    from openai import AsyncOpenAI
    from beep_agent.store import Store
    from beep_agent.providers import OpenAIProvider
    from beep_agent.worker import DurableWorker

    store = Store(database)
    store.initialize()
    session = active_session(store, recording=True)
    event = store.append_event(
        "tenant",
        session["id"],
        dict(
            id="event-one",
            kind="transcript",
            actor="client",
            text="Manager approves quotes.",
            at_ms=960001,
            consent_epoch=0,
        ),
    )
    client = AsyncOpenAI(
        api_key="synthetic",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(response)),
    )
    provider = OpenAIProvider(settings(), client=client)
    async with DurableWorker(
        settings().model_copy(update={"database_url": database}), store=store, provider=provider
    ) as worker:
        assert await worker.run_once()
        snapshot = store.load_snapshot("tenant", session["id"])
        assert snapshot["revision"] == 1
        assert snapshot["evidence"] == [event]
        assert snapshot["claims"][0]["text"] == "Manager approves quotes."
        config = {"configurable": {"thread_id": worker.thread_id("tenant", session["id"])}}
        checkpoint = await worker.checkpointer.aget_tuple(config)
        assert checkpoint is not None
        store.enqueue_job("tenant", session["id"], "discovery", {"event": event})
        assert await worker.run_once()
        assert store.load_snapshot("tenant", session["id"])["revision"] == 1
    await client.close()


async def test_worker_finalisation_replays_original_epochs_and_builds_report(database):
    from openai import AsyncOpenAI
    from beep_agent.store import Store
    from beep_agent.providers import OpenAIProvider
    from beep_agent.worker import DurableWorker

    store = Store(database)
    store.initialize()
    session = active_session(store)
    sid = session["id"]
    original = store.append_event(
        "tenant",
        sid,
        dict(
            id="old-epoch",
            kind="transcript",
            actor="client",
            text="Manager approves quotes.",
            at_ms=960001,
            consent_epoch=0,
        ),
    )
    store.control("tenant", sid, "client", "finish")
    client = AsyncOpenAI(
        api_key="synthetic",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(response)),
    )
    async with DurableWorker(
        settings().model_copy(update={"database_url": database}),
        store=store,
        provider=OpenAIProvider(settings(), client=client),
    ) as worker:
        for _ in range(12):
            if not await worker.run_once():
                break
        report = store.get_report("tenant", sid)
        assert report is not None
        assert report["evidence"] == [original]
        assert report["evidence"][0]["consent_epoch"] == 0
        assert store.load_snapshot("tenant", sid)["consent_epoch"] == 1
        assert report["claims"][0]["evidence_ids"] == ["old-epoch"]
        assert report["summary"].startswith("Source-linked review synthesis.")
        assert report["status"] == "partial"  # missing discovery coverage is never fabricated
    await client.close()


async def test_lease_renewal_prevents_competing_owner_and_pause_cancels_http(database):
    import asyncio
    from openai import AsyncOpenAI
    from beep_agent.store import Store
    from beep_agent.providers import OpenAIProvider
    from beep_agent.worker import DurableWorker

    store = Store(database)
    store.initialize()
    session = active_session(store, recording=True)
    sid = session["id"]
    store.append_event(
        "tenant",
        sid,
        dict(
            id="long-one",
            kind="transcript",
            actor="client",
            text="Manager approves.",
            at_ms=960001,
            consent_epoch=0,
        ),
    )
    store.enqueue_job("tenant", sid, "discovery", {})
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def slow_response(request):
        entered.set()
        try:
            await asyncio.sleep(20)
            return response(request)
        finally:
            cancelled.set()

    client = AsyncOpenAI(
        api_key="synthetic",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(slow_response)),
    )
    async with DurableWorker(
        settings().model_copy(update={"database_url": database}),
        store=store,
        provider=OpenAIProvider(settings(), client=client),
        lease_seconds=1,
        job_timeout=4,
    ) as worker:
        running = asyncio.create_task(worker.run_once())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            await asyncio.sleep(1.2)
            assert store.claim_job("competitor") is None
            store.control("tenant", sid, "client", "pause")
            await asyncio.wait_for(running, 2)
            assert cancelled.is_set()
            assert store.load_snapshot("tenant", sid)["revision"] == 0
        finally:
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
    await client.close()


async def test_recording_jobs_honor_epoch_and_stop_old_egress_without_stopping_new(database):
    from beep_agent.store import Store
    from beep_agent.worker import DurableWorker

    store = Store(database)
    store.initialize()
    session = active_session(store)
    sid = session["id"]
    calls = []

    class EgressBoundary:
        async def start(self, session):
            calls.append(("start", session["id"]))
            return "EG-current"

        async def stop(self, eid):
            calls.append(("stop", eid))

    store.enqueue_job("tenant", sid, "recording_start", {"consent_epoch": 0})
    async with DurableWorker(
        settings().model_copy(update={"database_url": database}),
        store=store,
        recording=EgressBoundary(),
    ) as worker:
        await worker.run_once()
        assert store.get_session("tenant", sid)["egress_id"] == "EG-current"
        store.enqueue_job("tenant", sid, "recording_start", {"consent_epoch": 0})
        await worker.run_once()
        assert len(calls) == 1  # retry/duplicate start is idempotent
        store.enqueue_job("tenant", sid, "recording_stop", {"egress_id": "EG-old"})
        await worker.run_once()
        assert calls[-1] == ("stop", "EG-old")
        assert store.get_session("tenant", sid)["recording_status"] == "recording"


async def test_worker_lazy_official_provider_records_measured_usage(database):
    from beep_agent.store import Store
    from beep_agent.worker import DurableWorker

    store = Store(database)
    store.initialize()
    session = active_session(store, recording=True)
    sid = session["id"]
    store.append_event(
        "tenant",
        sid,
        dict(
            id="meter-one",
            kind="transcript",
            actor="client",
            text="Manager approves.",
            at_ms=960001,
            consent_epoch=0,
        ),
    )
    async with DurableWorker(
        settings().model_copy(update={"database_url": database}),
        store=store,
        provider_transport=httpx.MockTransport(response),
    ) as worker:
        assert worker.provider is None  # no paid SDK client before eligible job
        await worker.run_once()
        assert store.load_snapshot("tenant", sid)["revision"] == 1
        with psycopg.connect(database) as conn:
            usage = conn.execute(
                "SELECT data FROM beep_usage WHERE session_id=%s", (sid,)
            ).fetchone()[0]
        assert usage["input_tokens"] == 100 and usage["output_tokens"] == 30
        assert usage["measurement"] == "measured"
        assert "cost_aud" not in usage  # no invented price/FX


async def test_worker_once_entrypoint_and_recording_independent_of_model_key(database):
    from pydantic import SecretStr
    from beep_agent.store import Store
    from beep_agent.worker import run_worker, DurableWorker

    cfg = settings().model_copy(update={"database_url": database, "openai_api_key": SecretStr("")})
    assert await run_worker(cfg, once=True) == 0
    store = Store(database)
    state = active_session(store)
    store.enqueue_job("tenant", state["id"], "recording_start", {"consent_epoch": 0})

    class EgressBoundary:
        async def start(self, state):
            return "EG-independent"

    async with DurableWorker(cfg, store=store, recording=EgressBoundary()) as worker:
        await worker.run_once()
        assert worker.provider is None
        assert store.get_session("tenant", state["id"])["recording_status"] == "recording"


async def test_ambiguous_provider_failure_is_not_replayed_by_next_queued_job(database):
    from beep_agent.store import Store
    from beep_agent.worker import DurableWorker

    store = Store(database)
    store.initialize()
    state = active_session(store, recording=True)
    sid = state["id"]
    store.append_event(
        "tenant",
        sid,
        dict(
            id="ambiguous",
            kind="transcript",
            actor="client",
            text="Approval uncertain.",
            at_ms=960001,
            consent_epoch=0,
        ),
    )
    store.enqueue_job("tenant", sid, "discovery", {})
    calls = []

    def fail(request):
        calls.append(request)
        raise httpx.ReadTimeout("synthetic lost response")

    async with DurableWorker(
        settings().model_copy(update={"database_url": database}),
        store=store,
        provider_transport=httpx.MockTransport(fail),
    ) as worker:
        await worker.run_once()
        await worker.run_once()
        assert len(calls) == 1
        assert store.load_snapshot("tenant", sid)["revision"] == 0
