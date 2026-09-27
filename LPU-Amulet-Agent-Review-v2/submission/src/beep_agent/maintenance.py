"""Explicit, dry-run-by-default retention administration."""
from __future__ import annotations

import asyncio

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .store import Store, StoreError

TERMINAL = {"awaiting_consent", "completed", "partial", "failed"}


class Maintenance:
    def __init__(self, settings, *, store=None, recording=None):
        self.settings = settings
        self.store = store or Store(settings.database_url)
        self.recording = recording

    def due(self) -> list[dict]:
        with psycopg.connect(self.settings.database_url, row_factory=dict_row) as connection:
            return [row["data"] for row in connection.execute(
                """SELECT data FROM beep_sessions s WHERE (data->>'status'=ANY(%s)
                OR (data->>'status'='finalising' AND EXISTS (
                    SELECT 1 FROM beep_jobs j WHERE j.session_id=s.id AND j.kind='report' AND j.status='failed')
                    AND NOT EXISTS (SELECT 1 FROM beep_jobs j WHERE j.session_id=s.id AND j.kind='report'
                        AND (j.status='running' OR (j.status='queued' AND j.attempts<3)))))
                AND (data->>'created_at')::timestamptz < clock_timestamp()-(%s*interval '1 day')
                ORDER BY data->>'created_at' LIMIT 500""",
                (sorted(TERMINAL), self.settings.retention_days)).fetchall()]

    async def reconcile_report(self, tenant_id, session_id, *, apply=False):
        """Only read/reuse a completed operation receipt; no provider or recording IO."""
        import hashlib
        import json
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from .worker import DurableWorker, store_call

        session = await store_call(self.store.get_session, tenant_id, session_id)
        if session["status"] == "finalising":
            session = await store_call(self.store.triage_report_failure, tenant_id, session_id, apply=apply)
            if not apply:
                return {"status": "terminalisation_required", "session_id": session_id}
        failure = session.get("report_failure")
        if session["status"] != "failed" or not failure or failure["state"] != "reconciliation_required":
            raise StoreError("No terminal report failure to reconcile")
        key = f"report:{failure['revision']}:{failure['consent_epoch']}"
        config = {"configurable": {"thread_id": DurableWorker.thread_id(tenant_id, session_id),
            "checkpoint_ns": "operation:" + hashlib.sha256(key.encode()).hexdigest()}}
        async with AsyncPostgresSaver.from_conn_string(self.settings.database_url) as saver:
            try:
                checkpoint = await saver.aget_tuple(config)
            except psycopg.errors.UndefinedTable:
                checkpoint = None  # A lease can expire before checkpoint setup.
        values = checkpoint.checkpoint["channel_values"] if checkpoint else {}
        if values.get("status") != "complete":
            return {"status": "reconciliation_required", "session_id": session_id,
                    "reason": "no_confirmed_report_result"}
        if not apply:
            return {"status": "retained_report_available", "session_id": session_id,
                    "revision": failure["revision"]}
        try:
            report = json.loads(values["result_json"])
        except (KeyError, ValueError, TypeError):
            raise StoreError("Retained report receipt is invalid") from None
        return await store_call(self.store.publish_retained_report, tenant_id, session_id, failure, report)

    async def purge(self, tenant_id, session_id, *, apply=False):
        session = self.store.get_session(tenant_id, session_id)
        if session["status"] not in TERMINAL:
            raise StoreError("Cannot purge a running review")
        if not apply:
            return {"status": "dry_run", "session_id": session_id}
        session = await asyncio.to_thread(self._fence_deletion, tenant_id, session_id)
        recording = self.recording
        if recording is None:
            from .recording import RecordingService
            recording = RecordingService(self.settings)
        # Delete every object in the exact prefix even after a lost egress acknowledgement.
        # The adapter reads the prefix back and refuses failed/partial deletion.
        await recording.delete(session)
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from .worker import DurableWorker
        thread_id = DurableWorker.thread_id(tenant_id, session_id)
        async with AsyncPostgresSaver.from_conn_string(self.settings.database_url) as saver:
            await saver.setup()
            await saver.adelete_thread(thread_id)
            if await saver.aget_tuple({"configurable": {"thread_id": thread_id}}) is not None:
                raise StoreError("Checkpoint deletion could not be confirmed")
        await asyncio.to_thread(self.store.set_recording, tenant_id, session_id, "deleted")
        await asyncio.to_thread(self.store.delete_session, tenant_id, session_id)
        await asyncio.to_thread(self._confirm_deleted, tenant_id, session_id)
        return {"status": "deleted", "session_id": session_id}

    def _fence_deletion(self, tenant_id, session_id):
        with psycopg.connect(self.settings.database_url, row_factory=dict_row) as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS beep_deletion_tombstones (
                tenant_id text NOT NULL, session_id text NOT NULL, requested_at timestamptz
                NOT NULL DEFAULT clock_timestamp(), deleted_at timestamptz,
                PRIMARY KEY(tenant_id,session_id))""")
            row = connection.execute("SELECT data FROM beep_sessions WHERE tenant_id=%s AND id=%s FOR UPDATE",
                                     (tenant_id, session_id)).fetchone()
            if row is None:
                raise StoreError("Session not found", 404)
            session = row["data"]
            if session.get("recording_cleanup_pending") or connection.execute(
                "SELECT 1 FROM beep_recording_reservations WHERE session_id=%s AND NOT settled", (session_id,)).fetchone():
                raise StoreError("Recording reconciliation must finish before deletion")
            if session["status"] not in TERMINAL or session["recording_status"] in {"queued", "starting", "recording", "stopping"}:
                raise StoreError("Recording and review must stop before deletion")
            if connection.execute("SELECT 1 FROM beep_jobs WHERE session_id=%s AND status='running'", (session_id,)).fetchone():
                raise StoreError("Cannot purge a leased review")
            session.update(status="failed", deletion_pending=True,
                           consent_epoch=session["consent_epoch"] + 1,
                           client_consent={"ai": False, "recording": False},
                           facilitator_consent={"ai": False, "recording": False})
            connection.execute("INSERT INTO beep_deletion_tombstones(tenant_id,session_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                               (tenant_id, session_id))
            connection.execute("UPDATE beep_sessions SET data=%s,report=NULL WHERE tenant_id=%s AND id=%s",
                               (Jsonb(session), tenant_id, session_id))
            connection.execute("DELETE FROM beep_auth_tokens WHERE session_id=%s", (session_id,))
            connection.execute("UPDATE beep_jobs SET status='failed',error='deletion_requested' WHERE session_id=%s AND status='queued'", (session_id,))
            return session

    def _confirm_deleted(self, tenant_id, session_id):
        with psycopg.connect(self.settings.database_url) as connection:
            if connection.execute("SELECT 1 FROM beep_sessions WHERE tenant_id=%s AND id=%s", (tenant_id, session_id)).fetchone():
                raise StoreError("Local deletion is not confirmed")
            connection.execute("UPDATE beep_deletion_tombstones SET deleted_at=clock_timestamp() WHERE tenant_id=%s AND session_id=%s", (tenant_id, session_id))


def main():
    import argparse
    import json
    from .config import Settings
    parser = argparse.ArgumentParser(description="Explicit BEEP retention/report recovery; dry-run by default")
    parser.add_argument("command", choices=["due", "purge", "reconcile-report"])
    parser.add_argument("--tenant")
    parser.add_argument("--session")
    parser.add_argument("--apply", action="store_true", help="Apply exact-session purge or retained-report publication")
    args = parser.parse_args()
    maintenance = Maintenance(Settings())
    if args.command == "due":
        print(json.dumps([{key: item[key] for key in ("tenant_id", "id", "status", "created_at")}
                          for item in maintenance.due()], indent=2))
    elif not args.tenant or not args.session:
        parser.error(f"{args.command} requires --tenant and --session")
    elif args.command == "reconcile-report":
        print(json.dumps(asyncio.run(maintenance.reconcile_report(args.tenant, args.session, apply=args.apply))))
    else:
        print(json.dumps(asyncio.run(maintenance.purge(args.tenant, args.session, apply=args.apply))))


if __name__ == "__main__":
    main()
