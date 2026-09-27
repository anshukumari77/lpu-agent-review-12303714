"""PR-04: real isolated PostgreSQL; synthetic identities, no provider calls."""
from datetime import datetime, timezone
import hashlib
import json
import uuid

import psycopg
import pytest

from beep_agent.store import Store, StoreError
from test_store import store as store_fixture, create
from test_api import settings as settings_fixture, client as client_fixture, new_session, exchange

store = store_fixture
settings = settings_fixture
client = client_fixture


def accept(policy, *, ai=True, recording=True):
    return dict(ai=ai, recording=recording, notice_version=policy["notice_version"],
                policy_id=policy["policy_id"])


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def test_internal_grant_withdrawal_regrant_is_durable_separate_history(store):
    sid = create(store)["id"]
    before = datetime.now(timezone.utc)
    initial = store.get_session("t1", sid)
    assert initial["client_consent"] == {"ai": False, "recording": False}
    for role, ai, recording in (("client", True, True), ("facilitator", True, True),
                                ("client", False, True), ("client", True, True)):
        store.set_consent("t1", sid, role, ai=ai, recording=recording)
    with store._connect() as c:
        assert c.execute("SELECT to_regclass('beep_consent_receipts') AS name").fetchone()["name"], (
            "consent changes must append durable receipts, not only latest booleans")
    receipts = Store(store.database_url).list_consent_receipts("t1", sid)
    assert len(receipts) == 4
    assert len({uuid.UUID(r["id"]) for r in receipts}) == 4
    assert [r["seq"] for r in receipts] == [1, 2, 3, 4]
    assert [(r["role"], r["ai"], r["recording"]) for r in receipts] == [
        ("client", True, True), ("facilitator", True, True),
        ("client", False, True), ("client", True, True)]
    assert [(r["epoch_before"], r["epoch_after"]) for r in receipts] == [(0, 0), (0, 0), (0, 1), (1, 1)]
    for receipt in receipts:
        assert receipt["tenant_id"] == "t1" and receipt["session_id"] == sid
        assert before <= datetime.fromisoformat(receipt["occurred_at"]) <= datetime.now(timezone.utc)
        assert receipt["principal_kind"] == "internal_test"
        assert receipt["token_digest"] is None and receipt["policy"] is None
        assert receipt["notice_binding"] == "internal_test_no_notice"
    assert store.get_session("t1", sid)["status"] == "paused"  # regrant does not resume
    assert store.get_session("t1", sid)["consent_epoch"] == 1
    assert store.list_events("t1", sid) == []
    assert store.load_snapshot("t1", sid)["evidence"] == []


def test_authenticated_grant_binds_exact_named_notice_not_raw_credentials(client, store, settings):
    settings.inference_provider = "codex"
    settings.codex_model = "gpt-5.5"
    made = new_session(client)
    sid = made["session"]["id"]
    assert exchange(client, made["invitations"]["client"]).status_code == 200
    response = client.get("/api/consent-policy")
    assert response.status_code == 200, "serve the actual versioned notice to this authenticated participant"
    policy = response.json()["policy"]
    assert policy["deployment_policy_status"] == "unapproved"
    assert policy["notice_hash"] == digest(policy["notice"])
    assert policy["provider_configuration_hash"] == digest(policy["provider_configuration"])
    assert policy["provider_configuration"]["voice"] == {"provider": "OpenAI", "route": "Realtime API", "model": "gpt-realtime"}
    assert policy["provider_configuration"]["reasoning_vision_report"] == {"provider": "OpenAI", "route": "Codex OAuth", "model": "gpt-5.5"}
    assert "OpenAI" in policy["notice"]["providers"] and "LiveKit" in policy["notice"]["providers"]
    assert client.post(f"/api/sessions/{sid}/consent", json={"ai": True, "recording": True}).status_code == 409
    assert client.post(f"/api/sessions/{sid}/consent", json=accept(policy)).status_code == 200
    r = client.get(f"/api/sessions/{sid}/consent-receipts")
    assert r.status_code == 200
    receipt, = r.json()["receipts"]
    cookie = client.cookies.get("beep_session")
    assert receipt["token_digest"] == hashlib.sha256(cookie.encode()).hexdigest()
    assert receipt["principal_kind"] == "authenticated_cookie"
    assert receipt["notice_binding"] == "accepted_current_notice"
    assert receipt["policy"] == policy
    for secret in (cookie, made["invitations"]["client"].split("#invite=")[1],
                   "synthetic-test-signing-secret-0123456789", "synthetic-operator-test-token-0123456789"):
        assert secret not in r.text + response.text
    assert store.list_events("local", sid) == []


