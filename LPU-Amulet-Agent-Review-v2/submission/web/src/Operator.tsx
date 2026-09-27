import { useEffect, useRef, useState } from "react";
import { ArrowUpRight, Check, Copy, Plus, RefreshCw } from "lucide-react";
import { api, ApiError, errorMessage, sessionPath } from "./api";
import { useResource } from "./useResource";
import type { Pack, Session, SessionView } from "./types";
import { dateLabel, statusLabel } from "./session";
import { EmptyState, ErrorNotice, Loading } from "./ui";
import { InferenceConnection } from "./InferenceConnection";
import "./client-journey.css";
type Created = {
  session: Session;
  invitations: { client: string; facilitator: string };
};
export function Operator() {
  const sessions = useResource<{ sessions: Session[] }>("/sessions", 10_000);
  const packs = useResource<{ packs: Pack[] }>("/packs");
  const [title, setTitle] = useState("");
  const [packId, setPackId] = useState("");
  const [offer, setOffer] = useState<"paid" | "sponsored" | "">("");
  const [paid, setPaid] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [created, setCreated] = useState<Created | null>(null);
  const [verifiedId, setVerifiedId] = useState<string | null>(null);
  const verified = !!created && verifiedId === created.session.id;
  const [copied, setCopied] = useState("");
  const [filter, setFilter] = useState("");
  const alive = useRef(true);
  const request = useRef<AbortController | null>(null);
  const generation = useRef(0);
  const createdId = useRef<string | null>(null);
  const invitationPanel = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (verified) invitationPanel.current?.focus();
  }, [verified, created?.session.id]);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      generation.current += 1;
      request.current?.abort();
    };
  }, []);
  const chosenPack = packId || packs.data?.packs[0]?.id || "";
  const valid =
    title.trim() && chosenPack && offer && (offer !== "paid" || paid);
  const creationHint = busy
    ? "Creating the session, then checking it in this operator workspace."
    : created && !verified
      ? "Verify the created session below before creating another."
      : created && verified && !title.trim()
        ? "Session created. Send the client and facilitator their separate invitations below."
        : !title.trim()
          ? "Name the workflow to continue."
          : !chosenPack
            ? "Choose the questions for this workflow."
            : !offer
              ? "Choose paid or sponsored. Nothing is selected for you."
              : offer === "paid" && !paid
                ? "Confirm payment only if you have checked it outside BEEP."
                : "Creates a real session and two private invitations. No call or device starts.";
  function current(stamp: number, abort: AbortController) {
    return (
      alive.current &&
      stamp === generation.current &&
      request.current === abort &&
      !abort.signal.aborted
    );
  }
  async function verify(
    result: Created,
    stamp: number,
    abort: AbortController,
  ) {
    const view = await api<SessionView>(sessionPath(result.session.id), {
      signal: abort.signal,
    });
    if (!current(stamp, abort) || createdId.current !== result.session.id)
      return;
    if (view.session.id !== result.session.id || view.role !== "operator")
      throw new ApiError(
        403,
        "The created review could not be verified in this workspace.",
      );
    setVerifiedId(result.session.id);
    await sessions.refresh();
  }
  async function create() {
    if (!valid || request.current || (created && !verified)) return;
    setBusy(true);
    setError("");
    setCopied("");
    createdId.current = null;
    setCreated(null);
    setVerifiedId(null);
    const abort = new AbortController();
    const stamp = ++generation.current;
    request.current = abort;
    try {
      const result = await api<Created>("/sessions", {
        body: { title: title.trim(), pack_id: chosenPack, offer },
        signal: abort.signal,
      });
      if (!current(stamp, abort)) return;
      createdId.current = result.session.id;
      setCreated(result);
      setVerifiedId(null);
      await verify(result, stamp, abort);
      if (current(stamp, abort) && createdId.current === result.session.id) {
        setTitle("");
        setOffer("");
        setPaid(false);
      }
    } catch (error) {
      if (current(stamp, abort)) setError(errorMessage(error));
    } finally {
      if (current(stamp, abort)) {
        request.current = null;
        setBusy(false);
      }
    }
  }
  async function retryVerification() {
    if (!created || request.current || verified) return;
    const abort = new AbortController();
    const stamp = ++generation.current;
    request.current = abort;
    setBusy(true);
    setError("");
    try {
      await verify(created, stamp, abort);
    } catch (error) {
      if (current(stamp, abort) && createdId.current === created.session.id)
        setError(errorMessage(error));
    } finally {
      if (current(stamp, abort) && createdId.current === created.session.id) {
        request.current = null;
        setBusy(false);
      }
    }
  }
  async function copy(role: "client" | "facilitator") {
    if (!created) return;
    try {
      await navigator.clipboard.writeText(created.invitations[role]);
      if (alive.current) setCopied(role);
    } catch {
      if (alive.current)
        setError(
          "Clipboard access was blocked. Open “Show link” and copy the invitation manually.",
        );
    }
  }
  const list = sessions.data?.sessions.filter((session) =>
    `${session.title} ${statusLabel(session.status)}`
      .toLowerCase()
      .includes(filter.toLowerCase()),
  );
  return (
    <main id="main" className="operator-workspace">
      <div className="workspace-heading">
        <div>
          <div className="eyebrow">BEEP / Private workspace</div>
          <h1>Workflow reviews</h1>
          <p>
            Choose one existing workflow, create a session, then invite the
            client and facilitator. BEEP asks questions to map the work; it does
            not automate it or build integrations.
          </p>
        </div>
        <button className="button" onClick={() => void sessions.refresh()}>
          <RefreshCw size={16} /> Refresh
        </button>
      </div>
      <div className="operator-layout">
        <aside className="create-panel">
          <div className="eyebrow">
            <Plus size={15} /> New review
          </div>
          <h2>Create a real review session</h2>
          <p className="muted">
            Start with a process the client can show, such as approving an
            invoice or preparing a quote. The title names that process, not a
            request to build software.
          </p>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void create();
            }}
          >
            <label htmlFor="review-title">
              Which workflow will you review?
            </label>
            <p id="review-title-help" className="field-help">
              Name one existing process. For example, “From enquiry to approved
              quote”.
            </p>
            <input
              id="review-title"
              aria-describedby="review-title-help"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder="e.g. From enquiry to approved quote"
              maxLength={200}
              required
              disabled={busy}
            />
            <label htmlFor="review-pack">Questions to guide the review</label>
            <select
              id="review-pack"
              value={chosenPack}
              onChange={(e) => setPackId(e.target.value)}
              disabled={busy || !packs.data}
            >
              <option value="" disabled>
                Select a pack
              </option>
              {packs.data?.packs.map((pack) => (
                <option key={pack.id} value={pack.id}>
                  {pack.title || pack.name || pack.id}
                </option>
              ))}
            </select>
            {packs.error && (
              <ErrorNotice onRetry={() => void packs.refresh()}>
                {packs.error.message}
              </ErrorNotice>
            )}
            <p className="field-help">
              {packs.data?.packs.find((pack) => pack.id === chosenPack)
                ?.description ||
                "Choose the closest question set. This does not install a workflow or connect any systems."}
            </p>
            <fieldset className="offer-choice" disabled={busy}>
              <legend>Session offer</legend>
              <label className="check-row">
                <input
                  type="radio"
                  name="offer"
                  checked={offer === "paid"}
                  onChange={() => {
                    setOffer("paid");
                    setPaid(false);
                  }}
                />
                <span>Paid · payment confirmed outside BEEP</span>
              </label>
              <label className="check-row">
                <input
                  type="radio"
                  name="offer"
                  checked={offer === "sponsored"}
                  onChange={() => {
                    setOffer("sponsored");
                    setPaid(false);
                  }}
                />
                <span>Sponsored · subject to workspace limits</span>
              </label>
            </fieldset>
            {offer === "paid" && (
              <label className="check-row payment-confirm">
                <input
                  type="checkbox"
                  checked={paid}
                  onChange={(e) => setPaid(e.target.checked)}
                  disabled={busy}
                />
                <span>
                  I have confirmed payment outside this app. Creating this
                  review does not charge the client.
                </span>
              </label>
            )}
            <button
              className="button primary full"
              aria-describedby="create-session-help"
              disabled={!valid || busy || (!!created && !verified)}
            >
              {busy
                ? "Creating and verifying…"
                : "Create session & invitations"}
            </button>
            <p id="create-session-help" className="field-help" role="status">
              {creationHint}
            </p>
            <p className="fine-print">
              Includes the configured human introduction and session cap. BEEP
              does not collect payment here.
            </p>
          </form>
          {error && <ErrorNotice>{error}</ErrorNotice>}
          {created && (
            <section
              className="invitation-result"
              aria-label="Private invitations"
              ref={invitationPanel}
              tabIndex={-1}
            >
              <h3>
                {verified
                  ? "Send the two invitations separately."
                  : "Review created; verification pending."}
              </h3>
              {!verified ? (
                <button
                  className="button"
                  disabled={busy}
                  onClick={() => void retryVerification()}
                >
                  Verify created review
                </button>
              ) : (
                <>
                  <p>
                    Session: <strong>{created.session.title}</strong>
                  </p>
                  <p>
                    Send each person only their own invitation. Each link can be
                    used once and expires under the workspace policy. Copying a
                    link does not open or use it.
                  </p>
                  {(["client", "facilitator"] as const).map((role) => (
                    <div className="invite-row" key={role}>
                      <h4>
                        {role === "client"
                          ? "Client invitation"
                          : "Facilitator invitation"}
                      </h4>
                      <p className="field-help">
                        {role === "client"
                          ? "Send to the person who will show how they do the work."
                          : "Send to the person leading the human introduction. If that is you, use a separate browser profile."}
                      </p>
                      <button
                        className="button"
                        onClick={() => void copy(role)}
                      >
                        {copied === role ? (
                          <Check size={16} />
                        ) : (
                          <Copy size={16} />
                        )}
                        Copy {role} invite
                      </button>
                      <details>
                        <summary>Show link</summary>
                        <input
                          aria-label={`${role} invitation URL`}
                          readOnly
                          value={created.invitations[role]}
                          onFocus={(e) => e.target.select()}
                        />
                      </details>
                    </div>
                  ))}
                  {copied && (
                    <span role="status" className="fine-print">
                      {copied} invitation copied.
                    </span>
                  )}
                  <a
                    className="text-button"
                    href={`/review/${encodeURIComponent(created.session.id)}`}
                  >
                    Open read-only operator view <ArrowUpRight size={16} />
                  </a>
                  <p className="fine-print">
                    To join as a participant, open that invitation in a separate
                    browser profile. Exchanging it replaces the current role
                    cookie.
                  </p>
                </>
              )}
            </section>
          )}
        </aside>
        <div className="operator-existing">
          <section className="session-ledger" aria-label="Reviews">
            <div className="section-heading">
              <h2>Your reviews</h2>
              <label className="sr-only" htmlFor="session-search">
                Find a review
              </label>
              <input
                id="session-search"
                type="search"
                placeholder="Find a review"
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
              />
            </div>
            {sessions.error && (
              <ErrorNotice onRetry={() => void sessions.refresh()}>
                {sessions.error.message}
              </ErrorNotice>
            )}
            {!sessions.data && sessions.loading ? (
              <Loading label="Loading private reviews…" />
            ) : list?.length ? (
              <ul className="session-list">
                {list.map((session) => (
                  <li key={session.id}>
                    <a href={`/review/${encodeURIComponent(session.id)}`}>
                      <div>
                        <span className="ledger-date">
                          {dateLabel(session.created_at)}
                        </span>
                        <h3>{session.title}</h3>
                        <span className="ledger-pack">
                          {packs.data?.packs.find(
                            (pack) => pack.id === session.pack_id,
                          )?.title || session.pack_id}{" "}
                          ·{" "}
                          {session.offer === "sponsored"
                            ? "Sponsored"
                            : "Operator-confirmed paid"}
                        </span>
                      </div>
                      <div className="session-row-end">
                        <span className={`pill ${session.status}`}>
                          {statusLabel(session.status)}
                        </span>
                        <ArrowUpRight size={18} aria-hidden="true" />
                      </div>
                    </a>
                  </li>
                ))}
              </ul>
            ) : (
              !sessions.error && (
                <EmptyState
                  title={filter ? "No reviews match." : "No reviews yet."}
                >
                  {filter
                    ? "Try another title or session state."
                    : "Create a private review when the client and facilitator are ready. Invitations are generated only for real sessions."}
                </EmptyState>
              )
            )}
          </section>
          <InferenceConnection />
        </div>
      </div>
    </main>
  );
}
