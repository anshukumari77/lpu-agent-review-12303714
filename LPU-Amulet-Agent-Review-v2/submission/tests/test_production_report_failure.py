"""PR-05 failure drills: real PostgreSQL/checkpoints, no paid/network calls."""
import hashlib

import pytest

from beep_agent.domain import Snapshot
from beep_agent.maintenance import Maintenance
from beep_agent.reports import build_report
from beep_agent.worker import DurableWorker
from test_store import store as store
from test_worker import active_session, settings


def finalising(store):
    state = active_session(store)
    return store.control("tenant", state["id"], "client", "finish")


def claim_report(store):
    for _ in range(8):
        job = store.claim_job("synthetic-report-owner")
        assert job is not None
        if job["kind"] == "report":
            return job
        assert store.finish_job(job["id"], job["lease_token"], {"status": "synthetic-no-recording"})
    raise AssertionError("No report job")


def job_row(store, job_id):
    with store._connect() as connection:
        return connection.execute("SELECT * FROM beep_jobs WHERE id=%s", (job_id,)).fetchone()


def operation_config(sid, revision, epoch):
    key = f"report:{revision}:{epoch}"
    return {"configurable": {"thread_id": DurableWorker.thread_id("tenant", sid),
        "checkpoint_ns": "operation:" + hashlib.sha256(key.encode()).hexdigest()}}


def assert_terminal_failure(store, sid, job_id, reason):
    state = store.get_session("tenant", sid)
    assert state["status"] == "failed", "A report failure must release finalising/admission"
    assert state["stop_reason"] == "report_reconciliation_required"
    assert state["report_failure"]["job_id"] == job_id
    assert state["report_failure"]["state"] == "reconciliation_required"
    assert state["report_failure"]["reason"] == reason
    assert state["report_failure"]["next_action"] == "inspect_retained_operation_or_close_without_report"
    assert store.get_report("tenant", sid) is None
    return state


async def test_report_provider_failure_is_terminal_due_and_never_replayed(store):
    store.configure_limits(max_concurrent_sessions=1)
    state = finalising(store)
    sid = state["id"]
    calls = []

    class Provider:
        async def synthesize_report(self, *args, **kwargs):
            calls.append("synthetic report")
            raise ConnectionError("synthetic-private-provider-detail")

    cfg = settings().model_copy(update={"database_url": store.database_url})
    async with DurableWorker(cfg, store=store, provider=Provider()) as worker:
        for _ in range(4):
            if not await worker.run_once():
                break
        with store._connect() as connection:
            report_job = connection.execute("SELECT * FROM beep_jobs WHERE session_id=%s AND kind='report'", (sid,)).fetchone()
        assert report_job["status"] == "failed"
        current = assert_terminal_failure(store, sid, report_job["id"], "report_generation_failed")
        assert "synthetic-private" not in str(current)
        checkpoint = await worker.checkpointer.aget_tuple(operation_config(sid, 0, state["consent_epoch"]))
        assert checkpoint.checkpoint["channel_values"] == {"status": "started"}
        assert not await worker.run_once()
        assert calls == ["synthetic report"]
    assert not any(s["id"] == sid for s in store.list_active_sessions())
    store.create_session("tenant", title="Released slot", pack_id="general", offer="paid")
    with store._connect() as connection:
        connection.execute("UPDATE beep_sessions SET data=jsonb_set(data,'{created_at}',to_jsonb((clock_timestamp()-interval '100 days')::text)) WHERE id=%s", (sid,))
    assert any(s["id"] == sid for s in Maintenance(cfg, store=store).due())


def test_expired_report_lease_terminalises_without_reclaiming_paid_call(store):
    state = finalising(store)
    sid = state["id"]
    job = claim_report(store)
    with store._connect() as connection:
        connection.execute("UPDATE beep_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s", (job["id"],))
    assert store.claim_job("replacement") is None
    expired = job_row(store, job["id"])
    assert expired["status"] == "failed" and expired["attempts"] == 1
    assert_terminal_failure(store, sid, job["id"], "lease_expired_ambiguous")
    assert not store.fail_job(job["id"], job["lease_token"], "stale-owner")


