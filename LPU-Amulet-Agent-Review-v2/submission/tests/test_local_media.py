"""Opt-in real local RTC/Egress/S3 acceptance; synthetic pixels/tone, no inference.

BEEP_RUN_LOCAL_MEDIA_TESTS=1 BEEP_CONFIG_FILE=.local/runtime.json uv run pytest tests/test_local_media.py
Durable per-run evidence survives failures. Private settings/provider objects are never emitted.
"""
import array
import asyncio
from contextlib import closing, suppress
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import time
from urllib.parse import quote
import uuid

import httpx
import pytest
from livekit import api, rtc
from PIL import Image, ImageChops, ImageDraw, ImageStat
from pydantic_settings import JsonConfigSettingsSource

from beep_agent.config import Settings
from beep_agent.recording import RecordingError, RecordingService

pytestmark = pytest.mark.skipif(
    os.environ.get("BEEP_RUN_LOCAL_MEDIA_TESTS") != "1", reason="explicit local media opt-in required"
)
ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {api.EGRESS_COMPLETE, api.EGRESS_FAILED, api.EGRESS_ABORTED, api.EGRESS_LIMIT_REACHED}


class LocalRecordingSettings(Settings):
    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings,
                                   dotenv_settings, file_secret_settings):
        # Do not silently inherit BEEP_* or OPENAI_API_KEY over the authorised file.
        return (JsonConfigSettingsSource(settings_cls, json_file=ROOT / ".local/runtime.json"),)


