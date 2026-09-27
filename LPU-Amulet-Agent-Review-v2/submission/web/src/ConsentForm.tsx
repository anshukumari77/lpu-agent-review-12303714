import { useEffect, useId, useState } from "react";
import { ArrowRight, ShieldCheck } from "lucide-react";
import { api } from "./api";
import type { ConsentDecision, ConsentPolicy } from "./types";
import "./client-journey.css";

// Only the explicit /preview route uses this sample. It is never a fallback
// for an unavailable service and its sentinel cannot pass API validation.
const PREVIEW_NOTICE: ConsentPolicy["notice"] = {
  heading: "A review you control.",
  introduction:
    "Preview only: no consent is recorded and no review is started.",
  capture:
    "In a real review, share only the window or tab needed for the review.",
  devices:
    "Your camera is never requested. Connecting does not turn on your microphone or share your screen.",
  ai_label: "Preview the separate AI processing permission.",
  recording_label:
    "Preview the separate voice and screen recording permission.",
  providers: "This sample does not select or contact any providers.",
  withdrawal: "The live review explains withdrawal before you consent.",
  deployment_policy:
    "A real review requires the current server-issued notice and an approved deployment policy.",
};

export function ConsentForm({
  onConsent,
  busy,
}: {
  onConsent: (value: ConsentDecision) => Promise<void>;
  busy: boolean;
}) {
  const [ai, setAi] = useState(false);
  const [recording, setRecording] = useState(false);
  const [policy, setPolicy] = useState<ConsentPolicy | null>(null);
  const [reload, setReload] = useState(0);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const headingId = useId();
  const preview = /^\/preview\/?$/.test(window.location.pathname);
  useEffect(() => {
    if (preview) return;
    const controller = new AbortController();
    setPolicy(null);
    setAi(false);
    setRecording(false);
    setError("");
    void api<{ policy: ConsentPolicy }>("/consent-policy", {
      signal: controller.signal,
    })
      .then(({ policy: served }) => {
        if (controller.signal.aborted) return;
        if (
          !served ||
          served.schema_version !== "1" ||
          !served.notice_version ||
          !/^[0-9a-f]{64}$/.test(served.policy_id) ||
          !served.notice ||
          [
            "heading",
            "introduction",
            "capture",
            "devices",
            "ai_label",
            "recording_label",
            "providers",
            "withdrawal",
            "deployment_policy",
          ].some(
            (key) =>
              typeof served.notice[key as keyof ConsentPolicy["notice"]] !==
              "string",
          )
        )
          throw new Error("Invalid consent notice");
        setPolicy(served);
      })
      .catch(() => {
        if (!controller.signal.aborted)
          setError(
            "Cannot load the consent notice. Reload it before consenting.",
          );
      });
    return () => controller.abort();
  }, [reload, preview]);
  const notice = preview ? PREVIEW_NOTICE : policy?.notice;
  const disabled = busy || saving || (!policy && !preview);
  return (
    <form
      className="consent-form journey-consent"
      aria-labelledby={headingId}
      onSubmit={(event) => {
        event.preventDefault();
        if (ai && recording && !disabled && (policy || preview)) {
          setSaving(true);
          void onConsent({
            ai: true,
            recording: true,
            notice_version: preview ? "preview-only" : policy!.notice_version,
            policy_id: preview ? "preview-only" : policy!.policy_id,
          })
            .catch(() =>
              setError(
                "Consent was not confirmed. Reload the notice and check the review state.",
              ),
            )
            .finally(() => setSaving(false));
        }
      }}
    >
      <div className="eyebrow">
        <ShieldCheck size={16} aria-hidden="true" /> Your permission, first
      </div>
      <h2 id={headingId}>{notice?.heading ?? "A review you control."}</h2>
      {!notice && !error && (
        <p role="status">Loading the current consent notice…</p>
      )}
      {error && <p role="alert">{error}</p>}
      <p>{notice?.introduction}</p>
      <p className="muted">{notice?.capture}</p>
      <p className="consent-device-note">{notice?.devices}</p>
      <p>{notice?.providers}</p>
      <p className="fine-print">{notice?.deployment_policy}</p>
      <fieldset disabled={disabled}>
        <legend>Required review permissions</legend>
        <label className="check-row">
          <input
            type="checkbox"
            checked={ai}
            onChange={(e) => setAi(e.target.checked)}
          />
          <span>
            <strong className="permission-title" aria-hidden="true">
              01 / AI processing
            </strong>
            {notice?.ai_label ?? "AI processing"}
          </span>
        </label>
        <label className="check-row">
          <input
            type="checkbox"
            checked={recording}
            onChange={(e) => setRecording(e.target.checked)}
          />
          <span>
            <strong className="permission-title" aria-hidden="true">
              02 / Voice and screen recording
            </strong>
            {notice?.recording_label ?? "Voice and screen recording"}
          </span>
        </label>
      </fieldset>
      <p className="fine-print">{notice?.withdrawal}</p>
      {!preview && (
        <button
          className="button"
          type="button"
          disabled={busy || saving}
          onClick={() => setReload((value) => value + 1)}
        >
          Reload consent notice
        </button>
      )}
      <button
        className="button primary"
        type="submit"
        disabled={!ai || !recording || disabled}
      >
        {busy || saving ? "Saving consent…" : "Confirm my consent"}
        <ArrowRight size={17} aria-hidden="true" />
      </button>
    </form>
  );
}
