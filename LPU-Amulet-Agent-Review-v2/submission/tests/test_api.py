"""FastAPI+real PostgreSQL integration. All people and keys here are synthetic."""
from urllib.parse import urlsplit, parse_qs
import pytest
from fastapi.testclient import TestClient
from beep_agent.api import create_app
from beep_agent.config import Settings
from test_store import store as store_fixture

store = store_fixture

ORIGIN = "http://127.0.0.1:8094"
ADMIN = "synthetic-operator-test-token-0123456789"


@pytest.fixture
def settings(store, tmp_path):
    return Settings(database_url=store.database_url, admin_token=ADMIN,
        signing_secret="synthetic-test-signing-secret-0123456789", public_origin=ORIGIN,
        openai_api_key="", livekit_api_key="", livekit_api_secret="",
        s3_access_key="", s3_secret_key="", web_dist=tmp_path)


@pytest.fixture
def client(settings, store):
    with TestClient(create_app(settings, store), base_url=ORIGIN, headers={"Origin": ORIGIN}) as c:
        yield c


def login(client):
    r = client.post("/api/operator/login", json={"token": ADMIN})
    assert r.status_code == 200
    return r


def new_session(client):
    login(client)
    r = client.post("/api/sessions", json={"title": "Synthetic review", "pack_id": "general", "offer": "paid"})
    assert r.status_code == 201, r.text
    return r.json()


def exchange(client, url):
    token = parse_qs(urlsplit(url).fragment)["invite"][0]
    return client.post("/api/invitations/exchange", json={"token": token})


def test_private_session_invitation_cookie_origin_and_actor_scoping(client, store):
    assert client.get("/api/me").status_code == 401
    assert client.post("/api/sessions", json={}).status_code == 401
    assert client.post("/api/operator/login", headers={"Origin": "https://evil.invalid"}, json={"token": ADMIN}).status_code == 403
    created = new_session(client)
    sid = created["session"]["id"]
    assert urlsplit(created["invitations"]["client"]).path == f"/review/{sid}"
    r = exchange(client, created["invitations"]["client"])
    assert r.status_code == 200
    assert "HttpOnly" in r.headers["set-cookie"] and "SameSite=strict" in r.headers["set-cookie"]
    assert client.get("/api/me").json() == {"role": "client", "session_id": sid}
    assert client.get("/api/sessions").status_code == 403
    assert client.get(f"/api/sessions/{sid}").json()["role"] == "client"
    other = store.create_session("local", title="Other synthetic", pack_id="general", offer="paid")
    assert client.get(f"/api/sessions/{other['id']}").status_code == 403
    assert client.post(f"/api/sessions/{sid}/consent", json={"ai": True,"recording": True,"role":"facilitator"}).status_code == 422
    assert client.post(f"/api/sessions/{sid}/consent", json={"ai": True,"recording": True}).status_code == 409
    policy_response = client.get("/api/consent-policy")
    assert policy_response.status_code == 200
    policy = policy_response.json()["policy"]
    assert client.post(f"/api/sessions/{sid}/consent", json={
        "ai": True, "recording": True, "notice_version": policy["notice_version"],
        "policy_id": policy["policy_id"],
    }).status_code == 200
    assert client.post(f"/api/sessions/{sid}/control", json={"action":"takeover"}).status_code == 403
    assert client.post(f"/api/sessions/{sid}/room-token", json={}).status_code == 409
    assert client.post("/api/logout", json={}).json() == {"ok": True}
    assert client.get("/api/me").status_code == 401


