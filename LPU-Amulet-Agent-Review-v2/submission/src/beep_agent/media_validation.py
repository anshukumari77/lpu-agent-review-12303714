"""Bounded exact-object download and full local H.264/AAC MP4 decode.

No URLs reach the decoders. No shell, inherited credentials, media excerpts or
untrusted decoder text reach a report. This proves physical decoding, not screen
semantics, capture continuity, consent coverage or evidence offset correspondence.
"""
from __future__ import annotations

from array import array
import asyncio
from contextlib import closing, suppress
from datetime import datetime, timezone
from fractions import Fraction
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from .domain import DecoderProcessReceipt, MediaValidationReceipt, RecordingManifest

# Hard bounds, not paid-service/runtime configuration or fixture switches.
MAX_BYTES = 2_000_000_000
VALIDATION_TIMEOUT = 120.0
MAX_OUTPUT = 65_536
MAX_PCM_BYTES = 192_000_000  # 6000 seconds at mono 16 kHz, s16le.
CHUNK_BYTES = 65_536
TOOL_PATH = "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"


class _Rejected(Exception):
    pass


def _check(deadline, cancelled):
    if cancelled.is_set():
        raise _Rejected("cancelled")
    if time.monotonic() >= deadline:
        raise _Rejected("timeout")


def _run(args, deadline, cancelled, *, consume=None):
    """Drain both pipes incrementally; kill/reap the group on *every* exit.

    stdout can be streamed PCM; only small probe/progress text is retained.
    Process receipts hash actual bounded bytes, including nonzero-exit output.
    """
    _check(deadline, cancelled)
    output = [bytearray(), bytearray()]
    counts = [0, 0]
    hashes = [hashlib.sha256(), hashlib.sha256()]
    outcome = "complete"
    env = {"PATH": TOOL_PATH, "LANG": "C", "LC_ALL": "C", "AV_LOG_FORCE_NOCOLOR": "1"}
    process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=env, cwd="/", start_new_session=True, close_fds=True)
    try:
        with selectors.DefaultSelector() as selector:
            for index, pipe in enumerate((process.stdout, process.stderr)):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ, index)
            try:
                while selector.get_map():
                    _check(deadline, cancelled)
                    for key, _ in selector.select(timeout=0.05):
                        chunk = os.read(key.fd, CHUNK_BYTES)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        index = key.data
                        counts[index] += len(chunk)
                        hashes[index].update(chunk)
                        limit = MAX_PCM_BYTES if index == 0 and consume else MAX_OUTPUT
                        if counts[index] > limit:
                            raise _Rejected("output_limit")
                        if index == 0 and consume:
                            consume(chunk)
                        else:
                            output[index].extend(chunk)
                # A child can close its pipes without exiting. Never unbounded wait().
                while process.poll() is None:
                    _check(deadline, cancelled)
                    time.sleep(0.01)
            except _Rejected as exc:
                outcome = str(exc)
    finally:
        # SIGKILL is deliberate: no graceful decoder shutdown can retain a descendant
        # or keep the secret temporary file open after timeout/cancellation.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
    return DecoderProcessReceipt(returncode=process.returncode, outcome=outcome,
        stdout_bytes=counts[0], stderr_bytes=counts[1], stdout_sha256=hashes[0].hexdigest(),
        stderr_sha256=hashes[1].hexdigest()), bytes(output[0]), bytes(output[1])


def _require_process(receipt, failure):
    if receipt.outcome != "complete":
        raise _Rejected(receipt.outcome)
    if receipt.returncode != 0:
        raise _Rejected(failure)


class _Audio:
    def __init__(self):
        self.count = self.squares = self.peak = 0
        self.remainder = b""

    def consume(self, chunk):
        chunk = self.remainder + chunk
        size = len(chunk) // 2 * 2
        self.remainder = chunk[size:]
        samples = array("h")
        samples.frombytes(chunk[:size])
        if sys.byteorder != "little":
            samples.byteswap()
        self.count += len(samples)
        self.squares += sum(int(s) * int(s) for s in samples)
        self.peak = max(self.peak, max((abs(s) for s in samples), default=0))


