import { ArrowRight, Clock3, ShieldCheck } from "lucide-react";
import "./client-journey.css";

/** Shared invitation surface. Continuing is navigation, never consent or capture. */
export function ClientWelcome({
  title,
  onContinue,
  preview = false,
}: {
  title: string;
  onContinue: () => void;
  preview?: boolean;
}) {
  return (
    <section className="client-welcome" aria-label="Your review invitation">
      <div className="welcome-copy">
        <p className="eyebrow">
          {preview
            ? "BEEP / Preview invitation"
            : "BEEP / Your private invitation"}
        </p>
        <h1>Let’s look at how the work gets done.</h1>
        <p className="welcome-intro">
          {preview
            ? "Walk through one workflow with us. Show the steps you take, where the work waits, and what happens when something needs another look."
            : "BEEP will ask questions to map your existing workflow. Show one recent example, explain each step, and point out where the work waits or needs another look."}
        </p>
        {!preview && (
          <p className="welcome-boundary">
            This session produces a workflow review. It does not automate your
            work or build integrations.
          </p>
        )}
        <div className="welcome-workflow">
          <span className="eyebrow">Your workflow</span>
          <p>{title}</p>
        </div>
        <p className="welcome-time">
          <Clock3 size={18} aria-hidden="true" />
          Up to 90 minutes, including 15 minutes with your facilitator.
        </p>
        <button
          className="button primary welcome-continue"
          type="button"
          onClick={() => onContinue()}
        >
          {preview ? "Prepare for my review" : "Review my permissions"}{" "}
          <ArrowRight size={18} aria-hidden="true" />
        </button>
        <p className="welcome-next">
          Next: your permissions. No devices start here.
        </p>
      </div>
      <div className="welcome-artwork" aria-hidden="true">
        <img src="/brand/amulet-3d.png" alt="" width="560" height="560" />
      </div>
      <div className="welcome-preparation">
        <span className="eyebrow">Bring one real example</span>
        <h2>The ordinary details are the useful ones.</h2>
        <p>
          Have a recent piece of work nearby. For example, follow one invoice
          from arrival to approval, including the email you send when a detail
          is missing. You do not need a presentation.
        </p>
        <p>
          The review builds towards a workflow map and evidence-linked report,
          with open questions and room for your corrections.
        </p>
      </div>
      <div className="welcome-privacy">
        <ShieldCheck size={20} aria-hidden="true" />
        <div>
          <h2>You choose what comes into view.</h2>
          <p>
            Share only the window or tab needed for the review. Close unrelated
            or sensitive material first. Your camera is never requested.
          </p>
          <p>
            Consent, connection and device controls are separate steps. Shared
            voice and screen go to AI and recording providers; you can pause new
            capture at any time.
          </p>
        </div>
      </div>
    </section>
  );
}