def test_report_downloads_are_scoped_client_safe_and_corrections_invalidate(client, store):
    created = new_session(client)
    sid = created["session"]["id"]
    for role in ("client", "facilitator"):
        store.set_consent("local", sid, role, ai=True, recording=True)
    store.control("local", sid, "client", "finish")
    report = dict(session_id=sid, revision=0, title="Synthetic", summary="<script>alert(1)</script>", recommendations=[], status="partial", internal_opportunity={"rationale":"PRIVATE INTERNAL"}, generated_at="2026-09-11T00:00:00+00:00")
    assert store.put_report("local", sid, report, expected_revision=0)
    assert client.get(f"/api/sessions/{sid}/report").json()["report"]["internal_opportunity"]["rationale"] == "PRIVATE INTERNAL"
    assert exchange(client, created["invitations"]["client"]).status_code == 200
    for suffix in ("report", "export"):
        r = client.get(f"/api/sessions/{sid}/{suffix}")
        assert r.status_code == 200
        assert "internal_opportunity" not in r.text and "PRIVATE INTERNAL" not in r.text
    html = client.get(f"/api/sessions/{sid}/report.html")
    assert html.status_code == 200 and "<script>alert" not in html.text and "&lt;script&gt;" in html.text
    assert "PRIVATE INTERNAL" not in html.text
    assert client.get(f"/api/sessions/{sid}/events?after_seq=-1").status_code == 422
    assert client.post(f"/api/sessions/{sid}/corrections",json={"text":"Synthetic correction"}).status_code == 200
    assert client.get(f"/api/sessions/{sid}/report").status_code in (404,409)
    assert client.get(f"/api/sessions/{sid}/events").json()["events"][0]["kind"] == "correction"
    client.post("/api/logout",json={})
    for suffix in ("report", "report.html", "export", "events"):
        assert client.get(f"/api/sessions/{sid}/{suffix}").status_code == 401


def test_secure_static_spa_does_not_serve_hidden_or_escaped_files(settings, store):
    settings.web_dist.joinpath("index.html").write_text("<!doctype html><title>Synthetic SPA</title>")
    settings.web_dist.joinpath(".private").write_text("must-not-serve")
    settings.web_dist.joinpath("leak.txt").symlink_to(settings.web_dist.parent / "secret.txt")
    settings.web_dist.parent.joinpath("secret.txt").write_text("must-not-serve")
    with TestClient(create_app(settings,store),base_url=ORIGIN) as c:
        for url in ("/", "/review/synthetic-id", "/preview", "/preview/"):
            r = c.get(url)
            assert r.status_code == 200 and "Synthetic SPA" in r.text
            assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        for url in ("/.private", "/leak.txt", "/%2e%2e/secret.txt", "/api/not-found", "/preview/nested", "/preview-other"):
            assert c.get(url).status_code == 404
        assert c.post("/api/operator/login",json={"token":ADMIN}).status_code == 403


def test_room_admission_uses_official_scoped_tokens_and_verified_named_dispatch(store):
    import json
    import jwt
    from pydantic import SecretStr
    from livekit import api
    from test_recording import provider_http, recording_settings, protobuf
    dispatched = []
    def responder(method,path,body):
        if path.endswith("ListDispatch"):
            req = api.ListAgentDispatchRequest.FromString(body)
            assert req.room.startswith("beep-")
            return protobuf(api.ListAgentDispatchResponse(agent_dispatches=dispatched))
        assert path.endswith("CreateDispatch")
        req = api.CreateAgentDispatchRequest.FromString(body)
        assert req.agent_name == "beep-reviewer"
        assert json.loads(req.metadata)["tenant_id"] == "local"
        dispatched.append(api.AgentDispatch(id="AD_synthetic",agent_name=req.agent_name,room=req.room,metadata=req.metadata))
        return protobuf(dispatched[0])
    with provider_http(responder) as (url,calls):
        cfg = recording_settings(url)
        cfg.database_url = store.database_url
        cfg.admin_token = SecretStr(ADMIN)
        cfg.openai_api_key = SecretStr("synthetic-not-an-inference-call")
        with TestClient(create_app(cfg,store),base_url=ORIGIN,headers={"Origin":ORIGIN}) as c:
            made = new_session(c)
            sid = made["session"]["id"]
            for role in ("client","facilitator"):
                store.set_consent("local",sid,role,ai=True,recording=True)
            with store._connect() as db:
                db.execute("UPDATE beep_sessions SET data=jsonb_set(data,'{started_at}',to_jsonb((clock_timestamp()-interval '901 seconds')::text)) WHERE id=%s",(sid,))
            store.control("local",sid,"client","handover")
            exchange(c,made["invitations"]["client"])
            for _ in range(2):
                r = c.post(f"/api/sessions/{sid}/room-token",json={})
                assert r.status_code == 200, r.text
                payload = jwt.decode(r.json()["participant_token"],cfg.livekit_api_secret.get_secret_value(),algorithms=["HS256"])
                assert payload["sub"] == f"client:{sid}"
                assert payload["video"]["room"] == f"beep-{sid}"
                assert payload["video"]["canPublishSources"] == ["microphone","screen_share","screen_share_audio"]
                assert not payload["video"].get("roomAdmin") and not payload["video"].get("agent")
            assert len(dispatched)==1
            assert sum(path.endswith("ListDispatch") for _,path,_ in calls)>=2


