import { describe, it, expect } from "vitest";
import { hasConsent, clocks, statusLabel, captureEligible } from "./session";
import type { Session } from "./types";
const session = {
  started_at: "2026-09-11T00:00:00Z",
  intro_seconds: 900,
  max_seconds: 5400,
  status: "introduction",
  client_consent: true,
  facilitator_consent: true,
} as Session;
describe("server-owned session phases", () => {
  it("never substitutes local elapsed time for server handover", () => {
    const time = clocks(session, Date.parse("2026-09-11T00:15:00Z"));
    expect(time.introRemaining).toBe(0);
    expect(time.totalRemaining).toBe(4500);
    expect(statusLabel(session.status)).toBe("Human introduction");
    expect(
      clocks(session, Date.parse("2026-09-10T23:59:00Z")).introRemaining,
    ).toBe(900);
  });
  it("fails capture closed on missing consent or paused/finalising phases", () => {
    expect(hasConsent({ ai: true, recording: false })).toBe(false);
    expect(captureEligible(session)).toBe(true);
    expect(captureEligible({ ...session, status: "paused" })).toBe(false);
    expect(captureEligible({ ...session, client_consent: false })).toBe(false);
  });
});
