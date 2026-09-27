"""PR-02: real HTTP/auth/PostgreSQL, SDK JWTs, synthetic provider IO only."""
import json
import asyncio
from types import SimpleNamespace

import jwt
import pytest
from livekit import api

from beep_agent.api import _room_admission, create_app
from beep_agent.config import Settings
from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_api import ADMIN, ORIGIN, exchange, new_session
from test_store import store as store_fixture

store = store_fixture


@pytest.fixture
def provider(monkeypatch):
    """Only provider IO is replaced; signing, auth, persistence and routes are real."""
    calls = []
    participants = {}

    class Provider:
        def __init__(self, *args, **kwargs):
            calls.append("open")
            self.agent_dispatch = self
            self.room = self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            calls.append("close")

        async def list_dispatch(self, room):
            calls.append("list_dispatch")
            return [api.AgentDispatch(
                id="AD_synthetic", agent_name="beep-reviewer", room=room,
                metadata=json.dumps({"tenant_id": "local", "session_id": room[5:]},
                                    sort_keys=True))]

        async def remove_participant(self, request):
            calls.append(("remove", request.room, request.identity))
            participants.pop((request.room, request.identity), None)
            return api.RemoveParticipantResponse()

        async def get_participant(self, request):
            calls.append(("get", request.room, request.identity))
            person = participants.get((request.room, request.identity))
            if person is None:
                raise api.TwirpError("not_found", "participant does not exist", status=404)
            return person

    monkeypatch.setattr(api, "LiveKitAPI", Provider)
    return SimpleNamespace(calls=calls, cls=Provider, participants=participants)


@pytest.fixture
def http(store, tmp_path):
    cfg = Settings(
        database_url=store.database_url, admin_token=ADMIN,
        signing_secret="synthetic-facilitator-test-signing-secret-0123456789",
        public_origin=ORIGIN, livekit_url="ws://media.invalid",
        livekit_api_key="synthetic-api-key",
        livekit_api_secret="synthetic-media-signing-secret-0123456789",
        openai_api_key="synthetic-no-inference", s3_bucket="synthetic-no-storage",
        s3_access_key="synthetic", s3_secret_key="synthetic", web_dist=tmp_path)
    with TestClient(create_app(cfg, store), base_url=ORIGIN,
                    headers={"Origin": ORIGIN}) as client:
        yield client, cfg


def introduction(client, store):
    made = new_session(client)
    sid = made["session"]["id"]
    for role in ("client", "facilitator"):
        store.set_consent("local", sid, role, ai=True, recording=True)
    return made


def age_introduction(store, sid):
    with store._connect() as db:
        db.execute("""UPDATE beep_sessions SET data=jsonb_set(data,'{started_at}',
            to_jsonb((clock_timestamp()-interval '901 seconds')::text)) WHERE id=%s""",
            (sid,))


def test_active_facilitator_cannot_obtain_media_token(http, store, provider):
    client, _ = http
    made = introduction(client, store)
    sid = made["session"]["id"]
    age_introduction(store, sid)
    store.control("local", sid, "client", "handover")
    assert exchange(client, made["invitations"]["facilitator"]).status_code == 200
    response = client.post(f"/api/sessions/{sid}/room-token", json={})
    assert response.status_code == 403, response.text
    assert "participant_token" not in response.json()
    assert provider.calls == [], "A forbidden facilitator must not touch dispatch/provider IO"


@pytest.mark.asyncio
async def test_stale_introduction_is_denied_before_admission_provider(http, store, provider):
    client, cfg = http
    made = introduction(client, store)
    sid = made["session"]["id"]
    old = store.get_session("local", sid)
    age_introduction(store, sid)
    store.control("local", sid, "client", "handover")
    with pytest.raises(HTTPException) as denied:
        await _room_admission(cfg, store, old, "facilitator")
    assert denied.value.status_code in {403, 409}
    assert provider.calls == []


