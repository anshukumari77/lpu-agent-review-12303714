import subprocess
import sys
from pathlib import Path


def test_live_oauth_script_never_runs_without_explicit_permission(tmp_path):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run([sys.executable, str(root / "scripts/verify_oauth.py"), "--codex-home", str(tmp_path)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "--allow-live" in result.stderr
    assert not (root / "docs/oauth-live-verification.json").exists() or result.stdout == ""
