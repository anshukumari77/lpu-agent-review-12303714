// Synthetic HTTP response for notice-binding tests; never a production fallback.
export const consentPolicyFixture = {
  schema_version: "1",
  notice_version: "synthetic-notice-v1",
  policy_id: "a".repeat(64),
  notice_hash: "b".repeat(64),
  provider_configuration_version: "1",
  provider_configuration_hash: "c".repeat(64),
  provider_configuration: {
    voice: { provider: "OpenAI", model: "synthetic-voice" },
  },
  deployment_policy_status: "unapproved",
  notice: {
    heading: "A review you control.",
    introduction:
      "Begin with your facilitator. After the 15-minute introduction, BEEP can ask questions and build an evidence-linked workflow review.",
    capture:
      "Nothing is captured until both participants consent, you connect, and you turn on a device. Share only the window or tab needed for this review; close unrelated or sensitive material first.",
    devices:
      "Your camera is never requested. Connecting does not turn on your microphone or share your screen.",
    ai_label:
      "I consent to AI processing of my voice, selected screen and review evidence by the providers named in this notice.",
    recording_label:
      "I consent to recording my voice and selected screen for this review and its evidence-linked report.",
    providers:
      "Voice: OpenAI, model synthetic-voice. Reasoning: OpenAI via Codex OAuth, model synthetic-reasoning. Media: LiveKit.",
    withdrawal:
      "You may withdraw either permission. Pause or finish stops new local capture immediately. It cannot retract material already received by a provider.",
    deployment_policy:
      "Deployment policy is UNAPPROVED. Do not use real client material until processing locations and retention terms are approved.",
  },
};