def test_handover_during_last_provider_await_cannot_mint_facilitator_token(
        http, store, provider, monkeypatch):
    client, _ = http
    made = introduction(client, store)
    sid = made["session"]["id"]
    age_introduction(store, sid)
    assert exchange(client, made["invitations"]["facilitator"]).status_code == 200

    async def late_close(self, *args):
        # Initial/pre-close reads saw introduction; transition after those reads.
        await asyncio.to_thread(store.control, "local", sid, "client", "handover")
        provider.calls.append("close_after_handover")

    monkeypatch.setattr(provider.cls, "__aexit__", late_close)
    response = client.post(f"/api/sessions/{sid}/room-token", json={})
    assert response.status_code in {403, 409}, response.text
    assert "participant_token" not in response.json()
    assert store.get_session("local", sid)["status"] == "active"


@pytest.mark.parametrize("boundary", ["enter", "reserve"])
@pytest.mark.asyncio
async def test_phase_fenced_before_each_dispatch_provider_action(
        http, store, provider, monkeypatch, boundary):
    client, cfg = http
    made = introduction(client, store)
    sid = made["session"]["id"]
    age_introduction(store, sid)
    current = store.control("local", sid, "client", "handover")

    async def no_dispatches(self, room):
        provider.calls.append("list_dispatch")
        return []

    async def create_dispatch(self, request):
        provider.calls.append("create_dispatch")
        raise RuntimeError("Forbidden dispatch attempt")

    monkeypatch.setattr(provider.cls, "list_dispatch", no_dispatches)
    monkeypatch.setattr(provider.cls, "create_dispatch", create_dispatch, raising=False)
    if boundary == "enter":
        async def enter_then_pause(self):
            await asyncio.to_thread(store.control, "local", sid, "client", "pause")
            return self
        monkeypatch.setattr(provider.cls, "__aenter__", enter_then_pause)
    else:
        reserve = store.reserve_dispatch
        def reserve_then_pause(*args):
            result = reserve(*args)
            store.control("local", sid, "client", "pause")
            return result
        monkeypatch.setattr(store, "reserve_dispatch", reserve_then_pause)
    with pytest.raises(HTTPException) as denied:
        await _room_admission(cfg, store, current, "client")
    assert denied.value.status_code == 409
    assert "create_dispatch" not in provider.calls
    if boundary == "enter":
        assert "list_dispatch" not in provider.calls


def test_http_handover_disconnects_only_facilitator_with_exact_readback(
        http, store, provider):
    client, _ = http
    made = introduction(client, store)
    sid = made["session"]["id"]
    room = made["session"]["room_name"]
    age_introduction(store, sid)
    before = store.get_session("local", sid)
    for role in ("client", "facilitator"):
        identity = f"{role}:{sid}"
        provider.participants[room, identity] = api.ParticipantInfo(
            identity=identity, sid=f"PA_{role}",
            permission=api.ParticipantPermission(can_publish=True, can_subscribe=True))
    assert exchange(client, made["invitations"]["client"]).status_code == 200
    response = client.post(f"/api/sessions/{sid}/control", json={"action": "handover"})
    assert response.status_code == 200, response.text
    assert (room, f"facilitator:{sid}") not in provider.participants
    assert (room, f"client:{sid}") in provider.participants
    assert [(c[0], c[1], c[2]) for c in provider.calls if isinstance(c, tuple)] == [
        ("remove", room, f"facilitator:{sid}"), ("get", room, f"facilitator:{sid}")]
    current = store.get_session("local", sid)
    assert current["status"] == "active"
    assert current["consent_epoch"] == before["consent_epoch"]
    assert current["room_name"] == before["room_name"]
    assert current["recording_status"] == before["recording_status"]
    assert current.get("facilitator_media") == {
        "status": "absent_at_readback", "token_revocation": False}
    with store._connect() as db:
        assert not db.execute("SELECT 1 FROM beep_jobs WHERE kind='recording_stop'").fetchone()


def test_failed_disconnect_retains_durable_reconciliation_obligation(http, store, provider, monkeypatch):
    client, _ = http
    made = introduction(client, store)
    sid = made["session"]["id"]
    age_introduction(store, sid)
    assert exchange(client, made["invitations"]["client"]).status_code == 200

    async def unavailable(self, request):
        raise api.TwirpError("unavailable", "synthetic provider failure", status=503)

    monkeypatch.setattr(provider.cls, "remove_participant", unavailable)
    response = client.post(f"/api/sessions/{sid}/control", json={"action": "handover"})
    assert response.status_code == 503
    current = store.get_session("local", sid)
    assert current["status"] == "active", "Never undo consent/control fences on IO failure"
    assert current.get("facilitator_media") == {
        "status": "reconciliation_required", "token_revocation": False}


