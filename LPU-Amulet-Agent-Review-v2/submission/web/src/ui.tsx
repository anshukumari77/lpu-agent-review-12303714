import { useId, type ReactNode } from "react";
import {
  AlertCircle,
  ArrowRight,
  LoaderCircle,
  ShieldCheck,
} from "lucide-react";
import type { Health, Role } from "./types";
import { runtimeReadiness } from "./readiness";
export function ErrorNotice({
  children,
  onRetry,
  retryLabel = "Try again",
}: {
  children: ReactNode;
  onRetry?: () => void;
  retryLabel?: string;
}) {
  return (
    <div className="notice error" role="alert">
      <AlertCircle size={18} aria-hidden="true" />
      <div>{children}</div>
      {onRetry && (
        <button className="button small" onClick={onRetry}>
          {retryLabel}
        </button>
      )}
    </div>
  );
}
export function Loading({ label = "Loading review…" }: { label?: string }) {
  return (
    <div className="loading" role="status">
      <LoaderCircle className="spinner" size={20} aria-hidden="true" />
      {label}
    </div>
  );
}
export function EmptyState({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="empty-state">
      <div className="eyebrow">BEEP / Review workspace</div>
      <h2>{title}</h2>
      <p>{children}</p>
    </div>
  );
}
export function Header({
  role,
  onLogout,
  preview = false,
}: {
  role?: Role;
  onLogout?: () => void;
  preview?: boolean;
}) {
  return (
    <header className="app-header">
      <a
        className="brand"
        href={preview ? "/preview" : "/"}
        aria-label="BEEP home"
      >
        <img src="/amulet-wordmark.svg" width="120" height="29" alt="Amulet" />
        <span className="brand-divider" />
        <span className="product-name">
          BEEP<span>Workflow review</span>
        </span>
      </a>
      <div className="header-right">
        <span className="private-label">
          <ShieldCheck size={15} aria-hidden="true" />
          {preview ? "Journey preview" : "Private review"}
        </span>
        {role && <span className="role-label">{role}</span>}
        {onLogout && (
          <button className="header-action" onClick={onLogout}>
            Sign out
          </button>
        )}
      </div>
    </header>
  );
}
export function Readiness({
  health,
  error,
  onRetry,
}: {
  health?: Health;
  error?: string;
  onRetry: () => void;
}) {
  const { ready, missing: blockers, message } = runtimeReadiness(health, error);
  if (error)
    return (
      <div className="readiness-bar">
        <ErrorNotice onRetry={onRetry} retryLabel="Check service">
          The service is currently unreachable. Check its status before starting
          or resuming a review.
        </ErrorNotice>
      </div>
    );
  if (!health) return null;
  return (
    <details className={`readiness-bar ${ready ? "ready" : "blocked"}`}>
      <summary>
        <span className="status-dot" />
        <span>{ready ? "Runtime configuration ready" : message}</span>
        <span className="readiness-hint">Service details</span>
      </summary>
      <div className="readiness-content">
        <p>
          {ready
            ? "Configuration checks passed. Room admission, provider connectivity and recording are checked separately when used."
            : "This review cannot start every service yet. No simulated interview or report is substituted."}
        </p>
        {blockers.length > 0 && (
          <ul>
            {blockers.map((name, index) => (
              <li key={index}>{name.replace(/_/g, " ")}</li>
            ))}
          </ul>
        )}
        <button className="text-button" onClick={onRetry}>
          Check again <ArrowRight size={14} />
        </button>
      </div>
    </details>
  );
}
export function Tabs({
  items,
  value,
  onChange,
  label,
}: {
  items: string[];
  value: string;
  onChange: (value: string) => void;
  label: string;
}) {
  const id = useId();
  return (
    <div className="tabs" role="tablist" aria-label={label}>
      {items.map((item, index) => (
        <button
          key={item}
          id={`${id}-${index}`}
          role="tab"
          type="button"
          aria-selected={value === item}
          tabIndex={value === item ? 0 : -1}
          onClick={() => onChange(item)}
          onKeyDown={(event) => {
            const offset =
              event.key === "ArrowRight"
                ? 1
                : event.key === "ArrowLeft"
                  ? -1
                  : 0;
            if (offset || event.key === "Home" || event.key === "End") {
              event.preventDefault();
              const next =
                event.key === "Home"
                  ? 0
                  : event.key === "End"
                    ? items.length - 1
                    : (index + offset + items.length) % items.length;
              onChange(items[next]);
              document.getElementById(`${id}-${next}`)?.focus();
            }
          }}
        >
          {item}
        </button>
      ))}
    </div>
  );
}
