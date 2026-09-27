"""Image packaging contracts; no provider, database or app-server sessions.

Docker-backed checks are opt-in and use synthetic files only.
"""
import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_runtime_has_pinned_native_codex():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert 'VERSION = "0.153.2"' in dockerfile, "Selected OAuth runtime needs pinned Codex"
    assert "https://registry.npmjs.org/@openai/codex/-/" in dockerfile
    assert "COPY --from=codex /out/codex /usr/local/bin/codex" in dockerfile
    assert "@latest" not in dockerfile


@pytest.mark.skipif(
    os.environ.get("BEEP_CONTAINER_CONTEXT_TEST") != "1",
    reason="Opt-in Docker context check: synthetic files, no services or credentials",
)
def test_docker_context_excludes_private_files(tmp_path):
    """Ask Docker itself, not an approximate .dockerignore pattern interpreter."""
    context, output = tmp_path / "context", tmp_path / "export"
    context.mkdir()
    (context / "Dockerfile").write_text("FROM scratch\nCOPY . /\n")
    (context / ".dockerignore").write_text((ROOT / ".dockerignore").read_text())
    public = {
        "pyproject.toml", "uv.lock", "src/beep_agent/config.py",
        "src/beep_agent/codex_oauth.py", "web/package.json", "web/package-lock.json",
        "web/tsconfig.json", "web/vite.config.ts", "web/playwright.config.ts",
        "web/index.html", "web/src/index.tsx", "web/public/brand/logo.svg",
        "web/e2e/review.spec.ts",
    }
    private = {
        ".local/runtime.json", ".env", ".env.production", ".codex/auth.json",
        ".codex/config.toml", "auth.json", "runtime.json", "oauth.json",
        "unreviewed/private.txt", "docs/recording.mp4", "web/.env.production",
        "web/.npmrc", "web/dist/stale.js", "web/node_modules/private.txt",
        "web/src/.local/runtime.json", "web/src/.codex/auth.json",
        "web/src/.env.local", "web/public/auth.json", "web/public/auth.json.bak",
        "web/public/config.toml", "web/public/oauth-tokens.json",
        "web/public/service-account.credentials.json", "web/public/server.key",
        "web/public/certificate.pem", "src/beep_agent/.local/runtime.json",
        "src/beep_agent/.codex/auth.json", "src/beep_agent/.env.test",
        "src/beep_agent/__pycache__/config.pyc", "src/beep_agent/runtime.json",
    }
    for name in public | private:
        path = context / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("SYNTHETIC BUILD CONTEXT SENTINEL ONLY\n")
    result = subprocess.run(
        ["docker", "buildx", "build", "--network", "none", "--progress", "plain",
         "--output", f"type=local,dest={output}", str(context)],
        text=True, capture_output=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    observed = {p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()}
    assert not (private & observed), f"Private context paths leaked: {sorted(private & observed)}"
    assert public <= observed, f"Required build files missing: {sorted(public - observed)}"
    assert observed <= public | {"Dockerfile", ".dockerignore"}
    print(f"docker_context: {len(public)} required files included; "
          f"{len(private)} synthetic private paths excluded")


def test_container_keeps_frozen_nonroot_and_explicit_auth_contract():
    dockerfile = (ROOT / "Dockerfile").read_text()
    instructions = [line for line in dockerfile.splitlines()
                    if line.startswith(("COPY ", "ENV ", "USER ", "CMD ", "RUN "))]
    assert "USER beep" in instructions
    assert "--uid 10001 beep" in dockerfile
    assert "uv sync --frozen --no-dev" in dockerfile
    assert "npm ci" in dockerfile and "npm run build" in dockerfile
    assert "COPY --from=web /web/dist ./web/dist" in instructions
    assert not any("CODEX_HOME=" in line or "BEEP_CODEX_HOME=" in line
                   for line in instructions if line.startswith("ENV "))
    assert not any("/home/" in line or ".local" in line or ".codex" in line
                   for line in instructions if line.startswith("COPY "))
    assert "int.from_bytes(binary[18:20]" in dockerfile  # Real ELF architecture gate.
    assert "hashlib.sha256(data).hexdigest() == expected" in dockerfile
    assert "signal.alarm(180)" in dockerfile
    assert "--no-install-recommends ffmpeg=7:5.1.9-0+deb12u1" in dockerfile
    assert "extractall" not in dockerfile


# Import and --version only. Never create an app, provider, CodexRPC session or thread.
IMAGE_SMOKE_SCRIPT = r'''
import hashlib
import importlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys


def no_connect(event, args):
    if event in {"socket.connect", "socket.getaddrinfo"}:
        raise RuntimeError("Container packaging smoke prohibits network calls")


sys.addaudithook(no_connect)
assert os.getuid() == 10001 and os.geteuid() == 10001
for variable in ("HOME", "CODEX_HOME"):
    home = Path(os.environ[variable])
    assert str(home).startswith("/tmp/beep-smoke-")
    home.mkdir(mode=0o700)
    assert list(home.iterdir()) == []
assert not any(key.startswith(("BEEP_", "OPENAI_", "LIVEKIT_", "AWS_")) for key in os.environ)
assert not Path("/home/beep/.codex").exists()
assert not Path("/app/.local").exists()
assert not Path("/app/.env").exists()
cli = shutil.which("codex")
assert cli == "/usr/local/bin/codex"
version = subprocess.run([cli, "--version"], capture_output=True, text=True,
                         timeout=15, check=True)
assert version.stdout.strip() == "codex-cli 0.153.2"
uvicorn = subprocess.run(["/app/.venv/bin/uvicorn", "--version"],
                         capture_output=True, text=True, timeout=15, check=True)
modules = ["beep_agent", "beep_agent.api", "beep_agent.worker", "beep_agent.realtime",
           "beep_agent.config", "beep_agent.codex_rpc", "beep_agent.codex_oauth",
           "openai", "livekit.agents", "psycopg"]
for module in modules:
    importlib.import_module(module)
entries = {}
for entry in metadata.distribution("amulet-beep-agent").entry_points:
    if entry.group == "console_scripts":
        executable = Path("/app/.venv/bin") / entry.name
        assert executable.is_file() and os.access(executable, os.X_OK)
        assert callable(entry.load())  # Resolve the callable; do not call it.
        entries[entry.name] = entry.value
assert set(entries) == {"beep-api", "beep-worker", "beep-realtime"}
from beep_agent.config import Settings
assert Settings().codex_home is None  # The image does not implicitly select any auth home.
settings = Settings(inference_provider="codex", codex_home=Path(os.environ["CODEX_HOME"]))
assert settings.codex_home == Path(os.environ["CODEX_HOME"])
assert settings.inference_model == "gpt-5.5"
assert (settings.web_dist / "index.html").is_file()
assert any((settings.web_dist / "assets").glob("*.js"))
media_tools = {}
for tool in ("ffmpeg", "ffprobe"):
    executable = shutil.which(tool)
    assert executable is not None, f"Missing recording-validation executable: {tool}"
    result = subprocess.run([executable, "-version"], capture_output=True, text=True,
                            timeout=15, check=True)
    assert result.stdout.startswith(f"{tool} version ")
    media_tools[tool] = {"path": executable, "version": result.stdout.splitlines()[0]}
for tool in ("node", "npm", "rg", "bwrap", "zsh", "codex-code-mode-host"):
    assert shutil.which(tool) is None, f"Unneeded coding helper included: {tool}"
for name in ("LICENSE", "NOTICE"):
    assert (Path("/usr/local/share/licenses/codex") / name).is_file()
source = {p.relative_to("/app").as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
          for p in Path("/app/src/beep_agent").rglob("*") if p.is_file()}
for name in ("pyproject.toml", "uv.lock"):
    source[name] = hashlib.sha256((Path("/app") / name).read_bytes()).hexdigest()
print(json.dumps({"scope": "offline packaging smoke only; not runtime/E2E acceptance",
                  "uid": os.getuid(), "gid": os.getgid(), "python": platform.python_version(),
                  "architecture": platform.machine(), "codex": version.stdout.strip(),
                  "codex_stderr": version.stderr, "uvicorn": uvicorn.stdout.strip(),
                  "media_tools": media_tools,
                  "entrypoints": entries, "imports": modules,
                  "packages": {name:metadata.version(name) for name in
                               ["amulet-beep-agent", "openai", "uvicorn", "livekit-agents"]},
                  "codex_binary_sha256": hashlib.sha256(Path(cli).read_bytes()).hexdigest(),
                  "web_dist": str(settings.web_dist), "source_sha256": source,
                  "empty_homes_at_start": True,
                  "auth_files_created": [name for name in ("auth.json", "config.toml")
                                         if (Path(os.environ["CODEX_HOME"]) / name).exists()]}))
'''


@pytest.mark.skipif(
    not os.environ.get("BEEP_CONTAINER_SMOKE_IMAGE"),
    reason="Opt-in immutable local image smoke; --rm --network none, no config mounts",
)
def test_local_image_offline_smoke():
    image = os.environ["BEEP_CONTAINER_SMOKE_IMAGE"]
    inspect = subprocess.run(["docker", "image", "inspect", image],
                             capture_output=True, text=True, timeout=20, check=True)
    image_id = json.loads(inspect.stdout)[0]["Id"]
    result = subprocess.run(
        ["docker", "run", "--rm", "--pull", "never", "--network", "none", "--read-only",
         "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
         "--tmpfs", "/tmp:rw,nosuid,nodev,size=64m", "--env", "HOME=/tmp/beep-smoke-home",
         "--env", "CODEX_HOME=/tmp/beep-smoke-codex", "--entrypoint", "/app/.venv/bin/python",
         image_id, "-B", "-c", IMAGE_SMOKE_SCRIPT],
        capture_output=True, text=True, timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    observed = json.loads(result.stdout)
    assert observed["auth_files_created"] == []
    print(json.dumps(observed, sort_keys=True))
