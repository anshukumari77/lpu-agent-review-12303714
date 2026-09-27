import type { Claim, Evidence, WorkflowEdge, WorkflowStep } from "./types";

export const SAMPLE_EVIDENCE: Evidence[] = [
  {
    id: "sample-E01",
    kind: "screen_observation",
    actor: "observer",
    text: "SAMPLE screen: invoice INV-2048 is waiting for receipt confirmation. Purchase order PO-1086 is linked; its receipt attachment is missing.",
    at_ms: 0,
    consent_epoch: 0,
    seq: 1,
    source_ref: "Synthetic invoice / approval panel",
  },
  {
    id: "sample-E02",
    kind: "transcript",
    actor: "client",
    text: "SAMPLE participant: I ask the operations manager to confirm the goods arrived. Finance holds payment until that confirmation comes back.",
    at_ms: 0,
    consent_epoch: 0,
    seq: 2,
    source_ref: "Scripted conversation / not recorded",
  },
  {
    id: "sample-E03",
    kind: "screen_observation",
    actor: "observer",
    text: "SAMPLE history: finance requested receipt confirmation. The request remains open, with no recorded response in this fixture.",
    at_ms: 0,
    consent_epoch: 0,
    seq: 3,
    source_ref: "Synthetic activity history",
  },
];
export const SAMPLE_FINDINGS: Claim[] = [
  {
    id: "sample-F01",
    text: "The approval record is missing evidence of receipt.",
    status: "observed",
    evidence_ids: ["sample-E01"],
  },
  {
    id: "sample-F02",
    text: "Operations confirms receipt before finance releases payment.",
    status: "reported",
    evidence_ids: ["sample-E02"],
  },
  {
    id: "sample-F03",
    text: "A receipt-confirmation handoff may be a source of waiting. This example does not establish how often it happens.",
    status: "inferred",
    evidence_ids: ["sample-E01", "sample-E03"],
  },
];
export const SAMPLE_STEPS: WorkflowStep[] = [
  {
    id: "sample-receive",
    title: "Receive invoice",
    actor: "Finance",
    system: "Sample purchase register",
    description: "Match the supplier invoice to its purchase order.",
    evidence_ids: ["sample-E01"],
  },
  {
    id: "sample-confirm",
    title: "Confirm receipt",
    actor: "Operations manager",
    system: "Sample approval request",
    description:
      "Confirm that the goods arrived. The example shows an outstanding request.",
    evidence_ids: ["sample-E02", "sample-E03"],
  },
  {
    id: "sample-release",
    title: "Review for payment",
    actor: "Finance",
    system: "System not established",
    description:
      "Reported next step only. No payment release is shown or performed in this preview.",
    evidence_ids: ["sample-E02"],
  },
];
export const SAMPLE_EDGES: WorkflowEdge[] = [
  {
    id: "sample-handoff",
    source: "sample-receive",
    target: "sample-confirm",
    kind: "handoff",
    label: "Receipt evidence missing",
  },
  {
    id: "sample-conditional",
    source: "sample-confirm",
    target: "sample-release",
    kind: "conditional",
    label: "Receipt confirmed (reported)",
  },
  {
    id: "sample-rework",
    source: "sample-confirm",
    target: "sample-receive",
    kind: "rework",
    label: "Clarify missing evidence (hypothesis)",
  },
];
export const SAMPLE_UNKNOWNS = [
  "Invoice volume and exception frequency",
  "Time spent and cost per approval",
  "Approval limits and delegated authority",
  "Measured benefits, implementation cost and ROI",
];

