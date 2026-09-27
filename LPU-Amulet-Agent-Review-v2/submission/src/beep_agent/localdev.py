"""Project-local setup. Credentials stay in private files and are never logged."""

import asyncio
import getpass
import ipaddress
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from urllib.parse import urlsplit

import yaml

from .config import Settings

OAUTH_STATUS_TIMEOUT_SECONDS = 15


def prepare_local(root: Path, *, rtc_ip: str = "127.0.0.1") -> Path:
    address = ipaddress.ip_address(rtc_ip)
    if not (address.is_private or address.is_loopback):
        raise ValueError("Local RTC address must be private or loopback")
    root = Path(root).resolve()
    local = root / ".local"
    local.mkdir(mode=0o700, parents=True, exist_ok=True)
    local.chmod(0o700)
    path = local / "runtime.json"
    if path.is_symlink():
        raise ValueError("Refusing symlink configuration")
    if path.exists():
        return path
    infra = local / "infra"
    infra.mkdir(mode=0o700, exist_ok=True)
    sock = local / "pgsocket"
    data = {
        "database_url": f"postgresql:///beep_agent?host={sock}&port=55439",
        "admin_token": secrets.token_urlsafe(36),
        "signing_secret": secrets.token_urlsafe(48),
        "public_origin": "http://127.0.0.1:8094",
        "livekit_url": "ws://127.0.0.1:7880",
        "livekit_api_key": secrets.token_urlsafe(18),
        "livekit_api_secret": secrets.token_urlsafe(48),
        "openai_api_key": "",
        "s3_bucket": "beep-local-recordings",
        "s3_endpoint": "http://127.0.0.1:8334",
        "s3_egress_endpoint": "http://seaweed:8333",
        "s3_region": "us-east-1",
        "s3_access_key": secrets.token_urlsafe(24),
        "s3_secret_key": secrets.token_urlsafe(48),
        "recording_enabled": True,
        "secure_cookies": False,
        "allow_insecure_local": True,
    }
    livekit = {
        "port": 7880,
        "bind_addresses": ["0.0.0.0"],
        "rtc": {"tcp_port": 7881, "udp_port": 7882, "node_ip": rtc_ip,
                "use_external_ip": False},
        "redis": {"address": "redis:6379"},
        "keys": {data["livekit_api_key"]: data["livekit_api_secret"]},
        "logging": {"level": "warn"},
    }
    egress = {
        "api_key": data["livekit_api_key"], "api_secret": data["livekit_api_secret"],
        "ws_url": "ws://livekit:7880", "redis": {"address": "redis:6379"},
        "log_level": "warn", "insecure": True,
    }
    s3 = {"identities": [{"name": "beep-local", "credentials": [
        {"accessKey": data["s3_access_key"], "secretKey": data["s3_secret_key"]}],
        "actions": ["Admin", "Read", "List", "Tagging", "Write"]}]}
    files = {
        path: json.dumps(data, indent=2) + "\n",
        infra / "livekit.yaml": yaml.safe_dump(livekit),
        infra / "egress.yaml": yaml.safe_dump(egress),
        infra / "s3.json": json.dumps(s3, indent=2) + "\n",
    }
    for target, content in files.items():
        # The host directories remain private; container users need read-only
        # access to their individual mounts, not to product credentials.
        mode = 0o600 if target == path else 0o644
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "w") as output:
            output.write(content)
    return path


def ensure_local_bucket(settings, *, client=None, timeout=45):
    """Bootstrap only the isolated localhost object store; never create a cloud bucket."""
    from botocore.exceptions import ClientError, EndpointConnectionError
    if urlsplit(settings.s3_endpoint).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Local bucket setup requires a loopback S3 endpoint")
    from .recording import RecordingService
    own = client is None
    client = client or RecordingService(settings)._s3()
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                client.head_bucket(Bucket=settings.s3_bucket)
                return
            except EndpointConnectionError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Local object store did not become ready") from None
                time.sleep(.25)
            except ClientError as exc:
                if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                    raise RuntimeError("Local bucket access could not be verified") from None
                try:
                    client.create_bucket(Bucket=settings.s3_bucket)
                except Exception:
                    pass  # One mutation attempt; an uncertain acknowledgement is read back.
                try:
                    client.head_bucket(Bucket=settings.s3_bucket)
                except Exception:
                    raise RuntimeError("Local bucket creation could not be confirmed") from None
                return
    finally:
        if own:
            client.close()