@pytest.mark.parametrize("failure", ["expired", "exception"])
def test_report_after_confirmed_publication_keeps_truthful_result(store, failure):
    state = finalising(store)
    sid = state["id"]
    job = claim_report(store)
    report = build_report(Snapshot.model_validate(store.load_snapshot("tenant", sid)), sid, partial=True)
    store.commit_report(job["id"], job["lease_token"], "tenant", sid, {
        "report": report.model_dump(mode="json"), "expected_revision": 0,
        "consent_epoch": state["consent_epoch"],
    })
    before = store.get_report("tenant", sid)
    if failure == "expired":
        with store._connect() as connection:
            connection.execute("UPDATE beep_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s", (job["id"],))
    else:
        assert store.fail_job(job["id"], job["lease_token"], "job_failed_reconciliation_required")
    assert store.claim_job("replacement") is None
    assert store.get_session("tenant", sid)["status"] == "partial"
    assert store.get_report("tenant", sid) == before
    settled = job_row(store, job["id"])
    assert settled["status"] == "done"
    assert settled["result"]["status"] == "reported"
    assert settled["result"]["publication_confirmed"] is True
    assert settled["error"] != "job_failed_no_result_published"


async def test_retained_complete_result_can_be_published_without_provider_replay(store, monkeypatch):
    state = finalising(store)
    sid = state["id"]
    calls = []

    class Provider:
        async def synthesize_report(self, snapshot, session_id, **kwargs):
            calls.append("synthetic report")
            return build_report(snapshot, session_id, partial=True)

    original = store.commit_report

    def interrupted_publication(*args, **kwargs):
        raise ConnectionError("synthetic crash after operation checkpoint")

    monkeypatch.setattr(store, "commit_report", interrupted_publication)
    cfg = settings().model_copy(update={"database_url": store.database_url})
    async with DurableWorker(cfg, store=store, provider=Provider()) as worker:
        for _ in range(4):
            if not await worker.run_once():
                break
        before = await worker.checkpointer.aget_tuple(operation_config(sid, 0, state["consent_epoch"]))
        assert before.checkpoint["channel_values"]["status"] == "complete"
        assert store.get_session("tenant", sid)["status"] == "failed"
        monkeypatch.setattr(store, "commit_report", original)
        maintenance = Maintenance(cfg, store=store)
        assert (await maintenance.reconcile_report("tenant", sid))["status"] == "retained_report_available"
        assert store.get_session("tenant", sid)["status"] == "failed", "Reconciliation defaults to read-only"
        result = await maintenance.reconcile_report("tenant", sid, apply=True)
        assert result["status"] == "reported" and result["reused_retained_result"] is True
        assert store.get_report("tenant", sid)["revision"] == 0
        current = store.get_session("tenant", sid)
        assert current["status"] == "partial"
        assert current["report_failure"]["state"] == "resolved_from_retained_result"
        assert not await worker.run_once()
        after = await worker.checkpointer.aget_tuple(operation_config(sid, 0, state["consent_epoch"]))
        assert after.checkpoint == before.checkpoint
    assert calls == ["synthetic report"]


async def test_ambiguous_checkpoint_refuses_reconciliation_replay(store):
    state = finalising(store)
    sid = state["id"]
    calls = []

    class Provider:
        async def synthesize_report(self, *args, **kwargs):
            calls.append("synthetic ambiguous call")
            raise TimeoutError("synthetic response lost")

    cfg = settings().model_copy(update={"database_url": store.database_url})
    async with DurableWorker(cfg, store=store, provider=Provider()) as worker:
        for _ in range(4):
            if not await worker.run_once():
                break
        before = store.get_session("tenant", sid)
        result = await Maintenance(cfg, store=store).reconcile_report("tenant", sid, apply=True)
        assert result["status"] == "reconciliation_required"
        assert result["reason"] == "no_confirmed_report_result"
        assert store.get_session("tenant", sid) == before
        assert not await worker.run_once()
    assert len(calls) == 1


def test_reconcile_report_cli_is_exact_session_and_dry_run_by_default(store, monkeypatch, capsys):
    import json
    from beep_agent import config, maintenance

    state = finalising(store)
    job = claim_report(store)
    assert store.fail_job(job["id"], job["lease_token"], "synthetic")
    before = store.get_session("tenant", state["id"])
    cfg = settings().model_copy(update={"database_url": store.database_url})
    monkeypatch.setattr(config, "Settings", lambda: cfg)
    monkeypatch.setattr("sys.argv", ["beep-maintenance", "reconcile-report", "--tenant", "tenant", "--session", state["id"]])
    maintenance.main()
    assert json.loads(capsys.readouterr().out) == {"status": "reconciliation_required",
        "session_id": state["id"], "reason": "no_confirmed_report_result"}
    assert store.get_session("tenant", state["id"]) == before


