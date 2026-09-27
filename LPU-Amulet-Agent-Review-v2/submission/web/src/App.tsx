import {
  Component,
  lazy,
  Suspense,
  useCallback,
  useEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { api, ApiError, errorMessage } from "./api";
import type { Actor, Health } from "./types";
import { useResource } from "./useResource";
import { EmptyState, ErrorNotice, Header, Loading, Readiness } from "./ui";
import { Login } from "./Login";
import { runtimeReadiness } from "./readiness";
const Operator = lazy(() =>
  import("./Operator").then((module) => ({ default: module.Operator })),
);
const Review = lazy(() =>
  import("./Review").then((module) => ({ default: module.Review })),
);
class ViewBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? (
      <main className="review-loading">
        <ErrorNotice>
          This view could not be rendered. Reload to check the service and
          reconnect explicitly.
        </ErrorNotice>
        <a className="button" href={location.pathname}>
          Reload view
        </a>
      </main>
    ) : (
      this.props.children
    );
  }
}
export function App({ bootstrap }: { bootstrap: Promise<Actor | null> }) {
  const [actor, setActor] = useState<Actor | null>(null);
  const [ready, setReady] = useState(false);
  const [bootError, setBootError] = useState("");
  const [route, setRoute] = useState(location.pathname);
  const [error, setError] = useState("");
  const [loggingOut, setLoggingOut] = useState(false);
  const stopRef = useRef<(() => void) | null>(null);
  const setStop = useCallback((stop: (() => void) | null) => {
    stopRef.current = stop;
  }, []);
  const health = useResource<Health>("/health", 15_000);
  useEffect(() => {
    let alive = true;
    bootstrap.then(
      (value) => {
        if (alive) {
          setActor(value);
          setReady(true);
        }
      },
      (error) => {
        if (alive) {
          setBootError(errorMessage(error));
          setReady(true);
        }
      },
    );
    return () => {
      alive = false;
    };
  }, [bootstrap]);
  useEffect(() => {
    const navigate = () => {
      stopRef.current?.();
      setRoute(location.pathname);
    };
    window.addEventListener("popstate", navigate);
    return () => window.removeEventListener("popstate", navigate);
  }, []);
  useEffect(() => {
    if (
      actor &&
      actor.role !== "operator" &&
      actor.session_id &&
      route === "/"
    ) {
      const target = `/review/${encodeURIComponent(actor.session_id)}`;
      history.replaceState(null, "", target);
      setRoute(target);
    }
  }, [actor, route]);
  async function logout() {
    stopRef.current?.();
    if (loggingOut) return;
    setLoggingOut(true);
    setError("");
    try {
      await api("/logout", { body: {} });
      let cleared = false;
      try {
        await api<Actor>("/me");
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) cleared = true;
        else throw error;
      }
      if (!cleared)
        throw new ApiError(
          500,
          "Sign out could not be verified. Please retry.",
        );
      setActor(null);
      setBootError("");
      history.replaceState(null, "", "/");
      setRoute("/");
    } catch (error) {
      setError(errorMessage(error));
    } finally {
      setLoggingOut(false);
    }
  }
  const match = route.match(/^\/review\/([^/]+)\/?$/);
  let id: string | null = null;
  try {
    id = match ? decodeURIComponent(match[1]) : null;
  } catch {
    /* malformed path is not a session identifier */
  }
  let content: ReactNode;
  if (!ready)
    content = (
      <main id="main" className="review-loading">
        <Loading label="Checking private access…" />
      </main>
    );
  else if (bootError)
    content = (
      <main id="main" className="entry-error">
        <EmptyState
          title={
            match
              ? "This invitation could not be opened."
              : "Access could not be checked."
          }
        >
          {bootError}
        </EmptyState>
        <p>
          {match
            ? "The invitation exchange did not complete. The service may be unavailable or the invitation may have expired. Reopen your original link to retry; its secret has been removed from the address bar. Contact the operator if it still cannot be opened."
            : "The service could not verify your access. This does not mean your password or invitation is invalid. Check the service status, then retry the access check."}
        </p>
        <a className="button" href="/">
          {match ? "Operator sign in instead" : "Retry access check"}
        </a>
      </main>
    );
  else if (!actor && match)
    content = (
      <main id="main" className="entry-error">
        <EmptyState title="A private invitation is needed.">
          Open the full client or facilitator invitation to enter this review. A
          review ID alone does not grant access.
        </EmptyState>
        <a className="button" href="/">
          Operator sign in
        </a>
      </main>
    );
  else if (!actor)
    content = (
      <Login
        onAuthenticated={(value) => {
          setActor(value);
          setBootError("");
        }}
      />
    );
  else if (id && actor.role !== "operator" && id !== actor.session_id)
    content = (
      <main id="main" className="entry-error">
        <ErrorNotice>
          This invitation belongs to a different review.
        </ErrorNotice>
        <a
          className="button"
          href={`/review/${encodeURIComponent(actor.session_id!)}`}
        >
          Open my review
        </a>
      </main>
    );
  else if (id)
    content = (
      <Review
        key={`${actor.role}:${id}`}
        sessionId={id}
        actor={actor}
        runtime={runtimeReadiness(health.data, health.error?.message)}
        onStopAvailable={setStop}
      />
    );
  else if (route === "/" && actor.role === "operator") content = <Operator />;
  else if (route === "/" && actor.session_id) content = <Loading />;
  else
    content = (
      <main id="main" className="entry-error">
        <EmptyState title="This page is not a review.">
          Check your link or return to your private workspace.
        </EmptyState>
        <a className="button" href="/">
          Return to workspace
        </a>
      </main>
    );
  return (
    <>
      <a className="skip-link" href="#main">
        Skip to review content
      </a>
      <Header
        role={actor?.role}
        onLogout={actor && !loggingOut ? () => void logout() : undefined}
      />
      <Readiness
        health={health.data}
        error={health.error?.message}
        onRetry={() => void health.refresh()}
      />
      {error && (
        <div className="global-error">
          <ErrorNotice>{error}</ErrorNotice>
        </div>
      )}
      <ViewBoundary key={`${actor?.role}:${route}`}>
        <Suspense
          fallback={
            <main className="review-loading">
              <Loading label="Opening workspace…" />
            </main>
          }
        >
          {content}
        </Suspense>
      </ViewBoundary>
    </>
  );
}
