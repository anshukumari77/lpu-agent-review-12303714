"""PostgreSQL integration: real isolated schemas and independent connections."""
import os
import uuid
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

from beep_agent.store import Store, StoreError


@pytest.fixture
def store():
    dsn = os.environ.get("BEEP_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("BEEP_TEST_DATABASE_URL required; no offline persistence substitute")
    info = conninfo_to_dict(dsn)
    assert info.get("dbname", "").endswith("_test"), "Refusing non-test database"
    assert info.get("host", "").startswith("/path/to/review-snapshot/"), "Refusing arbitrary database host"
    schema = "test_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    scoped = make_conninfo(dsn, options=f"-c search_path={schema}")
    instance = Store(scoped)
    instance.initialize()
    try:
        yield instance
    finally:
        instance.close()
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def create(store, tenant="t1", **kw):
    return store.create_session(tenant, title="Synthetic workflow", pack_id="general", offer="paid", **kw)


def activate(store):
    s = create(store)
    store.set_consent("t1", s["id"], "client", ai=True, recording=True)
    store.set_consent("t1", s["id"], "facilitator", ai=True, recording=True)
    with store._connect() as c:
        c.execute("UPDATE beep_sessions SET data=jsonb_set(data,'{started_at}',to_jsonb((clock_timestamp()-interval '901 seconds')::text)) WHERE id=%s", (s["id"],))
    return store.control("t1", s["id"], "client", "handover")


def test_both_consent_wallclock_handover_pause_epoch_and_cap(store):
    s = create(store)
    sid = s["id"]
    assert store.set_consent("t1", sid, "client", ai=True, recording=True)["status"] == "awaiting_consent"
    with pytest.raises(StoreError):
        store.control("t1", sid, "client", "handover")
    s = store.set_consent("t1", sid, "facilitator", ai=True, recording=True)
    assert s["status"] == "introduction" and s["started_at"]
    with pytest.raises(StoreError):
        store.control("t1", sid, "facilitator", "handover")
    with store._connect() as c:
        c.execute("UPDATE beep_sessions SET data=jsonb_set(data,'{started_at}',to_jsonb((clock_timestamp()-interval '901 seconds')::text)) WHERE id=%s", (sid,))
    s = store.control("t1", sid, "client", "handover")
    assert s["status"] == "active"
    s = store.control("t1", sid, "client", "pause")
    assert s["status"] == "paused" and s["consent_epoch"] == 1
    assert store.load_snapshot("t1", sid)["consent_epoch"] == 1
    with pytest.raises(StoreError):
        store.control("t1", sid, "client", "takeover")
    assert store.control("t1", sid, "client", "resume")["status"] == "active"
    with store._connect() as c:
        c.execute("UPDATE beep_sessions SET data=jsonb_set(data,'{started_at}',to_jsonb((clock_timestamp()-interval '5401 seconds')::text)) WHERE id=%s", (sid,))
    assert store.get_session("t1", sid)["status"] == "finalising"
    assert store.get_session("t1", sid)["consent_epoch"] == 2
    with pytest.raises(StoreError):
        store.control("t1", sid, "client", "resume")


def test_jobs_claim_independent_connections_serialize_session_and_fence_leases(store):
    from concurrent.futures import ThreadPoolExecutor
    s1, s2 = create(store), create(store, "t2")
    for s in (s1, s2):
        store.enqueue_job(s["tenant_id"], s["id"], "report", {"revision": 0})
        store.enqueue_job(s["tenant_id"], s["id"], "report", {"revision": 1})
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda n: Store(store.database_url).claim_job(str(n)), range(4)))
    claimed = [j for j in claims if j]
    assert len(claimed) == 2 and len({j["session_id"] for j in claimed}) == 2
    job = claimed[0]
    assert not store.finish_job(job["id"], "wrong-token", {})
    assert store.renew_job(job["id"], job["lease_token"], lease_seconds=2)
    with store._connect() as c:
        c.execute("UPDATE beep_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s", (job["id"],))
    assert not store.renew_job(job["id"], job["lease_token"])
    assert not store.finish_job(job["id"], job["lease_token"], {})
    new = store.claim_job("replacement")
    assert new and new["id"] != job["id"]  # ambiguous expired provider call is NOT retried
    assert store.finish_job(new["id"], new["lease_token"], {"ok": True})
    assert not store.finish_job(new["id"], new["lease_token"], {})
    assert store.fail_job(claimed[1]["id"], claimed[1]["lease_token"], "synthetic_failure")