def test_stale_failure_cannot_terminalise_a_fresh_job_owner(store):
    state = finalising(store)
    job = claim_report(store)
    assert store.fail_job(job["id"], job["lease_token"], "synthetic_pre_send_retry", retry=True)
    fresh = store.claim_job("fresh-owner")
    assert fresh["id"] == job["id"] and fresh["lease_token"] != job["lease_token"]
    before = store.get_session("tenant", state["id"])
    assert not store.fail_job(job["id"], job["lease_token"], "stale-owner")
    assert store.get_session("tenant", state["id"]) == before
    assert job_row(store, fresh["id"])["status"] == "running"


async def test_correction_during_report_publication_is_not_terminalised_by_old_failure(store):
    from beep_agent.discovery import Extraction

    state = finalising(store)
    sid = state["id"]
    calls = []

    class Provider:
        async def synthesize_report(self, snapshot, session_id, **kwargs):
            calls.append(snapshot.revision)
            if snapshot.revision == 0:
                store.append_event("tenant", sid, dict(id="late-correction", kind="correction", actor="client",
                    text="SYNTHETIC correction during report.", at_ms=0, consent_epoch=state["consent_epoch"]))
            return build_report(snapshot, session_id, partial=True)

        async def extract(self, *args):
            return Extraction()

    async with DurableWorker(settings().model_copy(update={"database_url": store.database_url}),
                             store=store, provider=Provider()) as worker:
        assert await worker.run_once()  # no-recording stop
        assert await worker.run_once()  # old report fenced by correction
        current = store.get_session("tenant", sid)
        assert current["status"] == "finalising" and "report_failure" not in current
        for _ in range(8):
            if not await worker.run_once():
                break
        report = store.get_report("tenant", sid)
        assert report["revision"] == 1 and report["evidence"][0]["id"] == "late-correction"
        assert calls == [0, 1], "New correction operation is not a replay of ambiguous old output"


async def test_report_failure_retention_preserves_unresolved_recording_obligations(store):
    from beep_agent.store import StoreError

    state = active_session(store)
    sid = state["id"]
    reservation = store.set_recording("tenant", sid, "starting")["recording_reservation"]
    store.set_recording("tenant", sid, "failed")
    store.control("tenant", sid, "client", "finish")
    # Explicit queue ordering fixture: report fails while cleanup is still queued.
    with store._connect() as connection:
        connection.execute("UPDATE beep_jobs SET created_at=clock_timestamp()-interval '1 day' WHERE session_id=%s AND kind='report'", (sid,))
    job = store.claim_job("report-owner")
    assert job["kind"] == "report"
    assert store.fail_job(job["id"], job["lease_token"], "synthetic_report_failure")
    current = assert_terminal_failure(store, sid, job["id"], "report_generation_failed")
    assert current["recording_cleanup_pending"] is True
    with store._connect() as connection:
        connection.execute("UPDATE beep_sessions SET data=jsonb_set(data,'{created_at}',to_jsonb((clock_timestamp()-interval '100 days')::text)) WHERE id=%s", (sid,))
    cfg = settings().model_copy(update={"database_url": store.database_url})
    due = Maintenance(cfg, store=store).due()
    assert any(s["id"] == sid and s["recording_cleanup_pending"] for s in due)

    def retained():
        with store._connect() as connection:
            return (store._row(connection, "tenant", sid),
                connection.execute("SELECT * FROM beep_jobs WHERE session_id=%s ORDER BY id", (sid,)).fetchall(),
                connection.execute("SELECT * FROM beep_recording_reservations WHERE session_id=%s", (sid,)).fetchall())

    before = retained()
    calls = []

    class Recording:
        async def delete(self, state):
            calls.append("forbidden-delete")

    cfg = settings().model_copy(update={"database_url": store.database_url})
    with pytest.raises(StoreError, match="reconciliation"):
        await Maintenance(cfg, store=store, recording=Recording()).purge("tenant", sid, apply=True)
    with pytest.raises(StoreError, match="reconciliation"):
        store.delete_session("tenant", sid)
    assert retained() == before and calls == []
    assert any(j["kind"] == "recording_stop" and j["status"] == "queued" for j in before[1])
    cleanup = store.claim_job("recording-reconciler")
    assert cleanup["kind"] == "recording_stop" and cleanup["payload"]["reservation"] == reservation


