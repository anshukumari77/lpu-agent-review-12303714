import type { Health } from "./types";

const labels: Record<string, string> = {
  database_url: "Review database",
  admin_token: "Operator access",
  signing_secret: "Invitation security",
  livekit_url: "LiveKit connection",
  livekit_api_key: "LiveKit connection",
  livekit_api_secret: "LiveKit connection",
  openai_api_key: "OpenAI API key (native voice)",
  codex_home: "Codex OAuth home selection",
  codex_executable: "Codex executable",
  codex_model: "Codex reasoning model",
  recording_enabled: "Recording must remain enabled",
  s3_bucket: "Recording storage",
  s3_region: "Recording storage",
  s3_access_key: "Recording storage",
  s3_secret_key: "Recording storage",
};
export function runtimeReadiness(health?: Health, error?: string) {
  const state = health?.readiness;
  const configured = state?.configured;
  const keys = [
    ...(Array.isArray(state?.blockers) ? state.blockers : []),
    ...(configured && typeof configured === "object"
      ? Object.entries(configured)
          .filter(([, value]) => value === false)
          .map(([key]) => key)
      : []),
  ];
  // Only predefined labels reach the UI; never echo arbitrary provider details.
  const missing = [
    ...new Set(
      keys.map((key) =>
        typeof key === "string" && Object.hasOwn(labels, key)
          ? labels[key]
          : "Service configuration",
      ),
    ),
  ];
  const ready =
    !error &&
    state?.ready === true &&
    state?.ok !== false &&
    missing.length === 0;
  const message = error
    ? "Service status unavailable. Check again before connecting."
    : !health
      ? "Checking service readiness before connecting…"
      : missing.length
        ? `Setup needed: ${missing.join(", ")}. Contact your operator.`
        : "Service setup is incomplete. Contact your operator.";
  return { ready, missing, message };
}
