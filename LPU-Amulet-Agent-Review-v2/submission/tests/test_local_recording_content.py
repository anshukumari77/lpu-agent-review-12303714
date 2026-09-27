"""Real ffmpeg decode/content regressions; synthetic files, no RTC/provider calls."""
import shutil
import subprocess

from PIL import Image, ImageDraw
import pytest

import test_local_media as acceptance


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg required")
@pytest.mark.parametrize("case", ["valid", "blank_video", "silent_audio", "wrong_tone"])
def test_decoded_media_requires_actual_screen_and_tone(tmp_path, monkeypatch, case):
    reference = Image.new("RGB", (640, 360), (248, 246, 243))
    draw = ImageDraw.Draw(reference)
    for bounds, color in [((570, 15, 615, 55), (20, 200, 80)),
                          ((30, 200, 180, 290), (210, 35, 45)),
                          ((230, 200, 380, 290), (20, 200, 80)),
                          ((430, 200, 580, 290), (35, 70, 210))]:
        draw.rectangle(bounds, fill=color)
    image = Image.new("RGB", reference.size) if case == "blank_video" else reference
    source = tmp_path / "source.png"
    image.save(source)
    path = tmp_path / "synthetic.mp4"
    audio = ("anullsrc=r=48000:cl=mono" if case == "silent_audio" else
             f"sine=frequency={880 if case == 'wrong_tone' else 440}:sample_rate=48000")
    result = subprocess.run(["ffmpeg", "-v", "error", "-y", "-loop", "1", "-i", str(source),
        "-f", "lavfi", "-i", audio, "-t", "4.2", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)], capture_output=True, timeout=30)
    assert result.returncode == 0
    monkeypatch.setattr(acceptance, "ROOT", tmp_path)
    if case == "valid":
        proof = acceptance.verify_decoded_media(path, reference)
        assert proof["tone_peak_hz"] == 440 and proof["post_departure_marker_verified"]
    else:
        message = {"blank_video": "does not match", "silent_audio": "silent", "wrong_tone": "lacks"}[case]
        with pytest.raises(AssertionError, match=message):
            acceptance.verify_decoded_media(path, reference)


def test_local_recording_settings_ignore_environment_overrides(tmp_path, monkeypatch):
    from beep_agent.localdev import prepare_local
    prepare_local(tmp_path)
    monkeypatch.setattr(acceptance, "ROOT", tmp_path)
    monkeypatch.setenv("BEEP_LIVEKIT_URL", "wss://unapproved.invalid")
    monkeypatch.setenv("BEEP_S3_ENDPOINT", "https://unapproved.invalid")
    monkeypatch.setenv("BEEP_RECORDING_ENABLED", "false")
    settings = acceptance.LocalRecordingSettings()
    assert settings.livekit_url == "ws://127.0.0.1:7880"
    assert settings.s3_endpoint == "http://127.0.0.1:8334"
    assert settings.recording_enabled is True