def test_withdrawal_uses_prior_notice_and_never_requires_accepting_changed_provider(client, store, settings):
    made = new_session(client)
    sid = made["session"]["id"]
    assert exchange(client, made["invitations"]["client"]).status_code == 200
    policy = client.get("/api/consent-policy").json()["policy"]
    assert client.post(f"/api/sessions/{sid}/consent", json=accept(policy)).status_code == 200
    store.set_consent("local", sid, "facilitator", ai=True, recording=True)
    saved = store.list_consent_receipts("local", sid)[0]
    settings.inference_provider = "codex"  # synthetic configuration change; no provider IO
    response = client.post(f"/api/sessions/{sid}/consent", json={"ai": False, "recording": True})
    assert response.status_code == 200, "withdrawal must not be held hostage to new notice acceptance"
    s = response.json()["session"]
    assert s["status"] == "paused" and s["consent_epoch"] == 1
    assert s["client_consent"] == {"ai": False, "recording": True}
    receipts = store.list_consent_receipts("local", sid)
    assert receipts[0] == saved
    assert receipts[-1]["policy"] == policy
    assert receipts[-1]["notice_binding"] == "withdrawal_prior_notice"
    assert (receipts[-1]["epoch_before"], receipts[-1]["epoch_after"]) == (0, 1)
    # A simultaneous withdrawal AND new grant still needs the current notice.
    assert client.post(f"/api/sessions/{sid}/consent", json={"ai": True, "recording": False}).status_code == 409
    assert client.post(f"/api/sessions/{sid}/consent", json=accept(policy)).status_code == 409
    fresh = client.get("/api/consent-policy").json()["policy"]
    assert fresh["policy_id"] != policy["policy_id"]
    assert client.post(f"/api/sessions/{sid}/consent", json=accept(fresh)).status_code == 200
    receipts = Store(store.database_url).list_consent_receipts("local", sid)
    assert receipts[-1]["policy"] == fresh and receipts[0] == saved
    assert receipts[-1]["epoch_before"] == receipts[-1]["epoch_after"] == 1
    assert store.get_session("local", sid)["status"] == "paused"


def test_database_rejects_receipt_rewrites_and_only_session_deletion_removes_history(store):
    one, two = create(store), create(store, "t2")
    for session in (one, two):
        store.set_consent(session["tenant_id"], session["id"], "client", ai=True, recording=False)
    receipt, = store.list_consent_receipts("t1", one["id"])
    with pytest.raises(psycopg.Error, match="immutable"):
        with store._connect() as c:
            c.execute("UPDATE beep_consent_receipts SET ai=false WHERE id=%s", (receipt["id"],))
    with pytest.raises(psycopg.Error, match="session deletion"):
        with store._connect() as c:
            c.execute("DELETE FROM beep_consent_receipts WHERE id=%s", (receipt["id"],))
    store.initialize()  # repeat migration does not replace history
    assert store.list_consent_receipts("t1", one["id"]) == [receipt]
    with pytest.raises(StoreError):
        store.delete_session("t2", one["id"])
    store.delete_session("t1", one["id"])
    with store._connect() as c:
        assert c.execute("SELECT count(*) AS n FROM beep_consent_receipts WHERE session_id=%s", (one["id"],)).fetchone()["n"] == 0
    assert len(store.list_consent_receipts("t2", two["id"])) == 1


def test_withdrawal_of_legacy_flags_does_not_invent_a_historical_notice(client, store):
    made = new_session(client)
    sid = made["session"]["id"]
    for role in ("client", "facilitator"):
        store.set_consent("local", sid, role, ai=True, recording=True)
    assert exchange(client, made["invitations"]["client"]).status_code == 200
    response = client.post(f"/api/sessions/{sid}/consent", json={"ai": False, "recording": False})
    assert response.status_code == 200, "legacy flags must still be withdrawable without invented notice proof"
    receipt = store.list_consent_receipts("local", sid)[-1]
    assert receipt["principal_kind"] == "authenticated_cookie"
    assert receipt["policy"] is None
    assert receipt["notice_binding"] == "withdrawal_notice_unavailable"


