"""OAuth setup regressions: explicitly fake account metadata, never real account access."""
import json
import stat

import pytest

from beep_agent import codex_oauth, localdev
from beep_agent.config import Settings
from test_api import ADMIN, ORIGIN, login, exchange, new_session
from test_store import store as store_fixture

store = store_fixture


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    import os
    for name in list(os.environ):
        if (name.startswith("BEEP_") and not name.startswith("BEEP_TEST_")) or name == "OPENAI_API_KEY":
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def fake_oauth(monkeypatch):
    calls = []

    async def fake_status(settings):
        calls.append(settings)
        return {"provider": "codex", "configured": True, "authenticated": True,
                "state": "connected", "model": settings.codex_model}

    monkeypatch.setattr(codex_oauth, "oauth_status", fake_status)
    return calls


async def test_configure_codex_checks_fake_auth_before_private_atomic_update(tmp_path, fake_oauth, capsys):
    path = localdev.prepare_local(tmp_path)
    original = json.loads(path.read_text())
    home = tmp_path / "explicit-synthetic-codex-home"
    home.mkdir()
    inode = path.stat().st_ino
    assert hasattr(localdev, "configure_codex"), "Explicit OAuth setup is missing"
    status = await localdev.configure_codex(path, codex_home=home, model="gpt-5.5")
    assert len(fake_oauth) == 1
    assert fake_oauth[0].inference_provider == "codex"
    assert fake_oauth[0].codex_home == home
    assert status["authenticated"] is True
    saved = json.loads(path.read_text())
    assert saved == {**original, "inference_provider": "codex", "codex_home": str(home),
                     "codex_model": "gpt-5.5", "codex_executable": "codex"}
    assert path.stat().st_ino != inode
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert not list(home.iterdir())  # BEEP never reads/writes a token or login file.
    assert capsys.readouterr().out == ""


@pytest.fixture
def operator_client(store, tmp_path):
    from fastapi.testclient import TestClient
    from beep_agent.api import create_app
    settings = Settings(database_url=store.database_url, admin_token=ADMIN,
                        signing_secret="synthetic-test-signing-secret-0123456789",
                        inference_provider="codex", codex_home=tmp_path,
                        openai_api_key="", web_dist=tmp_path)
    with TestClient(create_app(settings, store), base_url=ORIGIN,
                    headers={"Origin": ORIGIN}) as client:
        yield client


def test_operator_status_checks_fake_oauth_only_after_role_auth(operator_client, fake_oauth):
    client = operator_client
    assert client.get("/api/operator/inference").status_code == 401
    assert fake_oauth == []
    login(client)
    response = client.get("/api/operator/inference")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "reasoning": {"provider": "codex", "model": "gpt-5.5", "configured": True,
                      "authenticated": True, "state": "connected", "inference_verified": False},
        "native_voice": {"provider": "openai", "model": "gpt-realtime",
                         "api_key_configured": False, "live_verified": False,
                         "oauth_state": "experimental_unqualified"},
    }
    assert len(fake_oauth) == 1
    created = new_session(client)
    for role in ("client", "facilitator"):
        exchange(client, created["invitations"][role])
        assert client.get("/api/operator/inference").status_code == 403
    assert len(fake_oauth) == 1


@pytest.mark.parametrize("key", ["", "synthetic-never-live-checked-key"])
async def test_openai_status_is_local_configuration_not_auth_verification(key, fake_oauth):
    result = await localdev.inference_status(Settings(openai_api_key=key))
    assert fake_oauth == []
    assert result["reasoning"] == {
        "provider": "openai", "model": "gpt-5.4-mini", "configured": bool(key),
        "authenticated": None, "state": "locally_configured" if key else "not_configured",
        "inference_verified": False,
    }
    assert result["native_voice"]["api_key_configured"] is bool(key)
    assert result["native_voice"]["live_verified"] is False


async def test_codex_status_without_explicit_home_does_not_touch_account(fake_oauth):
    result = await localdev.inference_status(Settings(inference_provider="codex", codex_home=None))
    assert fake_oauth == []
    assert result["reasoning"]["state"] == "not_configured"
    assert result["reasoning"]["authenticated"] is False


