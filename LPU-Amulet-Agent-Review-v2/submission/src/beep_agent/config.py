"""Explicit product configuration; never borrow credentials from another application."""
import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, JsonConfigSettingsSource, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="BEEP_", extra="ignore", populate_by_name=True, env_file=None
    )

    database_url: str = Field(default="", repr=False)
    admin_token: SecretStr = SecretStr("")
    signing_secret: SecretStr = SecretStr("")
    public_origin: str = "http://127.0.0.1:8094"
    livekit_url: str = "ws://127.0.0.1:7880"
    livekit_api_key: SecretStr = SecretStr("")
    livekit_api_secret: SecretStr = SecretStr("")
    openai_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("BEEP_OPENAI_API_KEY", "OPENAI_API_KEY", "openai_api_key"),
    )
    realtime_model: str = "gpt-realtime"
    planner_model: str = "gpt-5.4-mini"
    inference_provider: Literal["openai", "codex"] = "openai"
    codex_home: Path | None = None
    codex_executable: str = "codex"
    codex_model: str = "gpt-5.5"
    voice: str = "marin"
    agent_name: str = "beep-reviewer"
    max_session_seconds: int = Field(default=5400, ge=1, le=5400)
    introduction_seconds: int = Field(default=900, ge=0, le=5400)
    model_rollover_seconds: int = Field(default=3000, ge=1, le=3300)
    max_concurrent_sessions: int = Field(default=10, ge=1, le=500)
    invitation_ttl_seconds: int = Field(default=86400, ge=60, le=604800)
    session_budget_aud: float = Field(default=60, gt=0, le=1000)
    recording_enabled: bool = True
    s3_bucket: str = ""
    s3_endpoint: str = ""
    s3_egress_endpoint: str = ""
    s3_region: str = "ap-southeast-2"
    s3_access_key: SecretStr = SecretStr("")
    s3_secret_key: SecretStr = SecretStr("")
    retention_days: int = Field(default=30, ge=1, le=365)
    secure_cookies: bool = False
    allow_insecure_local: bool = True
    web_dist: Path = Path(__file__).resolve().parents[2] / "web" / "dist"
    worker_poll_seconds: float = Field(default=0.5, ge=0.05, le=30)

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings, file_secret_settings
    ):
        path = os.environ.get("BEEP_CONFIG_FILE")
        product_file = JsonConfigSettingsSource(settings_cls, json_file=path) if path else lambda: {}
        return init_settings, env_settings, product_file

    @model_validator(mode="after")
    def validate_origin(self):
        origin = urlsplit(self.public_origin)
        local = origin.hostname in {"127.0.0.1", "localhost", "::1"}
        if origin.scheme not in {"http", "https"} or not origin.hostname:
            raise ValueError("public_origin must be an HTTP(S) origin")
        if origin.username or origin.password or origin.query or origin.fragment:
            raise ValueError("public_origin must not contain credentials, query or fragment")
        if origin.path not in {"", "/"}:
            raise ValueError("public_origin must not contain a path")
        if not local and (not self.secure_cookies or origin.scheme != "https"):
            raise ValueError("Non-local origins require HTTPS and secure cookies")
        if not self.secure_cookies and not (local and self.allow_insecure_local):
            raise ValueError("Insecure cookies are permitted only in explicit local mode")
        self.public_origin = self.public_origin.rstrip("/")
        return self

    @property
    def inference_model(self) -> str:
        return self.codex_model if self.inference_provider == "codex" else self.planner_model

    def inference_configuration(self) -> dict[str, bool]:
        """Local configuration only, never evidence of authentication or model access."""
        if self.inference_provider == "codex":
            return {
                "codex_home": self.codex_home is not None and self.codex_home.is_absolute(),
                "codex_executable": bool(self.codex_executable.strip()),
                "codex_model": bool(self.codex_model.strip()),
            }
        return {"openai_api_key": bool(self.openai_api_key.get_secret_value())}

    def require_inference(self) -> None:
        blockers = [key for key, present in self.inference_configuration().items() if not present]
        if blockers:
            raise RuntimeError("Inference configuration missing: " + ", ".join(blockers))

    def require_runtime(self) -> None:
        blockers = self.readiness()["blockers"]
        if blockers:
            raise RuntimeError("Runtime configuration missing: " + ", ".join(blockers))

    def readiness(self) -> dict:
        # Native voice still requires its own API key, even with OAuth reasoning.
        required = ["database_url", "admin_token", "signing_secret", "livekit_url",
                    "livekit_api_key", "livekit_api_secret", "openai_api_key"]
        if self.recording_enabled:
            required.extend(["s3_bucket", "s3_region", "s3_access_key", "s3_secret_key"])
        configured = {}
        for key in required:
            value = getattr(self, key)
            configured[key] = bool(value.get_secret_value() if isinstance(value, SecretStr) else value)
        configured.update(self.inference_configuration())
        blockers = [key for key, present in configured.items() if not present]
        if not self.recording_enabled:
            blockers.append("recording_enabled")
        return {"ready": not blockers, "configuration_only": True,
                "configured": configured, "blockers": blockers}