@pytest.mark.parametrize("change", ["epoch", "revision", "event_seq", "consent", "owner", "deletion"])
def test_retained_result_reconciliation_rechecks_current_scope(store, change):
    from beep_agent.store import StoreError

    state = finalising(store)
    sid = state["id"]
    job = claim_report(store)
    assert store.fail_job(job["id"], job["lease_token"], "synthetic")
    failure = store.get_session("tenant", sid)["report_failure"]
    report = build_report(Snapshot.model_validate(store.load_snapshot("tenant", sid)), sid, partial=True)
    with store._connect() as connection:
        current = store._row(connection, "tenant", sid, lock=True)["data"]
        if change in {"epoch", "revision", "event_seq"}:
            current["consent_epoch" if change == "epoch" else change] += 1
        elif change == "consent":
            current["client_consent"]["ai"] = False
        elif change == "deletion":
            current["deletion_pending"] = True
        store._write(connection, current)
    if change == "owner":
        store.enqueue_job("tenant", sid, "report", {})
        fresh = store.claim_job("fresh-owner")
        assert fresh["id"] != job["id"]
    before = store.get_session("tenant", sid)
    with pytest.raises(StoreError, match="reconciliation"):
        store.publish_retained_report("tenant", sid, failure, report.model_dump(mode="json"))
    assert store.get_session("tenant", sid) == before
    assert store.get_report("tenant", sid) is None


@pytest.mark.parametrize("race", ["correction", "expired_lease", "fresh_owner", "consent_epoch"])
def test_change_after_reconciliation_reservation_still_fences_publication(store, monkeypatch, race):
    from beep_agent.store import StoreError

    state = finalising(store)
    sid = state["id"]
    job = claim_report(store)
    assert store.fail_job(job["id"], job["lease_token"], "synthetic")
    failure = store.get_session("tenant", sid)["report_failure"]
    report = build_report(Snapshot.model_validate(store.load_snapshot("tenant", sid)), sid, partial=True)
    original = store.commit_report

    owners = []

    def change_before_commit(*args, **kwargs):
        if race == "correction":
            store.append_event("tenant", sid, dict(id="reconcile-correction", kind="correction", actor="client",
                text="SYNTHETIC new correction", at_ms=0, consent_epoch=state["consent_epoch"]))
        elif race == "consent_epoch":
            with store._connect() as connection:
                row = store._row(connection, "tenant", sid, lock=True)
                row["data"]["consent_epoch"] += 1
                store._write(connection, row["data"])
        else:
            with store._connect() as connection:
                connection.execute("UPDATE beep_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s", (args[0],))
            if race == "fresh_owner":
                store.enqueue_job("tenant", sid, "report", {})
                owners.append(store.claim_job("fresh-owner"))
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "commit_report", change_before_commit)
    with pytest.raises(StoreError, match="fence"):
        store.publish_retained_report("tenant", sid, failure, report.model_dump(mode="json"))
    assert store.get_report("tenant", sid) is None
    if race == "expired_lease":
        assert store.claim_job("expiry-reconciler") is None
    assert store.get_session("tenant", sid)["status"] == ("finalising" if race in {"correction", "fresh_owner"} else "failed")
    if owners:
        assert job_row(store, owners[0]["id"])["status"] == "running"


async def test_legacy_stranded_failure_is_due_and_can_be_terminalised_without_replay(store):
    state = finalising(store)
    sid = state["id"]
    job = claim_report(store)
    # Exact pre-fix persisted state, not a new provider attempt.
    with store._connect() as connection:
        connection.execute("UPDATE beep_jobs SET status='failed',error='lease_expired_ambiguous' WHERE id=%s", (job["id"],))
        connection.execute("UPDATE beep_sessions SET data=jsonb_set(data,'{created_at}',to_jsonb((clock_timestamp()-interval '100 days')::text)) WHERE id=%s", (sid,))
    maintenance = Maintenance(settings().model_copy(update={"database_url": store.database_url}), store=store)
    assert any(s["id"] == sid for s in maintenance.due()), "Already-stranded data also needs retention triage"
    before = store.get_session("tenant", sid)
    assert (await maintenance.reconcile_report("tenant", sid))["status"] == "terminalisation_required"
    assert store.get_session("tenant", sid) == before
    assert (await maintenance.reconcile_report("tenant", sid, apply=True))["status"] == "reconciliation_required"
    assert_terminal_failure(store, sid, job["id"], "lease_expired_ambiguous")
    assert store.claim_job("no-provider-replay") is None