def _object_matches(metadata, manifest):
    return (type(metadata.get("ContentLength")) is int
        and metadata["ContentLength"] == manifest.size_bytes
        and (not manifest.etag or metadata.get("ETag") == manifest.etag)
        and (not manifest.version_id or metadata.get("VersionId") == manifest.version_id))


def _download(factory, manifest, path, receipt, deadline, cancelled):
    # A VersionId of "null" is mutable in suspended/unversioned buckets: require ETag.
    versioned = bool(manifest.version_id and manifest.version_id != "null")
    if not versioned and not manifest.etag:
        raise _Rejected("identity_unbound")
    if manifest.size_bytes > MAX_BYTES:
        raise _Rejected("size_limit")
    request = {"Bucket": manifest.bucket, "Key": manifest.key}
    if versioned:
        request["VersionId"] = manifest.version_id
    if manifest.etag:
        request["IfMatch"] = manifest.etag
    digest = hashlib.sha256()
    try:
        with closing(factory()) as client:
            _check(deadline, cancelled)
            response = client.get_object(**request)
            with closing(response["Body"]) as body:
                if not _object_matches(response, manifest):
                    raise _Rejected("object_changed")
                # x creation, private directory plus 0600 file even with permissive umask.
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as target:
                    os.fchmod(target.fileno(), 0o600)
                    while True:
                        _check(deadline, cancelled)
                        chunk = body.read(min(CHUNK_BYTES, manifest.size_bytes - receipt["bytes_downloaded"] + 1))
                        if not chunk:
                            break
                        receipt["bytes_downloaded"] += len(chunk)
                        if receipt["bytes_downloaded"] > manifest.size_bytes:
                            raise _Rejected("size_mismatch")
                        digest.update(chunk)
                        target.write(chunk)
                if receipt["bytes_downloaded"] != manifest.size_bytes:
                    raise _Rejected("size_mismatch")
                receipt["sha256"] = digest.hexdigest()
            _check(deadline, cancelled)
            # Latest-object HEAD catches replacement even when the old pinned version
            # remained readable. A receipt is still a point-in-time fact, not retention.
            if not _object_matches(client.head_object(Bucket=manifest.bucket, Key=manifest.key), manifest):
                raise _Rejected("object_changed")
    except _Rejected:
        raise
    except Exception:
        raise _Rejected("download_failed") from None


