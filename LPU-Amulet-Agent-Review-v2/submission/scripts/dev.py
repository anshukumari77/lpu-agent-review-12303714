"""Local development launcher. No credentials are printed or passed on the command line."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

from beep_agent.config import Settings
from beep_agent.localdev import configure_codex, configure_openai, ensure_local_bucket, inference_status, prepare_local

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description="Run the local BEEP application and its isolated services")
parser.add_argument("command", choices=["init", "up", "down", "status", "configure-openai", "configure-codex", "oauth-status", "copy-admin", "api", "worker", "realtime"])
parser.add_argument("--rtc-ip", default="127.0.0.1", help="Private host IP for Docker RTC; no change to HTTP API binding")
parser.add_argument("--codex-home", type=Path, help="Explicit absolute Codex home; never selected implicitly")
parser.add_argument("--codex-executable", default="codex", help="Official Codex executable")
parser.add_argument("--model", default="gpt-5.5", help="Codex reasoning model, not a native voice model")
args = parser.parse_args()
if args.command == "configure-codex" and args.codex_home is None:
    parser.error("configure-codex requires --codex-home /explicit/absolute/path")
config_file = ROOT / ".local" / "runtime.json"
os.environ["BEEP_CONFIG_FILE"] = str(config_file)

if args.command in {"init", "up"}:
    prepare_local(ROOT, rtc_ip=args.rtc_ip)
    local = ROOT / ".local"
    pg = local / "pgdata"
    sock = local / "pgsocket"
    sock.mkdir(mode=0o700, exist_ok=True)
    if not (pg / "PG_VERSION").exists():
        subprocess.run(["initdb", "-D", str(pg), "--auth-local=trust", "--auth-host=scram-sha-256", "--encoding=UTF8", "--locale=C"], check=True)
    running = subprocess.run(["pg_ctl", "-D", str(pg), "status"], capture_output=True).returncode == 0
    if not running:
        subprocess.run(["pg_ctl", "-D", str(pg), "-l", str(local / "postgres.log"), "-o", f"-k {shlex.quote(str(sock))} -p 55439 -h ''", "-w", "start"], check=True)
    import psycopg
    from psycopg import sql
    with psycopg.connect(f"dbname=postgres host={sock} port=55439", autocommit=True) as connection:
        for database in ["beep_agent", "beep_agent_test", "beep_agent_runtime_test", "beep_agent_integration_test"]:
            if not connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database,)).fetchone():
                connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
    if args.command == "up":
        env = {**os.environ, "BEEP_RTC_IP": args.rtc_ip}
        subprocess.run(["docker", "compose", "-f", str(ROOT / "compose.local.yaml"), "up", "-d"], env=env, check=True)
        ensure_local_bucket(Settings())
    print("Local services prepared. Product credentials remain private. Run status for configuration checks.")
elif args.command == "down":
    subprocess.run(["docker", "compose", "-f", str(ROOT / "compose.local.yaml"), "down"], check=True)
    subprocess.run(["pg_ctl", "-D", str(ROOT / ".local" / "pgdata"), "-m", "fast", "-w", "stop"], check=True)
elif args.command == "configure-openai":
    configure_openai(config_file)
    print("Secure setup finished. No model request was made.")
elif args.command == "configure-codex":
    try:
        asyncio.run(configure_codex(config_file, codex_home=args.codex_home,
                                    model=args.model, executable=args.codex_executable))
    except Exception:
        raise SystemExit("Codex setup could not be confirmed. Run init first, select an absolute home, "
                         "and check official CLI sign-in. See docs/oauth-setup-verification.md.") from None
    print("Codex authentication checked; reasoning configuration saved. No inference or voice test was made. "
          "Restart API and workers. Environment overrides still take precedence.")
elif args.command == "oauth-status":
    try:
        settings = (Settings(inference_provider="codex", codex_home=args.codex_home,
                             codex_model=args.model, codex_executable=args.codex_executable)
                    if args.codex_home is not None else Settings())
        print(json.dumps(asyncio.run(inference_status(settings)), indent=2))
    except Exception:
        raise SystemExit("Inference status unavailable; configuration details withheld.") from None
elif args.command == "copy-admin":
    settings = Settings()
    if not settings.admin_token.get_secret_value():
        raise SystemExit("Run init first")
    if sys.platform != "darwin":
        raise SystemExit("Clipboard helper is macOS-only. Open the private runtime configuration locally.")
    subprocess.run(["pbcopy"], input=settings.admin_token.get_secret_value(), text=True, check=True)
    print("Operator password copied to your clipboard. Paste into the local login, then clear your clipboard.")
elif args.command == "status":
    print(json.dumps(Settings().readiness(), indent=2))
else:
    settings = Settings()
    if not settings.database_url or not settings.admin_token.get_secret_value():
        raise SystemExit("Run init first")
    if args.command == "realtime":
        os.environ["LIVEKIT_URL"] = settings.livekit_url
        os.environ["LIVEKIT_API_KEY"] = settings.livekit_api_key.get_secret_value()
        os.environ["LIVEKIT_API_SECRET"] = settings.livekit_api_secret.get_secret_value()
    commands = {
        "api": ["-m", "uvicorn", "beep_agent.api:create_app", "--factory", "--host", "127.0.0.1", "--port", "8094"],
        "worker": ["-m", "beep_agent.worker"],
        "realtime": ["-m", "beep_agent.realtime", "dev"],
    }
    os.chdir(ROOT)
    os.execv(sys.executable, [sys.executable, *commands[args.command]])
