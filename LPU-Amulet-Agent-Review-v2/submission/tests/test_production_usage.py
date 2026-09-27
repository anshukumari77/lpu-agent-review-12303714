"""PR-01: production subscription serializer through real PostgreSQL paths.

All model/voice/recording boundaries are synthetic; no credential or network IO.
"""
from types import SimpleNamespace

import pytest

from beep_agent.codex_oauth import _usage
from test_store import create, store as store


def subscription_usage():
    return _usage(SimpleNamespace(
        id="resp-production-usage-synthetic", model="gpt-5.5",
        usage=SimpleNamespace(
            input_tokens=100, output_tokens=40, total_tokens=140,
            input_tokens_details=SimpleNamespace(cached_tokens=20),
            output_tokens_details=SimpleNamespace(reasoning_tokens=10),
        ),
    ))


def test_production_subscription_cost_stays_unknown_with_tokens_and_attribution(store):
    session = create(store)
    usage = subscription_usage()
    assert usage["cost_aud"] is None
    store.add_usage("t1", session["id"], usage)
    with store._connect() as connection:
        rows = connection.execute("SELECT data FROM beep_usage WHERE session_id=%s", (session["id"],)).fetchall()
    assert [row["data"] for row in rows] == [usage]
    current = store.get_session("t1", session["id"])
    assert current.get("usage_aud") is None, "Unpriced subscription usage is not measured zero spend"
    assert current["usage_cost_unpriced"] is True


@pytest.mark.parametrize("cost", [True, False, -1, float("nan"), float("inf"), -float("inf"), "0"])
def test_invalid_costs_are_rejected_without_persisting(store, cost):
    from beep_agent.store import StoreError

    session = create(store)
    with pytest.raises(StoreError, match="Invalid usage cost"):
        store.add_usage("t1", session["id"], {**subscription_usage(), "cost_aud": cost})
    with store._connect() as connection:
        assert connection.execute("SELECT 1 FROM beep_usage").fetchone() is None


def test_unpriced_receipts_do_not_weaken_priced_budget_or_tenant_attribution(store):
    from beep_agent.store import StoreError
    from test_store import activate

    session = activate(store)
    sid = session["id"]
    store.add_usage("t1", sid, {"cost_aud": 1.25, "source": "synthetic_priced"})
    store.add_usage("t1", sid, subscription_usage())
    current = store.get_session("t1", sid)
    assert current["usage_aud"] == 1.25 and current["usage_cost_unpriced"] is True
    assert current["status"] == "active"
    with pytest.raises(StoreError, match="Session not found"):
        store.add_usage("wrong-tenant", sid, subscription_usage())
    store.add_usage("t1", sid, {"cost_aud": 58.75, "source": "synthetic_priced"})
    current = store.get_session("t1", sid)
    assert current["usage_aud"] == 60 and current["stop_reason"] == "budget_cap"
    assert current["status"] == "finalising" and current["consent_epoch"] == 1


async def test_worker_retains_success_if_post_publication_accounting_fails(store, monkeypatch):
    from beep_agent.discovery import Extraction
    from beep_agent.store import StoreError
    from beep_agent.worker import DurableWorker
    from test_worker import active_session, settings

    session = active_session(store, recording=True)
    sid = session["id"]
    store.append_event("tenant", sid, dict(id="accounting", kind="transcript", actor="client",
        text="SYNTHETIC manager approves.", at_ms=960001, consent_epoch=0))
    usage = subscription_usage()

    class Provider:
        async def extract(self, *args, **kwargs):
            worker._usage.append(usage)
            return Extraction()

    def unavailable(*args):
        raise StoreError("synthetic-private-accounting-detail")

    monkeypatch.setattr(store, "add_usage", unavailable)
    async with DurableWorker(settings().model_copy(update={"database_url": store.database_url}),
                             store=store, provider=Provider()) as worker:
        assert await worker.run_once()
        assert not await worker.run_once()
    assert store.load_snapshot("tenant", sid)["revision"] == 1
    with store._connect() as connection:
        job = connection.execute("SELECT * FROM beep_jobs WHERE session_id=%s", (sid,)).fetchone()
    assert job["status"] == "done", "Accounting failure cannot undo published evidence"
    assert job["error"] is None
    assert job["result"]["status"] == "saved"
    assert job["result"]["usage_reconciliation_required"] is True
    assert job["result"]["pending_usage"] == [{**usage, "stage": "discovery", "job_id": job["id"]}]
    assert "synthetic-private" not in str(job)


def install_subscription_boundary(monkeypatch):
    import json
    from beep_agent import codex_oauth

    receipts = []

    class Inference:
        def __init__(self, settings, *, before_send):
            self.before_send = before_send

        async def structured(self, schema, instructions, content):
            await self.before_send()
            if schema.__name__ == "VisualObservation":
                value = dict(readable=True, summary="SYNTHETIC approval column visible.",
                    focus="approval", uncertainties=["Requires client confirmation"], crop_box=None)
            else:
                data = json.loads(content[0]["text"])["untrusted_data"]
                event = data["event"]
                value = ({"claims": [{"id": "claim-" + event["id"], "text": event["text"],
                    "status": "reported", "evidence_ids": [event["id"]]}]} if event else
                    {"summary_claim_ids": [c["id"] for c in data["snapshot"]["claims"][:1]],
                     "opportunity_claim_ids": [], "priority_unknowns": []})
            usage = subscription_usage()  # Exact production serializer, including null and IDs.
            receipts.append(usage)
            return schema.model_validate(value), usage

    monkeypatch.setattr(codex_oauth, "CodexInference", Inference)
    return receipts


