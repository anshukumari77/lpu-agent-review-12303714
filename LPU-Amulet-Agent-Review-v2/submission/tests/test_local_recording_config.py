"""Focused local Egress regressions; no real infrastructure or credentials."""
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_local_egress_uses_native_platform_from_pinned_multiarch_image():
    """A forced amd64 browser aborts under emulation on the ARM local daemon."""
    compose = yaml.safe_load((ROOT / "compose.local.yaml").read_text())
    egress = compose["services"]["egress"]
    assert "platform" not in egress, "Allow the pinned multiarch image to select the native daemon platform"
    assert egress["image"] == (
        "livekit/egress@sha256:bf2b648b947349c3e9ff7aa8c718f00378d5c06af7624652a3653318e00333ce"
    )
    assert egress["cap_add"] == ["SYS_ADMIN"]
    assert egress["shm_size"] == "1gb"
