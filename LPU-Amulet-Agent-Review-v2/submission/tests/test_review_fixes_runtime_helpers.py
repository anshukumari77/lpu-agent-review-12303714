"""Peer-review runtime helper contracts against real isolated PostgreSQL."""
import pytest
import test_store



@pytest.fixture
def store():
    yield from test_store.store.__wrapped__()


def running_discovery(store):
    s = test_store.activate(store)
    start = store.claim_job("start")
    store.finish_job(start["id"], start["lease_token"], {})
    store.set_recording("t1", s["id"], "recording", "EG-current")
    store.enqueue_job("t1", s["id"], "discovery", {})
    return store.get_session("t1", s["id"]), store.claim_job("discovery")


def test_monitor_fences_during_discovery_lease_and_rejects_stale(store):
    s, job = running_discovery(store)
    assert not store.observe_recording("t1", s["id"], "failed", "EG-other", 0)
    assert not store.observe_recording("t1", s["id"], "failed", "EG-current", 1)
    assert store.observe_recording("t1", s["id"], "failed", "EG-current", 0)
    current = store.get_session("t1", s["id"])
    assert current["status"] == "paused" and current["recording_status"] == "failed"
    assert current["consent_epoch"] == 1 and current["egress_id"] == "EG-current"
    assert not store.observe_recording("t1", s["id"], "failed", "EG-current", 0)
    assert not store.observe_recording("t1", s["id"], "recording", "EG-current", 1)
    with store._connect() as c:
        assert c.execute("SELECT status FROM beep_jobs WHERE id=%s", (job["id"],)).fetchone()["status"] == "running"


def test_inference_admission_is_scoped_durable_and_revoked_by_pause(store):
    s, job = running_discovery(store)
    args = (job["id"], job["lease_token"], "t1", s["id"], 0)
    assert not store.admit_inference(job["id"], "wrong", "t1", s["id"], 0)
    assert not store.admit_inference(job["id"], job["lease_token"], "t1", s["id"], 1)
    assert store.admit_inference(*args)
    with store._connect() as c:
        marker = c.execute("SELECT payload->'inference_admission' AS marker FROM beep_jobs WHERE id=%s", (job["id"],)).fetchone()["marker"]
    assert marker["session_id"] == s["id"] and marker["consent_epoch"] == 0 and marker["request_marker"]
    store.control("t1", s["id"], "client", "pause")
    assert not store.admit_inference(*args)
    with store._connect() as c:
        marker = c.execute("SELECT payload->'inference_admission' AS marker FROM beep_jobs WHERE id=%s", (job["id"],)).fetchone()["marker"]
    assert marker["revoked"] is True


def test_expired_inference_lease_cannot_admit(store):
    s, job = running_discovery(store)
    with store._connect() as c:
        c.execute("UPDATE beep_jobs SET lease_until=clock_timestamp()-interval '1 second' WHERE id=%s", (job["id"],))
    assert not store.admit_inference(job["id"], job["lease_token"], "t1", s["id"], 0)