@pytest.mark.parametrize("boundary", ["withdrawal", "expiry"])
def test_non_http_fence_marks_facilitator_reconciliation_required(
        http, store, provider, boundary):
    client, _ = http
    made = introduction(client, store)
    sid = made["session"]["id"]
    age_introduction(store, sid)
    assert exchange(client, made["invitations"]["client"]).status_code == 200
    assert client.post(f"/api/sessions/{sid}/control", json={"action": "handover"}).status_code == 200
    if boundary == "withdrawal":
        current = store.set_consent("local", sid, "client", ai=False, recording=False)
        assert current["status"] == "paused"
    else:
        with store._connect() as db:
            db.execute("""UPDATE beep_sessions SET data=jsonb_set(data,'{started_at}',
                to_jsonb((clock_timestamp()-interval '5401 seconds')::text)) WHERE id=%s""", (sid,))
        current = store.get_session("local", sid)
        assert current["status"] == "finalising"
    assert current["facilitator_media"] == {
        "status": "reconciliation_required", "token_revocation": False}


def test_control_response_cannot_attach_old_absence_to_new_phase(http, store, provider, monkeypatch):
    from beep_agent import media_authority
    client, _ = http
    made = introduction(client, store)
    sid = made["session"]["id"]
    age_introduction(store, sid)
    assert exchange(client, made["invitations"]["client"]).status_code == 200
    disconnect = media_authority.disconnect_facilitator

    async def disconnect_then_pause(*args):
        result = await disconnect(*args)
        await asyncio.to_thread(store.control, "local", sid, "client", "pause")
        return result

    monkeypatch.setattr(media_authority, "disconnect_facilitator", disconnect_then_pause)
    response = client.post(f"/api/sessions/{sid}/control", json={"action": "handover"})
    assert response.status_code == 409
    assert store.get_session("local", sid)["facilitator_media"]["status"] == "reconciliation_required"


@pytest.mark.parametrize("role,phase", [
    ("facilitator", "awaiting_consent"), ("facilitator", "paused"),
    ("facilitator", "finalising"), ("client", "paused"), ("client", "finalising"),
    ("operator", "introduction"), ("operator", "active"),
])
def test_http_disallowed_roles_phases_never_call_provider(http, store, provider, role, phase):
    client, _ = http
    made = new_session(client) if phase == "awaiting_consent" else introduction(client, store)
    sid = made["session"]["id"]
    if phase == "active":
        age_introduction(store, sid)
        store.control("local", sid, "client", "handover")
    if phase in {"paused", "finalising"}:
        store.control("local", sid, "client", "pause" if phase == "paused" else "finish")
    if role != "operator":
        assert exchange(client, made["invitations"][role]).status_code == 200
    response = client.post(f"/api/sessions/{sid}/room-token", json={})
    assert response.status_code == (409 if role == "client" else 403)
    assert "participant_token" not in response.json()
    assert provider.calls == []


@pytest.mark.parametrize("role,phase", [
    ("facilitator", "introduction"), ("client", "introduction"), ("client", "active"),
])
def test_allowed_role_grants_do_not_require_agent_presence(http, store, provider, role, phase):
    client, cfg = http
    made = introduction(client, store)
    sid = made["session"]["id"]
    if phase == "active":
        age_introduction(store, sid)
        store.control("local", sid, "client", "handover")
    assert exchange(client, made["invitations"][role]).status_code == 200
    response = client.post(f"/api/sessions/{sid}/room-token", json={})
    assert response.status_code == 200, response.text
    payload = jwt.decode(response.json()["participant_token"],
                         cfg.livekit_api_secret.get_secret_value(), algorithms=["HS256"])
    grant = payload["video"]
    assert payload["sub"] == f"{role}:{sid}"
    assert grant["room"] == made["session"]["room_name"]
    assert grant["canSubscribe"] is True and grant["canPublish"] is True
    assert grant["canPublishSources"] == (["microphone"] if role == "facilitator" else
        ["microphone", "screen_share", "screen_share_audio"])
    for key in ("roomAdmin", "roomCreate", "roomList", "roomRecord", "canPublishData",
                "canUpdateOwnMetadata", "agent"):
        assert grant[key] is False
    assert 0 < payload["exp"] - payload["nbf"] <= 300
    assert not provider.participants, "No agent/client presence double was supplied"