def test_receipt_insert_failure_rolls_back_withdrawal_epoch_snapshot_and_cleanup(store):
    sid = create(store)["id"]
    for role in ("client", "facilitator"):
        store.set_consent("t1", sid, role, ai=True, recording=True)
    before = store.get_session("t1", sid)
    snapshot = store.load_snapshot("t1", sid)
    receipts = store.list_consent_receipts("t1", sid)
    with store._connect() as c:
        jobs = c.execute("SELECT * FROM beep_jobs ORDER BY id").fetchall()
        c.execute("""CREATE FUNCTION reject_test_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'synthetic receipt failure'; END $$""")
        c.execute("""CREATE TRIGGER reject_test_receipt BEFORE INSERT ON beep_consent_receipts
            FOR EACH ROW EXECUTE FUNCTION reject_test_receipt()""")
    with pytest.raises(psycopg.Error, match="synthetic receipt failure"):
        store.set_consent("t1", sid, "client", ai=False, recording=True)
    assert store.get_session("t1", sid) == before
    assert store.load_snapshot("t1", sid) == snapshot
    assert store.list_consent_receipts("t1", sid) == receipts
    with store._connect() as c:
        assert c.execute("SELECT * FROM beep_jobs ORDER BY id").fetchall() == jobs
        c.execute("DROP TRIGGER reject_test_receipt ON beep_consent_receipts")
    assert store.set_consent("t1", sid, "client", ai=False, recording=True)["consent_epoch"] == 1
    assert store.load_snapshot("t1", sid)["consent_epoch"] == 1
    assert len(store.list_consent_receipts("t1", sid)) == 3
    with store._connect() as c:
        assert c.execute("SELECT count(*) AS n FROM beep_jobs WHERE kind='recording_stop'").fetchone()["n"] == 1


def test_receipt_auth_scopes_and_readonly_routes(client, store, settings):
    assert client.get("/api/consent-policy").status_code == 401
    assert client.get("/api/sessions/missing/consent-receipts").status_code == 401
    made = new_session(client)
    sid = made["session"]["id"]
    other = store.create_session("local", title="Other synthetic", pack_id="general", offer="paid")["id"]
    foreign = create(store, "t2")["id"]
    policy = client.get(f"/api/sessions/{sid}/consent-policy").json()["policy"]
    assert client.get("/api/consent-policy").status_code == 403
    assert client.post(f"/api/sessions/{sid}/consent", json=accept(policy)).status_code == 403
    assert client.get(f"/api/sessions/{foreign}/consent-receipts").status_code == 404
    assert exchange(client, made["invitations"]["client"]).status_code == 200
    for other_sid in (other, foreign):
        for suffix in ("consent-policy", "consent-receipts"):
            assert client.get(f"/api/sessions/{other_sid}/{suffix}").status_code == 403
        assert client.post(f"/api/sessions/{other_sid}/consent", json=accept(policy)).status_code == 403
    assert client.post(f"/api/sessions/{sid}/consent", json={**accept(policy), "role": "facilitator"}).status_code == 422
    assert client.post(f"/api/sessions/{sid}/consent", json=accept(policy)).status_code == 200
    client_cookie = client.cookies.get("beep_session")
    assert exchange(client, made["invitations"]["facilitator"]).status_code == 200
    assert client.post(f"/api/sessions/{sid}/consent", json=accept(policy)).status_code == 200
    receipts = client.get(f"/api/sessions/{sid}/consent-receipts").json()["receipts"]
    assert [r["role"] for r in receipts] == ["facilitator"]
    assert hashlib.sha256(client_cookie.encode()).hexdigest() not in json.dumps(receipts)
    assert client.get(f"/api/sessions/{sid}/consent-receipts?after_seq=-1").status_code == 422
    with store._connect() as c:
        c.execute("UPDATE beep_sessions SET data=jsonb_set(data,'{started_at}',to_jsonb((clock_timestamp()-interval '6000 seconds')::text)) WHERE id=%s", (sid,))
        before = c.execute("SELECT * FROM beep_sessions WHERE id=%s", (sid,)).fetchone()
        jobs = c.execute("SELECT * FROM beep_jobs ORDER BY id").fetchall()
    assert client.get("/api/consent-policy").status_code == 200
    assert client.get(f"/api/sessions/{sid}/consent-policy").status_code == 200
    assert client.get(f"/api/sessions/{sid}/consent-receipts").status_code == 200
    with store._connect() as c:
        assert c.execute("SELECT * FROM beep_sessions WHERE id=%s", (sid,)).fetchone() == before
        assert c.execute("SELECT * FROM beep_jobs ORDER BY id").fetchall() == jobs


