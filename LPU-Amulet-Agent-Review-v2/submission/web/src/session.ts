import type { Session, SessionStatus } from "./types";
export function hasConsent(consent: Session["client_consent"]) {
  return (
    consent === true ||
    (typeof consent === "object" &&
      consent !== null &&
      consent.ai === true &&
      consent.recording === true)
  );
}
export function captureEligible(session: Session) {
  return (
    ["introduction", "active"].includes(session.status) &&
    hasConsent(session.client_consent) &&
    hasConsent(session.facilitator_consent)
  );
}
export function clocks(session: Session, now: number) {
  const started = session.started_at ? Date.parse(session.started_at) : now;
  const elapsed = Number.isFinite(started)
    ? Math.max(0, Math.floor((now - started) / 1000))
    : 0;
  return {
    introRemaining: Math.max(0, session.intro_seconds - elapsed),
    totalRemaining: Math.max(0, session.max_seconds - elapsed),
  };
}
export function duration(seconds: number) {
  const n = Math.max(0, Math.floor(seconds));
  return `${Math.floor(n / 60)
    .toString()
    .padStart(2, "0")}:${(n % 60).toString().padStart(2, "0")}`;
}
export function statusLabel(status: SessionStatus) {
  return (
    (
      {
        awaiting_consent: "Awaiting consent",
        introduction: "Human introduction",
        active: "AI-led review",
        paused: "Paused",
        finalising: "Preparing report",
        completed: "Review complete",
        partial: "Partial review",
        failed: "Review interrupted",
      } as const
    )[status] ?? "Unknown state"
  );
}
export function dateLabel(value: string) {
  const date = new Date(value);
  return Number.isFinite(date.getTime())
    ? new Intl.DateTimeFormat("en-AU", {
        dateStyle: "medium",
        timeStyle: "short",
      }).format(date)
    : "Date unavailable";
}
