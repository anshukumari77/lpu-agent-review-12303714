import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { Download, FileText, MessageSquarePlus } from "lucide-react";
import type { Evidence, Report, Role } from "./types";
import { useResource } from "./useResource";
import { sessionPath, errorMessage } from "./api";
import { dateLabel, duration } from "./session";
import { EmptyState, ErrorNotice, Loading, Tabs } from "./ui";
const WorkflowMap = lazy(() => import("./WorkflowMap"));
export function EvidenceList({
  evidence,
  selectedIds,
}: {
  evidence: Evidence[];
  selectedIds?: string[];
}) {
  const [query, setQuery] = useState("");
  const visible = evidence.filter(
    (item) =>
      (!selectedIds || selectedIds.includes(item.id)) &&
      `${item.text} ${item.actor} ${item.kind}`
        .toLowerCase()
        .includes(query.toLowerCase()),
  );
  return (
    <section className="evidence-view">
      <label className="search-label">
        Find in evidence
        <input
          type="search"
          placeholder="Search words or speaker"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </label>
      {visible.length === 0 ? (
        <EmptyState title="No matching evidence">
          Evidence appears here only after it is received and saved by the
          service.
        </EmptyState>
      ) : (
        <ol className="evidence-list">
          {visible.map((item) => (
            <li key={item.id} id={`evidence-${encodeURIComponent(item.id)}`}>
              <div className="evidence-meta">
                <span>{item.actor}</span>
                <span>{item.kind.replace(/_/g, " ")}</span>
                <time>{duration(item.at_ms / 1000)}</time>
              </div>
              <p>{item.text}</p>
              <span className="evidence-id">
                {item.id}
                {item.source_ref ? ` · Source: ${item.source_ref}` : ""}
              </span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
export function ReportView({
  sessionId,
  role,
  onCorrect,
}: {
  sessionId: string;
  role: Role;
  onCorrect: (text: string) => Promise<void>;
}) {
  const resource = useResource<{ report: Report }>(
    `${sessionPath(sessionId)}/report`,
    4000,
  );
  const [tab, setTab] = useState("Summary");
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  // The component is keyed by authenticated role + session in the review shell.
  const report = resource.data?.report;
  const mismatch = report && report.session_id !== sessionId;
  const unavailable =
    resource.error && [404, 409].includes(resource.error.status);
  const [selectedEvidence, setSelectedEvidence] = useState<
    string[] | undefined
  >();
  const items = [
    "Summary",
    "Workflow map",
    "Evidence",
    ...(role === "operator" ? ["Internal draft"] : []),
  ];
  async function correct() {
    if (!text.trim() || busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    resource.cancel();
    try {
      await onCorrect(text.trim());
      if (alive.current) {
        setText("");
        setNotice(
          "Correction saved. The previous report is invalidated; a revised report will appear when ready.",
        );
      }
    } catch (error) {
      if (alive.current) setError(errorMessage(error));
    } finally {
      if (alive.current) {
        await resource.refresh();
        setBusy(false);
      }
    }
  }
  return (
    <section className="report-workspace" aria-label="Review report">
      {mismatch ? (
        <ErrorNotice>
          The report could not be verified for this review. Refresh or contact
          your facilitator.
        </ErrorNotice>
      ) : (
        <>
          <div className="report-heading">
            <div>
              <div className="eyebrow">
                Your workflow / Evidence-linked report
              </div>
              <h2>
                {report && !busy ? report.title : "Your review, in one place."}
              </h2>
            </div>
            {report && !busy && (
              <div className="export-actions">
                <a
                  className="button"
                  href={`/api${sessionPath(sessionId)}/report.html`}
                  target="_blank"
                  rel="noreferrer"
                >
                  <FileText size={16} /> Print / save PDF
                </a>
                <a
                  className="button"
                  href={`/api${sessionPath(sessionId)}/export`}
                  download
                >
                  <Download size={16} /> Export JSON
                </a>
              </div>
            )}
          </div>
          {resource.error && !unavailable && (
            <ErrorNotice onRetry={() => void resource.refresh()}>
              {resource.error.message}
            </ErrorNotice>
          )}
          {busy ? (
            <Loading label="Saving correction and checking the current report…" />
          ) : report ? (
            <>
              <div className="report-version">
                <span
                  className={`pill ${report.status === "partial" ? "warn" : ""}`}
                >
                  {report.status === "partial"
                    ? "Partial report"
                    : "Complete report"}
                </span>
                <span>
                  Revision {report.revision} · {dateLabel(report.generated_at)}
                </span>
              </div>
              <Tabs
                items={items}
                value={tab}
                onChange={(next) => {
                  setTab(next);
                  setSelectedEvidence(undefined);
                }}
                label="Report sections"
              />
              <div
                className="report-panel"
                role="tabpanel"
                aria-label={tab}
                tabIndex={0}
              >
                {tab === "Summary" && (
                  <div className="summary-layout">
                    <article className="report-prose">
                      <h3>What we learned</h3>
                      <p className="report-summary">
                        {report.summary || "No supported summary was returned."}
                      </p>
                      <h3>Supported findings</h3>
                      {report.claims.length === 0 ? (
                        <p className="muted">
                          No findings have been established from the available
                          evidence.
                        </p>
                      ) : (
                        <ol className="claim-list">
                          {report.claims.map((claim) => (
                            <li key={claim.id}>
                              <span className={`claim-status ${claim.status}`}>
                                {claim.status}
                              </span>
                              <p>{claim.text}</p>
                              {claim.evidence_ids.length > 0 ? (
                                <button
                                  className="text-button"
                                  onClick={() => {
                                    setSelectedEvidence(claim.evidence_ids);
                                    setTab("Evidence");
                                  }}
                                >
                                  View supporting evidence (
                                  {claim.evidence_ids.length})
                                </button>
                              ) : (
                                <span className="fine-print">
                                  No supporting evidence linked
                                </span>
                              )}
                            </li>
                          ))}
                        </ol>
                      )}
                      {report.recommendations.length > 0 && (
                        <>
                          <h3>Recommendations to consider</h3>
                          <ul className="plain-list">
                            {report.recommendations.map((text, i) => (
                              <li key={i}>{text}</li>
                            ))}
                          </ul>
                        </>
                      )}
                    </article>
                    <aside className="unknowns">
                      <div className="eyebrow">Still to establish</div>
                      <h3>Open questions</h3>
                      {report.unknowns.length > 0 ? (
                        <ul>
                          {report.unknowns.map((text, i) => (
                            <li key={i}>{text}</li>
                          ))}
                        </ul>
                      ) : (
                        <p>
                          No open questions are listed in this report. This is
                          not a guarantee of completeness.
                        </p>
                      )}
                      <p className="fine-print">
                        Observed, reported and inferred findings remain
                        distinct. An application seen on screen does not
                        establish API access or implementation feasibility.
                      </p>
                    </aside>
                  </div>
                )}
                {tab === "Workflow map" && (
                  <Suspense
                    fallback={<Loading label="Opening workflow map…" />}
                  >
                    <WorkflowMap
                      steps={report.steps}
                      edges={report.edges}
                      evidence={report.evidence}
                    />
                  </Suspense>
                )}
                {tab === "Evidence" && (
                  <>
                    {selectedEvidence && (
                      <button
                        className="text-button"
                        onClick={() => setSelectedEvidence(undefined)}
                      >
                        Show all evidence
                      </button>
                    )}
                    <EvidenceList
                      evidence={report.evidence}
                      selectedIds={selectedEvidence}
                    />
                  </>
                )}
                {tab === "Internal draft" && role === "operator" && (
                  <section className="internal-draft">
                    <div className="notice">
                      Operator only · Opportunity draft · Not a binding quote
                    </div>
                    {report.internal_opportunity ? (
                      <pre>
                        {JSON.stringify(report.internal_opportunity, null, 2)}
                      </pre>
                    ) : (
                      <p>No internal opportunity was returned.</p>
                    )}
                  </section>
                )}
              </div>
            </>
          ) : (
            <EmptyState
              title={
                unavailable
                  ? "The report is not ready yet."
                  : "Waiting for a report."
              }
            >
              The service assembles a report from this review’s saved evidence.
              It may be incomplete if the session ended early or a service was
              unavailable. This view checks for updates automatically.
            </EmptyState>
          )}
        </>
      )}
      {role === "client" && (
        <form
          className="correction-form"
          onSubmit={(event) => {
            event.preventDefault();
            void correct();
          }}
        >
          <div>
            <div className="eyebrow">
              <MessageSquarePlus size={15} /> Make it accurate
            </div>
            <h3>Something to correct?</h3>
            <p>
              Tell us what is wrong and what should replace it. Your correction
              becomes evidence and triggers a new report revision.
            </p>
          </div>
          <div>
            <label htmlFor="correction-text">Your correction</label>
            <textarea
              id="correction-text"
              maxLength={12000}
              rows={4}
              placeholder="The approval is done by…"
              value={text}
              onChange={(e) => setText(e.target.value)}
              disabled={busy}
            />
            <button className="button primary" disabled={busy || !text.trim()}>
              {busy ? "Saving correction…" : "Submit correction"}
            </button>
          </div>
        </form>
      )}
      {notice && (
        <p className="notice" role="status">
          {notice}
        </p>
      )}
      {error && <ErrorNotice>{error}</ErrorNotice>}
    </section>
  );
}