def configure_openai(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError("Run local setup before configuring the model key")
    entered = getpass.getpass("Product OpenAI API key (hidden; blank cancels): ").strip()
    if not entered:
        return
    current = json.loads(path.read_text())
    current["openai_api_key"] = entered
    fd, filename = tempfile.mkstemp(prefix=".runtime-", suffix=".json", dir=path.parent)
    replacement = Path(filename)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(current, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        replacement.replace(path)
    finally:
        replacement.unlink(missing_ok=True)


async def inference_status(settings: Settings) -> dict:
    """Read-only operator diagnostics, not a prompt or runtime acceptance test."""
    from .codex_oauth import oauth_status

    configured = all(settings.inference_configuration().values())
    if settings.inference_provider == "openai":
        status = {"provider": "openai", "model": settings.inference_model,
                  "configured": configured, "authenticated": None,
                  "state": "locally_configured" if configured else "not_configured"}
    elif not configured:
        status = {"provider": "codex", "model": settings.inference_model,
                  "configured": False, "authenticated": False, "state": "not_configured"}
    else:
        status = {"provider": "codex", "model": settings.inference_model,
                  "configured": True, "authenticated": False, "state": "unavailable"}
        try:
            async with asyncio.timeout(OAUTH_STATUS_TIMEOUT_SECONDS):
                checked = await oauth_status(settings)
            state = checked.get("state")
            authenticated = checked.get("authenticated")
            if (checked.get("provider") == "codex" and checked.get("configured") is True
                    and isinstance(authenticated, bool)
                    and state in {"connected", "sign_in_required", "unavailable"}
                    and authenticated == (state == "connected")):
                status.update(authenticated=authenticated, state=state)
        except Exception:
            pass  # Provider details and exception strings are deliberately withheld.
    return {
        "reasoning": {**status, "inference_verified": False},
        "native_voice": {"provider": "openai", "model": settings.realtime_model,
                         "api_key_configured": bool(settings.openai_api_key.get_secret_value()),
                         "live_verified": False, "oauth_state": "experimental_unqualified"},
    }


async def configure_codex(path: Path, *, codex_home: Path, model: str = "gpt-5.5",
                          executable: str = "codex") -> dict:
    """Select an explicit official-CLI login; never copy its credentials."""
    settings = Settings(inference_provider="codex", codex_home=codex_home,
                        codex_model=model, codex_executable=executable)
    settings.require_inference()
    if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
        raise ValueError("Run local setup before configuring Codex")
    status = (await inference_status(settings))["reasoning"]
    if status.get("authenticated") is not True:
        raise RuntimeError("Codex authentication not confirmed; configuration unchanged")
    if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
        raise ValueError("Runtime configuration changed during authentication check")
    current = json.loads(path.read_text())
    selected = {"inference_provider": "codex", "codex_home": str(codex_home),
                "codex_model": model, "codex_executable": executable}
    current.update(selected)
    path.parent.chmod(0o700)
    fd, filename = tempfile.mkstemp(prefix=".runtime-", suffix=".json", dir=path.parent)
    replacement = Path(filename)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(current, output, indent=2)
            output.flush()
            os.fsync(output.fileno())
        replacement.replace(path)
        saved = json.loads(path.read_text())
        if any(saved.get(key) != value for key, value in selected.items()):
            raise RuntimeError("Codex configuration readback failed")
    finally:
        replacement.unlink(missing_ok=True)
    return status
