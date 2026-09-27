import type { SessionView } from "./types";
/** Synthetic unit-test input only. Never imported by application code. */
export function testView(
  overrides: Partial<SessionView["session"]> = {},
): SessionView {
  return {
    role: "client",
    session: {
      id: "review-one",
      tenant_id: "unit-tests",
      title: "Unit test workflow",
      pack_id: "general",
      offer: "sponsored",
      status: "awaiting_consent",
      created_at: "2026-09-11T00:00:00Z",
      started_at: null,
      consent_epoch: 0,
      revision: 0,
      event_seq: 0,
      client_consent: false,
      facilitator_consent: false,
      max_seconds: 5400,
      intro_seconds: 900,
      budget_aud: 60,
      recording_status: "not_started",
      egress_id: null,
      room_name: "unit-room",
      ...overrides,
    },
    snapshot: {
      schema_version: "1",
      title: "Unit test workflow",
      pack_id: "general",
      revision: 0,
      consent_epoch: 0,
      last_event_seq: 0,
      evidence: [],
      claims: [],
      steps: [],
      edges: [],
      coverage: [],
      unknowns: [],
      probe: null,
    },
  };
}