async def test_production_codex_worker_publishes_discovery_and_report_once(store, tmp_path, monkeypatch):
    from pydantic import SecretStr
    from beep_agent.worker import DurableWorker
    from test_worker import active_session, settings

    receipts = install_subscription_boundary(monkeypatch)
    current = active_session(store, recording=True)
    sid = current["id"]
    store.append_event("tenant", sid, dict(id="subscription", kind="transcript", actor="client",
        text="SYNTHETIC manager approves.", at_ms=960001, consent_epoch=0))
    cfg = settings().model_copy(update={"database_url": store.database_url, "inference_provider": "codex",
        "codex_home": tmp_path, "openai_api_key": SecretStr("")})

    class Recording:
        async def stop(self, eid):
            assert eid == "EG-synthetic-fixture"

    async with DurableWorker(cfg, store=store, recording=Recording()) as worker:
        assert await worker.run_once()
        assert store.load_snapshot("tenant", sid)["revision"] == 1
        store.control("tenant", sid, "client", "finish")
        for _ in range(8):
            if not await worker.run_once():
                break
        assert not await worker.run_once()
    assert store.get_report("tenant", sid)["revision"] == 1
    with store._connect() as connection:
        jobs = connection.execute("SELECT * FROM beep_jobs WHERE session_id=%s ORDER BY created_at", (sid,)).fetchall()
        usage = [r["data"] for r in connection.execute("SELECT data FROM beep_usage WHERE session_id=%s ORDER BY id", (sid,))]
    assert len(receipts) == 2 and len(usage) == 2
    assert all(job["status"] == "done" and job["error"] is None for job in jobs)
    assert {u["stage"] for u in usage} == {"discovery", "report"}
    for actual, raw in zip(usage, receipts, strict=True):
        assert {key: actual[key] for key in raw} == raw
        assert actual["job_id"] in {j["id"] for j in jobs}
        assert actual["effective_hard_budget_aud"] is None
        assert actual["monetary_enforcement"] == "UNAVAILABLE"


async def test_realtime_vision_queue_persists_exact_subscription_usage_and_keeps_generation(store, tmp_path, monkeypatch):
    import asyncio
    import time
    from PIL import Image
    from beep_agent.media import ScreenFrame
    from beep_agent.realtime import InterviewRuntime
    from test_worker import active_session, settings

    receipts = install_subscription_boundary(monkeypatch)
    current = active_session(store, recording=True)
    sid = current["id"]
    cfg = settings().model_copy(update={"database_url": store.database_url,
        "inference_provider": "codex", "codex_home": tmp_path})
    closed = []

    class NativeTransport:
        agent_state = "listening"
        user_state = "listening"
        input = SimpleNamespace(set_audio_enabled=lambda enabled: None, set_video_enabled=lambda enabled: None)
        output = SimpleNamespace(audio=None)

        def on(self, *args):
            pass

        async def start(self, *args, **kwargs):
            pass

        def generate_reply(self, **kwargs):
            return None

        def clear_user_turn(self):
            pass

        async def interrupt(self, **kwargs):
            pass

        async def aclose(self):
            closed.append("native")

    async def update_chat(ctx):
        agent.chat_ctx = ctx

    async def close_model():
        closed.append("model")

    def assembly_factory(cfg, current, *, chat_ctx):
        agent.chat_ctx = chat_ctx
        return SimpleNamespace(session=NativeTransport(), agent=agent,
            model=SimpleNamespace(aclose=close_model), options=None)

    agent = SimpleNamespace(update_chat_ctx=update_chat)
    runtime = InterviewRuntime(cfg, store, "tenant", sid, SimpleNamespace(), assembly_factory=assembly_factory)
    usage_attempted = asyncio.Event()
    original_db = runtime.db

    async def observed_db(method, *args, **kwargs):
        try:
            return await original_db(method, *args, **kwargs)
        finally:
            if method == store.add_usage:
                usage_attempted.set()

    monkeypatch.setattr(runtime, "db", observed_db)
    runtime.fence.update(current, time.monotonic())
    runtime.capture.latest = ScreenFrame.from_image(Image.new("RGB", (100, 80)),
        track_sid="TR-synthetic-selected", at_ms=960002, captured=time.monotonic())
    task = asyncio.create_task(runtime._generation(runtime.fence.ticket))
    try:
        await asyncio.wait_for(usage_attempted.wait(), 2)
        await asyncio.sleep(.1)
        with store._connect() as connection:
            rows = connection.execute("SELECT data FROM beep_usage WHERE session_id=%s", (sid,)).fetchall()
        assert len(rows) == 1, "Production nullable vision usage must pass the live-generation persister"
        assert {key: rows[0]["data"][key] for key in receipts[0]} == receipts[0]
        assert rows[0]["data"]["stage"] == "screen_observation"
        events = store.list_events("tenant", sid)
        assert events[0]["kind"] == "screen_observation"
        assert events[0]["source_ref"].startswith("livekit:TR-synthetic-selected@960002:")
        await asyncio.sleep(.3)  # Cross the generation's persister-failure check.
        assert not task.done() and runtime.failures == 0
        runtime.fence.invalidate()
        await asyncio.wait_for(task, 3)
    finally:
        runtime.fence.invalidate()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert closed == ["native", "model"]