def test_legacy_triage_cannot_override_newer_queued_or_running_report(store):
    state = finalising(store)
    sid = state["id"]
    job = claim_report(store)
    with store._connect() as connection:
        connection.execute("UPDATE beep_jobs SET status='failed' WHERE id=%s", (job["id"],))
    store.enqueue_job("tenant", sid, "report", {"after_seq": 1})
    before = store.get_session("tenant", sid)
    from beep_agent.store import StoreError
    with pytest.raises(StoreError, match="newer|leased"):
        store.triage_report_failure("tenant", sid, apply=True)
    fresh = store.claim_job("fresh-owner")
    with pytest.raises(StoreError, match="newer|leased"):
        store.triage_report_failure("tenant", sid, apply=True)
    assert store.get_session("tenant", sid) == before
    assert job_row(store, fresh["id"])["status"] == "running"


async def test_cancelled_report_retains_ambiguous_checkpoint_and_releases_slot(store):
    import asyncio

    state = finalising(store)
    sid = state["id"]
    entered = asyncio.Event()

    class Provider:
        async def synthesize_report(self, *args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

    async with DurableWorker(settings().model_copy(update={"database_url": store.database_url}),
                             store=store, provider=Provider()) as worker:
        assert await worker.run_once()  # no-recording stop
        task = asyncio.create_task(worker.run_once())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            with store._connect() as connection:
                job = connection.execute("SELECT * FROM beep_jobs WHERE session_id=%s AND kind='report'", (sid,)).fetchone()
            assert_terminal_failure(store, sid, job["id"], "cancelled_ambiguous")
            checkpoint = await worker.checkpointer.aget_tuple(operation_config(sid, 0, state["consent_epoch"]))
            assert checkpoint.checkpoint["channel_values"] == {"status": "started"}
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_failure_waiting_on_session_lock_rechecks_fresh_owner_token(store):
    from concurrent.futures import ThreadPoolExecutor
    import time

    state = finalising(store)
    sid = state["id"]
    job = claim_report(store)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with store._connect() as blocker:
            blocker.execute("SELECT id FROM beep_sessions WHERE id=%s FOR UPDATE", (sid,))
            pid = blocker.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            stale_failure = pool.submit(store.fail_job, job["id"], job["lease_token"], "stale-owner")
            deadline = time.monotonic() + 5
            waiting = None
            while time.monotonic() < deadline:
                with store._connect() as observer:
                    waiting = observer.execute("SELECT 1 FROM pg_stat_activity WHERE %s=ANY(pg_blocking_pids(pid))", (pid,)).fetchone()
                if waiting:
                    break
                time.sleep(.01)
            assert waiting, "Stale failure must reach the real session-row lock"
            # Isolated owner-rotation fixture commits while stale failure is blocked.
            blocker.execute("UPDATE beep_jobs SET lease_token='synthetic-fresh-token',worker_id='fresh-owner' WHERE id=%s", (job["id"],))
        assert stale_failure.result(timeout=5) is False
    assert store.get_session("tenant", sid)["status"] == "finalising"
    assert job_row(store, job["id"])["lease_token"] == "synthetic-fresh-token"


async def test_consent_denied_report_ends_honestly_without_provider_or_replay(store):
    state = active_session(store)
    sid = state["id"]
    store.set_consent("tenant", sid, "client", ai=False, recording=True)
    store.control("tenant", sid, "client", "finish")
    calls = []

    class Provider:
        async def synthesize_report(self, *args, **kwargs):
            calls.append("forbidden-provider")
            raise AssertionError("Revoked consent must never reach inference")

    async with DurableWorker(settings().model_copy(update={"database_url": store.database_url}),
                             store=store, provider=Provider()) as worker:
        for _ in range(5):
            if not await worker.run_once():
                break
        current = store.get_session("tenant", sid)
        assert current["status"] == "failed", "Consent denial is not successful finalisation"
        assert current["client_consent"]["ai"] is False
        assert current["report_failure"]["reason"] == "report_not_publishable"
        assert store.get_report("tenant", sid) is None and calls == []