@pytest.mark.parametrize("action", ["pause", "takeover", "finish"])
def test_controls_exclude_facilitator_without_regranting_takeover_media(
        http, store, provider, action):
    client, _ = http
    made = introduction(client, store)
    sid, room = made["session"]["id"], made["session"]["room_name"]
    provider.participants[room, f"facilitator:{sid}"] = api.ParticipantInfo(identity=f"facilitator:{sid}")
    assert exchange(client, made["invitations"]["facilitator"]).status_code == 200
    response = client.post(f"/api/sessions/{sid}/control", json={"action": action})
    assert response.status_code == 200, response.text
    assert response.json()["session"]["status"] == ("finalising" if action == "finish" else "paused")
    assert not provider.participants
    assert response.json()["facilitator_media"]["token_revocation"] is False
    assert client.post(f"/api/sessions/{sid}/room-token", json={}).status_code == 403


@pytest.mark.parametrize("failure", ["still_present", "unavailable", "permission_denied"])
def test_removal_acknowledgement_without_absence_readback_is_not_success(
        http, store, provider, monkeypatch, failure):
    client, _ = http
    made = introduction(client, store)
    sid = made["session"]["id"]
    assert exchange(client, made["invitations"]["client"]).status_code == 200

    async def unverified(self, request):
        if failure == "still_present":
            return api.ParticipantInfo(identity=request.identity)
        raise api.TwirpError(failure, "synthetic readback failure", status=503)

    monkeypatch.setattr(provider.cls, "get_participant", unverified)
    response = client.post(f"/api/sessions/{sid}/control", json={"action": "pause"})
    assert response.status_code == 503
    assert store.get_session("local", sid)["facilitator_media"]["status"] == "reconciliation_required"


@pytest.mark.parametrize("boundary", ["enter", "remove", "exit"])
def test_late_disconnection_cannot_clear_a_newer_authority_fence(
        http, store, provider, monkeypatch, boundary):
    client, _ = http
    made = introduction(client, store)
    sid = made["session"]["id"]
    age_introduction(store, sid)
    assert exchange(client, made["invitations"]["client"]).status_code == 200
    method = {"enter": "__aenter__", "remove": "remove_participant", "exit": "__aexit__"}[boundary]
    original = getattr(provider.cls, method)

    async def old_completion(self, *args):
        result = await original(self, *args)
        await asyncio.to_thread(store.control, "local", sid, "client", "pause")
        return result

    monkeypatch.setattr(provider.cls, method, old_completion)
    response = client.post(f"/api/sessions/{sid}/control", json={"action": "handover"})
    assert response.status_code == 409
    current = store.get_session("local", sid)
    assert current["status"] == "paused"
    assert current["facilitator_media"]["status"] == "reconciliation_required"
    if boundary == "enter":
        assert not any(isinstance(c, tuple) for c in provider.calls)


def test_resume_introduction_does_not_evict_or_open_devices(http, store, provider):
    client, _ = http
    made = introduction(client, store)
    sid = made["session"]["id"]
    store.control("local", sid, "facilitator", "takeover")
    assert exchange(client, made["invitations"]["facilitator"]).status_code == 200
    response = client.post(f"/api/sessions/{sid}/control", json={"action": "resume"})
    assert response.status_code == 200
    assert response.json()["session"]["status"] == "introduction"
    assert response.json()["facilitator_media"]["status"] == "introduction_allowed"
    assert provider.calls == []