def test_events_are_idempotent_ordered_and_snapshot_revision_fenced(store):
    s = activate(store)
    sid = s["id"]
    event = dict(id="evt-1", kind="transcript", actor="client", text="Synthetic evidence", at_ms=902000, consent_epoch=0)
    saved = store.append_event("t1", sid, event)
    assert saved["seq"] == 1
    assert store.append_event("t1", sid, event) == saved
    assert store.get_session("t1", sid)["revision"] == 0
    with pytest.raises(StoreError):
        store.append_event("t1", sid, {**event, "text": "Changed collision"})
    snap = store.load_snapshot("t1", sid)
    snap.update(revision=1, last_event_seq=1, evidence=[saved])
    assert store.save_snapshot("t1", sid, snap, expected_revision=0, consent_epoch=0)
    assert not store.save_snapshot("t1", sid, snap, expected_revision=0, consent_epoch=0)
    store.control("t1", sid, "client", "pause")
    with pytest.raises(StoreError):
        store.append_event("t1", sid, {**event, "id": "evt-stale"})
    snap["revision"] = 2
    assert not store.save_snapshot("t1", sid, snap, expected_revision=1, consent_epoch=0)
    assert store.list_events("t1", sid, after_seq=0) == [saved]
    assert store.list_events("t1", sid, after_seq=1) == []


def test_report_correction_lifecycle_and_recording_jobs(store):
    s = activate(store)
    sid = s["id"]
    assert any(x["id"] == sid for x in store.list_active_sessions())
    store.set_recording("t1", sid, "recording", "EG_synthetic")
    store.add_usage("t1", sid, {"cost_aud": 1.25, "source": "synthetic_test"})
    with pytest.raises(StoreError):
        store.delete_session("t1", sid)
    store.control("t1", sid, "client", "finish")
    report = dict(session_id=sid, revision=0, title="Synthetic", summary="No evidence supplied.", recommendations=[], status="partial", internal_opportunity={}, generated_at="2026-09-11T00:00:00+00:00")
    assert store.put_report("t1", sid, report, expected_revision=0)
    assert store.get_report("t1", sid)["summary"] == "No evidence supplied."
    assert store.get_session("t1", sid)["status"] == "partial"
    correction = dict(id="fix-1", kind="correction", actor="client", text="Correction from synthetic participant", at_ms=0, consent_epoch=1)
    store.append_event("t1", sid, correction)
    assert store.get_report("t1", sid) is None
    assert not store.put_report("t1", sid, report, expected_revision=0)
    with store._connect() as c:
        kinds = [r["kind"] for r in c.execute("SELECT kind FROM beep_jobs WHERE session_id=%s", (sid,))]
    assert {"recording_start", "recording_stop", "discovery", "report"} <= set(kinds)


def test_admission_limits_are_atomic_and_sponsorship_cannot_reset_on_delete(store):
    from concurrent.futures import ThreadPoolExecutor
    store.configure_limits(max_concurrent_sessions=1, sponsored_limit=1)
    def attempt(n):
        try:
            return create(store)
        except StoreError:
            return None
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(attempt, range(4)))
    accepted = [s for s in results if s]
    assert len(accepted) == 1
    store.delete_session("t1", accepted[0]["id"])
    s = store.create_session("t1", title="Sponsored", pack_id="general", offer="sponsored")
    store.delete_session("t1", s["id"])
    with pytest.raises(StoreError):
        store.create_session("t1", title="Sponsored again", pack_id="general", offer="sponsored")