def _decode(path, receipt, deadline, cancelled):
    tools = {name: shutil.which(name, path=TOOL_PATH) for name in ("ffprobe", "ffmpeg")}
    if not all(tools.values()):
        raise _Rejected("decoder_unavailable")
    for name, tool in tools.items():
        process, stdout, _ = _run([tool, "-version"], deadline, cancelled)
        _require_process(process, "decoder_unavailable")
        version = stdout.decode("utf-8", errors="replace").splitlines()[0]
        if not version.startswith(name + " version ") or len(version) > 500:
            raise _Rejected("decoder_unavailable")
        receipt[name + "_version"] = version
    # MP4 demuxing only. External data references/absolute aliases disabled; file
    # is the sole permitted input protocol. Codec allowlist prevents fallback.
    inputs = ["-max_alloc", "67108864", "-threads", "1", "-protocol_whitelist", "file",
        "-format_whitelist", "mov", "-codec_whitelist", "h264,aac", "-enable_drefs", "0",
        "-use_absolute_path", "0", "-err_detect", "explode", "-f", "mov", "-i", str(path)]
    probe, stdout, stderr = _run([tools["ffprobe"], "-v", "error", *inputs,
        "-show_entries", "stream=index,codec_name,codec_type,width,height,channels,sample_rate,nb_frames,avg_frame_rate,duration:format=duration",
        "-of", "json"], deadline, cancelled)
    receipt["probe"] = probe
    _require_process(probe, "probe_failed")
    if stderr:
        raise _Rejected("probe_failed")
    try:
        data = json.loads(stdout)
        streams = data["streams"]
        video = next(s for s in streams if s["codec_type"] == "video")
        audio = next(s for s in streams if s["codec_type"] == "audio")
        duration = float(data["format"]["duration"])
        if (len(streams) != 2 or video["codec_name"] != "h264" or audio["codec_name"] != "aac"
                or not 0 < video["width"] <= 1920 or not 0 < video["height"] <= 1080
                or not 0 < Fraction(video["avg_frame_rate"]) <= 60
                or not 0 < audio["channels"] <= 2 or not 0 < int(audio["sample_rate"]) <= 48000
                or not 0 < duration <= 6000):
            raise ValueError("outside bounded stream policy")
        expected_frames = int(video["nb_frames"])
        if expected_frames <= 0:
            raise ValueError("MP4 sample table frame count required")
    except (ValueError, KeyError, TypeError, StopIteration, ZeroDivisionError):
        raise _Rejected("stream_policy") from None
    receipt.update(video_codec="h264", audio_codec="aac", duration_seconds=duration)
    audio_measurement = _Audio()
    # Decode EVERY audio/video packet to EOF. Video frames go to the null muxer,
    # mono PCM is measured incrementally; no excerpt, stream copy or saved PCM.
    decode, _, progress = _run([tools["ffmpeg"], "-v", "error", "-nostdin", "-nostats",
        "-xerror", "-filter_threads", "1", *inputs, "-progress", "pipe:2", "-stats_period", "5",
        "-map", "0:v:0", "-an", "-sn", "-dn", "-threads", "1", "-fps_mode", "passthrough",
        "-f", "null", "/dev/null", "-map", "0:a:0", "-vn", "-sn", "-dn", "-threads", "1",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"],
        deadline, cancelled, consume=audio_measurement.consume)
    receipt["decode"] = decode
    _require_process(decode, "decode_failed")
    try:
        fields = dict(line.split("=", 1) for line in progress.decode("ascii").splitlines())
        frames = int(fields["frame"])
        if fields["progress"] != "end" or frames != expected_frames or audio_measurement.remainder:
            raise ValueError("decode did not reach all frames")
        if not audio_measurement.count:
            raise ValueError("no decoded samples")
    except (ValueError, KeyError, UnicodeError):
        raise _Rejected("decode_failed") from None
    receipt.update(video_frames=frames, audio_samples=audio_measurement.count,
        audio_rms=math.sqrt(audio_measurement.squares / audio_measurement.count) / 32768,
        audio_peak=audio_measurement.peak / 32768)
    if audio_measurement.peak == 0:
        raise _Rejected("silent_audio")


def _validate(factory, manifest, cancelled):
    deadline = time.monotonic() + VALIDATION_TIMEOUT
    receipt = dict(outcome="failed", failure_code="validation_failed", bytes_downloaded=0,
        object_etag=manifest.etag, object_version_id=manifest.version_id,
        validated_at=datetime.now(timezone.utc))
    try:
        with tempfile.TemporaryDirectory(prefix="beep-decode-") as folder:
            os.chmod(folder, 0o700)
            path = Path(folder) / "recording.mp4"
            _download(factory, manifest, path, receipt, deadline, cancelled)
            _decode(path, receipt, deadline, cancelled)
            _check(deadline, cancelled)
            with closing(factory()) as client:
                if not _object_matches(client.head_object(Bucket=manifest.bucket, Key=manifest.key), manifest):
                    raise _Rejected("object_changed")
            _check(deadline, cancelled)
            receipt.update(outcome="verified", failure_code=None)
    except _Rejected as exc:
        receipt.update(outcome="failed", failure_code=str(exc))
    except Exception:
        # Never expose provider text, paths, keys, media metadata or command output.
        receipt.update(outcome="failed", failure_code="validation_failed")
    receipt["validated_at"] = datetime.now(timezone.utc)
    return MediaValidationReceipt.model_validate(receipt)


async def verify_media(factory, manifest: RecordingManifest) -> RecordingManifest:
    manifest = RecordingManifest.model_validate(manifest)
    if manifest.artifact_status != "metadata_verified":
        return manifest
    cancelled = threading.Event()
    work = asyncio.create_task(asyncio.to_thread(_validate, factory, manifest, cancelled))
    try:
        receipt = await asyncio.shield(work)
    except asyncio.CancelledError:
        cancelled.set()
        # to_thread cancellation alone leaves the child/file running. Join the
        # cooperatively cancelled IO and killed process before propagating lease loss.
        while not work.done():
            with suppress(asyncio.CancelledError):
                await asyncio.shield(work)
        with suppress(Exception):
            work.result()
        raise
    return RecordingManifest.model_validate({**manifest.model_dump(),
        "media_validation": receipt.outcome, "validation_receipt": receipt})
