"""Same-origin role-scoped BEEP API. No runtime demo/provider substitutes."""
from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
import hmac
from typing import Literal
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import psycopg
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .auth import AuthError, TokenService
from .config import Settings
from .store import Store, StoreError

COOKIE = "beep_session"
TENANT = "local"


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TokenBody(DTO):
    token: str = Field(min_length=1, max_length=4096)


class SessionBody(DTO):
    title: str = Field(min_length=1, max_length=200)
    pack_id: Literal["general", "quotation", "recurring_reporting"]
    offer: Literal["paid", "sponsored"]


class ConsentBody(DTO):
    ai: StrictBool
    recording: StrictBool
    notice_version: str | None = Field(default=None, min_length=1, max_length=100)
    policy_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class ControlBody(DTO):
    action: Literal["handover", "pause", "resume", "finish", "takeover"]


class CorrectionBody(DTO):
    text: str = Field(min_length=1, max_length=12000)


class EmptyBody(DTO):
    pass


def create_app(settings: Settings | None = None, store: Store | None = None):
    settings = settings or Settings()
    store = store or Store(settings.database_url)
    store.configure_limits(max_concurrent_sessions=settings.max_concurrent_sessions)

    @asynccontextmanager
    async def lifespan(app):
        import asyncio
        if settings.database_url:
            await asyncio.to_thread(store.initialize)
        yield
        store.close()

    app = FastAPI(title="BEEP", version="0.1.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.settings = settings
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[urlsplit(settings.public_origin).hostname])

    @app.middleware("http")
    async def same_origin(request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("origin") != settings.public_origin:
                return JSONResponse({"detail": "Same-origin request required"}, status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse({"detail": "Cross-site request denied"}, status_code=403)
        try:
            length = int(request.headers.get("content-length", "0") or 0)
            if length < 0:
                raise ValueError
        except ValueError:
            return JSONResponse({"detail": "Invalid content length"}, status_code=400)
        if length > 65536:
            return JSONResponse({"detail": "Request too large"}, status_code=413)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > 65536:
                    return JSONResponse({"detail": "Request too large"}, status_code=413)
                body.extend(chunk)
            request._body = bytes(body)  # Starlette's cached request replays this bounded body.

        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(self), display-capture=(self)"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; media-src 'self' blob:; connect-src 'self' " + settings.livekit_url +
            "; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    @app.exception_handler(psycopg.Error)
    async def database_error(request, exc):
        return JSONResponse({"detail": "Database is unavailable"}, status_code=503)

    @app.exception_handler(StoreError)
    async def store_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    @app.exception_handler(AuthError)
    async def auth_error(request, exc):
        return JSONResponse({"detail": "Invalid or expired authentication"}, status_code=401)

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"detail": "Invalid request"}, status_code=422)

    def tokens():
        secret = settings.signing_secret.get_secret_value()
        if len(secret) < 32:
            raise HTTPException(503, "Authentication is not configured")
        return TokenService(secret, settings.invitation_ttl_seconds)

    def actor(request: Request):
        token = request.cookies.get(COOKIE)
        if not token:
            raise HTTPException(401, "Authentication required")
        who = tokens().verify(token, "cookie")
        if not store.cookie_valid(hashlib.sha256(token.encode()).hexdigest()):
            raise HTTPException(401, "Authentication expired or revoked")
        return who

    def operator(who=Depends(actor)):
        if who["role"] != "operator" or who["tenant_id"] != TENANT:
            raise HTTPException(403, "Operator required")
        return who

    def scoped(session_id: str, who=Depends(actor)):
        if who["role"] != "operator" and who["session_id"] != session_id:
            raise HTTPException(403, "Session access denied")
        store.get_session(who["tenant_id"], session_id)
        return who

    def set_cookie(response, who):
        token = tokens().issue("cookie", who["tenant_id"], who["role"], who["session_id"])
        store.register_auth_token(hashlib.sha256(token.encode()).hexdigest(), "cookie", who["session_id"], settings.invitation_ttl_seconds)
        response.set_cookie(COOKIE, token, max_age=settings.invitation_ttl_seconds,
                            httponly=True, secure=settings.secure_cookies, samesite="strict", path="/api")

    @app.get("/api/health")
    def health():
        return {"status": "ok", "readiness": settings.readiness(), "version": "0.1.0"}

    @app.get("/api/packs")
    def packs():
        from .packs import list_packs
        return {"packs": list_packs()}

    @app.post("/api/operator/login")
    def login(body: TokenBody, request: Request, response: Response):
        key = hashlib.sha256((request.client.host if request.client else "unknown").encode()).hexdigest()
        if not store.allow_login("global", limit=60) or not store.allow_login(key):
            raise HTTPException(429, "Login attempt limit reached; retry later")
        expected = settings.admin_token.get_secret_value()
        if len(expected) < 32:
            raise HTTPException(503, "Operator authentication is not configured")
        if not hmac.compare_digest(body.token.encode(), expected.encode()):
            raise HTTPException(401, "Invalid operator authentication")
        set_cookie(response, dict(tenant_id=TENANT, role="operator", session_id=None))
        return {"role": "operator"}

    @app.get("/api/operator/inference")
    async def operator_inference(who=Depends(operator)):
        from .localdev import inference_status
        return await inference_status(settings)

    @app.post("/api/logout")
    def logout(request: Request, response: Response, body: EmptyBody, who=Depends(actor)):
        store.revoke_cookie(hashlib.sha256(request.cookies[COOKIE].encode()).hexdigest())
        response.delete_cookie(COOKIE, path="/api", httponly=True,
                               secure=settings.secure_cookies, samesite="strict")
        return {"ok": True}

    @app.get("/api/me")
    def me(who=Depends(actor)):
        return {"role": who["role"], **({"session_id": who["session_id"]} if who["session_id"] else {})}

    @app.post("/api/sessions", status_code=201)
    def create(body: SessionBody, who=Depends(operator)):
        s = store.create_session(who["tenant_id"], **body.model_dump(),
                                 max_seconds=settings.max_session_seconds,
                                 intro_seconds=settings.introduction_seconds,
                                 budget_aud=settings.session_budget_aud)
        invitations = {role: f"{settings.public_origin}/review/{s['id']}#invite=" + quote(
            tokens().issue("invite", who["tenant_id"], role, s["id"]), safe="")
            for role in ("client", "facilitator")}
        return {"session": s, "invitations": invitations}

    @app.get("/api/sessions")
    def sessions(who=Depends(operator)):
        return {"sessions": store.list_sessions(who["tenant_id"])}

    @app.post("/api/invitations/exchange")
    def exchange(body: TokenBody, response: Response):
        who = tokens().verify(body.token, "invite")
        store.get_session(who["tenant_id"], who["session_id"])
        if not store.register_auth_token(hashlib.sha256(body.token.encode()).hexdigest(), "invite", who["session_id"], settings.invitation_ttl_seconds):
            raise HTTPException(409, "Invitation was already exchanged")
        set_cookie(response, who)
        return {"role": who["role"], "session_id": who["session_id"]}

    @app.get("/api/sessions/{session_id}")
    def session(session_id: str, who=Depends(scoped)):
        return {"session": store.get_session(who["tenant_id"], session_id),
                "snapshot": store.load_snapshot(who["tenant_id"], session_id), "role": who["role"]}

    @app.post("/api/sessions/{session_id}/consent")
    def consent(session_id: str, body: ConsentBody, request: Request, who=Depends(scoped)):
        from .consent import consent_policy
        principal = {**who, "token_digest": hashlib.sha256(request.cookies[COOKIE].encode()).hexdigest()}
        return {"session": store.set_consent(who["tenant_id"], session_id, who["role"],
            **body.model_dump(), principal=principal, policy=consent_policy(settings))}

    @app.get("/api/consent-policy")
    def participant_consent_policy(who=Depends(actor)):
        # The cookie supplies scope; a participant cannot request another review.
        if who["role"] not in {"client", "facilitator"}:
            raise HTTPException(403, "Participant required")
        return {"policy": store.read_consent_policy(who["tenant_id"], who["session_id"], settings)}

    @app.get("/api/sessions/{session_id}/consent-policy")
    def session_consent_policy(session_id: str, who=Depends(actor)):
        if who["role"] != "operator" and who["session_id"] != session_id:
            raise HTTPException(403, "Session access denied")
        return {"policy": store.read_consent_policy(who["tenant_id"], session_id, settings)}

    @app.get("/api/sessions/{session_id}/consent-receipts")
    def consent_receipts(session_id: str, after_seq: int = 0, who=Depends(actor)):
        if who["role"] != "operator" and who["session_id"] != session_id:
            raise HTTPException(403, "Session access denied")
        receipts = store.list_consent_receipts(who["tenant_id"], session_id,
            role=None if who["role"] == "operator" else who["role"], after_seq=after_seq)
        return {"receipts": receipts, "next_after_seq": receipts[-1]["seq"] if receipts else after_seq}

    @app.post("/api/sessions/{session_id}/control")
    async def control(session_id: str, body: ControlBody, who=Depends(scoped)):
        import asyncio
        from .media_authority import disconnect_facilitator, _same_authority
        s = await asyncio.to_thread(store.control, who["tenant_id"], session_id, who["role"], body.action)
        media = await disconnect_facilitator(settings, store, s)
        current = await asyncio.to_thread(store.get_session, who["tenant_id"], session_id)
        _same_authority(current, s)
        return {"session": current, "facilitator_media": media}

    @app.post("/api/sessions/{session_id}/room-token")
    async def room_token(session_id: str, body: EmptyBody, who=Depends(scoped)):
        import asyncio
        s = await asyncio.to_thread(store.get_session, who["tenant_id"], session_id)
        _require_room_admission(s, who["role"])
        try:
            settings.require_runtime()
        except RuntimeError:
            raise HTTPException(503, "Room providers are not configured") from None
        return await _room_admission(settings, store, s, who["role"])

    @app.post("/api/sessions/{session_id}/corrections")
    def correction(session_id: str, body: CorrectionBody, who=Depends(scoped)):
        import uuid
        if who["role"] != "client":
            raise HTTPException(403, "Client correction required")
        s = store.get_session(who["tenant_id"], session_id)
        store.append_event(who["tenant_id"], session_id, dict(id=str(uuid.uuid4()), kind="correction",
            actor="client", text=body.text, at_ms=0, consent_epoch=s["consent_epoch"]))
        return {"session": store.get_session(who["tenant_id"], session_id)}

    def report_for(who, session_id):
        from .reports import report_for_delivery
        report = store.get_report(who["tenant_id"], session_id)
        if report is None:
            raise HTTPException(409, "Report is not yet available")
        return report_for_delivery(report)

    @app.get("/api/sessions/{session_id}/report")
    def report(session_id: str, who=Depends(scoped)):
        data = report_for(who, session_id)
        if who["role"] != "operator":
            data.pop("internal_opportunity", None)
        return {"report": data}

    @app.get("/api/sessions/{session_id}/report.html")
    def report_html(session_id: str, who=Depends(scoped)):
        from .domain import Report
        from .reports import render_report_html
        from fastapi.responses import HTMLResponse
        # Downloaded/printable artifacts are always client-safe, even for operators.
        html = render_report_html(Report.model_validate(report_for(who, session_id)), internal=False)
        return HTMLResponse(html, headers={"Content-Disposition": 'inline; filename="beep-report.html"'})

    @app.get("/api/sessions/{session_id}/export")
    def export(session_id: str, who=Depends(scoped)):
        data = report_for(who, session_id)
        data.pop("internal_opportunity", None)
        return JSONResponse({"report": data}, headers={"Content-Disposition": 'attachment; filename="beep-report.json"'})

    @app.get("/api/sessions/{session_id}/events")
    def events(session_id: str, after_seq: int = 0, who=Depends(scoped)):
        return {"events": store.list_events(who["tenant_id"], session_id, after_seq)}

    @app.get("/{path:path}")
    def static(path: str):
        from pathlib import Path
        from fastapi.responses import FileResponse
        if path.startswith("api/") or any(p.startswith(".") for p in Path(path).parts):
            raise HTTPException(404, "Not found")
        root = settings.web_dist.resolve()
        is_app_route = path in {"", "preview", "preview/"} or (path.startswith("review/") and len(Path(path).parts) == 2)
        candidate = (root / ("index.html" if is_app_route else path)).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            raise HTTPException(404, "Not found")
        if candidate.suffix.lower() not in {".html", ".js", ".css", ".svg", ".png", ".jpg", ".ico", ".woff", ".woff2"}:
            raise HTTPException(404, "Not found")
        return FileResponse(candidate)

    return app


