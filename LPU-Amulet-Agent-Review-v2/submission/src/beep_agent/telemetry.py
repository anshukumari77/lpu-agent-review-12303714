"""Allowlisted telemetry: provider usage is measured, prices are not inferred."""

from functools import lru_cache
from importlib.metadata import version
import logging


def budget_capability(configured_budget_aud=None) -> dict:
    """Tokens are measured; no pricing/FX/reservations means no hard money cap."""
    return {
        "monetary_enforcement": "UNAVAILABLE",
        "configured_budget_aud": configured_budget_aud,
        "effective_hard_budget_aud": None,
        "budget_semantics": "advisory_only_no_pricing_or_spend_reservation",
    }


@lru_cache(maxsize=1)
def source_versions() -> dict[str, str]:
    return {
        name: version(name)
        for name in (
            "livekit-agents",
            "livekit-plugins-openai",
            "livekit",
            "openai",
            "langgraph",
            "langgraph-checkpoint-postgres",
        )
    }


def usage_record(metric, *, model: str, stage: str = "native_realtime") -> dict:
    data = metric.model_dump(mode="json")
    allowed = {
        key: data[key]
        for key in (
            "request_id",
            "timestamp",
            "duration",
            "session_duration",
            "ttft",
            "cancelled",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "input_token_details",
            "output_token_details",
        )
        if key in data
    }
    return {
        **allowed,
        **budget_capability(),
        "measurement": "measured",
        "stage": stage,
        "model": model,
        "source": "livekit.metrics.provider_usage",
        "versions": source_versions(),
    }


class SessionUsageDelta:
    """Convert 1.8 cumulative usage events to non-overlapping measured deltas."""

    def __init__(self):
        self.previous = {}

    def collect(self, usage) -> list[dict]:
        result = []
        for item in usage.model_usage:
            data = item.model_dump(mode="json")
            key = (data["type"], data["provider"], data["model"])
            old = self.previous.get(key, {})
            delta = {
                k: max(0, v - old.get(k, 0))
                for k, v in data.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            }
            self.previous[key] = data
            if any(delta.values()):
                result.append(
                    {
                        **delta,
                        **budget_capability(),
                        "model": data["model"],
                        "provider": data["provider"],
                        "usage_type": data["type"],
                        "measurement": "measured",
                        "stage": "native_realtime",
                        "source": "livekit.session_usage_updated.delta",
                        "versions": source_versions(),
                    }
                )
        return result


class SDKPrivacyFilter(logging.Filter):
    """SDK exceptions may contain speech, full request payloads, tokens or URLs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith(("livekit", "openai", "httpx", "httpcore", "aiohttp")):
            record.msg = "SDK event (payload withheld)"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
            # Remove nonstandard SDK extras; structured formatters can otherwise leak them.
            standard = logging.LogRecord("", 0, "", 0, "", (), None).__dict__
            for key in list(record.__dict__):
                if key not in standard:
                    del record.__dict__[key]
        return True


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    for handler in logging.getLogger().handlers:
        handler.addFilter(SDKPrivacyFilter())
    for name in ("livekit", "openai", "httpx", "httpcore", "aiohttp"):
        logging.getLogger(name).setLevel(logging.WARNING)
