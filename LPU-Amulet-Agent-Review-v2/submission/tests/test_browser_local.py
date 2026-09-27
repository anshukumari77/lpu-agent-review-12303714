"""Real HTTP/PG/browser acceptance, isolated DB and random test-only auth."""
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest

pytestmark = pytest.mark.skipif(os.environ.get("BEEP_RUN_BROWSER_TESTS") != "1",
                                reason="explicit browser acceptance opt-in required")


def test_real_browser_operator_invites_consent_and_role_boundaries():
    root = Path(__file__).resolve().parents[1]
    dsn = os.environ["BEEP_TEST_DATABASE_URL"]
    parsed = conninfo_to_dict(dsn)
    assert parsed["dbname"].endswith("_test")
    assert parsed["host"] == str(root / ".local" / "pgsocket")
    schema = "browser_" + uuid.uuid4().hex
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    password = secrets.token_urlsafe(36)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("BEEP_", "OPENAI_", "LIVEKIT_", "AWS_"))}
    env.update(BEEP_DATABASE_URL=make_conninfo(dsn, options=f"-c search_path={schema}"),
               BEEP_ADMIN_TOKEN=password, BEEP_SIGNING_SECRET=secrets.token_urlsafe(48),
               BEEP_PUBLIC_ORIGIN=origin)
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "beep_agent.api:create_app",
        "--factory", "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
        cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail("Isolated API startup failed")
            try:
                with urllib.request.urlopen(origin + "/api/health", timeout=1) as reply:
                    if reply.status == 200:
                        break
            except OSError:
                time.sleep(.1)
        else:
            pytest.fail("Isolated API readiness timed out")
        result = subprocess.run(["node", "scripts/verify_browser.mjs"], cwd=root,
            input=json.dumps({"origin": origin, "admin_token": password,
                              "output_dir": str(root / "docs" / "screenshots" / "real-http")}),
            text=True, capture_output=True, timeout=150)
        safe = (result.stdout + result.stderr).replace(password, "[REDACTED]")
        assert result.returncode == 0, safe
        results = json.loads(result.stdout)
        assert {entry["engine"] for entry in results} == {"chromium", "webkit"}
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        with psycopg.connect(dsn, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
