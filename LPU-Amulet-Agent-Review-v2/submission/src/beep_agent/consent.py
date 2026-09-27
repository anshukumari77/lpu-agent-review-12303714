"""Versioned notice actually rendered by ConsentForm, not legal approval.

Hash contract: SHA-256 of UTF-8 JSON, sorted keys, compact separators,
ensure_ascii=False. Never serialise Settings or credentials into a receipt.
"""
import hashlib
import json

NOTICE_VERSION = "beep-consent-v1"
PROVIDER_CONFIGURATION_VERSION = "1"


def content_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def consent_policy(settings):
    """Allowlisted product configuration only; no authentication/provider IO."""
    voice = dict(provider="OpenAI", route="Realtime API", model=settings.realtime_model)
    reasoning = dict(provider="OpenAI", route="Codex OAuth" if settings.inference_provider == "codex"
                     else "Responses API", model=settings.inference_model)
    storage = "Operator-configured S3-compatible storage" if settings.s3_endpoint else "Amazon S3"
    providers = dict(voice=voice, reasoning_vision_report=reasoning,
                     transport_recording=dict(provider="LiveKit", recording_enabled=settings.recording_enabled),
                     storage=dict(provider=storage), retention_selection_days=settings.retention_days,
                     # Opaque routing reference detects changes without disclosing private
                     # buckets, endpoints (possibly credential-bearing) or local paths.
                     routing_ref=content_hash(dict(livekit_url=settings.livekit_url,
                         s3_endpoint=settings.s3_endpoint, s3_egress_endpoint=settings.s3_egress_endpoint,
                         s3_bucket=settings.s3_bucket, s3_region=settings.s3_region)))
    notice = dict(
        heading="A review you control.",
        introduction="Begin with your facilitator. After the 15-minute introduction, BEEP can ask questions and build an evidence-linked workflow review.",
        capture="Nothing is captured until both participants consent, you connect, and you turn on a device. Share only the window or tab needed for this review; close unrelated or sensitive material first.",
        devices="Your camera is never requested. Connecting does not turn on your microphone or share your screen.",
        ai_label="I consent to AI processing of my voice, selected screen and review evidence by the providers named in this notice.",
        recording_label="I consent to recording my voice and selected screen for this review and its evidence-linked report.",
        providers=(f"Voice: OpenAI via {voice['route']}, model {voice['model']}. "
                   f"Reasoning, screen interpretation and reporting: OpenAI via {reasoning['route']}, model {reasoning['model']}. "
                   f"Media transport and recording: LiveKit. Recording storage: {storage}. "
                   f"Recording configuration: {'enabled' if settings.recording_enabled else 'disabled'}; "
                   f"routing reference {providers['routing_ref']}. "
                   "These are selected services, not verification of their availability or deployment locations."),
        withdrawal="You may withdraw AI or recording permission separately. Withdrawal pauses the review. Pause or finish stops new local capture immediately. It cannot retract material already received by a provider.",
        deployment_policy=("Deployment policy is UNAPPROVED. Do not use real client material until the storage operator, "
            "processing and storage locations, provider retention/deletion terms and contact/withdrawal procedures are approved. "
            f"The configured application retention selector is {settings.retention_days} days; "
            "this is not a promise of scheduled deletion or provider-held retention. "
            "Consent receipts follow the session deletion lifecycle, not an indefinite retention exception."),
    )
    policy = dict(schema_version="1", notice_version=NOTICE_VERSION, notice=notice,
                  notice_hash=content_hash(notice),
                  provider_configuration_version=PROVIDER_CONFIGURATION_VERSION,
                  provider_configuration=providers, provider_configuration_hash=content_hash(providers),
                  deployment_policy_status="unapproved")
    return {**policy, "policy_id": content_hash(policy)}