def _require_room_admission(session, role, expected=None):
    """Media role policy is independent of agent presence and UI capture readiness."""
    if role not in {"client", "facilitator"}:
        raise HTTPException(403, "Participant required")
    if role == "facilitator" and session["status"] != "introduction":
        raise HTTPException(403, "Facilitator media is available only during introduction")
    if session["status"] not in {"active", "introduction"} or not Store._consented(session):
        raise HTTPException(409, "Both parties must consent before joining")
    if expected and any(session[key] != expected[key]
                        for key in ("tenant_id", "id", "room_name", "consent_epoch")):
        raise HTTPException(409, "Session changed during room admission")


async def _room_admission(settings, store, session, role):
    import asyncio
    import json
    from datetime import datetime, timedelta, timezone
    import aiohttp
    from livekit import api

    room = session["room_name"]
    tenant, sid = session["tenant_id"], session["id"]
    _require_room_admission(session, role)

    async def authority():
        current = await asyncio.to_thread(store.get_session, tenant, sid)
        _require_room_admission(current, role, session)
        return current

    await authority()
    try:
        async with api.LiveKitAPI(settings.livekit_url, settings.livekit_api_key.get_secret_value(),
                settings.livekit_api_secret.get_secret_value(), timeout=aiohttp.ClientTimeout(total=10), failover=False) as lk:
            current = await authority()
            if current["status"] == "active":
                metadata = json.dumps({"tenant_id": tenant, "session_id": sid}, sort_keys=True)
                dispatches = await lk.agent_dispatch.list_dispatch(room)
                await authority()
                matching = [d for d in dispatches if d.agent_name == settings.agent_name and d.room == room and d.metadata == metadata]
                if not matching:
                    reserved = await asyncio.to_thread(store.reserve_dispatch, tenant, sid, session["consent_epoch"])
                    await authority()
                    if not reserved:
                        raise HTTPException(503, "Agent dispatch is pending or requires reconciliation")
                    created = await lk.agent_dispatch.create_dispatch(api.CreateAgentDispatchRequest(
                        room=room, agent_name=settings.agent_name, metadata=metadata))
                    await authority()
                    verified = await lk.agent_dispatch.get_dispatch(created.id, room)
                    await authority()
                    if not verified or verified.id != created.id or verified.agent_name != settings.agent_name or verified.room != room or verified.metadata != metadata:
                        raise HTTPException(503, "Agent dispatch could not be confirmed")
                    await asyncio.to_thread(store.confirm_dispatch, tenant, sid, session["consent_epoch"], verified.id)
            await authority()
        def mint():
            # Final authority read AND local signing serialize with Store.control.
            # No provider IO/await under this lock; an old to_thread read or async
            # context-manager exit cannot sign after a committed handover.
            with store._connect() as c:
                current = store._expired(c, store._row(c, tenant, sid, lock=True))
                _require_room_admission(current, role, session)
                elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(current["started_at"])).total_seconds()
                remaining = min(300, max(1, int(current["max_seconds"] - elapsed)))
                identity = f"{role}:{sid}"
                token = (api.AccessToken(settings.livekit_api_key.get_secret_value(), settings.livekit_api_secret.get_secret_value())
                         .with_identity(identity).with_ttl(timedelta(seconds=remaining))
                         .with_metadata(json.dumps({"consent_epoch": current["consent_epoch"]}))
                         .with_grants(api.VideoGrants(room_join=True, room=room, room_create=False, room_list=False,
                            room_record=False, room_admin=False, can_publish=True, can_subscribe=True,
                            can_publish_data=False, can_update_own_metadata=False, agent=False,
                            can_publish_sources=["microphone", "screen_share", "screen_share_audio"] if role == "client" else ["microphone"]))
                         .to_jwt())
                return {"server_url": settings.livekit_url, "participant_token": token,
                        "participant_identity": identity, "room_name": room}

        return await asyncio.to_thread(mint)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "Room admission provider unavailable") from None


def main():
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=8094, access_log=False)