def test_changed_notice_version_and_selected_configuration_require_fresh_acceptance(client, store, settings, monkeypatch):
    from beep_agent import consent
    from pydantic import SecretStr
    made = new_session(client)
    sid = made["session"]["id"]
    assert exchange(client, made["invitations"]["client"]).status_code == 200
    previous = client.get("/api/consent-policy").json()["policy"]
    assert client.post(f"/api/sessions/{sid}/consent", json=accept(previous)).status_code == 200
    settings.openai_api_key = SecretStr("synthetic-never-expose-provider-key")
    settings.s3_secret_key = SecretStr("synthetic-never-expose-storage-key")
    assert consent.consent_policy(settings) == previous  # credentials are not disclosure data
    settings.s3_endpoint = "https://fake-user:fake-password@storage.invalid/private?secret=fake-query-secret"
    settings.s3_bucket = "synthetic-private-bucket"
    changed = consent.consent_policy(settings)
    assert changed["policy_id"] != previous["policy_id"]
    assert changed["provider_configuration_hash"] != previous["provider_configuration_hash"]
    assert "fake-password" not in json.dumps(changed) and "fake-query-secret" not in json.dumps(changed)
    assert "synthetic-private-bucket" not in json.dumps(changed)
    assert client.post(f"/api/sessions/{sid}/consent", json=accept(previous)).status_code == 409
    assert client.post(f"/api/sessions/{sid}/consent", json=accept(changed)).status_code == 200
    monkeypatch.setattr(consent, "NOTICE_VERSION", "synthetic-notice-v2")
    current = client.get("/api/consent-policy").json()["policy"]
    assert current["notice_version"] != changed["notice_version"]
    assert current["policy_id"] != changed["policy_id"]
    assert client.post(f"/api/sessions/{sid}/consent", json=accept(changed)).status_code == 409
    assert client.post(f"/api/sessions/{sid}/consent", json=accept(current)).status_code == 200
    receipts = store.list_consent_receipts("local", sid)
    assert [r["policy"] for r in receipts] == [previous, changed, current]
    assert "synthetic-never-expose" not in json.dumps(receipts)


def test_history_survives_logout_and_expired_auth_cleanup_without_reusable_tokens(client, store):
    made = new_session(client)
    sid = made["session"]["id"]
    assert exchange(client, made["invitations"]["client"]).status_code == 200
    policy = client.get("/api/consent-policy").json()["policy"]
    assert client.post(f"/api/sessions/{sid}/consent", json=accept(policy)).status_code == 200
    saved = store.list_consent_receipts("local", sid)
    token = client.cookies.get("beep_session")
    assert client.post("/api/logout", json={}).status_code == 200
    assert client.get(f"/api/sessions/{sid}/consent-receipts").status_code == 401
    client.cookies.set("beep_session", token, path="/api")
    assert client.post(f"/api/sessions/{sid}/consent", json=accept(policy)).status_code == 401
    with store._connect() as c:
        c.execute("DELETE FROM beep_auth_tokens WHERE session_id=%s", (sid,))
    assert Store(store.database_url).list_consent_receipts("local", sid) == saved


def test_repeated_and_concurrent_requests_append_history_with_lossless_pagination(store):
    from concurrent.futures import ThreadPoolExecutor
    sid = create(store)["id"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: Store(store.database_url).set_consent("t1", sid, "client", ai=True, recording=False), range(103)))
    first = store.list_consent_receipts("t1", sid)
    rest = store.list_consent_receipts("t1", sid, after_seq=first[-1]["seq"])
    assert len(first) == 100 and len(rest) == 3
    assert [r["seq"] for r in first + rest] == list(range(1, 104))
    assert len({r["id"] for r in first + rest}) == 103
    assert store.list_consent_receipts("t1", sid, after_seq=rest[-1]["seq"]) == []