@pytest.mark.parametrize("outcome", ["exception", "hang", "extra_fields", "inconsistent"])
async def test_status_is_bounded_allowlisted_and_never_leaks_account_details(tmp_path, monkeypatch, outcome):
    import asyncio
    marker = "SYNTHETIC-PRIVATE-ACCOUNT-DETAIL"

    async def fake_status(settings):
        if outcome == "exception":
            raise RuntimeError(marker)
        if outcome == "hang":
            await asyncio.sleep(60)
        return {"provider": "codex", "configured": True, "authenticated": True,
                "state": "connected" if outcome == "extra_fields" else "sign_in_required",
                "model": marker, "identity": marker, "access_token": marker}

    monkeypatch.setattr(codex_oauth, "oauth_status", fake_status)
    monkeypatch.setattr(localdev, "OAUTH_STATUS_TIMEOUT_SECONDS", .01, raising=False)
    result = await asyncio.wait_for(localdev.inference_status(
        Settings(inference_provider="codex", codex_home=tmp_path)), .25)
    assert marker not in json.dumps(result)
    connected = outcome == "extra_fields"
    assert result["reasoning"]["authenticated"] is connected
    assert result["reasoning"]["state"] == ("connected" if connected else "unavailable")


@pytest.mark.parametrize("state", ["sign_in_required", "unavailable", "not_configured", "inconsistent"])
async def test_configure_refuses_unconfirmed_auth_without_touching_runtime(tmp_path, monkeypatch, state):
    path = localdev.prepare_local(tmp_path)
    before = path.read_bytes()

    async def fake_status(settings):
        return {"provider": "codex", "configured": True, "authenticated": state == "inconsistent",
                "state": "sign_in_required" if state == "inconsistent" else state,
                "model": settings.codex_model}

    monkeypatch.setattr(codex_oauth, "oauth_status", fake_status)
    with pytest.raises(RuntimeError, match="authentication not confirmed"):
        await localdev.configure_codex(path, codex_home=tmp_path)
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".runtime-*"))


def run_dev_script(tmp_path, monkeypatch, *arguments):
    """Run the real script in a temporary product root, never the user's .local."""
    import runpy
    import shutil
    import sys
    from pathlib import Path
    script = tmp_path / "scripts" / "dev.py"
    script.parent.mkdir(exist_ok=True)
    shutil.copyfile(Path(__file__).resolve().parents[1] / "scripts" / "dev.py", script)
    monkeypatch.setattr(sys, "argv", [str(script), *arguments])
    monkeypatch.setenv("BEEP_CONFIG_FILE", str(tmp_path / ".local" / "runtime.json"))
    runpy.run_path(str(script), run_name="__main__")


def test_cli_configure_then_status_reads_selected_provider_without_model_requests(
        tmp_path, monkeypatch, fake_oauth, capsys):
    path = localdev.prepare_local(tmp_path)
    home = tmp_path / "synthetic-cli-home"
    home.mkdir()
    run_dev_script(tmp_path, monkeypatch, "configure-codex", "--codex-home", str(home),
                   "--model", "gpt-5.5")
    assert json.loads(path.read_text())["inference_provider"] == "codex"
    assert "No inference or voice test" in capsys.readouterr().out
    run_dev_script(tmp_path, monkeypatch, "oauth-status")
    status = json.loads(capsys.readouterr().out)
    assert status["reasoning"]["state"] == "connected"
    assert status["native_voice"]["api_key_configured"] is False
    assert len(fake_oauth) == 2


def test_cli_configure_requires_explicit_home_without_account_access(tmp_path, monkeypatch, fake_oauth, capsys):
    with pytest.raises(SystemExit):
        run_dev_script(tmp_path, monkeypatch, "configure-codex")
    assert "--codex-home" in capsys.readouterr().err
    assert fake_oauth == []


async def test_codex_refuses_symlink_private_directory_before_account_check(tmp_path, fake_oauth):
    actual = localdev.prepare_local(tmp_path / "actual")
    linked = tmp_path / "linked"
    linked.symlink_to(actual.parent, target_is_directory=True)
    before = actual.read_bytes()
    with pytest.raises(ValueError, match="setup"):
        await localdev.configure_codex(linked / "runtime.json", codex_home=tmp_path)
    assert fake_oauth == []
    assert actual.read_bytes() == before


async def test_atomic_replace_failure_preserves_previous_runtime(tmp_path, monkeypatch):
    from pathlib import Path
    path = localdev.prepare_local(tmp_path)
    before = path.read_bytes()

    def fail_replace(self, target):
        raise OSError("synthetic atomic replacement failure")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError):
        await localdev.configure_codex(path, codex_home=tmp_path)
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".runtime-*"))