def test_invite_exchange_is_single_use_and_logout_revokes_cookie(client):
    made = new_session(client)
    r = exchange(client,made["invitations"]["client"])
    assert r.status_code==200
    cookie = client.cookies.get("beep_session")
    assert exchange(client,made["invitations"]["client"]).status_code in (401,409)
    assert client.post("/api/logout",json={}).status_code==200
    client.cookies.set("beep_session",cookie,path="/api")
    assert client.get("/api/me").status_code==401


@pytest.mark.asyncio
async def test_concurrent_room_admissions_do_not_duplicate_agent_dispatch(store):
    import asyncio
    from threading import Barrier
    from livekit import api
    from beep_agent.api import _room_admission
    from fastapi import HTTPException
    from test_store import activate
    from test_recording import provider_http, recording_settings, protobuf
    session = activate(store)
    barrier = Barrier(2)
    dispatched=[]
    def responder(method,path,body):
        if path.endswith("ListDispatch"):
            req=api.ListAgentDispatchRequest.FromString(body)
            if not req.dispatch_id:
                barrier.wait(timeout=5)
                return protobuf(api.ListAgentDispatchResponse())
            return protobuf(api.ListAgentDispatchResponse(agent_dispatches=dispatched))
        req=api.CreateAgentDispatchRequest.FromString(body)
        obj=api.AgentDispatch(id=f"AD_{len(dispatched)}",agent_name=req.agent_name,room=req.room,metadata=req.metadata)
        dispatched.append(obj)
        return protobuf(obj)
    with provider_http(responder) as (url,calls):
        settings=recording_settings(url)
        results=await asyncio.gather(_room_admission(settings,store,session,"client"),
            _room_admission(settings,store,session,"client"),return_exceptions=True)
        assert len(dispatched)==1
        assert sum(isinstance(r,dict) for r in results)==1
        assert sum(isinstance(r,HTTPException) and r.status_code==503 for r in results)==1


def test_mutation_body_limit_cannot_be_bypassed_with_false_length(client):
    r=client.post("/api/operator/login",content=b"x"*65537,
        headers={"Content-Type":"application/json","Content-Length":"0"})
    assert r.status_code==413
    r=client.post("/api/operator/login",content=b"{}",headers={"Content-Length":"invalid"})
    assert r.status_code==400


def test_missing_database_is_diagnostic_only_not_default_postgres_fallback(settings):
    settings.database_url=""
    with TestClient(create_app(settings),base_url=ORIGIN,headers={"Origin":ORIGIN}) as c:
        assert c.get("/api/health").status_code==200
        r=c.post("/api/operator/login",json={"token":ADMIN})
        assert r.status_code==503
        assert r.json()=={"detail":"Database is not configured"}


def test_operator_login_is_bounded_in_shared_database(client, settings, store):
    for _ in range(5):
        assert client.post("/api/operator/login", json={"token":"wrong"}).status_code == 401
    with TestClient(create_app(settings, store), base_url=ORIGIN, headers={"Origin":ORIGIN}) as other:
        assert other.post("/api/operator/login", json={"token":ADMIN}).status_code == 429
