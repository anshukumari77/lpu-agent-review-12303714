"""Real PostgreSQL fencing regressions, unique test schemas only."""
import pytest
from psycopg.types.json import Jsonb
import test_store
from beep_agent.store import StoreError


@pytest.fixture
def store():
    yield from test_store.store.__wrapped__()


activate = test_store.activate


@pytest.mark.parametrize("status", ["stopped", "stopping", "failed", "deleted", "queued", "not_started"])
def test_expired_recording_worker_cannot_mutate_new_current(store, status):
    s = activate(store)
    sid = s["id"]
    old = store.claim_job("old")
    with store.job_lease(old["id"], old["lease_token"]):
        store.set_recording("t1", sid, "starting")
        store.set_recording("t1", sid, "recording", "EG-old")
    with store._connect() as c:
        c.execute("UPDATE beep_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s", (old["id"],))
        current = {**store._row(c, "t1", sid)["data"], "egress_id": "EG-new", "recording_status": "recording", "consent_epoch": 1}
        c.execute("UPDATE beep_sessions SET data=%s WHERE id=%s", (Jsonb(current), sid))
    with store.job_lease(old["id"], old["lease_token"]), pytest.raises(StoreError):
        store.set_recording("t1", sid, status, "EG-old")
    assert store.get_session("t1", sid) == current


def test_valid_cleanup_lease_cannot_replace_new_egress(store):
    s = activate(store)
    sid = s["id"]
    old = store.claim_job("start")
    store.finish_job(old["id"], old["lease_token"], {})
    store.set_recording("t1", sid, "recording", "EG-new")
    store.enqueue_job("t1", sid, "recording_stop", {"egress_id": "EG-old", "consent_epoch": 0})
    cleanup = store.claim_job("cleanup")
    before = store.get_session("t1", sid)
    with store.job_lease(cleanup["id"], cleanup["lease_token"]), pytest.raises(StoreError):
        store.set_recording("t1", sid, "stopped", "EG-old")
    assert store.get_session("t1", sid) == before


def test_unknown_start_failure_keeps_original_reservation_and_signed_cleanup(store):
    s = activate(store)
    sid = s["id"]
    job = store.claim_job("start")
    with store.job_lease(job["id"], job["lease_token"]):
        starting = store.set_recording("t1", sid, "starting")
        failed = store.set_recording("t1", sid, "failed")
    with store._connect() as c:
        cleanup = c.execute("SELECT * FROM beep_jobs WHERE session_id=%s AND kind='recording_stop'", (sid,)).fetchone()
    assert cleanup is not None, "Unknown acknowledgement must leave durable reconciliation work"
    reservation = starting["recording_reservation"]
    assert reservation["consent_epoch"] == 0
    assert reservation["output_prefix"].startswith(f"beep/t1/{sid}/epoch-0-")
    assert cleanup["payload"]["reservation"] == reservation
    assert cleanup["payload"]["recording_epoch"] == 0
    assert cleanup["payload"]["cleanup_signature"]
    assert failed["consent_epoch"] == 1 and failed["recording_cleanup_pending"]
    store.finish_job(job["id"], job["lease_token"], {})
    cleanup = store.claim_job("cleanup")
    with store.job_lease(cleanup["id"], cleanup["lease_token"]):
        with pytest.raises(StoreError):
            store.set_recording("t1", sid, "stopped")
        with pytest.raises(StoreError):
            store.complete_recording_cleanup("t1", sid, reservation, [])
        store.complete_recording_cleanup("t1", sid, reservation, ["EG-reconciled"])
    stopped = store.get_session("t1", sid)
    assert stopped["recording_status"] == "stopped" and not stopped["recording_cleanup_pending"]


def test_late_identity_is_durable_cleanup_only_never_current(store):
    s = activate(store)
    sid = s["id"]
    old = store.claim_job("old")
    with store.job_lease(old["id"], old["lease_token"]):
        reserved = store.set_recording("t1", sid, "starting")["recording_reservation"]
    with store._connect() as c:
        c.execute("UPDATE beep_jobs SET status='failed' WHERE id=%s", (old["id"],))
        current = {**store._row(c, "t1", sid)["data"], "recording_reservation": {"id": "new"},
                   "egress_id": "EG-new", "recording_epoch": 1, "consent_epoch": 1,
                   "recording_status": "recording"}
        c.execute("UPDATE beep_sessions SET data=%s WHERE id=%s", (Jsonb(current), sid))
    with store.job_lease(old["id"], old["lease_token"]):
        store.request_recording_cleanup("t1", sid, reserved, "EG-late")
    cleanup = store.claim_job("cleanup")
    assert cleanup["payload"]["egress_id"] == "EG-late"
    with store.job_lease(cleanup["id"], cleanup["lease_token"]):
        store.complete_recording_cleanup("t1", sid, reserved, ["EG-late"])
    assert store.get_session("t1", sid) == current
    with store._connect() as c:
        row = c.execute("SELECT settled,egress_ids FROM beep_recording_reservations WHERE id=%s", (reserved["id"],)).fetchone()
    assert row == {"settled": True, "egress_ids": ["EG-late"]}
