"""Tenant-bound PostgreSQL persistence. Transactions never span provider calls."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import hashlib
import hmac
import json
import uuid
from urllib.parse import quote

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb


_JOB_LEASE = ContextVar("beep_job_lease", default=None)
_PUBLICATION_EPOCH = ContextVar("beep_publication_epoch", default=None)


class StoreError(ValueError):
    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, database_url: str):
        self.database_url = database_url
        self.max_concurrent_sessions = 10
        self.sponsored_limit = 3

    def configure_limits(self, *, max_concurrent_sessions: int, sponsored_limit: int = 3):
        if not 1 <= max_concurrent_sessions <= 500 or not 0 <= sponsored_limit <= 100:
            raise StoreError("Invalid admission limits", 422)
        self.max_concurrent_sessions = max_concurrent_sessions
        self.sponsored_limit = sponsored_limit

    @contextmanager
    def job_lease(self, job_id: str, lease_token: str):
        """Worker wraps execution; context propagates through asyncio.to_thread."""
        token = _JOB_LEASE.set((job_id, lease_token))
        try:
            yield
        finally:
            _JOB_LEASE.reset(token)

    @staticmethod
    def _lease_valid(c, session_id):
        lease = _JOB_LEASE.get()
        if lease is None:
            # Direct administrative/test writes are allowed only without a running worker.
            return c.execute("SELECT 1 FROM beep_jobs WHERE session_id=%s AND status='running'", (session_id,)).fetchone() is None
        return c.execute("""SELECT 1 FROM beep_jobs WHERE session_id=%s AND id=%s AND lease_token=%s
            AND status='running' AND lease_until>clock_timestamp() FOR SHARE""", (session_id, *lease)).fetchone() is not None

    def _connect(self):
        if not self.database_url.strip():
            raise StoreError("Database is not configured", 503)
        return psycopg.connect(self.database_url, row_factory=dict_row, connect_timeout=5)

    def initialize(self):
        with self._connect() as c:
            c.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema() || ':beep-schema'))")
            c.execute("""CREATE TABLE IF NOT EXISTS beep_sessions (
                id text PRIMARY KEY, tenant_id text NOT NULL, data jsonb NOT NULL,
                snapshot jsonb NOT NULL, report jsonb,
                UNIQUE(tenant_id,id)
            )""")

            c.execute("""CREATE TABLE IF NOT EXISTS beep_consent_receipts (
                id text PRIMARY KEY, tenant_id text NOT NULL, session_id text NOT NULL,
                seq bigint NOT NULL CHECK (seq > 0),
                role text NOT NULL CHECK (role IN ('client','facilitator')),
                ai bool NOT NULL, recording bool NOT NULL,
                occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
                epoch_before int NOT NULL CHECK (epoch_before >= 0),
                epoch_after int NOT NULL CHECK (epoch_after >= epoch_before),
                principal_kind text NOT NULL CHECK (principal_kind IN ('authenticated_cookie','internal_test')),
                token_digest text CHECK (token_digest ~ '^[0-9a-f]{64}$'),
                notice_binding text NOT NULL, policy jsonb,
                CHECK ((principal_kind='internal_test' AND token_digest IS NULL AND policy IS NULL)
                    OR (principal_kind='authenticated_cookie' AND token_digest IS NOT NULL
                        AND (policy IS NOT NULL OR notice_binding='withdrawal_notice_unavailable'))),
                UNIQUE(session_id,seq),
                FOREIGN KEY(tenant_id,session_id) REFERENCES beep_sessions(tenant_id,id) ON DELETE CASCADE
            )""")
            c.execute("""CREATE OR REPLACE FUNCTION beep_consent_receipt_immutable() RETURNS trigger
                LANGUAGE plpgsql AS $$ BEGIN
                    IF TG_OP='UPDATE' THEN
                        RAISE EXCEPTION 'Consent receipts are immutable';
                    END IF;
                    IF EXISTS (SELECT 1 FROM beep_sessions WHERE tenant_id=OLD.tenant_id AND id=OLD.session_id) THEN
                        RAISE EXCEPTION 'Consent receipts are removed only by session deletion';
                    END IF;
                    RETURN OLD;
                END $$""")
            c.execute("""CREATE OR REPLACE TRIGGER beep_consent_receipts_immutable
                BEFORE UPDATE OR DELETE ON beep_consent_receipts FOR EACH ROW
                EXECUTE FUNCTION beep_consent_receipt_immutable()""")

            c.execute("""CREATE TABLE IF NOT EXISTS beep_jobs (
                id text PRIMARY KEY, tenant_id text NOT NULL, session_id text NOT NULL,
                kind text NOT NULL, payload jsonb NOT NULL, status text NOT NULL DEFAULT 'queued',
                created_at timestamptz NOT NULL DEFAULT clock_timestamp(), attempts int NOT NULL DEFAULT 0,
                worker_id text, lease_token text, lease_until timestamptz, result jsonb, error text,
                FOREIGN KEY(tenant_id,session_id) REFERENCES beep_sessions(tenant_id,id) ON DELETE CASCADE
            )""")
            c.execute("CREATE INDEX IF NOT EXISTS beep_jobs_claim ON beep_jobs(status,created_at)")
            c.execute("CREATE UNIQUE INDEX IF NOT EXISTS beep_jobs_one_running ON beep_jobs(session_id) WHERE status='running'")

            c.execute("""CREATE TABLE IF NOT EXISTS beep_events (
                tenant_id text NOT NULL,session_id text NOT NULL,id text NOT NULL,seq bigint NOT NULL,
                data jsonb NOT NULL,PRIMARY KEY(session_id,id),UNIQUE(session_id,seq),
                FOREIGN KEY(tenant_id,session_id) REFERENCES beep_sessions(tenant_id,id) ON DELETE CASCADE
            )""")

            c.execute("""CREATE TABLE IF NOT EXISTS beep_reports (
                session_id text NOT NULL REFERENCES beep_sessions(id) ON DELETE CASCADE,
                revision int NOT NULL,data jsonb NOT NULL,valid bool NOT NULL DEFAULT true,
                PRIMARY KEY(session_id,revision)
            )""")
            c.execute("""CREATE TABLE IF NOT EXISTS beep_usage (
                id bigserial PRIMARY KEY,session_id text NOT NULL REFERENCES beep_sessions(id) ON DELETE CASCADE,
                data jsonb NOT NULL,created_at timestamptz NOT NULL DEFAULT clock_timestamp()
            )""")

            c.execute("""CREATE TABLE IF NOT EXISTS beep_login_bounds (
                key text PRIMARY KEY,window_start timestamptz NOT NULL,count int NOT NULL
            )""")

            c.execute("CREATE TABLE IF NOT EXISTS beep_sponsorships (tenant_id text PRIMARY KEY, admitted int NOT NULL)")

            c.execute("""CREATE TABLE IF NOT EXISTS beep_auth_tokens (
                digest text PRIMARY KEY,purpose text NOT NULL,expires_at timestamptz NOT NULL,
                session_id text REFERENCES beep_sessions(id) ON DELETE CASCADE
            )""")

            c.execute("""CREATE TABLE IF NOT EXISTS beep_dispatches (
                session_id text NOT NULL REFERENCES beep_sessions(id) ON DELETE CASCADE,
                consent_epoch int NOT NULL,dispatch_id text,
                PRIMARY KEY(session_id,consent_epoch)
            )""")

            c.execute("""CREATE TABLE IF NOT EXISTS beep_recording_reservations (
                id text PRIMARY KEY, session_id text NOT NULL REFERENCES beep_sessions(id) ON DELETE CASCADE,
                consent_epoch int NOT NULL, data jsonb NOT NULL, signing_key text NOT NULL,
                settled bool NOT NULL DEFAULT false, egress_ids jsonb NOT NULL DEFAULT '[]',
                UNIQUE(session_id,consent_epoch)
            )""")

    def close(self):
        # Each operation owns and closes its connection; no process-global transaction.
        pass

    def _row(self, c, tenant_id, session_id, *, lock=False):
        row = c.execute("SELECT * FROM beep_sessions WHERE tenant_id=%s AND id=%s" +
                        (" FOR UPDATE" if lock else ""), (tenant_id, session_id)).fetchone()
        if row is None:
            raise StoreError("Session not found", 404)
        return row

    def _write(self, c, data):
        c.execute("UPDATE beep_sessions SET data=%s WHERE tenant_id=%s AND id=%s",
                  (Jsonb(data), data["tenant_id"], data["id"]))
        return data

    def create_session(self, tenant_id: str, *, title: str, pack_id: str, offer: str,
                       max_seconds: int = 5400, intro_seconds: int = 900,
                       budget_aud: float = 60) -> dict:
        if not tenant_id or not title.strip() or len(title) > 200:
            raise StoreError("Invalid session metadata", 422)
        if offer not in {"paid", "sponsored"} or pack_id not in {"general", "quotation", "recurring_reporting"}:
            raise StoreError("Invalid offer or pack", 422)
        if not 1 <= max_seconds <= 5400 or not 900 <= intro_seconds <= max_seconds or not 0 < budget_aud <= 10000:
            raise StoreError("Invalid session limits", 422)
        sid = str(uuid.uuid4())
        data = dict(id=sid, tenant_id=tenant_id, title=title, pack_id=pack_id, offer=offer,
                    status="awaiting_consent", created_at=utcnow(), started_at=None,
                    consent_epoch=0, revision=0, event_seq=0,
                    client_consent={"ai": False, "recording": False},
                    facilitator_consent={"ai": False, "recording": False},
                    max_seconds=max_seconds, intro_seconds=intro_seconds, budget_aud=budget_aud,
                    recording_status="not_started", egress_id=None, room_name="beep-" + sid)
        snapshot = dict(schema_version="1", title=title, pack_id=pack_id, revision=0,
                        consent_epoch=0, last_event_seq=0, evidence=[], claims=[], steps=[],
                        edges=[], coverage=[], unknowns=[], probe=None)
        with self._connect() as c:
            c.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema() || ':beep-admission'))")
            count = c.execute("""SELECT count(*) AS n FROM beep_sessions WHERE data->>'status'
                IN ('awaiting_consent','introduction','active','paused','finalising')""").fetchone()["n"]
            if count >= self.max_concurrent_sessions:
                raise StoreError("Concurrent session admission limit reached", 429)
            if offer == "sponsored":
                previous = c.execute("SELECT admitted FROM beep_sponsorships WHERE tenant_id=%s", (tenant_id,)).fetchone()
                if (previous["admitted"] if previous else 0) >= self.sponsored_limit:
                    raise StoreError("Sponsored session allocation exhausted", 429)
                c.execute("""INSERT INTO beep_sponsorships VALUES (%s,1) ON CONFLICT(tenant_id)
                    DO UPDATE SET admitted=beep_sponsorships.admitted+1""", (tenant_id,))
            c.execute("INSERT INTO beep_sessions(id,tenant_id,data,snapshot) VALUES (%s,%s,%s,%s)",
                      (sid, tenant_id, Jsonb(data), Jsonb(snapshot)))
        return data

    def _fence(self, c, row):
        s = row["data"]
        c.execute("""UPDATE beep_jobs SET payload=jsonb_set(payload,
            '{inference_admission,revoked}', 'true'::jsonb)
            WHERE session_id=%s AND payload ? 'inference_admission'""", (s["id"],))
        original_epoch = s.get("recording_epoch", s["consent_epoch"])
        reservation = s.get("recording_reservation")
        s["consent_epoch"] += 1
        s["facilitator_media"] = {"status": "reconciliation_required", "token_revocation": False}
        snap = row["snapshot"]
        snap["consent_epoch"] = s["consent_epoch"]
        snap["probe"] = None
        c.execute("UPDATE beep_sessions SET snapshot=%s WHERE id=%s", (Jsonb(snap), s["id"]))
        if reservation and s.get("recording_cleanup_pending"):
            self._enqueue_recording_cleanup(c, s, reservation, s.get("egress_id"))
        elif s.get("egress_id") or s["recording_status"] in {"starting", "queued", "recording"}:
            self._enqueue(c, s["tenant_id"], s["id"], "recording_stop", {
                "egress_id": s.get("egress_id"), "consent_epoch": s["consent_epoch"],
                "recording_epoch": original_epoch})

    @staticmethod
    def _cleanup_signature(key, payload):
        return hmac.new(key.encode(), json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()

    def _enqueue_recording_cleanup(self, c, s, reservation, egress_id=None):
        record = c.execute("SELECT * FROM beep_recording_reservations WHERE id=%s AND session_id=%s",
                           (reservation["id"], s["id"])).fetchone()
        if not record or record["data"] != reservation:
            raise StoreError("Recording reservation not found")
        payload = {"reservation": reservation, "egress_id": egress_id,
                   "consent_epoch": s["consent_epoch"], "recording_epoch": reservation["consent_epoch"]}
        payload["cleanup_signature"] = self._cleanup_signature(record["signing_key"], payload)
        return self._enqueue(c, s["tenant_id"], s["id"], "recording_stop", payload)

    def request_recording_cleanup(self, tenant_id, session_id, reservation, egress_id=None):
        """Append cleanup work only; an expired owning start may retain a late ID."""
        if egress_id is not None and (not isinstance(egress_id, str) or not egress_id or len(egress_id)>200):
            raise StoreError("Invalid cleanup egress identity", 422)
        with self._connect() as c:
            s = self._row(c, tenant_id, session_id, lock=True)["data"]
            lease = _JOB_LEASE.get()
            if lease:
                owner = c.execute("""SELECT 1 FROM beep_jobs WHERE id=%s AND lease_token=%s
                    AND session_id=%s AND kind='recording_start'
                    AND (payload->>'consent_epoch')::int=%s""",
                    (*lease, session_id, reservation["consent_epoch"])).fetchone()
                if not owner:
                    raise StoreError("Recording reservation owner required")
            elif not self._lease_valid(c, session_id):
                raise StoreError("Cannot administratively reconcile a leased session")
            return self._enqueue_recording_cleanup(c, s, reservation, egress_id)

    def complete_recording_cleanup(self, tenant_id, session_id, reservation, egress_ids):
        """Commit confirmed cleanup to its own reservation, never a newer recorder."""
        from .domain import RecordingManifest
        from pydantic import ValidationError
        if (not egress_ids or len(egress_ids) > 32
                or any(not isinstance(e, str) or not e or len(e)>200 for e in egress_ids)
                or len(set(egress_ids)) != len(egress_ids)):
            raise StoreError("Recording shutdown is not confirmed")
        failed_ids = getattr(egress_ids, "failed_ids", [])
        if not set(failed_ids).issubset(egress_ids):
            raise StoreError("Failed recording identity is outside cleanup scope")
        try:
            manifests = [RecordingManifest.model_validate(m) for m in getattr(egress_ids, "manifests", [])]
        except ValidationError:
            raise StoreError("Invalid recording artifact manifest") from None
        with self._connect() as c:
            s = self._row(c, tenant_id, session_id, lock=True)["data"]
            lease = _JOB_LEASE.get()
            if not lease or not self._lease_valid(c, session_id):
                raise StoreError("Recording cleanup lease lost")
            job = c.execute("SELECT kind,payload FROM beep_jobs WHERE id=%s", (lease[0],)).fetchone()
            record = c.execute("SELECT * FROM beep_recording_reservations WHERE id=%s AND session_id=%s FOR UPDATE",
                               (reservation["id"], session_id)).fetchone()
            payload = dict(job["payload"])
            signature = payload.pop("cleanup_signature", "")
            if (job["kind"] != "recording_stop" or not record or record["data"] != reservation
                or payload.get("reservation") != reservation
                or not hmac.compare_digest(signature, self._cleanup_signature(record["signing_key"], payload))):
                raise StoreError("Recording cleanup scope or signature invalid")
            if (payload.get("egress_id") and payload["egress_id"] not in egress_ids
                    or not set(record["egress_ids"]).issubset(egress_ids)):
                raise StoreError("Known recording identity was not confirmed stopped")
            if manifests:
                binding_row = c.execute("""SELECT result->'recording_storage_binding' AS binding
                    FROM beep_jobs WHERE session_id=%s AND kind='recording_start'
                    AND result->'recording_storage_binding'->>'reservation_id'=%s""",
                    (session_id, reservation["id"])).fetchone()
                bucket = binding_row["binding"]["bucket"] if binding_row else None
                if (len(manifests) != len(egress_ids) or {m.egress_id for m in manifests} != set(egress_ids)
                        or {m.egress_id for m in manifests if m.artifact_status == "unavailable"} != set(failed_ids)
                        or (len(manifests) > 1 and len(failed_ids) != len(manifests))
                        or any(m.reservation_id != reservation["id"] or m.consent_epoch != reservation["consent_epoch"]
                            or m.room_name != reservation["room_name"] or m.bucket != bucket
                            or m.key != reservation["output_prefix"] + ".mp4" for m in manifests)):
                    raise StoreError("Recording manifest does not match the reserved artifact")
            # Private, durable result JSON is separate from signed reservation identity
            # and from session JSON returned to participants. Keep it even across a crash.
            receipt = {"recording_manifests": [m.model_dump(mode="json") for m in manifests],
                       "egress_ids": list(egress_ids), "failed_ids": list(failed_ids)}
            c.execute("UPDATE beep_jobs SET result=COALESCE(result,'{}'::jsonb) || %s WHERE id=%s",
                      (Jsonb(receipt), lease[0]))
            c.execute("UPDATE beep_recording_reservations SET settled=true,egress_ids=%s WHERE id=%s",
                      (Jsonb(sorted(set(egress_ids))), reservation["id"]))
            if s.get("recording_reservation") == reservation:
                if s.get("egress_id") and s["egress_id"] not in egress_ids:
                    raise StoreError("Current egress was not confirmed stopped")
                s.update(recording_status="failed" if failed_ids else "stopped",
                         recording_cleanup_pending=False,
                         recording_artifact_status="unavailable" if failed_ids else "metadata_verified" if manifests else "unverified",
                         recording_cleanup_complete=True)
                self._write(c, s)
            return s

    def _expired(self, c, row):
        s = row["data"]
        if s["started_at"] and s["status"] in {"introduction", "active", "paused"}:
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(s["started_at"])).total_seconds()
            if elapsed >= s["max_seconds"]:
                self._fence(c, row)
                s["status"] = "finalising"
                self._enqueue(c, s["tenant_id"], s["id"], "report", {"after_seq": s["event_seq"]})
                self._write(c, s)
        return s

    def get_session(self, tenant_id, session_id):
        with self._connect() as c:
            return self._expired(c, self._row(c, tenant_id, session_id, lock=True))

    def load_snapshot(self, tenant_id, session_id):
        with self._connect() as c:
            return self._row(c, tenant_id, session_id)["snapshot"]

    @staticmethod
    def _consented(s):
        return all(s[key].get("ai") is True and s[key].get("recording") is True
                   for key in ("client_consent", "facilitator_consent"))

    def set_consent(self, tenant_id, session_id, role: str, *, ai: bool, recording: bool,
                    principal=None, policy=None, notice_version=None, policy_id=None):
        if role not in {"client", "facilitator"} or type(ai) is not bool or type(recording) is not bool:
            raise StoreError("Participant consent required", 403)
        self.get_session(tenant_id, session_id)
        with self._connect() as c:
            row = self._row(c, tenant_id, session_id, lock=True)
            s = row["data"]
            if s["status"] not in {"awaiting_consent", "introduction", "active", "paused"}:
                raise StoreError("Session no longer accepts consent")
            if s.get("deletion_pending"):
                raise StoreError("Session deletion is pending")
            receipt_context = self._consent_receipt_context(c, s, role, principal, policy,
                                                            notice_version, policy_id, ai, recording)
            epoch_before = s["consent_epoch"]
            s[role + "_consent"] = {"ai": ai, "recording": recording}
            if self._consented(s) and s["status"] == "awaiting_consent":
                s["started_at"] = utcnow()
                s["status"] = "introduction"
                s["recording_status"] = "queued"
                self._enqueue(c, tenant_id, session_id, "recording_start", {"consent_epoch": s["consent_epoch"]})
            elif not self._consented(s) and s["status"] in {"introduction", "active"}:
                s["resume_status"] = s["status"]
                s["status"] = "paused"
                self._fence(c, row)
            self._append_consent_receipt(c, s, role, epoch_before, receipt_context)
            return self._write(c, s)

    @staticmethod
    def _consent_receipt_context(c, s, role, principal, policy, notice_version, policy_id, ai, recording):
        if principal is None:
            if any(v is not None for v in (policy, notice_version, policy_id)):
                raise StoreError("Authenticated consent context required", 403)
            return dict(principal_kind="internal_test", token_digest=None, policy=None,
                        notice_binding="internal_test_no_notice")
        import re
        if (not isinstance(principal, dict)
            or set(principal) != {"tenant_id", "session_id", "role", "token_digest"}
            or (principal["tenant_id"], principal["session_id"], principal["role"]) != (s["tenant_id"], s["id"], role)
            or not isinstance(principal["token_digest"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", principal["token_digest"])):
            raise StoreError("Authenticated consent scope required", 403)
        # Recheck under the session lock and retain the token row against logout
        # until this transaction commits. Expired auth rows are NOT receipt FKs.
        if c.execute("""SELECT 1 FROM beep_auth_tokens WHERE digest=%s AND session_id=%s
            AND purpose='cookie' AND expires_at>clock_timestamp() FOR SHARE""",
            (principal["token_digest"], s["id"])).fetchone() is None:
            raise StoreError("Authentication expired or revoked", 401)
        if not policy or notice_version != policy["notice_version"] or policy_id != policy["policy_id"]:
            previous = s[role + "_consent"]
            withdrawing = (previous["ai"] and not ai) or (previous["recording"] and not recording)
            reducing_only = (withdrawing or not (ai or recording)) and (
                not ai or previous["ai"]) and (not recording or previous["recording"])
            if reducing_only:
                prior = c.execute("""SELECT policy FROM beep_consent_receipts
                    WHERE tenant_id=%s AND session_id=%s AND role=%s ORDER BY seq DESC LIMIT 1""",
                    (s["tenant_id"], s["id"], role)).fetchone()
                if prior and prior["policy"]:
                    return dict(principal_kind="authenticated_cookie", token_digest=principal["token_digest"],
                                policy=prior["policy"], notice_binding="withdrawal_prior_notice")
                # No backfill of pre-receipt grants or internal/test notices.
                # Withdrawal remains possible, with the historical gap explicit.
                return dict(principal_kind="authenticated_cookie", token_digest=principal["token_digest"],
                            policy=None, notice_binding="withdrawal_notice_unavailable")
            raise StoreError("Consent notice changed or is missing; reload and review the current notice")
        return dict(principal_kind="authenticated_cookie", token_digest=principal["token_digest"],
                    policy=policy, notice_binding="accepted_current_notice")

    @staticmethod
    def _append_consent_receipt(c, s, role, epoch_before, context):
        # The caller owns the session row lock: decisions, fencing, cleanup work
        # and this append commit together. Receipt sequence is not evidence seq.
        seq = c.execute("SELECT COALESCE(max(seq),0)+1 AS next FROM beep_consent_receipts WHERE session_id=%s",
                        (s["id"],)).fetchone()["next"]
        decisions = s[role + "_consent"]
        c.execute("""INSERT INTO beep_consent_receipts
            (id,tenant_id,session_id,seq,role,ai,recording,epoch_before,epoch_after,
             principal_kind,token_digest,notice_binding,policy) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (str(uuid.uuid4()), s["tenant_id"], s["id"], seq, role, decisions["ai"],
             decisions["recording"], epoch_before, s["consent_epoch"], context["principal_kind"],
             context["token_digest"], context["notice_binding"], Jsonb(context["policy"]) if context["policy"] else None))

    def read_consent_policy(self, tenant_id, session_id, settings):
        from .consent import consent_policy
        with self._connect() as c:
            self._row(c, tenant_id, session_id)  # Deliberately no expiry/state mutation.
        return consent_policy(settings)

    def list_consent_receipts(self, tenant_id, session_id, *, role=None, after_seq=0):
        """Read-only, session-scoped audit history; paginate until an empty page."""
        if role not in {None, "client", "facilitator"} or type(after_seq) is not int or after_seq < 0:
            raise StoreError("Invalid consent receipt cursor or role", 422)
        with self._connect() as c:
            self._row(c, tenant_id, session_id)
            rows = c.execute("""SELECT * FROM beep_consent_receipts
                WHERE tenant_id=%s AND session_id=%s AND seq>%s""" +
                (" AND role=%s" if role else "") + " ORDER BY seq LIMIT 100",
                (tenant_id, session_id, after_seq, role) if role else
                (tenant_id, session_id, after_seq)).fetchall()
            return [{**r, "occurred_at": r["occurred_at"].astimezone(timezone.utc).isoformat()} for r in rows]

    def control(self, tenant_id, session_id, role: str, action: str):
        if role not in {"client", "facilitator"}:
            raise StoreError("Participant control required", 403)
        if action not in {"handover", "pause", "resume", "finish", "takeover"}:
            raise StoreError("Unknown control", 422)
        if action == "takeover" and role != "facilitator":
            raise StoreError("Facilitator required", 403)
        self.get_session(tenant_id, session_id)
        with self._connect() as c:
            row = self._row(c, tenant_id, session_id, lock=True)
            s = row["data"]
            state = s["status"]
            if state not in {"introduction", "active", "paused"}:
                raise StoreError("Control is unavailable in this session state")
            if action == "handover":
                if state != "introduction" or not self._consented(s):
                    raise StoreError("Both parties must consent before handover")
                if (datetime.now(timezone.utc)-datetime.fromisoformat(s["started_at"])).total_seconds() < s["intro_seconds"]:
                    raise StoreError("Human introduction is not complete")
                s["status"] = "active"
            elif action in {"pause", "takeover"}:
                if state != "paused":
                    s["resume_status"] = state
                    self._fence(c, row)
                    s["status"] = "paused"
            elif action == "resume":
                if state != "paused" or not self._consented(s):
                    raise StoreError("Cannot resume without both parties' consent")
                s["status"] = s.get("resume_status", "active")
                s["recording_status"] = "queued"
                self._enqueue(c, tenant_id, session_id, "recording_start", {"consent_epoch": s["consent_epoch"]})
            else:
                self._fence(c, row)
                s["status"] = "finalising"
                self._enqueue(c, tenant_id, session_id, "report", {"after_seq": s["event_seq"]})
            # Durable obligation only: provider disconnection is verified outside
            # this transaction. Never relabel it as self-hosted token revocation.
            s["facilitator_media"] = {
                "status": "introduction_allowed" if s["status"] == "introduction"
                          else "reconciliation_required",
                "token_revocation": False,
            }
            return self._write(c, s)

    def append_event(self, tenant_id, session_id, event: dict):
        from .domain import Evidence
        event = Evidence.model_validate(event).model_dump(mode="json")
        self.get_session(tenant_id, session_id)
        with self._connect() as c:
            row = self._row(c, tenant_id, session_id, lock=True)
            s = row["data"]
            existing = c.execute("SELECT data FROM beep_events WHERE session_id=%s AND id=%s",
                                 (session_id, event["id"])).fetchone()
            if existing:
                old = existing["data"]
                if {**event, "seq": old["seq"]} != old:
                    raise StoreError("Event identifier conflicts with existing evidence")
                return old
            if event["consent_epoch"] != s["consent_epoch"]:
                raise StoreError("Stale consent epoch")
            correction = event["kind"] == "correction" and event["actor"] == "client"
            if s["status"] != "active" and not (correction and s["status"] in {"finalising", "completed", "partial"}):
                raise StoreError("Evidence capture is unavailable in this session state")
            if not correction and not self._consented(s):
                raise StoreError("Consent is required")
            if event["at_ms"] > s["max_seconds"] * 1000:
                raise StoreError("Evidence offset exceeds session cap", 422)
            if s["event_seq"] >= 4096:
                raise StoreError("Evidence capacity reached")
            s["event_seq"] += 1
            event["seq"] = s["event_seq"]
            c.execute("INSERT INTO beep_events VALUES (%s,%s,%s,%s,%s)",
                      (tenant_id, session_id, event["id"], event["seq"], Jsonb(event)))
            if event["actor"] in {"client", "observer"}:
                self._enqueue(c, tenant_id, session_id, "discovery", {"event": event})
            if correction:
                c.execute("UPDATE beep_reports SET valid=false WHERE session_id=%s", (session_id,))
                c.execute("UPDATE beep_sessions SET report=NULL WHERE id=%s", (session_id,))
                if s["status"] in {"completed", "partial", "finalising"}:
                    s["status"] = "finalising"
                    self._enqueue(c, tenant_id, session_id, "report", {"after_seq": event["seq"]})
            self._write(c, s)
            return event

    def list_events(self, tenant_id, session_id, after_seq: int = 0):
        if type(after_seq) is not int or after_seq < 0:
            raise StoreError("Invalid event cursor", 422)
        with self._connect() as c:
            self._row(c, tenant_id, session_id)
            return [r["data"] for r in c.execute("""SELECT data FROM beep_events
                WHERE tenant_id=%s AND session_id=%s AND seq>%s ORDER BY seq LIMIT 500""",
                (tenant_id, session_id, after_seq)).fetchall()]

    def commit_snapshot(self, job_id, lease_token, tenant_id, session_id, snapshot,
                        expected_revision, consent_epoch) -> bool:
        with self.job_lease(job_id, lease_token):
            return self.save_snapshot(tenant_id, session_id, snapshot,
                expected_revision=expected_revision, consent_epoch=consent_epoch)

    def commit_report(self, job_id, lease_token, tenant_id, session_id, bundle) -> None:
        if not isinstance(bundle, dict) or set(bundle) != {"report", "expected_revision", "consent_epoch"}:
            raise StoreError("Invalid report commit bundle", 422)
        epoch = _PUBLICATION_EPOCH.set(bundle["consent_epoch"])
        try:
            with self.job_lease(job_id, lease_token):
                if not self.put_report(tenant_id, session_id, bundle["report"],
                                       expected_revision=bundle["expected_revision"]):
                    raise StoreError("Report lease, revision or consent fence lost")
        finally:
            _PUBLICATION_EPOCH.reset(epoch)

    def save_snapshot(self, tenant_id, session_id, snapshot: dict, *, expected_revision: int, consent_epoch: int):
        from .domain import Snapshot
        snapshot = Snapshot.model_validate(snapshot).model_dump(mode="json")
        self.get_session(tenant_id, session_id)
        with self._connect() as c:
            row = self._row(c, tenant_id, session_id, lock=True)
            s, old = row["data"], row["snapshot"]
            if not self._lease_valid(c, session_id):
                return False
            if (s["revision"] != expected_revision or s["consent_epoch"] != consent_epoch
                or snapshot["revision"] != expected_revision + 1
                or snapshot["consent_epoch"] != consent_epoch or s["status"] not in {"active", "finalising"}
                or not old["last_event_seq"] <= snapshot["last_event_seq"] <= s["event_seq"]):
                return False
            original = {e["id"]: e for e in old["evidence"]}
            proposed = {e["id"]: e for e in snapshot["evidence"]}
            if any(proposed.get(k) != v for k, v in original.items()):
                raise StoreError("Snapshot cannot erase or rewrite evidence")
            persisted = {r["id"]: r["data"] for r in c.execute(
                "SELECT id,data FROM beep_events WHERE session_id=%s AND seq<=%s",
                (session_id, snapshot["last_event_seq"])).fetchall()}
            if persisted != proposed:
                raise StoreError("Snapshot must preserve every persisted event through its cursor")
            if snapshot["last_event_seq"] < s["event_seq"] or s["status"] != "active":
                snapshot["probe"] = None
            s["revision"] = snapshot["revision"]
            c.execute("UPDATE beep_sessions SET snapshot=%s,report=NULL WHERE id=%s", (Jsonb(snapshot), session_id))
            self._write(c, s)
            return True

    @staticmethod
    def _enqueue(c, tenant_id, session_id, kind, payload):
        if kind not in {"discovery", "report", "recording_start", "recording_stop"}:
            raise StoreError("Invalid job kind", 422)
        if not isinstance(payload, dict):
            raise StoreError("Invalid job payload", 422)
        jid = str(uuid.uuid4())
        return c.execute("""INSERT INTO beep_jobs(id,tenant_id,session_id,kind,payload)
            VALUES (%s,%s,%s,%s,%s) RETURNING *""",
            (jid, tenant_id, session_id, kind, Jsonb(payload))).fetchone()

    def enqueue_job(self, tenant_id, session_id, kind: str, payload: dict):
        with self._connect() as c:
            self._row(c, tenant_id, session_id, lock=True)
            return self._enqueue(c, tenant_id, session_id, kind, payload)

    def claim_job(self, worker_id: str, lease_seconds: int = 120):
        if not worker_id or not 1 <= lease_seconds <= 600:
            raise StoreError("Invalid lease", 422)
        # Cleanup is an obligation retained before activation, not an instruction
        # to terminate the healthy current recording immediately after starting it.
        eligible = """NOT COALESCE((j.kind='recording_stop'
            AND j.payload->'reservation'=s.data->'recording_reservation'
            AND s.data->>'recording_status'='recording'
            AND s.data->>'status' IN ('active','introduction')
            AND s.data->'client_consent'->>'ai'='true'
            AND s.data->'client_consent'->>'recording'='true'
            AND s.data->'facilitator_consent'->>'ai'='true'
            AND s.data->'facilitator_consent'->>'recording'='true'
            AND (s.data->>'recording_verified_at')::timestamptz > clock_timestamp()-interval '5 seconds'
            ),false)"""
        with self._connect() as c:
            # Use the same session -> job lock order as publication/control. Recheck
            # expiry after the mutex; a renewal or confirmed commit can win the race.
            expired_sessions = c.execute("""SELECT s.* FROM beep_sessions s WHERE EXISTS (
                SELECT 1 FROM beep_jobs j WHERE j.session_id=s.id AND j.status='running'
                AND j.lease_until<=clock_timestamp())
                ORDER BY s.id FOR UPDATE OF s SKIP LOCKED LIMIT 100""").fetchall()
            for row in expired_sessions:
                expired = c.execute("""UPDATE beep_jobs SET status='failed',error='lease_expired_ambiguous'
                    WHERE session_id=%s AND status='running' AND lease_until<=clock_timestamp()
                    RETURNING *""", (row["id"],)).fetchall()
                for job in expired:
                    self._settle_report_failure(c, row, job, "lease_expired_ambiguous")
            session = c.execute(f"""SELECT s.id,s.tenant_id FROM beep_sessions s
                WHERE EXISTS (SELECT 1 FROM beep_jobs j WHERE j.session_id=s.id
                    AND j.status='queued' AND j.attempts<3 AND {eligible})
                AND NOT EXISTS (SELECT 1 FROM beep_jobs j WHERE j.session_id=s.id AND j.status='running')
                ORDER BY s.id FOR UPDATE OF s SKIP LOCKED LIMIT 1""").fetchone()
            if not session:
                return None
            # READ COMMITTED can select the session using an older statement snapshot
            # even after the previous claimant released its row lock. Recheck *after*
            # acquiring the session mutex with a fresh statement snapshot.
            if c.execute("SELECT 1 FROM beep_jobs WHERE session_id=%s AND status='running'", (session["id"],)).fetchone():
                return None
            job = c.execute(f"""SELECT j.id FROM beep_jobs j JOIN beep_sessions s ON s.id=j.session_id
                WHERE j.session_id=%s AND j.status='queued' AND j.attempts<3 AND {eligible}
                ORDER BY j.created_at,j.id FOR UPDATE OF j SKIP LOCKED LIMIT 1""", (session["id"],)).fetchone()
            if not job:
                return None
            return c.execute("""UPDATE beep_jobs SET status='running',attempts=attempts+1,
                worker_id=%s,lease_token=%s,lease_until=clock_timestamp()+(%s*interval '1 second')
                WHERE id=%s RETURNING *""", (worker_id, str(uuid.uuid4()), lease_seconds, job["id"])).fetchone()

    def finish_job(self, job_id, lease_token, result: dict):
        with self._connect() as c:
            return c.execute("""UPDATE beep_jobs SET status='done',result=%s WHERE id=%s
                AND lease_token=%s AND status='running' AND lease_until>clock_timestamp()""",
                (Jsonb(result), job_id, lease_token)).rowcount == 1

    def fail_job(self, job_id, lease_token, error: str, *, retry: bool = False):
        with self._connect() as c:
            scope = c.execute("SELECT tenant_id,session_id FROM beep_jobs WHERE id=%s", (job_id,)).fetchone()
            if not scope:
                return False
            row = self._row(c, scope["tenant_id"], scope["session_id"], lock=True)
            job = c.execute("""UPDATE beep_jobs SET status=CASE WHEN %s AND attempts<3
                THEN 'queued' ELSE 'failed' END,error=%s WHERE id=%s
                AND lease_token=%s AND status='running' AND lease_until>clock_timestamp() RETURNING *""",
                (retry, str(error)[:500], job_id, lease_token)).fetchone()
            if not job:
                return False
            if job["status"] == "failed":
                self._settle_report_failure(c, row, job, error)
            return True

    def _settle_report_failure(self, c, row, job, reason):
        """Session-locked terminalisation; never erase operation or cleanup receipts."""
        if job["kind"] != "report":
            return
        s, report = row["data"], row["report"]
        if (report is not None and s["status"] in {"completed", "partial"}
            and report.get("revision") == s["revision"]
            and c.execute("""SELECT 1 FROM beep_reports WHERE session_id=%s AND revision=%s
                AND valid AND data=%s""", (s["id"], s["revision"], Jsonb(report))).fetchone()):
            # Publication and job acknowledgement are separate commits. A lost
            # acknowledgement does not make a confirmed delivered report disappear.
            result = {**(job["result"] or {}), "status": "reported", "revision": s["revision"],
                      "publication_confirmed": True}
            c.execute("""UPDATE beep_jobs SET status='done',result=%s,
                error='post_publication_reconciliation_required' WHERE id=%s""", (Jsonb(result), job["id"]))
            return
        if s["status"] != "finalising" or report is not None or s.get("deletion_pending"):
            return
        # A correction or continuation owns its newer report. The old failure must
        # not terminalise that work; its own eventual failure will settle the review.
        if c.execute("""SELECT 1 FROM beep_jobs WHERE session_id=%s AND id<>%s AND kind='report'
            AND (status='running' OR (status='queued' AND attempts<3))""", (s["id"], job["id"])).fetchone():
            return
        safe_reason = reason if reason in {"lease_expired_ambiguous", "cancelled_ambiguous", "report_not_publishable"} else "report_generation_failed"
        s.update(status="failed", stop_reason="report_reconciliation_required", report_failure={
            "job_id": job["id"], "state": "reconciliation_required", "reason": safe_reason,
            "revision": s["revision"], "consent_epoch": s["consent_epoch"], "event_seq": s["event_seq"],
            "next_action": "inspect_retained_operation_or_close_without_report", "recorded_at": utcnow(),
        })
        self._write(c, s)

    def triage_report_failure(self, tenant_id, session_id, *, apply=False):
        """Recognise pre-fix stranded rows; exact-session, no inference or deletion."""
        with self._connect() as c:
            row = self._row(c, tenant_id, session_id, lock=True)
            s = row["data"]
            if s["status"] != "finalising" or row["report"] is not None or s.get("deletion_pending"):
                raise StoreError("No stranded report failure to triage")
            if c.execute("""SELECT 1 FROM beep_jobs WHERE session_id=%s AND
                (status='running' OR (kind='report' AND status='queued' AND attempts<3))""", (session_id,)).fetchone():
                raise StoreError("Report triage has newer or leased work")
            job = c.execute("""SELECT * FROM beep_jobs WHERE tenant_id=%s AND session_id=%s
                AND kind='report' AND status='failed' ORDER BY created_at DESC,id DESC LIMIT 1 FOR UPDATE""",
                (tenant_id, session_id)).fetchone()
            if not job:
                raise StoreError("No failed report receipt to triage")
            if apply:
                self._settle_report_failure(c, row, job, job["error"])
            return s

    def publish_retained_report(self, tenant_id, session_id, failure, report):
        """Exact-session admin recovery: publish a confirmed checkpoint, never infer.

        The caller reads the scoped operation checkpoint. This fresh CAS/lease and
        commit_report's existing fences reject a correction, consent or owner race.
        """
        with self._connect() as c:
            row = self._row(c, tenant_id, session_id, lock=True)
            s = row["data"]
            if (not failure or s.get("report_failure") != failure or s["status"] != "failed"
                or failure.get("state") != "reconciliation_required" or row["report"] is not None
                or s.get("deletion_pending") or not self._consented(s)
                or any(s[key] != failure.get(key) for key in ("revision", "consent_epoch", "event_seq"))):
                raise StoreError("Report reconciliation state changed")
            if c.execute("""SELECT 1 FROM beep_jobs WHERE session_id=%s AND
                (status='running' OR (status='queued' AND kind IN ('discovery','report')))""", (session_id,)).fetchone():
                raise StoreError("Report reconciliation has newer or leased work")
            source = c.execute("""SELECT 1 FROM beep_jobs WHERE id=%s AND session_id=%s
                AND tenant_id=%s AND kind='report' AND status='failed'""",
                (failure["job_id"], session_id, tenant_id)).fetchone()
            if not source:
                raise StoreError("Report failure receipt changed")
            job = self._enqueue(c, tenant_id, session_id, "report", {
                "reconciliation_only": True, "source_job_id": failure["job_id"]})
            job = c.execute("""UPDATE beep_jobs SET status='running',attempts=1,
                worker_id='report-reconciliation',lease_token=%s,
                lease_until=clock_timestamp()+interval '120 seconds' WHERE id=%s RETURNING *""",
                (str(uuid.uuid4()), job["id"])).fetchone()
            s["status"] = "finalising"
            self._write(c, s)
        try:
            self.commit_report(job["id"], job["lease_token"], tenant_id, session_id, {
                "report": report, "expected_revision": failure["revision"],
                "consent_epoch": failure["consent_epoch"],
            })
            with self.job_lease(job["id"], job["lease_token"]), self._connect() as c:
                row = self._row(c, tenant_id, session_id, lock=True)
                s = row["data"]
                if (not self._lease_valid(c, session_id) or row["report"] is None
                    or s["status"] not in {"completed", "partial"}
                    or s.get("report_failure") != failure):
                    raise StoreError("Report reconciliation publication changed")
                result = {"status": "reported", "revision": s["revision"], "reused_retained_result": True}
                c.execute("UPDATE beep_jobs SET status='done',result=%s WHERE id=%s", (Jsonb(result), job["id"]))
                s["report_failure"] = {**failure, "state": "resolved_from_retained_result", "resolved_at": utcnow()}
                s["stop_reason"] = "report_reconciled_from_retained_result"
                self._write(c, s)
                return result
        except BaseException:
            self.fail_job(job["id"], job["lease_token"], "report_reconciliation_failed")
            raise

    def renew_job(self, job_id, lease_token, lease_seconds: int = 120):
        if not 1 <= lease_seconds <= 600:
            raise StoreError("Invalid lease", 422)
        with self._connect() as c:
            return c.execute("""UPDATE beep_jobs SET lease_until=clock_timestamp()+(%s*interval '1 second')
                WHERE id=%s AND lease_token=%s AND status='running' AND lease_until>clock_timestamp()""",
                (lease_seconds, job_id, lease_token)).rowcount == 1

    def list_active_sessions(self):
        with self._connect() as c:
            rows = c.execute("""SELECT * FROM beep_sessions WHERE data->>'status' IN
                ('introduction','active','paused','finalising') ORDER BY id FOR UPDATE SKIP LOCKED""").fetchall()
            return [self._expired(c, row) for row in rows]

    def put_report(self, tenant_id, session_id, report: dict, *, expected_revision: int):
        from .domain import Report, Snapshot
        from .reports import build_report, recording_provenance
        # Discard untrusted recording claims before model validation can remove gaps.
        report = Report.model_validate({**report, "recording": {}}).model_dump(mode="json")
        with self._connect() as c:
            row = self._row(c, tenant_id, session_id, lock=True)
            s, snap = row["data"], row["snapshot"]
            if _PUBLICATION_EPOCH.get() is not None and _PUBLICATION_EPOCH.get() != s["consent_epoch"]:
                return False
            if not self._lease_valid(c, session_id):
                return False
            latest = c.execute("""SELECT COALESCE(max(seq),0) AS seq FROM beep_events
                WHERE session_id=%s AND data->>'actor' IN ('client','observer')""", (session_id,)).fetchone()["seq"]
            if (s["revision"] != expected_revision or report["revision"] != expected_revision
                or report["session_id"] != session_id or s["status"] != "finalising"
                or snap["last_event_seq"] < latest):
                return False
            if report["evidence"] != snap["evidence"]:
                raise StoreError("Report evidence must match the current snapshot")
            # Preserve explicit interruption, but independently re-evaluate actual
            # stored coverage/corrections. Playable media cannot repair discovery.
            forced_partial = report["content_status"] != "complete"
            lease = _JOB_LEASE.get()
            if lease:
                owner = c.execute("SELECT payload FROM beep_jobs WHERE id=%s", (lease[0],)).fetchone()
                forced_partial = forced_partial or bool(owner["payload"].get("partial"))
            evaluated = build_report(Snapshot.model_validate(snap), session_id, partial=forced_partial)
            report["content_status"] = evaluated.content_status
            report["unknowns"] = list(dict.fromkeys([*report["unknowns"], *evaluated.unknowns]))
            # Server receipts, not provider-generated claims, determine recording provenance.
            reservations = c.execute("""SELECT r.data,r.settled,r.egress_ids,
                (SELECT j.result->'recording_storage_binding'->>'bucket' FROM beep_jobs j
                 WHERE j.session_id=r.session_id AND j.kind='recording_start'
                 AND j.result->'recording_storage_binding'->>'reservation_id'=r.id LIMIT 1) AS bucket
                FROM beep_recording_reservations r WHERE r.session_id=%s
                ORDER BY r.consent_epoch,r.id""", (session_id,)).fetchall()
            receipts = c.execute("""SELECT result FROM beep_jobs WHERE session_id=%s
                AND kind='recording_stop' AND result ? 'recording_manifests'
                ORDER BY created_at,id""", (session_id,)).fetchall()
            report["recording"] = recording_provenance(s, Report.model_validate(report).evidence,
                reservations, [m for r in receipts for m in (
                    r["result"]["recording_manifests"] if isinstance(r["result"]["recording_manifests"], list)
                    else [{}])]
            ).model_dump(mode="json")
            report = Report.model_validate(report).model_dump(mode="json")
            c.execute("""INSERT INTO beep_reports(session_id,revision,data) VALUES (%s,%s,%s)
                ON CONFLICT(session_id,revision) DO NOTHING""", (session_id, expected_revision, Jsonb(report)))
            c.execute("UPDATE beep_sessions SET report=%s WHERE id=%s", (Jsonb(report), session_id))
            s["status"] = "completed" if report["status"] == "complete" else "partial"
            self._write(c, s)
            return True

    def get_report(self, tenant_id, session_id):
        with self._connect() as c:
            return self._row(c, tenant_id, session_id)["report"]

    def add_usage(self, tenant_id, session_id, usage: dict):
        import math
        if not isinstance(usage, dict):
            raise StoreError("Invalid usage", 422)
        cost = usage.get("cost_aud")
        if cost is not None and (isinstance(cost, bool) or not isinstance(cost, (int,float)) or not math.isfinite(cost) or cost < 0):
            raise StoreError("Invalid usage cost", 422)
        with self._connect() as c:
            row = self._row(c, tenant_id, session_id, lock=True)
            s = row["data"]
            c.execute("INSERT INTO beep_usage(session_id,data) VALUES (%s,%s)", (session_id, Jsonb(usage)))
            # Retain the provider receipt unchanged. usage_aud is a priced subtotal,
            # not a claim that token-only/subscription inference costs zero.
            if cost is None:
                s["usage_cost_unpriced"] = True
            else:
                s["usage_aud"] = s.get("usage_aud", 0) + cost
            if s.get("usage_aud", 0) >= s["budget_aud"] and s["status"] in {"introduction", "active", "paused"}:
                self._fence(c, row)
                s["status"] = "finalising"
                s["stop_reason"] = "budget_cap"
                self._enqueue(c, tenant_id, session_id, "report", {"partial": True})
            self._write(c, s)

    def admit_inference(self, job_id, lease_token, tenant_id, session_id, consent_epoch) -> bool:
        """Linearize provider admission against the session control mutex."""
        with self._connect() as c:
            s = self._row(c, tenant_id, session_id, lock=True)["data"]
            if (s["consent_epoch"] != consent_epoch or not self._consented(s)
                or s["status"] not in {"active", "finalising"}):
                return False
            if s['status'] == 'active':
                try:
                    age = (datetime.now(timezone.utc) - datetime.fromisoformat(s['recording_verified_at'])).total_seconds()
                except (KeyError, ValueError, TypeError):
                    return False
                if not s.get('egress_id') or s['recording_status'] != 'recording' or not 0 <= age <= 5:
                    return False
            marker = {"job_id": job_id, "tenant_id": tenant_id, "session_id": session_id,
                      "consent_epoch": consent_epoch, "request_marker": uuid.uuid4().hex,
                      "admitted_at": utcnow(), "revoked": False}
            return c.execute("""UPDATE beep_jobs SET payload=jsonb_set(payload,'{inference_admission}',%s)
                WHERE id=%s AND lease_token=%s AND tenant_id=%s AND session_id=%s
                AND kind IN ('discovery','report') AND status='running'
                AND lease_until>clock_timestamp()""",
                (Jsonb(marker), job_id, lease_token, tenant_id, session_id)).rowcount == 1

    def observe_recording(self, tenant_id, session_id, status, egress_id, consent_epoch) -> bool:
        """Independent exact-ID/epoch health CAS, permitted during graph leases."""
        if status not in {"recording", "failed"}:
            raise StoreError("Invalid recording observation", 422)
        with self._connect() as c:
            row = self._row(c, tenant_id, session_id, lock=True)
            s = row["data"]
            if (not egress_id or s.get("egress_id") != egress_id
                or s["consent_epoch"] != consent_epoch
                or s.get("recording_epoch", consent_epoch) != consent_epoch
                or s["recording_status"] != "recording"
                or s["status"] not in {"active", "introduction"}):
                return False
            if status == 'recording':
                s['recording_verified_at'] = utcnow()
            else:
                s.update(recording_status="failed", resume_status=s["status"], status="paused")
                self._fence(c, row)
            self._write(c, s)
            return True

    def set_recording(self, tenant_id, session_id, status: str, egress_id: str | None = None):
        if status not in {"not_started", "queued", "starting", "recording", "stopping", "stopped", "failed", "deleted"}:
            raise StoreError("Invalid recording status", 422)
        with self._connect() as c:
            row = self._row(c, tenant_id, session_id, lock=True)
            s = row["data"]
            if not self._lease_valid(c, session_id):
                raise StoreError("Recording lease lost")
            lease = _JOB_LEASE.get()
            if lease:
                job = c.execute("SELECT kind,payload FROM beep_jobs WHERE id=%s", (lease[0],)).fetchone()
                expected_epoch = job["payload"].get("recording_epoch", job["payload"].get("consent_epoch"))
                current_epoch = s.get("recording_epoch", s["consent_epoch"])
                if job["kind"] not in {"recording_start", "recording_stop"}:
                    raise StoreError("Recording job required")
                if expected_epoch != (s["consent_epoch"] if status == "starting" else current_epoch):
                    raise StoreError("Stale recording consent epoch")
                if job["kind"] == "recording_start" and expected_epoch != s["consent_epoch"]:
                    raise StoreError("Stale recording consent epoch")
                if job["kind"] == "recording_stop" and (
                    job["payload"].get("egress_id") != s.get("egress_id")
                    or egress_id not in {None, s.get("egress_id")}
                    or status not in {"stopping", "stopped", "failed"}
                ):
                    raise StoreError("Recording cleanup does not own the current egress")
                if status != "starting" and s.get("egress_id") and egress_id not in {None, s["egress_id"]}:
                    raise StoreError("Recording egress identity changed")
                if status == "starting":
                    s["recording_epoch"] = expected_epoch
                    s["egress_id"] = None
            if status == "starting":
                if s.get("recording_cleanup_pending"):
                    raise StoreError("Previous recording outcome requires reconciliation")
                epoch = s["consent_epoch"]
                rid = uuid.uuid4().hex
                reservation = {"id": rid, "consent_epoch": epoch, "room_name": s["room_name"],
                    "output_prefix": "beep/" + quote(tenant_id, safe="") + "/" + quote(session_id, safe="") + f"/epoch-{epoch}-{rid}"}
                inserted = c.execute("""INSERT INTO beep_recording_reservations(id,session_id,consent_epoch,data,signing_key)
                    VALUES (%s,%s,%s,%s,%s) ON CONFLICT(session_id,consent_epoch) DO NOTHING""",
                    (rid, session_id, epoch, Jsonb(reservation), uuid.uuid4().hex)).rowcount
                if not inserted:
                    raise StoreError("Recording start was already reserved; do not retry")
                s.update(recording_reservation=reservation, recording_epoch=epoch, recording_cleanup_pending=True)
            if status in {"stopped", "deleted", "not_started", "queued"} and s.get("recording_cleanup_pending"):
                raise StoreError("Recording reservation requires confirmed cleanup")
            if status == "recording" and (not egress_id or s["status"] not in {"active", "introduction"} or not self._consented(s)):
                raise StoreError("Recording cannot be activated in this state")
            s["recording_status"] = status
            if status == 'recording':
                s['recording_verified_at'] = utcnow()
                s.setdefault('recording_epoch', s['consent_epoch'])
            if egress_id is not None:
                s["egress_id"] = egress_id
            if status == "failed" and s["status"] in {"introduction", "active"}:
                s["resume_status"] = s["status"]
                s["status"] = "paused"
                self._fence(c, row)
            return self._write(c, s)

    def delete_session(self, tenant_id, session_id):
        with self._connect() as c:
            row = self._row(c, tenant_id, session_id, lock=True)
            s = row["data"]
            if s["status"] not in {"awaiting_consent", "completed", "partial", "failed"}:
                raise StoreError("Cannot delete a running session")
            if s.get("recording_cleanup_pending") or c.execute(
                "SELECT 1 FROM beep_recording_reservations WHERE session_id=%s AND NOT settled", (session_id,)).fetchone():
                raise StoreError("Recording reconciliation must finish before deletion")
            if s.get("egress_id") and s["recording_status"] != "deleted":
                raise StoreError("Remote recording deletion must be confirmed first")
            if c.execute("SELECT 1 FROM beep_jobs WHERE session_id=%s AND status='running'", (session_id,)).fetchone():
                raise StoreError("Cannot delete a leased session")
            c.execute("DELETE FROM beep_sessions WHERE tenant_id=%s AND id=%s", (tenant_id, session_id))

    def reserve_dispatch(self, tenant_id, session_id, consent_epoch: int):
        # Durable one-attempt claim: no SQL lock spans the external dispatch call.
        # Unknown outcomes are reconciled by listing the room, not blind retries.
        with self._connect() as c:
            s = self._row(c, tenant_id, session_id, lock=True)["data"]
            if s["consent_epoch"] != consent_epoch or s["status"] != "active" or not self._consented(s):
                return False
            return c.execute("""INSERT INTO beep_dispatches(session_id,consent_epoch) VALUES (%s,%s)
                ON CONFLICT DO NOTHING""", (session_id, consent_epoch)).rowcount == 1

    def confirm_dispatch(self, tenant_id, session_id, consent_epoch: int, dispatch_id: str):
        with self._connect() as c:
            self._row(c, tenant_id, session_id, lock=True)
            return c.execute("UPDATE beep_dispatches SET dispatch_id=%s WHERE session_id=%s AND consent_epoch=%s",
                             (dispatch_id, session_id, consent_epoch)).rowcount == 1

    def register_auth_token(self, digest: str, purpose: str, session_id: str | None, ttl_seconds: int):
        if purpose not in {"cookie", "invite"} or not 1 <= ttl_seconds <= 604800:
            raise StoreError("Invalid authentication registration", 422)
        with self._connect() as c:
            if session_id is not None:
                # Serialize participant issuance with maintenance's deletion fence.
                row = c.execute("SELECT data FROM beep_sessions WHERE id=%s FOR UPDATE", (session_id,)).fetchone()
                if row is None:
                    raise StoreError("Session not found", 404)
                if row["data"].get("deletion_pending"):
                    raise StoreError("Session deletion is pending")
            c.execute("DELETE FROM beep_auth_tokens WHERE expires_at<=clock_timestamp()")
            return c.execute("""INSERT INTO beep_auth_tokens(digest,purpose,session_id,expires_at)
                VALUES (%s,%s,%s,clock_timestamp()+(%s*interval '1 second'))
                ON CONFLICT(digest) DO NOTHING""", (digest, purpose, session_id, ttl_seconds)).rowcount == 1

    def cookie_valid(self, digest: str):
        with self._connect() as c:
            return c.execute("""SELECT 1 FROM beep_auth_tokens AS token
                WHERE digest=%s AND purpose='cookie' AND expires_at>clock_timestamp()
                AND (session_id IS NULL OR EXISTS (
                    SELECT 1 FROM beep_sessions AS session WHERE session.id=token.session_id
                    AND NOT COALESCE((session.data->>'deletion_pending')::boolean, false)))""", (digest,)).fetchone() is not None

    def revoke_cookie(self, digest: str):
        with self._connect() as c:
            c.execute("DELETE FROM beep_auth_tokens WHERE digest=%s AND purpose='cookie'", (digest,))

    def allow_login(self, key: str, *, limit: int = 5, window_seconds: int = 60):
        # Count attempts, not just failures, atomically across API processes.
        with self._connect() as c:
            row = c.execute("""INSERT INTO beep_login_bounds(key,window_start,count)
                VALUES (%s,clock_timestamp(),1) ON CONFLICT(key) DO UPDATE SET
                count=CASE WHEN beep_login_bounds.window_start<clock_timestamp()-(%s*interval '1 second')
                    THEN 1 ELSE beep_login_bounds.count+1 END,
                window_start=CASE WHEN beep_login_bounds.window_start<clock_timestamp()-(%s*interval '1 second')
                    THEN clock_timestamp() ELSE beep_login_bounds.window_start END RETURNING count""",
                (key, window_seconds, window_seconds)).fetchone()
            c.execute("DELETE FROM beep_login_bounds WHERE window_start<clock_timestamp()-interval '1 day'")
            return row["count"] <= limit

    def list_sessions(self, tenant_id):
        with self._connect() as c:
            return [r["data"] for r in c.execute(
                "SELECT data FROM beep_sessions WHERE tenant_id=%s ORDER BY data->>'created_at' DESC LIMIT 500",
                (tenant_id,)).fetchall()]
