"""Exact-session facilitator disconnection, NOT self-hosted JWT revocation."""
import asyncio

import aiohttp
from fastapi import HTTPException
from livekit import api


def _same_authority(current, expected):
    if any(current[k] != expected[k] for k in
           ("tenant_id", "id", "room_name", "consent_epoch", "status")):
        raise HTTPException(409, "Session changed during facilitator disconnection")


def _save_absence_readback(store, expected):
    with store._connect() as c:
        current = store._expired(c, store._row(
            c, expected["tenant_id"], expected["id"], lock=True))
        _same_authority(current, expected)
        current["facilitator_media"] = {"status": "absent_at_readback", "token_revocation": False}
        store._write(c, current)
        return current["facilitator_media"]


async def disconnect_facilitator(settings, store, expected):
    """Read back one current connection; never evict a client, agent or recorder.

    This cannot reject an old or server-refreshed JWT at the self-hosted RTC
    server. A successful result establishes absence at readback, not a ban.
    """
    tenant, sid = expected["tenant_id"], expected["id"]

    async def current_authority():
        current = await asyncio.to_thread(store.get_session, tenant, sid)
        _same_authority(current, expected)
        return current

    current = await current_authority()
    if current["status"] == "introduction":
        return {"status": "introduction_allowed", "token_revocation": False}
    if not (settings.livekit_url and settings.livekit_api_key.get_secret_value()
            and settings.livekit_api_secret.get_secret_value()):
        raise HTTPException(503, "Control saved; facilitator disconnection is not configured")

    target = api.RoomParticipantIdentity(room=current["room_name"], identity=f"facilitator:{sid}")
    try:
        async with asyncio.timeout(10):
            async with api.LiveKitAPI(
                settings.livekit_url, settings.livekit_api_key.get_secret_value(),
                settings.livekit_api_secret.get_secret_value(),
                timeout=aiohttp.ClientTimeout(total=5), failover=False,
            ) as lk:
                await current_authority()
                try:
                    await lk.room.remove_participant(target)
                except api.TwirpError as exc:
                    if exc.code != "not_found":
                        raise
                await current_authority()
                # Even a successful/not-found mutation acknowledgement needs an
                # exact target read. Never treat unauthorised/unavailable as absent.
                try:
                    await lk.room.get_participant(target)
                except api.TwirpError as exc:
                    if exc.code != "not_found":
                        raise
                else:
                    raise RuntimeError("Facilitator still present")
                await current_authority()
            await current_authority()
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(503, "Control saved; facilitator disconnection could not be verified") from None
    return await asyncio.to_thread(_save_absence_readback, store, expected)