def verify_decoded_media(path, reference):
    """Decode the actual downloaded file; reject silent/blank or wrong content."""
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format",
                            "-of", "json", str(path)], capture_output=True, timeout=30)
    assert probe.returncode == 0, "ffprobe rejected the downloaded recording"
    metadata = json.loads(probe.stdout)
    streams = metadata["streams"]
    assert {s["codec_type"] for s in streams} >= {"audio", "video"}
    duration = float(metadata["format"]["duration"])
    assert duration >= 4
    decoded = subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(path),
        "-map", "0:v:0", "-map", "0:a:0", "-progress", "pipe:1", "-nostats",
        "-f", "null", "-"], capture_output=True, timeout=45)
    assert decoded.returncode == 0 and not decoded.stderr, "Full audio/video decode failed"
    progress = dict(line.split("=", 1) for line in decoded.stdout.decode().splitlines() if "=" in line)
    assert progress.get("progress") == "end" and int(progress["frame"]) >= 60
    image_path = path.with_name("decoded-frame.png")
    frame = subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(duration / 2),
        "-i", str(path), "-frames:v", "1", str(image_path)], capture_output=True, timeout=30)
    assert frame.returncode == 0 and image_path.is_file()
    with Image.open(image_path) as image:
        actual = image.convert("RGB").resize(reference.size)
        expected = reference.convert("RGB")
        error = sum(ImageStat.Stat(ImageChops.difference(actual, expected)).mean) / 3
        assert error < 12, "Decoded video does not match the synthetic screen"
        # Includes the green marker only published AFTER facilitator departure.
        for xy in [(590, 30), (100, 245), (300, 245), (500, 245)]:
            assert max(abs(a - b) for a, b in zip(actual.getpixel(xy), expected.getpixel(xy))) < 30
    audio = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0",
        "-ac", "1", "-ar", "48000", "-f", "f32le", "pipe:1"], capture_output=True, timeout=30)
    assert audio.returncode == 0 and not audio.stderr
    samples = array.array("f")
    samples.frombytes(audio.stdout)
    assert len(samples) >= 48000
    rms = math.sqrt(sum(x * x for x in samples) / len(samples))
    assert rms > 0.002, "Decoded recording is silent"
    # Goertzel on a central second of real decoded PCM, not provider metadata.
    window = samples[len(samples) // 2:len(samples) // 2 + 48000]
    powers = {}
    for frequency in range(350, 531, 5):
        coefficient = 2 * math.cos(2 * math.pi * frequency / 48000)
        previous = previous2 = 0.0
        for sample in window:
            current = sample + coefficient * previous - previous2
            previous2, previous = previous, current
        powers[frequency] = previous ** 2 + previous2 ** 2 - coefficient * previous * previous2
    peak = max(powers, key=powers.get)
    dominance = powers[peak] / sum(powers.values())
    assert 435 <= peak <= 445 and dominance > 0.2, "Decoded audio lacks the synthetic 440 Hz tone"
    return {"ffprobe_streams": [{k: s[k] for k in (
                "codec_name", "codec_type", "width", "height", "sample_rate", "channels", "nb_frames")
                if k in s} for s in streams],
            "duration_seconds": duration, "full_av_decode_exit": decoded.returncode,
            "decoded_video_frames": int(progress["frame"]), "decoded_audio_samples": len(samples),
            "audio_rms": rms, "tone_peak_hz": peak, "tone_dominance": dominance,
            "screen_mean_absolute_error": error, "post_departure_marker_verified": True,
            "decoded_frame_path": str(image_path.relative_to(ROOT)),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.mark.asyncio
async def test_real_screen_track_survives_facilitator_departure_and_records_to_private_s3():
    assert Path(os.environ["BEEP_CONFIG_FILE"]).resolve() == ROOT / ".local/runtime.json"
    settings = LocalRecordingSettings()
    assert settings.livekit_url == "ws://127.0.0.1:7880", "local-only acceptance"
    assert settings.s3_endpoint == "http://127.0.0.1:8334", "local-only object store"
    assert settings.s3_egress_endpoint == "http://seaweed:8333", "local-only Egress upload"
    sid = str(uuid.uuid4())
    session = {"id": sid, "tenant_id": "synthetic-local-acceptance", "room_name": "beep-" + sid,
               "status": "active", "consent_epoch": 1,
               "client_consent": {"ai": True, "recording": True},
               "facilitator_consent": {"ai": True, "recording": True}}
    service = RecordingService(settings)
    prefix = service.prefix(session)
    reservation = {"id": uuid.uuid4().hex, "consent_epoch": 1, "room_name": session["room_name"]}
    reservation["output_prefix"] = prefix + "epoch-1-" + reservation["id"]
    session["recording_reservation"] = reservation
    exact_key = reservation["output_prefix"] + ".mp4"
    target = ROOT / ".local/verification" / ("recording-" + sid)
    target.mkdir(parents=True, exist_ok=False)
    rooms, streams = [], []
    running = True
    producer = None
    egress_id = None
    start_attempted = False
    report = {"synthetic_media": True, "paid_model_calls": 0, "session_id": sid,
              "room_name": session["room_name"], "private_bucket": settings.s3_bucket,
              "exact_prefix": prefix, "exact_key": exact_key, "reservation": reservation,
              "settings_source": "explicit_product_file_no_environment", "passed": False}

    def checkpoint(stage):
        report.update(stage=stage, updated_at=datetime.now(timezone.utc).isoformat())
        (target / "report.json").write_text(json.dumps(report, indent=2) + "\n")

    checkpoint("reserved_before_external_mutation")
    async with service._api() as lk:
        pre_rooms = await lk.room.list_rooms(api.ListRoomsRequest())
        pre_jobs = await lk.egress.list_egress(api.ListEgressRequest(active=True))
        assert not pre_rooms.rooms and not pre_jobs.items, "Refuse to overlap any existing room/job"
        try:
            created = await lk.room.create_room(api.CreateRoomRequest(name=session["room_name"]))
            assert created.name == session["room_name"]
            readback = await lk.room.list_rooms(api.ListRoomsRequest(names=[session["room_name"]]))
            assert [r.name for r in readback.rooms] == [session["room_name"]]
            checkpoint("room_created_and_read_back")
            for identity in ["facilitator:" + sid, "client:" + sid, "test-observer:" + sid]:
                token = (api.AccessToken(settings.livekit_api_key.get_secret_value(),
                                        settings.livekit_api_secret.get_secret_value())
                         .with_identity(identity).with_grants(api.VideoGrants(
                             room_join=True, room=session["room_name"], can_publish=True,
                             can_subscribe=True, can_publish_data=False)).to_jwt())
                room = rtc.Room()
                await asyncio.wait_for(room.connect(settings.livekit_url, token), timeout=20)
                rooms.append(room)
            facilitator, client, observer = rooms
            received = asyncio.Event()
            captured = {}

            @observer.on("track_subscribed")
            def subscribed(track, publication, participant):
                if publication.source == rtc.TrackSource.SOURCE_SCREENSHARE:
                    captured.update(track=track, participant=participant.identity)
                    received.set()

            source = rtc.VideoSource(640, 360, is_screencast=True)
            video = rtc.LocalVideoTrack.create_video_track("Synthetic workflow screen", source)
            await client.local_participant.publish_track(video, rtc.TrackPublishOptions(
                source=rtc.TrackSource.SOURCE_SCREENSHARE))
            audio_source = rtc.AudioSource(48000, 1)
            audio = rtc.LocalAudioTrack.create_audio_track("Synthetic 440 Hz tone", audio_source)
            await client.local_participant.publish_track(audio, rtc.TrackPublishOptions(
                source=rtc.TrackSource.SOURCE_MICROPHONE))
            await asyncio.wait_for(received.wait(), timeout=20)
            image = Image.new("RGBA", (640, 360), (248, 246, 243, 255))
            draw = ImageDraw.Draw(image)
            draw.text((25, 35), "SYNTHETIC LOCAL ACCEPTANCE - NOT A CLIENT WORKFLOW", fill=(45, 52, 54))
            draw.rectangle((570, 15, 615, 55), fill=(210, 35, 45))
            for bounds, color in [((30, 200, 180, 290), (210, 35, 45)),
                                  ((230, 200, 380, 290), (20, 200, 80)),
                                  ((430, 200, 580, 290), (35, 70, 210))]:
                draw.rectangle(bounds, fill=color)
            frame = rtc.VideoFrame(640, 360, rtc.VideoBufferType.RGBA, image.tobytes())
            samples_per_frame = 960

            async def publish_media():
                count = 0
                while running:
                    if count % 5 == 0:
                        source.capture_frame(frame)
                    tone = struct.pack("<" + "h" * samples_per_frame, *[
                        int(1000 * math.sin(2 * math.pi * 440 *
                            (count * samples_per_frame + i) / 48000)) for i in range(samples_per_frame)])
                    await audio_source.capture_frame(rtc.AudioFrame(tone, 48000, 1, samples_per_frame))
                    count += 1
                    await asyncio.sleep(0.02)

            producer = asyncio.create_task(publish_media())
            stream = rtc.VideoStream(captured["track"], capacity=1)
            streams.append(stream)
            first = await asyncio.wait_for(stream.__anext__(), timeout=15)
            assert (first.frame.width, first.frame.height) == (640, 360)
            assert captured["participant"] == "client:" + sid
            await facilitator.disconnect()
            draw.rectangle((570, 15, 615, 55), fill=(20, 200, 80))
            draw.text((25, 95), "AFTER FACILITATOR LEFT: client screen still publishing", fill=(45, 52, 54))
            draw.text((25, 130), "Synthetic 440 Hz tone + RGB content markers", fill=(45, 52, 54))
            frame = rtc.VideoFrame(640, 360, rtc.VideoBufferType.RGBA, image.tobytes())
            image.save(target / "source-after-departure.png")
            async with asyncio.timeout(10):
                while True:
                    event = await stream.__anext__()
                    rgba = event.frame.convert(rtc.VideoBufferType.RGBA)
                    pixel = Image.frombytes("RGBA", (rgba.width, rgba.height), bytes(rgba.data)).getpixel((590, 30))
                    if pixel[1] > 150 and pixel[0] < 80:
                        break
            participants = await lk.room.list_participants(api.ListParticipantsRequest(room=session["room_name"]))
            assert "facilitator:" + sid not in {p.identity for p in participants.participants}
            assert "client:" + sid in {p.identity for p in participants.participants}
            report["client_screen_after_facilitator_departure"] = True
            checkpoint("post_departure_screen_observed_over_rtc")
            started = time.monotonic()
            start_attempted = True
            try:
                egress_id = await service.start(session)
            except RecordingError as exc:
                egress_id = exc.egress_id
                report["egress_id"] = egress_id
                raise
            report["egress_id"] = egress_id
            session["egress_id"] = egress_id
            await service.check_active(session)
            report["egress_start_confirmed_active"] = True
            checkpoint("EGRESS_ACTIVE_exact_scope_verified")
            await asyncio.sleep(6)  # Deliberate media duration, not a readiness wait.
            await service.check_active(session)
            cleanup = await service.reconcile_stop(session, {"reservation": reservation, "egress_id": egress_id})
            assert list(cleanup) == [egress_id] and not cleanup.failed_ids
            assert len(cleanup.manifests) == 1
            manifest = cleanup.manifests[0]
            assert manifest.media_validation == "verified"
            assert manifest.validation_receipt.outcome == "verified"
            report["application_decode_manifest"] = manifest.model_dump(mode="json")
            report["application_decode_verified"] = True
            report["recording_seconds_test_wallclock"] = round(time.monotonic() - started, 2)
            listing = await lk.egress.list_egress(api.ListEgressRequest(egress_id=egress_id))
            info = next(i for i in listing.items if i.egress_id == egress_id)
            assert info.status == api.EGRESS_COMPLETE and info.room_name == session["room_name"]
            assert len(info.file_results) == 1 and info.file_results[0].filename == exact_key
            assert info.file_results[0].size > 0
            report.update(egress_complete=True, object_bytes=info.file_results[0].size)
            checkpoint("EGRESS_COMPLETE_exact_file_verified")
            with closing(service._s3()) as s3:
                head = s3.head_object(Bucket=settings.s3_bucket, Key=exact_key)
                assert head["ContentLength"] == info.file_results[0].size > 0
                report["authenticated_head"] = {"status": head["ResponseMetadata"]["HTTPStatusCode"],
                                                "content_length": head["ContentLength"]}
                url = settings.s3_endpoint + "/" + settings.s3_bucket + "/" + quote(exact_key, safe="/")
                with httpx.Client(timeout=5, trust_env=False) as anonymous:
                    report["anonymous_head_status"] = anonymous.head(url).status_code
                    report["anonymous_get_status"] = anonymous.get(url, headers={"Range": "bytes=0-63"}).status_code
                assert report["anonymous_head_status"] in {401, 403}
                assert report["anonymous_get_status"] in {401, 403}
                path = target / "synthetic-recording.mp4"
                s3.download_file(settings.s3_bucket, exact_key, str(path))
                assert path.stat().st_size == head["ContentLength"]
            report["recording_path"] = str(path.relative_to(ROOT))
            checkpoint("private_head_download_and_anonymous_denial_verified")
            report["media"] = await asyncio.to_thread(verify_decoded_media, path, image)
            report["media_verified"] = True
            checkpoint("actual_audio_video_decode_and_content_verified")
        except BaseException as exc:
            report["failure_class"] = type(exc).__name__  # Never provider text or request objects.
            checkpoint("failed_before_cleanup")
            raise
        finally:
            running = False
            if producer:
                producer.cancel()
                with suppress(asyncio.CancelledError):
                    await producer
            for stream in streams:
                await stream.aclose()
            for room in rooms:
                with suppress(Exception):
                    await room.disconnect()
            # Reconcile an ambiguous start by exact reserved room/key, never replay start.
            listing = await lk.egress.list_egress(api.ListEgressRequest(room_name=session["room_name"]))
            matched = [i for i in listing.items if i.room_name == session["room_name"] and any(
                f.filepath == exact_key and f.s3.bucket == settings.s3_bucket
                for f in i.room_composite.file_outputs)]
            report["cleanup_egress_ids"] = sorted({i.egress_id for i in matched})
            for info in matched:
                if info.status not in TERMINAL:
                    await service.stop(info.egress_id)
            verified = await lk.egress.list_egress(api.ListEgressRequest(room_name=session["room_name"]))
            assert all(i.status in TERMINAL for i in verified.items), "Recording shutdown is unconfirmed"
            report["terminal_egress_readback"] = [
                {"egress_id": i.egress_id, "status": api.EgressStatus.Name(i.status)} for i in verified.items]
            await lk.room.delete_room(api.DeleteRoomRequest(room=session["room_name"]))
            readback = await lk.room.list_rooms(api.ListRoomsRequest(names=[session["room_name"]]))
            assert not readback.rooms
            report["exact_room_absent"] = True
            active = await lk.egress.list_egress(api.ListEgressRequest(room_name=session["room_name"], active=True))
            assert not active.items
            report["exact_active_egress_count"] = len(active.items)
            # Unknown starts without an ID/listed terminal job are NOT assumed stopped.
            may_delete = bool(matched) or not start_attempted
            if may_delete:
                session.update(status="completed", recording_status="stopped")
                await service.delete(session)
                with closing(service._s3()) as s3:
                    versioned = s3.get_bucket_versioning(Bucket=settings.s3_bucket).get("Status") in {"Enabled", "Suspended"}
                    paginator = s3.get_paginator("list_object_versions" if versioned else "list_objects_v2")
                    remaining = sum(len(p.get("Contents", [])) + len(p.get("Versions", [])) +
                                    len(p.get("DeleteMarkers", [])) for p in paginator.paginate(
                                        Bucket=settings.s3_bucket, Prefix=prefix))
                    assert remaining == 0
                report.update(exact_prefix_object_count=remaining, remote_recording_deleted_and_readback_verified=True)
            else:
                report["cleanup_unresolved"] = True
                report["passed"] = False
            report["passed"] = bool(report.get("media_verified") and report.get("application_decode_verified") and
                                    report.get("remote_recording_deleted_and_readback_verified"))
            checkpoint("cleanup_read_back")
    assert report["passed"] and report.get("remote_recording_deleted_and_readback_verified")
    (ROOT / ".local/verification/real-media.json").write_text(json.dumps(report, indent=2) + "\n")
