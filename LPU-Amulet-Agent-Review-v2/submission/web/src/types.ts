export type Role = "operator" | "client" | "facilitator";
export interface Actor {
  role: Role;
  session_id?: string;
}
export interface ConsentPolicy {
  schema_version: string;
  notice_version: string;
  policy_id: string;
  notice_hash: string;
  provider_configuration_version: string;
  provider_configuration_hash: string;
  provider_configuration: Record<string, unknown>;
  deployment_policy_status: string;
  notice: {
    heading: string;
    introduction: string;
    capture: string;
    devices: string;
    ai_label: string;
    recording_label: string;
    providers: string;
    withdrawal: string;
    deployment_policy: string;
  };
}
export interface ConsentDecision {
  ai: true;
  recording: true;
  notice_version: string;
  policy_id: string;
}
export type SessionStatus =
  | "awaiting_consent"
  | "introduction"
  | "active"
  | "paused"
  | "finalising"
  | "completed"
  | "partial"
  | "failed";
export interface Session {
  id: string;
  tenant_id: string;
  title: string;
  pack_id: string;
  offer: "paid" | "sponsored";
  status: SessionStatus;
  created_at: string;
  started_at: string | null;
  consent_epoch: number;
  revision: number;
  event_seq: number;
  client_consent: boolean | { ai: boolean; recording: boolean };
  facilitator_consent: boolean | { ai: boolean; recording: boolean };
  max_seconds: number;
  intro_seconds: number;
  budget_aud: number;
  recording_status: string;
  egress_id: string | null;
  room_name: string;
}
export interface Evidence {
  id: string;
  kind: "transcript" | "screen_observation" | "correction" | "system";
  text: string;
  actor: "client" | "facilitator" | "agent" | "observer" | "system";
  at_ms: number;
  consent_epoch: number;
  seq: number;
  source_ref?: string | null;
}
export interface Claim {
  id: string;
  text: string;
  evidence_ids: string[];
  status: "observed" | "reported" | "inferred" | "unknown" | "contradicted";
}
export interface WorkflowStep {
  id: string;
  title: string;
  actor: string;
  system: string;
  description: string;
  evidence_ids: string[];
}
export interface WorkflowEdge {
  id: string;
  source: string;
  target: string;
  kind: "next" | "conditional" | "rework" | "handoff";
  label: string;
}
export interface Snapshot {
  schema_version: string;
  title: string;
  pack_id: string;
  revision: number;
  consent_epoch: number;
  last_event_seq: number;
  evidence: Evidence[];
  claims: Claim[];
  steps: WorkflowStep[];
  edges: WorkflowEdge[];
  coverage: string[];
  unknowns: string[];
  probe: {
    id: string;
    text: string;
    reason: string;
    evidence_ids: string[];
    based_on_revision: number;
    consent_epoch: number;
  } | null;
}
export interface Report {
  schema_version: string;
  session_id: string;
  revision: number;
  title: string;
  summary: string;
  claims: Claim[];
  steps: WorkflowStep[];
  edges: WorkflowEdge[];
  unknowns: string[];
  recommendations: string[];
  evidence: Evidence[];
  status: "complete" | "partial";
  internal_opportunity?: Record<string, unknown>;
  generated_at: string;
}
export interface SessionView {
  session: Session;
  snapshot: Snapshot;
  role: Role;
}
export interface Pack {
  id: string;
  title?: string;
  name?: string;
  description?: string;
  version?: string;
}
export interface Health {
  status: string;
  version: string;
  readiness: Record<string, unknown>;
}
export interface RoomCredentials {
  server_url: string;
  participant_token: string;
  participant_identity: string;
  room_name: string;
}
export type ControlAction =
  "handover" | "pause" | "resume" | "finish" | "takeover";