def test_store_rechecks_scoped_cookie_digest_without_accepting_raw_or_expired_secrets(store, settings):
    from beep_agent.consent import consent_policy
    policy = consent_policy(settings)
    sid = create(store)["id"]
    other = create(store)["id"]
    token_digest = hashlib.sha256(b"synthetic-cookie-only-for-tests").hexdigest()
    principal = dict(tenant_id="t1", session_id=sid, role="client", token_digest=token_digest)
    store.register_auth_token(token_digest, "cookie", other, 60)
    for principal_value, status in ((principal, 401),
        ({**principal, "role": "facilitator"}, 403),
        ({**principal, "tenant_id": "other"}, 403),
        ({**principal, "token_digest": "synthetic-raw-secret"}, 403)):
        with pytest.raises(StoreError) as exc:
            store.set_consent("t1", sid, "client", **accept(policy), principal=principal_value, policy=policy)
        assert exc.value.status_code == status and "synthetic-raw-secret" not in str(exc.value)
    store.revoke_cookie(token_digest)
    store.register_auth_token(token_digest, "cookie", sid, 60)
    with store._connect() as c:
        c.execute("UPDATE beep_auth_tokens SET expires_at=clock_timestamp()-interval '1 second' WHERE digest=%s", (token_digest,))
    with pytest.raises(StoreError) as exc:
        store.set_consent("t1", sid, "client", **accept(policy), principal=principal, policy=policy)
    assert exc.value.status_code == 401
    with pytest.raises(StoreError):
        store.set_consent("t1", sid, "client", **accept(policy), policy=policy)
    assert store.list_consent_receipts("t1", sid) == []


def test_api_keeps_decisions_separate_and_does_not_echo_arbitrary_request_secrets(client, store):
    made = new_session(client)
    sid = made["session"]["id"]
    assert exchange(client, made["invitations"]["client"]).status_code == 200
    policy = client.get("/api/consent-policy").json()["policy"]
    for ai, recording in ((True, False), (False, True), (False, False), (True, True)):
        response = client.post(f"/api/sessions/{sid}/consent", json=accept(policy, ai=ai, recording=recording))
        assert response.status_code == 200
        assert response.json()["session"]["client_consent"] == {"ai": ai, "recording": recording}
        assert response.json()["session"]["facilitator_consent"] == {"ai": False, "recording": False}
    before = store.list_consent_receipts("local", sid)
    for values in ({"ai": "true"}, {"policy_id": "synthetic-raw-secret"},
                   {"principal": "synthetic-raw-secret"}, {"token_digest": "synthetic-raw-secret"},
                   {"notice_version": "synthetic-stale-version"}):
        response = client.post(f"/api/sessions/{sid}/consent", json={**accept(policy), **values})
        assert response.status_code in (409, 422)
        assert "synthetic-raw-secret" not in response.text
    assert store.list_consent_receipts("local", sid) == before
    for suffix in ("", "/events"):
        response = client.get(f"/api/sessions/{sid}{suffix}")
        assert "token_digest" not in response.text and "policy_id" not in response.text


def test_deletion_fence_prevents_new_consent_receipts(store):
    sid = create(store)["id"]
    store.set_consent("t1", sid, "client", ai=True, recording=False)
    receipts = store.list_consent_receipts("t1", sid)
    with store._connect() as c:
        c.execute("UPDATE beep_sessions SET data=jsonb_set(data,'{deletion_pending}','true') WHERE id=%s", (sid,))
    with pytest.raises(StoreError, match="deletion"):
        store.set_consent("t1", sid, "client", ai=True, recording=True)
    assert store.list_consent_receipts("t1", sid) == receipts


def test_repeated_partial_grant_cannot_masquerade_as_withdrawal_of_changed_notice(client, store, settings):
    made = new_session(client)
    sid = made["session"]["id"]
    assert exchange(client, made["invitations"]["client"]).status_code == 200
    old = client.get("/api/consent-policy").json()["policy"]
    partial = accept(old, ai=True, recording=False)
    assert client.post(f"/api/sessions/{sid}/consent", json=partial).status_code == 200
    saved = store.list_consent_receipts("local", sid)
    settings.inference_provider = "codex"
    assert client.post(f"/api/sessions/{sid}/consent", json=partial).status_code == 409
    assert client.post(f"/api/sessions/{sid}/consent", json={"ai": True, "recording": False}).status_code == 409
    assert store.list_consent_receipts("local", sid) == saved
    for _ in range(2):
        assert client.post(f"/api/sessions/{sid}/consent", json={"ai": False, "recording": False}).status_code == 200