function escapeHtml(value: string): string {
  return value.replace(
    /[&<>"']/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        char
      ]!,
  );
}
export function buildSampleDownloadHtml(
  state: Pick<PreviewState, "keyFact" | "correction">,
): string {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'"><title>SAMPLE · Harbour Services workflow review</title><style>body{font:16px/1.6 -apple-system,BlinkMacSystemFont,sans-serif;background:#fffdf9;color:#2d3436;max-width:840px;margin:40px auto;padding:24px}h1{font-size:42px;line-height:1.1;letter-spacing:-.04em}h2{margin-top:36px}a{color:#833055}small{color:#596064}.mark{background:#fff0f5;padding:12px}section{border-top:1px solid #dddcd7;margin-top:24px}p,li{overflow-wrap:anywhere}li{margin:12px 0}@media print{body{margin:0;max-width:none}}</style></head><body><p class="mark"><strong>SAMPLE · Interactive preview · No recording</strong><br>Fictional Harbour Services. Synthetic evidence, not a client assessment.</p><h1>Where the approval waits.</h1><p>A sample purchase-to-payment workflow review. No integration, model analysis or real recording was used.</p><section><h2>Working fact · local preview copy</h2><p>${escapeHtml(state.keyFact)}</p><p><small>Editable sample text, not a verified correction to the original evidence.</small></p></section><section><h2>Sample findings</h2><ol>${SAMPLE_FINDINGS.map((finding) => `<li><strong>${escapeHtml(finding.status)}</strong> · ${escapeHtml(finding.text)} ${finding.evidence_ids.map((id) => `<a href="#${id}">${id}</a>`).join(" ")}</li>`).join("")}</ol></section><section><h2>Sample workflow</h2><ol>${SAMPLE_STEPS.map((step) => `<li><strong>${escapeHtml(step.title)}</strong> · ${escapeHtml(step.actor)}<br>${escapeHtml(step.description)}</li>`).join("")}</ol><p>Receipt confirmation is a reported condition, not an observed payment approval. The rework route is a hypothesis.</p></section><section><h2>Not established</h2><ul>${SAMPLE_UNKNOWNS.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ul></section><section><h2>Sample evidence</h2>${SAMPLE_EVIDENCE.map((item) => `<article id="${item.id}"><h3>${item.id} · ${escapeHtml(item.kind.replaceAll("_", " "))}</h3><p>${escapeHtml(item.text)}</p><small>${escapeHtml(item.source_ref || "")}</small></article>`).join("")}</section><section><h2>Local correction note</h2><p>${escapeHtml(state.correction || "No correction added.")}</p><p>Saved only in this preview copy. The report has not been regenerated; nothing was sent to Amulet.</p></section><footer><p>SAMPLE ONLY · Not an operational approval or a recommendation to release payment.</p></footer></body></html>`;
}
export function downloadSampleReport(
  state: Pick<PreviewState, "keyFact" | "correction">,
): void {
  const blob = new Blob([buildSampleDownloadHtml(state)], {
    type: "text/html;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = "amulet-beep-SAMPLE-report.html";
  document.body.append(anchor);
  try {
    anchor.click();
  } finally {
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 0);
  }
}

export const JOURNEY_STEPS = [
  "welcome",
  "permissions",
  "setup",
  "introduction",
  "review",
  "recap",
  "report",
] as const;
export type JourneyStep = (typeof JOURNEY_STEPS)[number];
export const STEP_LABELS: Record<JourneyStep, string> = {
  welcome: "Welcome",
  permissions: "Permission",
  setup: "Preparation",
  introduction: "Introduction",
  review: "Review",
  recap: "Wrap-up",
  report: "Report",
};
export const SAMPLE_KEY_FACT =
  "The operations manager confirms receipt before finance releases payment.";
export interface PreviewState {
  step: JourneyStep;
  consent: boolean;
  mic: boolean;
  screen: boolean;
  status: "active" | "paused" | "disconnected";
  keyFact: string;
  factDraft: string;
  correction: string;
  correctionDraft: string;
  notice: string;
}
export type PreviewAction =
  | {
      type:
        | "next"
        | "consent"
        | "toggle-mic"
        | "toggle-screen"
        | "pause"
        | "resume"
        | "disconnect"
        | "back"
        | "restart"
        | "save-fact"
        | "save-correction";
    }
  | { type: "explore"; step: JourneyStep }
  | { type: "fact-draft" | "correction-draft"; value: string };
export function createPreviewState(): PreviewState {
  return {
    step: "welcome",
    consent: false,
    mic: false,
    screen: false,
    status: "active",
    keyFact: SAMPLE_KEY_FACT,
    factDraft: SAMPLE_KEY_FACT,
    correction: "",
    correctionDraft: "",
    notice: "",
  };
}
export function previewReducer(
  state: PreviewState,
  action: PreviewAction,
): PreviewState {
  if (action.type === "restart") return createPreviewState();
  if (action.type === "explore")
    return { ...createPreviewState(), step: action.step };
  if (action.type === "back")
    return {
      ...state,
      step: JOURNEY_STEPS[Math.max(0, JOURNEY_STEPS.indexOf(state.step) - 1)],
      mic: false,
      screen: false,
      status: "active",
      notice: "",
      consent: state.step === "setup" ? false : state.consent,
    };
  if (action.type === "fact-draft")
    return { ...state, factDraft: action.value, notice: "" };
  if (action.type === "correction-draft")
    return { ...state, correctionDraft: action.value, notice: "" };
  if (action.type === "save-fact" && state.factDraft.trim())
    return {
      ...state,
      keyFact: state.factDraft.trim(),
      notice: "Sample fact saved locally in this preview. Nothing was sent.",
    };
  if (action.type === "save-correction" && state.correctionDraft.trim())
    return {
      ...state,
      correction: state.correctionDraft.trim(),
      correctionDraft: "",
      notice:
        "Sample correction saved locally in this preview. The report was not regenerated and nothing was sent.",
    };
  if (
    action.type === "pause" ||
    action.type === "disconnect" ||
    action.type === "resume"
  )
    return {
      ...state,
      mic: false,
      screen: false,
      status:
        action.type === "pause"
          ? "paused"
          : action.type === "disconnect"
            ? "disconnected"
            : "active",
    };
  if (state.status === "active" && ["setup", "review"].includes(state.step)) {
    if (action.type === "toggle-mic") return { ...state, mic: !state.mic };
    if (action.type === "toggle-screen")
      return { ...state, screen: !state.screen };
  }
  if (action.type === "consent" && state.step === "permissions")
    return { ...state, consent: true, step: "setup" };
  if (
    action.type === "next" &&
    state.step !== "report" &&
    state.step !== "permissions"
  )
    return {
      ...state,
      mic: false,
      screen: false,
      status: "active",
      notice: "",
      step: JOURNEY_STEPS[JOURNEY_STEPS.indexOf(state.step) + 1],
    };
  return state;
}
