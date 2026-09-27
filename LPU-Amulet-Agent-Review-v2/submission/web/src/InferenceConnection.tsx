import { useResource } from "./useResource";
import { ErrorNotice, Loading } from "./ui";

interface InferenceStatus {
  reasoning: {
    provider: "openai" | "codex";
    model: string;
    configured: boolean;
    authenticated: boolean | null;
    state:
      | "connected"
      | "not_configured"
      | "unavailable"
      | "sign_in_required"
      | "locally_configured";
    inference_verified: false;
  };
  native_voice: {
    provider: "openai";
    model: string;
    api_key_configured: boolean;
    live_verified: false;
    oauth_state: "experimental_unqualified";
  };
}

export function InferenceConnection() {
  const status = useResource<InferenceStatus>("/operator/inference");
  const reasoning = status.data?.reasoning;
  const voice = status.data?.native_voice;
  const label =
    reasoning?.provider === "codex"
      ? reasoning.state === "connected" &&
        reasoning.authenticated === true &&
        reasoning.configured === true
        ? "OAuth sign-in checked"
        : reasoning.state === "sign_in_required"
          ? "OAuth sign-in required"
          : reasoning.state === "not_configured"
            ? "OAuth not configured"
            : "OAuth status unavailable"
      : reasoning?.configured
        ? "API key configured locally; not live verified"
        : "OpenAI API key not configured";
  return (
    <section className="inference-panel" aria-label="Inference connection">
      <div className="section-heading">
        <h2>Inference connection</h2>
        <button
          className="button small"
          disabled={status.loading}
          onClick={() => void status.refresh()}
        >
          Check connection status
        </button>
      </div>
      {status.loading ? (
        <Loading label="Checking connection status…" />
      ) : reasoning && voice ? (
        <>
          <p>
            Reasoning:{" "}
            <strong>
              {reasoning.provider === "codex"
                ? "ChatGPT / Codex"
                : "OpenAI API"}
            </strong>
            {" · "}
            <span>{reasoning.model}</span>
            {" · "}
            <span>{label}</span>
          </p>
          <p>
            {voice.api_key_configured
              ? "Native voice API key configured locally; voice has not been live verified."
              : "Native voice requires a separate OpenAI API key. OAuth reasoning does not enable the voice session."}
          </p>
        </>
      ) : (
        <ErrorNotice>
          Connection status unavailable. Check again; no provider change was
          made.
        </ErrorNotice>
      )}
      <p className="fine-print">
        No inference or voice test was made by this status check. Native voice
        via OAuth remains experimental and unqualified.
      </p>
      <p className="fine-print">
        Read-only setup status. Use the local CLI to select an explicit Codex
        home or configure the voice API key. No automatic provider fallback;
        never enter provider credentials here.
      </p>
    </section>
  );
}
