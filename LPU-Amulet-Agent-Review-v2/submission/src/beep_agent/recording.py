"""Independent official LiveKit Egress/private-S3 recording lifecycle.

HTTP acknowledgement is not recording: start polls ACTIVE, stop polls COMPLETE.
Failures retain the egress id for explicit cleanup, without leaking provider text.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import asyncio
import time
import uuid
from urllib.parse import quote

import aiohttp
from livekit import api

from .domain import RecordingManifest


class RecordingError(RuntimeError):
    def __init__(self, message: str, egress_id: str | None = None):
        super().__init__(message)
        self.egress_id = egress_id


class CleanupResult(list):
    """Stopped identities plus explicit failed-artifact outcomes; never fake files."""

    def __init__(self, ids, failed_ids=(), *, manifests=()):
        super().__init__(ids)
        self.failed_ids = sorted(failed_ids)
        self.manifests = [RecordingManifest.model_validate(m) for m in manifests]


class RecordingService:
    START_TIMEOUT = 60.0
    STOP_TIMEOUT = 120.0
    POLL_INTERVAL = 0.5

    def __init__(self, settings):
        self.settings = settings

    def _require(self):
        s = self.settings
        required = (s.livekit_url, s.livekit_api_key.get_secret_value(), s.livekit_api_secret.get_secret_value(),
                    s.s3_bucket, s.s3_region, s.s3_access_key.get_secret_value(), s.s3_secret_key.get_secret_value())
        if not s.recording_enabled or not all(required):
            raise RecordingError("Recording configuration is incomplete")

    def _api(self):
        s = self.settings
        return api.LiveKitAPI(s.livekit_url, s.livekit_api_key.get_secret_value(),
                              s.livekit_api_secret.get_secret_value(),
                              timeout=aiohttp.ClientTimeout(total=15), failover=False)

    @staticmethod
    def prefix(session: dict):
        parts = [session.get("tenant_id"), session.get("id")]
        if any(not isinstance(p, str) or not p or p in {".", ".."} or len(p)>200 for p in parts):
            raise RecordingError("Invalid recording scope")
        return "beep/" + "/".join(quote(p, safe="") for p in parts) + "/"

    @staticmethod
    def _validate(info, egress_id=None):
        if not info.egress_id or (egress_id and info.egress_id != egress_id):
            raise RecordingError("Egress response has an invalid identity", egress_id)
        if info.error or info.status in {api.EGRESS_FAILED, api.EGRESS_ABORTED, api.EGRESS_LIMIT_REACHED}:
            raise RecordingError("Egress failed; recording is not confirmed", info.egress_id)

    async def _poll(self, client, egress_id, desired, timeout, *, room=None):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            listing = await client.egress.list_egress(api.ListEgressRequest(egress_id=egress_id))
            info = next((i for i in listing.items if i.egress_id == egress_id), None)
            if info is not None:
                self._validate(info, egress_id)
                if room and info.room_name != room:
                    raise RecordingError("Egress room scope mismatch", egress_id)
                if info.status == desired:
                    return info
                if desired == api.EGRESS_ACTIVE and info.status in {api.EGRESS_ENDING, api.EGRESS_COMPLETE}:
                    raise RecordingError("Egress ended before recording was confirmed", egress_id)
            await asyncio.sleep(self.POLL_INTERVAL)
        raise RecordingError("Egress status confirmation timed out", egress_id)

    def _s3(self):
        import boto3
        from botocore.config import Config
        s = self.settings
        return boto3.client("s3", endpoint_url=s.s3_endpoint or None, region_name=s.s3_region,
            aws_access_key_id=s.s3_access_key.get_secret_value(),
            aws_secret_access_key=s.s3_secret_key.get_secret_value(),
            config=Config(connect_timeout=5, read_timeout=15,
                          retries={"total_max_attempts": 1, "mode": "standard"},
                          s3={"addressing_style": "path"}))

    def _verify_files(self, info, session=None, reservation=None):
        if session is None:
            raise RecordingError("Exact recording reservation is required", info.egress_id)
        reservation = self._reservation(session, reservation)
        self._validate(info)
        key = reservation["output_prefix"] + ".mp4"
        bucket = session.get("recording_storage_binding", {}).get("bucket", self.settings.s3_bucket)
        files = list(info.room_composite.file_outputs)
        if info.room_composite.HasField("file"):
            files.append(info.room_composite.file)
        if (info.status != api.EGRESS_COMPLETE or info.room_name != reservation["room_name"]
                or info.room_composite.room_name != reservation["room_name"]
                or len(files) != 1 or files[0].filepath != key or files[0].s3.bucket != bucket
                or len(info.file_results) != 1):
            raise RecordingError("Completed recording does not match its exact reservation", info.egress_id)
        result = info.file_results[0]
        if result.filename != key or result.size <= 0:
            raise RecordingError("Invalid or empty reserved recording output", info.egress_id)
        if result.location:
            # Location is checked only for consistency, never fetched or retained as
            # a permanent media link. Storage readback always uses the reserved key.
            bases = {f"s3://{bucket}", f"https://{bucket}.s3.amazonaws.com",
                     f"https://{bucket}.s3.{self.settings.s3_region}.amazonaws.com"}
            for endpoint in (self.settings.s3_endpoint, getattr(self.settings, "s3_egress_endpoint", "")):
                if endpoint:
                    bases.add(endpoint.rstrip("/") + "/" + bucket)
                    # Pinned livekit/storage location() always reports HTTPS,
                    # even when its configured upload transport is HTTP. This
                    # is an exact metadata form, never a URL we fetch or a
                    # change to the configured authenticated storage endpoint.
                    if endpoint.startswith("http://"):
                        bases.add("https://" + endpoint[7:].rstrip("/") + "/" + bucket)
            if not (self.settings.s3_endpoint or getattr(self.settings, "s3_egress_endpoint", "")):
                bases.add("https://s3.amazonaws.com/" + bucket)
            locations = {base + "/" + path for base in bases for path in (key, quote(key, safe="/"))}
            if result.location not in locations:
                raise RecordingError("Recording location contradicts its reserved object", info.egress_id)
        with closing(self._s3()) as s3:
            metadata = s3.head_object(Bucket=bucket, Key=key)
        if type(metadata.get("ContentLength")) is not int or metadata["ContentLength"] != result.size:
            raise RecordingError("Recording object size does not match the final file result", info.egress_id)
        return RecordingManifest(
            reservation_id=reservation["id"], consent_epoch=reservation["consent_epoch"],
            room_name=reservation["room_name"], bucket=bucket, key=key, egress_id=info.egress_id,
            terminal_status="complete", artifact_status="metadata_verified",
            observed_at=datetime.now(timezone.utc), started_at_ns=info.started_at or None,
            ended_at_ns=info.ended_at or None, file_started_at_ns=result.started_at or None,
            file_ended_at_ns=result.ended_at or None, duration_ns=result.duration or None,
            size_bytes=result.size, head_size_bytes=metadata["ContentLength"],
            content_type=metadata.get("ContentType"), etag=metadata.get("ETag"),
            version_id=metadata.get("VersionId"))

    async def _verify_media(self, manifest):
        from .media_validation import verify_media
        return await verify_media(self._s3, manifest)

    async def stop(self, egress_id: str, *, session=None, reservation=None):
        """An ID-only legacy stop proves shutdown, never reserved artifact success."""
        self._require()
        if not isinstance(egress_id, str) or not egress_id or len(egress_id)>200:
            raise RecordingError("Invalid egress identity")
        try:
            async with self._api() as client:
                async with asyncio.timeout(self.STOP_TIMEOUT + 30):
                    listing = await client.egress.list_egress(api.ListEgressRequest(egress_id=egress_id))
                    info = next((i for i in listing.items if i.egress_id == egress_id), None)
                    if session is not None:
                        reserved = self._reservation(session, reservation)
                        bucket = session.get("recording_storage_binding", {}).get("bucket", self.settings.s3_bucket)
                        outputs = list(info.room_composite.file_outputs) if info is not None else []
                        if info is not None and info.room_composite.HasField("file"):
                            outputs.append(info.room_composite.file)
                        if (info is None or info.room_name != reserved["room_name"]
                                or info.room_composite.room_name != reserved["room_name"]
                                or len(outputs) != 1 or outputs[0].s3.bucket != bucket
                                or outputs[0].filepath != reserved["output_prefix"] + ".mp4"):
                            raise RecordingError("Recording stop target does not match its reservation", egress_id)
                    if info is not None:
                        self._validate(info, egress_id)
                    if info is None or info.status != api.EGRESS_COMPLETE:
                        info = await client.egress.stop_egress(api.StopEgressRequest(egress_id=egress_id))
                        self._validate(info, egress_id)
                        if info.status != api.EGRESS_COMPLETE:
                            info = await self._poll(client, egress_id, api.EGRESS_COMPLETE, self.STOP_TIMEOUT)
                    if session is not None:
                        return await asyncio.to_thread(self._verify_files, info, session, reservation)
        except RecordingError:
            raise
        except Exception:
            raise RecordingError("Recording stop or storage confirmation failed", egress_id) from None

    def _reservation(self, session, reservation=None):
        reservation = reservation or session.get("recording_reservation")
        if isinstance(reservation, dict) and "reservation" in reservation:
            reservation = reservation["reservation"]
        if not isinstance(reservation, dict):
            raise RecordingError("Recording reservation is required")
        epoch = reservation.get("consent_epoch")
        rid = reservation.get("id")
        if (type(epoch) is not int or epoch < 0 or not isinstance(rid, str)
            or not rid.isalnum() or len(rid) > 64
            or reservation.get("room_name") != session.get("room_name")
            or session.get("room_name") != "beep-" + session["id"]
            or reservation.get("output_prefix") != self.prefix(session) + f"epoch-{epoch}-{rid}"):
            raise RecordingError("Invalid recording reservation scope")
        return reservation

    async def check_active(self, session: dict) -> None:
        """Fresh official SDK observation of the exact current recording scope."""
        self._require()
        eid = session.get("egress_id")
        room = session.get("room_name")
        if not isinstance(eid, str) or not eid or len(eid)>200 or room != "beep-" + session["id"]:
            raise RecordingError("Active recording identity is not confirmed")
        reservation = session.get("recording_reservation")
        if reservation:
            reservation = self._reservation(session, reservation)
            if reservation["consent_epoch"] != session["consent_epoch"]:
                raise RecordingError("Active recording consent epoch is stale", eid)
        prefix = self.prefix(session) + f"epoch-{int(session['consent_epoch'])}-"
        try:
            async with self._api() as client:
                async with asyncio.timeout(15):
                    listing = await client.egress.list_egress(api.ListEgressRequest(egress_id=eid))
                    items = [i for i in listing.items if i.egress_id == eid]
                    if len(items) != 1:
                        raise RecordingError("Active recording is not confirmed", eid)
                    info = items[0]
                    self._validate(info, eid)
                    files = list(info.room_composite.file_outputs)
                    if info.room_composite.HasField("file"):
                        files.append(info.room_composite.file)
                    scoped = any(f.s3.bucket == self.settings.s3_bucket and (
                        f.filepath == reservation["output_prefix"] + ".mp4" if reservation else
                        f.filepath.startswith(prefix)) for f in files)
                    if info.status != api.EGRESS_ACTIVE or info.room_name != room or not scoped:
                        raise RecordingError("Active recording scope/status is not confirmed", eid)
        except RecordingError:
            raise
        except Exception:
            raise RecordingError("Active recording verification failed", eid) from None

    async def reconcile_stop(self, session: dict, payload: dict | None = None) -> list[str]:
        """Read the reserved room/output, stop every match, never replay a start.

        An empty listing is not proof an ambiguous start never happened. Raise and
        retain the durable cleanup reservation for an explicit later reconciliation.
        """
        self._require()
        reservation = self._reservation(session, payload)
        room = reservation["room_name"]
        path = reservation["output_prefix"] + ".mp4"
        bucket = session.get("recording_storage_binding", {}).get("bucket", self.settings.s3_bucket)
        expected_id = payload.get("egress_id") if payload else None
        def matches(info):
            files = list(info.room_composite.file_outputs)
            if info.room_composite.HasField("file"):
                files.append(info.room_composite.file)
            return info.room_name == room and any(
                f.filepath == path and f.s3.bucket == bucket for f in files)
        try:
            async with self._api() as client:
                async with asyncio.timeout(self.STOP_TIMEOUT + 30):
                    listing = await client.egress.list_egress(api.ListEgressRequest(room_name=room))
                    ids = sorted({i.egress_id for i in listing.items if matches(i) and i.egress_id})
                    if not ids or len(ids) > 32 or (expected_id and expected_id not in ids):
                        raise RecordingError("Recording shutdown is not confirmed; reconcile again")
                    terminal_failure = {api.EGRESS_FAILED, api.EGRESS_ABORTED, api.EGRESS_LIMIT_REACHED}
                    initial = {i.egress_id: i for i in listing.items if matches(i)}
                    for eid in ids:
                        if initial[eid].status in terminal_failure:
                            continue
                        try:
                            await self.stop(eid, session=session, reservation=reservation)
                        except RecordingError:
                            latest = await client.egress.list_egress(api.ListEgressRequest(egress_id=eid))
                            if not any(i.egress_id == eid and matches(i) and i.status in terminal_failure for i in latest.items):
                                raise
                    verified = await client.egress.list_egress(api.ListEgressRequest(room_name=room))
                    current = {i.egress_id: i for i in verified.items if matches(i)}
                    if set(current) != set(ids) or any(i.status not in terminal_failure | {api.EGRESS_COMPLETE} for i in current.values()):
                        raise RecordingError("Recording shutdown is not confirmed; reconcile again")
                    failed_ids = [eid for eid in ids if current[eid].status in terminal_failure]
                    if len(ids) > 1 and len(failed_ids) != len(ids):
                        raise RecordingError("Multiple egress identities target the same artifact; reconcile again")
                    manifests = []
                    for eid in ids:
                        info = current[eid]
                        if eid not in failed_ids:
                            # Validate the final readback too, not merely the stop response.
                            manifests.append(await asyncio.to_thread(self._verify_files, info, session, reservation))
                        else:
                            manifests.append(RecordingManifest(
                                reservation_id=reservation["id"], consent_epoch=reservation["consent_epoch"],
                                room_name=room, bucket=bucket, key=path, egress_id=eid,
                                terminal_status={api.EGRESS_FAILED: "failed", api.EGRESS_ABORTED: "aborted",
                                    api.EGRESS_LIMIT_REACHED: "limit_reached"}[info.status],
                                artifact_status="unavailable", observed_at=datetime.now(timezone.utc),
                                started_at_ns=info.started_at or None, ended_at_ns=info.ended_at or None))
                    result = CleanupResult(ids, failed_ids, manifests=manifests)
        except RecordingError:
            raise
        except Exception:
            raise RecordingError("Recording reconciliation failed; shutdown is not confirmed") from None
        # Terminal provider readback is established before any expensive decoding.
        # Decode failure is an artifact outcome, not a reason to replay stop/start.
        return CleanupResult(result, result.failed_ids,
            manifests=[await self._verify_media(m) for m in result.manifests])

    def _delete_objects(self, session):
        prefix = self.prefix(session)
        bucket = self.settings.s3_bucket
        with closing(self._s3()) as s3:
            versioned = s3.get_bucket_versioning(Bucket=bucket).get("Status") in {"Enabled", "Suspended"}
            paginator = s3.get_paginator("list_object_versions" if versioned else "list_objects_v2")
            count = 0
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                entries = page.get("Versions", []) + page.get("DeleteMarkers", []) if versioned else page.get("Contents", [])
                objects = []
                for entry in entries:
                    if not entry["Key"].startswith(prefix):
                        raise RecordingError("Storage listing escaped the session prefix")
                    objects.append({"Key": entry["Key"], **({"VersionId": entry["VersionId"]} if versioned else {})})
                count += len(objects)
                if count > 10000:
                    raise RecordingError("Recording deletion exceeded bounded object limit")
                for offset in range(0, len(objects), 1000):
                    result = s3.delete_objects(Bucket=bucket, Delete={"Objects": objects[offset:offset+1000], "Quiet": False})
                    if result.get("Errors"):
                        raise RecordingError("Private recording deletion was rejected")
            # Read back the exact prefix; an acknowledged delete is not sufficient.
            for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
                if page.get("Contents") or page.get("Versions") or page.get("DeleteMarkers"):
                    raise RecordingError("Private recording deletion is incomplete")

    async def delete(self, session: dict) -> None:
        self._require()
        if session.get("status") not in {"awaiting_consent", "completed", "partial", "failed"}:
            raise RecordingError("Cannot delete a running session recording")
        if session.get("recording_cleanup_pending") or session.get("recording_status") in {"starting", "queued", "recording", "stopping"}:
            raise RecordingError("Recording must be stopped before deletion")
        try:
            await asyncio.to_thread(self._delete_objects, session)
        except RecordingError:
            raise
        except Exception:
            raise RecordingError("Private recording deletion failed") from None

    async def _existing(self, client, session):
        room = session["room_name"]
        prefix = self.prefix(session) + f"epoch-{int(session['consent_epoch'])}-"
        listing = await client.egress.list_egress(api.ListEgressRequest(room_name=room))
        for info in listing.items:
            if info.room_name != room:
                raise RecordingError("Egress listing escaped room scope")
            files = list(info.room_composite.file_outputs)
            if info.room_composite.HasField("file"):
                files.append(info.room_composite.file)
            scoped = any(f.filepath.startswith(prefix) and f.s3.bucket == self.settings.s3_bucket for f in files)
            if info.status in {api.EGRESS_STARTING, api.EGRESS_ACTIVE, api.EGRESS_ENDING}:
                if not scoped or info.status == api.EGRESS_ENDING:
                    raise RecordingError("An existing room recording must be stopped first", info.egress_id)
                self._validate(info)
                if info.status != api.EGRESS_ACTIVE:
                    await self._poll(client, info.egress_id, api.EGRESS_ACTIVE, self.START_TIMEOUT, room=room)
                return info.egress_id
            if scoped and info.status == api.EGRESS_COMPLETE:
                raise RecordingError("This consent epoch already has a completed recording", info.egress_id)
        return None

    async def start(self, session: dict) -> str:
        self._require()
        if session.get("status") not in {"introduction", "active"} or not all(
                session.get(k, {}).get("ai") is True and session.get(k, {}).get("recording") is True
                for k in ("client_consent", "facilitator_consent")):
            raise RecordingError("Explicit consent from both parties is required")
        room = session.get("room_name")
        if room != "beep-" + session["id"]:
            raise RecordingError("Invalid recording room scope")
        s = self.settings
        reservation = session.get("recording_reservation")
        if reservation:
            reservation = self._reservation(session, reservation)
            if reservation["consent_epoch"] != session["consent_epoch"]:
                raise RecordingError("Recording reservation consent epoch is stale")
        path = (reservation["output_prefix"] + ".mp4" if reservation else
                self.prefix(session) + f"epoch-{int(session['consent_epoch'])}-{uuid.uuid4().hex}.mp4")
        request = api.RoomCompositeEgressRequest(room_name=room, layout="speaker", audio_only=False,
            file_outputs=[api.EncodedFileOutput(file_type=api.EncodedFileType.MP4, filepath=path,
                disable_manifest=False, s3=api.S3Upload(bucket=s.s3_bucket, region=s.s3_region,
                    endpoint=getattr(s, "s3_egress_endpoint", "") or s.s3_endpoint,
                    access_key=s.s3_access_key.get_secret_value(), secret=s.s3_secret_key.get_secret_value(),
                    force_path_style=True))])
        egress_id = None
        try:
            async with self._api() as client:
                async with asyncio.timeout(self.START_TIMEOUT + 15):
                    created = await client.room.create_room(api.CreateRoomRequest(
                        name=room, empty_timeout=600, max_participants=4))
                    rooms = await client.room.list_rooms(api.ListRoomsRequest(names=[room]))
                    if created.name != room or not any(r.name == room for r in rooms.rooms):
                        raise RecordingError("Recording room creation could not be confirmed")
                    existing = await self._existing(client, session)
                    if existing:
                        return existing
                    try:
                        info = await client.egress.start_room_composite_egress(request)
                    except Exception:
                        # No replay of an ambiguous mutation. Reconcile the actual room
                        # and exact tenant/session/epoch output prefix using a read.
                        existing = await self._existing(client, session)
                        if existing:
                            return existing
                        raise RecordingError("Recording start outcome is unknown") from None
                    egress_id = info.egress_id or None
                    self._validate(info)
                    if info.room_name != room:
                        raise RecordingError("Egress room scope mismatch", egress_id)
                    if info.status != api.EGRESS_ACTIVE:
                        await self._poll(client, egress_id, api.EGRESS_ACTIVE, self.START_TIMEOUT, room=room)
                    return egress_id
        except RecordingError:
            raise
        except Exception:
            raise RecordingError("Recording start failed or its outcome is unknown", egress_id) from None
