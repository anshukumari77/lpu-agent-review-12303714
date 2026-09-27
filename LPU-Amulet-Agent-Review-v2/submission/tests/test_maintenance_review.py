"""Deletion review regressions: real API and isolated PostgreSQL transactions."""
from concurrent.futures import ThreadPoolExecutor
import time

import pytest
from psycopg.types.json import Jsonb

import test_api
import test_store
from beep_agent.maintenance import Maintenance
from beep_agent.store import StoreError

store = test_api.store
settings = test_api.settings
client = test_api.client


def test_exchange_after_deletion_rejects_consumed_and_unused_invites(client, settings, store):
    created = test_api.new_session(client)
    sid = created["session"]["id"]
    assert test_api.exchange(client, created["invitations"]["client"]).status_code == 200
    Maintenance(settings, store=store)._fence_deletion("local", sid)
    assert client.get(f"/api/sessions/{sid}").status_code == 401
    for invitation in created["invitations"].values():
        response = test_api.exchange(client, invitation)
        assert response.status_code == 409, response.text
        assert "set-cookie" not in response.headers
    with store._connect() as connection:
        assert connection.execute("SELECT 1 FROM beep_auth_tokens WHERE session_id=%s", (sid,)).fetchone() is None


def test_cookie_validation_rejects_pending_state_even_with_retained_token(client, store):
    created = test_api.new_session(client)
    sid = created["session"]["id"]
    assert test_api.exchange(client, created["invitations"]["client"]).status_code == 200
    # Storage fixture represents retained credentials during a deletion retry.
    with store._connect() as connection:
        connection.execute("UPDATE beep_sessions SET data=jsonb_set(data,'{deletion_pending}','true') WHERE id=%s", (sid,))
    assert client.get(f"/api/sessions/{sid}").status_code == 401
    with pytest.raises(StoreError, match="deletion"):
        store.register_auth_token("late-cookie", "cookie", sid, 60)


def test_actual_exchange_waiting_behind_deletion_cannot_restore_access(client, settings, store):
    created = test_api.new_session(client)
    sid = created["session"]["id"]
    maintenance = Maintenance(settings, store=store)
    with ThreadPoolExecutor(max_workers=2) as pool:
        with store._connect() as blocker:
            blocker.execute("SELECT id FROM beep_sessions WHERE id=%s FOR UPDATE", (sid,))
            pid = blocker.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
            fence = pool.submit(maintenance._fence_deletion, "local", sid)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                with store._connect() as observer:
                    waiting = observer.execute("SELECT 1 FROM pg_stat_activity WHERE %s=ANY(pg_blocking_pids(pid))", (pid,)).fetchone()
                if waiting:
                    break
                time.sleep(0.01)
            assert waiting, "Deletion must be queued on the session lock before exchange"
            exchange = pool.submit(test_api.exchange, client, created["invitations"]["client"])
            # Wait until the actual HTTP route is also blocked on PostgreSQL.
            while time.monotonic() < deadline:
                with store._connect() as observer:
                    count = observer.execute("SELECT count(*) AS n FROM pg_stat_activity WHERE cardinality(pg_blocking_pids(pid))>0 AND query LIKE '%%beep_%%'").fetchone()["n"]
                if count >= 2 or exchange.done():
                    break
                time.sleep(0.01)
            assert count >= 2 or exchange.done(), "API exchange did not reach database"
        fence.result(timeout=5)
        response = exchange.result(timeout=5)
    assert response.status_code == 409, response.text
    assert "set-cookie" not in response.headers
    assert client.get(f"/api/sessions/{sid}").status_code == 200  # Operator is unaffected.
    with store._connect() as connection:
        assert connection.execute("SELECT 1 FROM beep_auth_tokens WHERE session_id=%s", (sid,)).fetchone() is None


@pytest.mark.parametrize("pending,settled", [(True, False), (False, False), (True, True)])
async def test_purge_refuses_cleanup_before_any_mutation(settings, store, pending, settled):
    session = test_store.activate(store)
    sid = session["id"]
    start = store.claim_job("start")
    with store.job_lease(start["id"], start["lease_token"]):
        reservation = store.set_recording("t1", sid, "starting")["recording_reservation"]
        store.set_recording("t1", sid, "failed")
    store.finish_job(start["id"], start["lease_token"], {})
    store.control("t1", sid, "client", "finish")
    store.register_auth_token("retained-cookie", "cookie", sid, 60)
    store.register_auth_token("consumed-invite", "invite", sid, 60)
    # Storage fixture: partially completed report; stale flag variants must fail closed.
    with store._connect() as connection:
        current = store._row(connection, "t1", sid)["data"]
        current.update(status="partial", recording_cleanup_pending=pending)
        connection.execute("UPDATE beep_sessions SET data=%s,report=%s WHERE id=%s", (Jsonb(current), Jsonb({"summary": "retain"}), sid))
        connection.execute("UPDATE beep_recording_reservations SET settled=%s WHERE id=%s", (settled, reservation["id"]))

    def snapshot():
        with store._connect() as connection:
            return {
                "session": store._row(connection, "t1", sid),
                "jobs": connection.execute("SELECT * FROM beep_jobs WHERE session_id=%s ORDER BY id", (sid,)).fetchall(),
                "auth": connection.execute("SELECT * FROM beep_auth_tokens WHERE session_id=%s ORDER BY digest", (sid,)).fetchall(),
                "reservations": connection.execute("SELECT * FROM beep_recording_reservations WHERE session_id=%s ORDER BY id", (sid,)).fetchall(),
            }

    calls = []

    class StorageBoundary:
        async def delete(self, state):
            calls.append(state)
            raise RuntimeError("Synthetic remote deletion boundary reached")

    before = snapshot()
    assert any(job["kind"] == "recording_stop" and job["status"] == "queued" for job in before["jobs"])
    refused = False
    try:
        await Maintenance(settings, store=store, recording=StorageBoundary()).purge("t1", sid, apply=True)
    except StoreError as exc:
        assert "reconciliation" in str(exc)
        refused = True
    except RuntimeError:
        pass
    assert snapshot() == before, "Refusal must preserve session, report, credentials and cleanup jobs"
    assert refused and calls == []
    with store._connect() as connection:
        table = connection.execute("SELECT to_regclass('beep_deletion_tombstones') AS name").fetchone()["name"]
        if table:
            assert connection.execute("SELECT 1 FROM beep_deletion_tombstones WHERE session_id=%s", (sid,)).fetchone() is None
    assert store.cookie_valid("retained-cookie")
    cleanup = store.claim_job("cleanup")
    assert cleanup["kind"] == "recording_stop"
    with store.job_lease(cleanup["id"], cleanup["lease_token"]):
        store.complete_recording_cleanup("t1", sid, reservation, ["EG-confirmed-stopped"])
    with store._connect() as connection:
        assert connection.execute("SELECT settled FROM beep_recording_reservations WHERE id=%s", (reservation["id"],)).fetchone()["settled"]
