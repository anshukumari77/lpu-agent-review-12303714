"""Real local FFmpeg decoding; only S3/SDK boundaries are synthetic doubles."""
import hashlib
import io
from pathlib import Path
import shutil
import subprocess

import pytest

from test_production_recording_identity import completed_info, reserved_session, service_with_head


@pytest.fixture(scope="module")
def media_files(tmp_path_factory):
    assert shutil.which("ffmpeg") and shutil.which("ffprobe"), "Real decoders are required"
    folder = tmp_path_factory.mktemp("decode-controls")
    commands = {}
    for name, audio, video in (("av", "sine=frequency=440:sample_rate=16000", True),
            ("silent", "anullsrc=r=16000:cl=mono", True),
            ("video", None, True), ("audio", "sine=frequency=440:sample_rate=16000", False)):
        command = [shutil.which("ffmpeg"), "-v", "error", "-nostdin", "-y"]
        if video:
            command += ["-f", "lavfi", "-i", "testsrc2=size=96x64:rate=10"]
        if audio:
            command += ["-f", "lavfi", "-i", audio]
        command += ["-t", "0.6", "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-movflags", "+faststart", str(folder / (name + ".mp4"))]
        subprocess.run(command, check=True, capture_output=True, timeout=20,
                       env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"})
        commands[name] = command
    (folder / "wrong.mp4").write_bytes(b"not a media file\n")
    data = (folder / "av.mp4").read_bytes()
    (folder / "truncated.mp4").write_bytes(data[:-400])
    (folder / "corrupt.mp4").write_bytes(data[:len(data)//2] + b"\xff" * (len(data)//2))
    return folder


class ObjectBoundary:
    """Version/conditional S3 read double; never contacts storage."""
    def __init__(self, data, *, version='synthetic-v1', etag='"synthetic-etag"'):
        self.data, self.version, self.etag = data, version, etag
        self.gets, self.heads, self.bodies = [], [], []
        self.on_get = None

    def head_object(self, **kwargs):
        self.heads.append(kwargs)
        return {"ContentLength": len(self.data), "ContentType": "video/mp4", "ETag": self.etag,
                **({"VersionId": self.version} if self.version else {})}

    def get_object(self, **kwargs):
        self.gets.append(kwargs)
        if self.on_get:
            self.on_get(kwargs)
        body = io.BytesIO(self.data)
        self.bodies.append(body)
        return {**self.head_object(**kwargs), "Body": body}

    def close(self):
        pass


def bound_service(monkeypatch, path, *, session=None, version="synthetic-v1"):
    service, _ = service_with_head(monkeypatch)
    session = session or reserved_session()
    boundary = ObjectBoundary(Path(path).read_bytes(), version=version)
    monkeypatch.setattr(service, "_s3", lambda: boundary)
    info = completed_info(session)
    info.file_results[0].size = len(boundary.data)
    info.file_results[0].duration = 600_000_000
    manifest = service._verify_files(info, session)
    return service, boundary, manifest


async def test_reserved_version_download_real_full_av_decode(monkeypatch, media_files, tmp_path):
    import beep_agent.media_validation as validation
    service, boundary, manifest = bound_service(monkeypatch, media_files / "av.mp4")
    monkeypatch.setattr(validation.tempfile, "tempdir", str(tmp_path))
    original = subprocess.Popen
    commands = []
    def observe(args, **kwargs):
        if "-i" in args:
            path = Path(args[args.index("-i") + 1])
            assert path.stat().st_mode & 0o777 == 0o600
            assert path.parent.stat().st_mode & 0o777 == 0o700
        assert kwargs["start_new_session"] is True
        assert not any(key.startswith(("AWS_", "BEEP_", "OPENAI_", "DYLD_")) for key in kwargs["env"])
        commands.append(args)
        return original(args, **kwargs)
    monkeypatch.setattr(validation.subprocess, "Popen", observe)
    verified = await service._verify_media(manifest)
    assert verified.media_validation == "verified"
    receipt = verified.validation_receipt
    assert receipt.outcome == "verified" and receipt.failure_code is None
    assert receipt.sha256 == hashlib.sha256(boundary.data).hexdigest()
    assert receipt.bytes_downloaded == len(boundary.data) == manifest.size_bytes
    assert receipt.video_frames == 6 and receipt.audio_samples > 0 and receipt.audio_rms > 0
    assert receipt.ffprobe_version.startswith("ffprobe version ")
    assert receipt.ffmpeg_version.startswith("ffmpeg version ")
    assert receipt.probe.returncode == receipt.decode.returncode == 0
    assert receipt.probe.stdout_bytes > 0 and receipt.decode.stdout_bytes > 0
    assert receipt.object_version_id == "synthetic-v1"
    assert boundary.gets == [{"Bucket": manifest.bucket, "Key": manifest.key, "VersionId": "synthetic-v1", "IfMatch": manifest.etag}]
    assert all(body.closed for body in boundary.bodies)
    assert not list(tmp_path.iterdir())
    for args in commands:
        if "-i" in args:
            assert args[args.index("-f") + 1] == "mov"
            assert args[args.index("-protocol_whitelist") + 1] == "file"
            assert args[args.index("-enable_drefs") + 1] == "0"
            assert args[args.index("-use_absolute_path") + 1] == "0"
            assert "-t" not in args and "-ss" not in args


@pytest.mark.parametrize("name,reason", [("wrong", "probe_failed"), ("truncated", None),
    ("corrupt", None), ("silent", "silent_audio"), ("video", "stream_policy"), ("audio", "stream_policy")])
async def test_bad_physical_media_never_verifies(monkeypatch, media_files, tmp_path, name, reason):
    import beep_agent.media_validation as validation
    service, boundary, manifest = bound_service(monkeypatch, media_files / (name + ".mp4"))
    monkeypatch.setattr(validation.tempfile, "tempdir", str(tmp_path))
    result = await service._verify_media(manifest)
    assert result.media_validation == "failed"
    assert result.validation_receipt.outcome == "failed"
    if reason:
        assert result.validation_receipt.failure_code == reason
    assert all(body.closed for body in boundary.bodies)
    assert not list(tmp_path.iterdir())


async def test_unversioned_download_requires_exact_etag(monkeypatch, media_files):
    service, boundary, manifest = bound_service(monkeypatch, media_files / "av.mp4", version=None)
    result = await service._verify_media(manifest)
    assert result.media_validation == "verified"
    assert boundary.gets == [{"Bucket": manifest.bucket, "Key": manifest.key, "IfMatch": manifest.etag}]


async def test_object_changed_while_decoder_ran_is_not_verified(monkeypatch, media_files):
    service, boundary, manifest = bound_service(monkeypatch, media_files / "av.mp4")
    original = subprocess.Popen
    def change(args, **kwargs):
        if "-progress" in args:
            boundary.version = "synthetic-replacement"
            boundary.etag = '"replacement"'
        return original(args, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", change)
    result = await service._verify_media(manifest)
    assert result.media_validation == "failed"
    assert result.validation_receipt.failure_code == "object_changed"
    assert result.validation_receipt.decode.returncode == 0


@pytest.mark.parametrize("fault,reason", [("no_binding", "identity_unbound"), ("null_version", "identity_unbound"),
    ("get_version", "object_changed"), ("get_etag", "object_changed"), ("short", "size_mismatch"),
    ("long", "size_mismatch"), ("get_error", "download_failed"), ("oversize", "size_limit")])
async def test_download_identity_and_byte_bounds(monkeypatch, media_files, tmp_path, fault, reason):
    import beep_agent.media_validation as validation
    service, boundary, manifest = bound_service(monkeypatch, media_files / "av.mp4")
    monkeypatch.setattr(validation.tempfile, "tempdir", str(tmp_path))
    original = boundary.get_object
    def get(**kwargs):
        if fault == "get_error":
            raise OSError("synthetic private provider text must never escape")
        response = original(**kwargs)
        if fault == "get_version":
            response["VersionId"] = "different"
        if fault == "get_etag":
            response["ETag"] = '"different"'
        if fault in {"short", "long"}:
            response["Body"].close()
            response["Body"] = io.BytesIO(boundary.data[:-1] if fault == "short" else boundary.data + b"x")
            boundary.bodies.append(response["Body"])
        return response
    monkeypatch.setattr(boundary, "get_object", get)
    if fault in {"no_binding", "null_version"}:
        manifest = manifest.model_copy(update={"version_id": "null" if fault == "null_version" else None, "etag": None})
    if fault == "oversize":
        monkeypatch.setattr(validation, "MAX_BYTES", 1)
    result = await service._verify_media(manifest)
    assert result.media_validation == "failed" and result.validation_receipt.failure_code == reason
    assert result.validation_receipt.probe is None
    assert not list(tmp_path.iterdir()) and all(body.closed for body in boundary.bodies)
    assert "private provider text" not in result.model_dump_json()
    if fault in {"no_binding", "null_version", "oversize"}:
        assert not boundary.gets


async def test_missing_decoder_fails_closed(monkeypatch, media_files, tmp_path):
    import beep_agent.media_validation as validation
    service, boundary, manifest = bound_service(monkeypatch, media_files / "av.mp4")
    monkeypatch.setattr(validation.tempfile, "tempdir", str(tmp_path))
    monkeypatch.setattr(validation.shutil, "which", lambda *a, **k: None)
    result = await service._verify_media(manifest)
    assert result.media_validation == "failed" and result.validation_receipt.failure_code == "decoder_unavailable"
    assert not list(tmp_path.iterdir())


async def test_cancellation_kills_decoder_and_joins_temp_cleanup(monkeypatch, media_files, tmp_path):
    import asyncio
    import sys
    import threading
    import beep_agent.media_validation as validation
    service, boundary, manifest = bound_service(monkeypatch, media_files / "av.mp4")
    monkeypatch.setattr(validation.tempfile, "tempdir", str(tmp_path))
    original = subprocess.Popen
    started = threading.Event()
    processes = []
    def hang(args, **kwargs):
        if "-i" in args:
            process = original([sys.executable, "-c", "import time; time.sleep(60)"], **kwargs)
            processes.append(process)
            started.set()
            return process
        return original(args, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", hang)
    task = asyncio.create_task(service._verify_media(manifest))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert processes and all(process.poll() == -9 for process in processes)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("failure", ["timeout", "output_limit"])
def test_process_budget_kills_and_reaps_real_process_group(tmp_path, failure):
    import sys
    import threading
    import time
    from beep_agent.media_validation import _run
    script = ("import time; time.sleep(60)" if failure == "timeout" else
              "import os,time; os.write(2,b'x'*100000); time.sleep(60)")
    receipt, _, _ = _run([sys.executable, "-c", script], time.monotonic() + 0.2, threading.Event())
    assert receipt.outcome == failure
    assert receipt.returncode == -9
    assert receipt.stderr_bytes <= 131072


async def test_parent_retained_mp4_decodes_locally_without_remote_claim(monkeypatch):
    import json
    root = Path(__file__).resolve().parents[1]
    attestation = json.loads((root / "docs/production-recording-parent-verification.json").read_text())
    path = root / attestation["artifact"]
    if not path.exists():
        pytest.skip("Parent's private retained local MP4 is not distributed with the repository")
    service, boundary, manifest = bound_service(monkeypatch, path)
    result = await service._verify_media(manifest)
    assert result.media_validation == "verified"
    receipt = result.validation_receipt
    assert receipt.sha256 == attestation["sha256"]
    assert receipt.bytes_downloaded == attestation["bytes"]
    assert receipt.video_frames == attestation["decoded_video_frames"]
    assert receipt.audio_rms > 0


@pytest.mark.parametrize("corruption", ["empty_output", "null_identity", "fake_version", "missing_segment_counts"])
async def test_corrupt_success_receipt_cannot_certify_media(monkeypatch, media_files, corruption):
    from pydantic import ValidationError
    from beep_agent.domain import RecordingManifest
    service, _, manifest = bound_service(monkeypatch, media_files / "av.mp4")
    result = await service._verify_media(manifest)
    data = result.model_dump(mode="json")
    receipt = data["validation_receipt"]
    if corruption == "empty_output":
        receipt["decode"]["stdout_bytes"] = 0
    if corruption == "null_identity":
        receipt.update(object_etag=None, object_version_id="null")
        data.update(etag=None, version_id="null")
    if corruption == "fake_version":
        receipt["ffmpeg_version"] = "metadata-only"
    if corruption == "missing_segment_counts":
        receipt["audio_samples"] = None
    with pytest.raises(ValidationError):
        RecordingManifest.model_validate(data)


async def test_cancel_during_bounded_download_waits_for_file_cleanup(monkeypatch, media_files, tmp_path):
    import asyncio
    import threading
    import beep_agent.media_validation as validation
    service, boundary, manifest = bound_service(monkeypatch, media_files / "av.mp4")
    monkeypatch.setattr(validation.tempfile, "tempdir", str(tmp_path))
    entered, release = threading.Event(), threading.Event()
    def block(_):
        entered.set()
        assert release.wait(3)
    boundary.on_get = block
    task = asyncio.create_task(service._verify_media(manifest))
    assert await asyncio.to_thread(entered.wait, 3)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()  # Cancellation must join the owned synchronous IO.
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 3)
    assert not list(tmp_path.iterdir()) and all(body.closed for body in boundary.bodies)


def test_real_process_group_descendant_is_killed_on_timeout():
    import os
    import sys
    import threading
    import time
    from beep_agent.media_validation import _run
    script = ("import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        "print(child.pid,flush=True); time.sleep(60)")
    receipt, stdout, _ = _run([sys.executable, "-c", script], time.monotonic() + 0.4, threading.Event())
    assert receipt.outcome == "timeout" and receipt.returncode == -9
    pid = int(stdout.strip())
    # A terminated orphan can briefly remain a zombie until the OS reaper runs.
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        state = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, timeout=2).stdout
        if "Z" in state:
            break
        time.sleep(0.02)
    else:
        raise AssertionError("decoder descendant survived process-group cleanup")


@pytest.mark.parametrize("mode", ["version_only", "null_with_etag"])
async def test_version_or_etag_binding_modes(monkeypatch, media_files, mode):
    service, boundary, manifest = bound_service(monkeypatch, media_files / "av.mp4")
    if mode == "version_only":
        boundary.etag = None
        manifest = manifest.model_copy(update={"etag": None})
        expected = {"Bucket": manifest.bucket, "Key": manifest.key, "VersionId": "synthetic-v1"}
    else:
        boundary.version = "null"
        manifest = manifest.model_copy(update={"version_id": "null"})
        expected = {"Bucket": manifest.bucket, "Key": manifest.key, "IfMatch": manifest.etag}
    result = await service._verify_media(manifest)
    assert result.media_validation == "verified"
    assert boundary.gets == [expected]