def test_job_context_fences_snapshot_write_after_lease_loss(store):
    s = activate(store)
    sid = s["id"]
    event = store.append_event("t1",sid,dict(id="lease-event",kind="transcript",actor="client",text="Synthetic",at_ms=902000,consent_epoch=0))
    first = store.claim_job("recorder")
    store.finish_job(first["id"],first["lease_token"],{})
    job = store.claim_job("worker")
    snapshot = store.load_snapshot("t1",sid)
    snapshot.update(revision=1,last_event_seq=1,evidence=[event])
    with store._connect() as c:
        c.execute("UPDATE beep_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s",(job["id"],))
    with store.job_lease(job["id"],job["lease_token"]):
        assert not store.save_snapshot("t1",sid,snapshot,expected_revision=0,consent_epoch=0)
    store.enqueue_job("t1",sid,"discovery",{})
    replacement = store.claim_job("replacement")
    with store.job_lease(replacement["id"],replacement["lease_token"]):
        assert store.save_snapshot("t1",sid,snapshot,expected_revision=0,consent_epoch=0)


def test_recording_failure_pauses_capture_and_fences_late_success(store):
    s=activate(store)
    sid=s["id"]
    job=store.claim_job("recorder")
    with store.job_lease(job["id"],job["lease_token"]):
        failed=store.set_recording("t1",sid,"failed","EG_failed")
        assert failed["status"]=="paused" and failed["consent_epoch"]==1
        with pytest.raises(StoreError):
            store.set_recording("t1",sid,"recording","EG_failed")
    assert store.get_session("t1",sid)["recording_status"]=="failed"


def test_snapshot_cannot_advance_cursor_while_omitting_persisted_evidence(store):
    s=activate(store)
    store.append_event("t1",s["id"],dict(id="event-required",kind="transcript",actor="client",text="Synthetic",at_ms=902000,consent_epoch=0))
    snapshot=store.load_snapshot("t1",s["id"])
    snapshot.update(revision=1,last_event_seq=1)
    with pytest.raises(StoreError):
        store.save_snapshot("t1",s["id"],snapshot,expected_revision=0,consent_epoch=0)


def test_atomic_worker_commit_interfaces_reject_wrong_scope_and_expired_leases(store):
    s=activate(store)
    sid=s["id"]
    event=store.append_event("t1",sid,dict(id="commit-event",kind="transcript",actor="client",text="Synthetic",at_ms=902000,consent_epoch=0))
    first=store.claim_job("recorder")
    store.finish_job(first["id"],first["lease_token"],{})
    job=store.claim_job("worker")
    snapshot=store.load_snapshot("t1",sid)
    snapshot.update(revision=1,last_event_seq=1,evidence=[event])
    assert not store.commit_snapshot(job["id"],"wrong","t1",sid,snapshot,0,0)
    assert store.commit_snapshot(job["id"],job["lease_token"],"t1",sid,snapshot,0,0)
    store.finish_job(job["id"],job["lease_token"],{})
    store.control("t1",sid,"client","finish")
    while True:
        job=store.claim_job("reporter")
        if job["kind"]=="report":
            break
        store.finish_job(job["id"],job["lease_token"],{})
    report=dict(session_id=sid,revision=1,title="Synthetic",summary="Synthetic evidence",evidence=[event],recommendations=[],status="partial",internal_opportunity={},generated_at="2026-09-11T00:00:00+00:00")
    bundle={"report":report,"expected_revision":1,"consent_epoch":1}
    with pytest.raises(StoreError):
        store.commit_report(job["id"],"wrong","t1",sid,bundle)
    assert store.commit_report(job["id"],job["lease_token"],"t1",sid,bundle) is None
    assert store.get_report("t1",sid)["revision"]==1


def test_create_is_durable_and_tenant_isolated(store):
    session = create(store)
    second = Store(store.database_url)
    assert second.get_session("t1", session["id"])["status"] == "awaiting_consent"
    assert session["room_name"].startswith("beep-")
    assert session["client_consent"] == {"ai": False, "recording": False}
    assert second.list_sessions("t2") == []
    with pytest.raises(StoreError):
        second.get_session("t2", session["id"])
    with pytest.raises(StoreError):
        create(store, max_seconds=5401)