def test_even_late_old_store_reads_cannot_mint_after_handover(http, store, provider, monkeypatch):
    client, _ = http
    made = introduction(client, store)
    sid = made["session"]["id"]
    assert exchange(client, made["invitations"]["facilitator"]).status_code == 200
    old = store.get_session("local", sid)
    age_introduction(store, sid)
    store.control("local", sid, "client", "handover")
    # Simulate arbitrarily late old reads at every async read seam. Only the
    # final lock+sign transaction can settle present authority in this test.
    monkeypatch.setattr(store, "get_session", lambda *args: old)
    response = client.post(f"/api/sessions/{sid}/room-token", json={})
    assert response.status_code == 403
    assert "participant_token" not in response.json()


@pytest.mark.asyncio
async def test_concurrent_allowed_client_admissions_keep_dispatch_reservation_fence(
        http, store, provider, monkeypatch):
    client, cfg = http
    made = introduction(client, store)
    sid = made["session"]["id"]
    age_introduction(store, sid)
    current = store.control("local", sid, "client", "handover")
    listed = 0
    together = asyncio.Event()
    created = []

    async def list_dispatch(self, room):
        nonlocal listed
        listed += 1
        if listed == 2:
            together.set()
        await asyncio.wait_for(together.wait(), 2)
        return []

    async def create_dispatch(self, request):
        dispatched = api.AgentDispatch(id="AD_only_one", room=request.room,
            agent_name=request.agent_name, metadata=request.metadata)
        created.append(dispatched)
        return dispatched

    async def get_dispatch(self, dispatch_id, room):
        return created[0]

    monkeypatch.setattr(provider.cls, "list_dispatch", list_dispatch)
    monkeypatch.setattr(provider.cls, "create_dispatch", create_dispatch, raising=False)
    monkeypatch.setattr(provider.cls, "get_dispatch", get_dispatch, raising=False)
    results = await asyncio.gather(*(_room_admission(cfg, store, current, "client")
                                    for _ in range(2)), return_exceptions=True)
    assert len(created) == 1
    assert sum(isinstance(r, dict) for r in results) == 1
    assert sum(isinstance(r, HTTPException) and r.status_code == 503 for r in results) == 1


def test_issued_jwt_is_not_misrepresented_as_revoked_by_disconnect(http, store, provider):
    """Local signature verification, NOT a real RTC replay or revocation test."""
    client, cfg = http
    made = introduction(client, store)
    sid = made["session"]["id"]
    age_introduction(store, sid)
    assert exchange(client, made["invitations"]["facilitator"]).status_code == 200
    response = client.post(f"/api/sessions/{sid}/room-token", json={})
    assert response.status_code == 200
    issued = response.json()["participant_token"]
    control = client.post(f"/api/sessions/{sid}/control", json={"action": "handover"})
    assert control.status_code == 200
    assert control.json()["facilitator_media"]["token_revocation"] is False
    assert client.post(f"/api/sessions/{sid}/room-token", json={}).status_code == 403
    old = jwt.decode(issued, cfg.livekit_api_secret.get_secret_value(), algorithms=["HS256"])
    assert old["video"]["canSubscribe"] is True
    assert old["video"]["canPublishSources"] == ["microphone"]


@pytest.mark.asyncio
async def test_old_absence_commit_cannot_clear_new_fence_after_final_await(http, store):
    from beep_agent.media_authority import _save_absence_readback
    client, _ = http
    made = introduction(client, store)
    sid = made["session"]["id"]
    age_introduction(store, sid)
    old = store.control("local", sid, "client", "handover")
    store.control("local", sid, "client", "pause")
    with pytest.raises(HTTPException) as conflict:
        await asyncio.to_thread(_save_absence_readback, store, old)
    assert conflict.value.status_code == 409
    assert store.get_session("local", sid)["facilitator_media"]["status"] == "reconciliation_required"


def test_missing_media_configuration_does_not_undo_pause(http, store, provider):
    from pydantic import SecretStr
    client, cfg = http
    made = introduction(client, store)
    sid = made["session"]["id"]
    assert exchange(client, made["invitations"]["client"]).status_code == 200
    cfg.livekit_api_key = SecretStr("")
    response = client.post(f"/api/sessions/{sid}/control", json={"action": "pause"})
    assert response.status_code == 503
    assert store.get_session("local", sid)["status"] == "paused"
    assert store.get_session("local", sid)["facilitator_media"]["status"] == "reconciliation_required"
    assert provider.calls == []