import { useEffect, useRef, useState } from "react";
import { ArrowRight, LockKeyhole } from "lucide-react";
import { api, ApiError, errorMessage } from "./api";
import type { Actor } from "./types";
import { ErrorNotice } from "./ui";
export function Login({
  onAuthenticated,
}: {
  onAuthenticated: (actor: Actor) => void;
}) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const alive = useRef(true);
  const request = useRef<AbortController | null>(null);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      request.current?.abort();
    };
  }, []);
  async function submit() {
    if (!token || busy) return;
    setBusy(true);
    setError("");
    const abort = new AbortController();
    request.current = abort;
    try {
      await api("/operator/login", { body: { token }, signal: abort.signal });
      const actor = await api<Actor>("/me", { signal: abort.signal });
      if (actor.role !== "operator")
        throw new ApiError(403, "The service did not verify operator access.");
      if (alive.current) {
        setToken("");
        onAuthenticated(actor);
      }
    } catch (error) {
      if (alive.current) setError(errorMessage(error));
    } finally {
      if (alive.current) {
        setToken("");
        setBusy(false);
      }
    }
  }
  return (
    <main id="main" className="login-layout">
      <section className="login-context">
        <div className="eyebrow">BEEP / Operator workspace</div>
        <h1>
          Understand the work.
          <br />
          <span>Start with a conversation.</span>
        </h1>
        <p>
          Create a private review, invite the client and facilitator, and follow
          the evidence into a shared workflow report.
        </p>
        <div className="login-note">
          <span className="note-index">01 — 03</span>
          <p>
            Human introduction.
            <br />A focused, AI-led review.
            <br />A report open to correction.
          </p>
        </div>
        <a className="button journey-link" href="/preview">
          See the client journey <ArrowRight size={17} aria-hidden="true" />
        </a>
        <p className="fine-print">
          Interactive sample. No account, microphone or screen access needed.
        </p>
      </section>
      <section className="login-panel">
        <div className="eyebrow">
          <LockKeyhole size={15} /> Private access
        </div>
        <h2>Operator sign in</h2>
        <p>
          Use the operator password configured for this workspace. Client and
          facilitator access is by private invitation.
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <label htmlFor="operator-password">Operator password</label>
          <input
            autoComplete="current-password"
            id="operator-password"
            type="password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            required
            disabled={busy}
          />
          <button className="button primary" disabled={!token || busy}>
            {busy ? (
              "Verifying access…"
            ) : (
              <>
                Sign in <ArrowRight size={17} />
              </>
            )}
          </button>
        </form>
        {error && <ErrorNotice>{error}</ErrorNotice>}
        <p className="fine-print">
          Access uses a secure, role-scoped session cookie. No password or
          invitation is saved in browser storage.
        </p>
      </section>
    </main>
  );
}
