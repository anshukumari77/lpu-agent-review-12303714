import {
  lazy,
  Suspense,
  useEffect,
  useId,
  useReducer,
  useRef,
  useState,
  type Dispatch,
  type ReactNode,
} from "react";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  CheckCheck,
  ChevronDown,
  ChevronUp,
  CircleHelp,
  Download,
  FileText,
  Headphones,
  Link2,
  Mic,
  MicOff,
  Monitor,
  MonitorOff,
  Pause,
  Play,
  RotateCcw,
  ShieldCheck,
  Square,
  Users,
  WifiOff,
  X,
} from "lucide-react";
import { ClientWelcome } from "./ClientWelcome";
import { ConsentForm } from "./ConsentForm";
import { Header } from "./ui";
import {
  createPreviewState,
  downloadSampleReport,
  JOURNEY_STEPS,
  previewReducer,
  SAMPLE_EDGES,
  SAMPLE_EVIDENCE,
  SAMPLE_FINDINGS,
  SAMPLE_KEY_FACT,
  SAMPLE_STEPS,
  SAMPLE_UNKNOWNS,
  STEP_LABELS,
  type PreviewAction,
  type PreviewState,
} from "./preview-data";
import "./journey-preview.css";

// This graph takes plain fixtures. Never mount ReportView or its resource hook here.
const WorkflowMap = lazy(() => import("./WorkflowMap"));
type StepProps = { state: PreviewState; dispatch: Dispatch<PreviewAction> };
const SAMPLE_LABEL = "Interactive preview · Sample workflow · No recording";

export function JourneyPreview() {
  const [state, dispatch] = useReducer(
    previewReducer,
    undefined,
    createPreviewState,
  );
  const [exploration, setExploration] = useState(0);
  const main = useRef<HTMLElement>(null);
  const first = useRef(true);
  const current = JOURNEY_STEPS.indexOf(state.step);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    main.current?.focus({ preventScroll: true });
  }, [state.step, exploration]);
  function reset(action: PreviewAction) {
    dispatch(action);
    setExploration((value) => value + 1);
  }
  return (
    <div
      className={`journey-preview jp-step-${state.step}`}
      data-preview-state={state.step}
    >
      <a className="skip-link" href="#main">
        Skip to walkthrough
      </a>
      <Header preview />
      <div className="jp-preview-label">
        <span className="jp-label-dot" aria-hidden="true" />
        {SAMPLE_LABEL}
      </div>
      <div className="jp-journey-bar">
        <div className="jp-journey-caption">
          Explore the journey <span>Designer preview</span>
        </div>
        <nav aria-label="Designer preview journey" className="jp-journey-nav">
          <ol>
            {JOURNEY_STEPS.map((step, index) => (
              <li key={step}>
                <button
                  type="button"
                  data-preview-step={step}
                  aria-label={`Explore ${STEP_LABELS[step]}`}
                  aria-current={step === state.step ? "step" : undefined}
                  onClick={() => reset({ type: "explore", step })}
                  title="Jump to this sample step. Resets preview choices and local edits."
                >
                  <span className="jp-step-number">{index + 1}</span>
                  <span>{STEP_LABELS[step]}</span>
                </button>
              </li>
            ))}
          </ol>
        </nav>
        <button
          className="jp-restart"
          type="button"
          aria-label="Restart preview"
          onClick={() => reset({ type: "restart" })}
        >
          <RotateCcw size={14} aria-hidden="true" />
          <span>Restart</span>
        </button>
      </div>
      <main
        id="main"
        className="jp-main"
        ref={main}
        tabIndex={-1}
        aria-label={`${STEP_LABELS[state.step]} · interactive preview`}
      >
        {state.step !== "welcome" && (
          <div className="jp-step-context">
            <button
              className="jp-back"
              onClick={() => dispatch({ type: "back" })}
            >
              <ArrowLeft size={15} aria-hidden="true" />
              Back
            </button>
            <span>
              Step {current + 1} of {JOURNEY_STEPS.length} ·{" "}
              {STEP_LABELS[state.step]}
            </span>
            {!state.consent && current > 1 && (
              <span className="jp-skip-note">
                Permission step skipped for exploration. No consent given.
              </span>
            )}
          </div>
        )}
        <div key={`${state.step}-${exploration}`} className="jp-step-content">
          {state.step === "welcome" && (
            <ClientWelcome
              title="Harbour Services · Fictional sample workflow"
              preview
              onContinue={() => dispatch({ type: "next" })}
            />
          )}
          {state.step === "permissions" && (
            <PermissionStep dispatch={dispatch} />
          )}
          {state.step === "setup" && (
            <SetupStep state={state} dispatch={dispatch} />
          )}
          {state.step === "introduction" && (
            <IntroductionStep dispatch={dispatch} />
          )}
          {state.step === "review" && (
            <ReviewStep state={state} dispatch={dispatch} />
          )}
          {state.step === "recap" && (
            <RecapStep state={state} dispatch={dispatch} />
          )}
          {state.step === "report" && (
            <SampleReport state={state} dispatch={dispatch} />
          )}
        </div>
      </main>
      <footer className="jp-footer">
        <span>
          Fictional Harbour Services. Synthetic content, not a client
          assessment.
        </span>
        <span>Journey links reset the sample. Refresh clears local edits.</span>
      </footer>
    </div>
  );
}

function SectionIntro({
  eyebrow,
  title,
  children,
}: {
  eyebrow: string;
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="jp-section-intro">
      <p className="eyebrow">{eyebrow}</p>
      <h1>{title}</h1>
      <div className="jp-lead">{children}</div>
    </div>
  );
}
function PermissionStep({ dispatch }: Pick<StepProps, "dispatch">) {
  return (
    <div className="jp-permission-layout">
      <aside className="jp-permission-context">
        <span className="jp-small-mark">
          <ShieldCheck size={23} aria-hidden="true" />
        </span>
        <p className="eyebrow">A deliberate choice</p>
        <h1>
          Your work.
          <br />
          Your permission.
        </h1>
        <p>
          These are the choices you would make before a real review. Read each
          one, then choose for yourself.
        </p>
        <div className="jp-callout">
          <strong>Sample consent form</strong>
          <p>
            Checking these boxes only advances this interactive preview. It does
            not grant real consent, request a device or start recording.
          </p>
        </div>
        <p className="jp-note">
          No camera is needed. For a real review, ask about recipients and
          retention before agreeing.
        </p>
      </aside>
      <div className="jp-permission-form">
        <ConsentForm
          busy={false}
          onConsent={async () => {
            dispatch({ type: "consent" });
          }}
        />
        <p className="jp-note">
          Prefer to inspect the design without choosing? The numbered journey
          links skip ahead without granting consent.
        </p>
      </div>
    </div>
  );
}
function SetupStep({ state, dispatch }: StepProps) {
  const [speaker, setSpeaker] = useState(false);
  const [screenChoice, setScreenChoice] = useState("Sample purchase register");
  return (
    <div className="jp-narrow-layout">
      <SectionIntro
        eyebrow="Preparation / Device choices"
        title="Make yourself comfortable."
      >
        <p>
          A quiet spot and one useful screen are enough. Try the controls below
          to see how preparation would work.
        </p>
      </SectionIntro>
      <div className="jp-inline-disclosure">
        <ShieldCheck size={17} aria-hidden="true" />
        <span>
          Demonstration only. No microphone, speaker or screen permission will
          be requested.
        </span>
      </div>
      <div className="jp-setup-list">
        <section className="jp-setup-row">
          <Mic size={22} aria-hidden="true" />
          <div>
            <h2>Your microphone</h2>
            <p>
              {state.mic
                ? "Sample choice: microphone on. No audio is captured."
                : "Start off. Turn on only when you choose."}
            </p>
          </div>
          <button
            className="button"
            aria-pressed={state.mic}
            onClick={() => dispatch({ type: "toggle-mic" })}
          >
            {state.mic
              ? "Reset microphone choice"
              : "Demonstrate microphone choice"}
          </button>
        </section>
        <section className="jp-setup-row">
          <Headphones size={22} aria-hidden="true" />
          <div>
            <h2>Your speaker</h2>
            <p>
              {speaker
                ? "No audio played. This demonstrates the speaker-check result, not a device test."
                : "In a real review, check you can hear the conversation."}
            </p>
          </div>
          <button className="button" onClick={() => setSpeaker(!speaker)}>
            {speaker ? "Reset speaker check" : "Demonstrate speaker check"}
          </button>
        </section>
        <section className="jp-setup-row jp-screen-choice">
          <Monitor size={22} aria-hidden="true" />
          <div>
            <h2>One screen, not everything</h2>
            <p>
              Select a fixture to stand in for a shared tab. Nothing on your
              device is visible.
            </p>
            <label htmlFor="preview-screen-choice">Sample screen choice</label>
            <select
              id="preview-screen-choice"
              value={screenChoice}
              onChange={(event) => setScreenChoice(event.target.value)}
            >
              <option>Sample purchase register</option>
              <option>No screen selected</option>
            </select>
          </div>
          <span className="jp-tag">
            {screenChoice === "No screen selected"
              ? "Not selected"
              : "Sample only"}
          </span>
        </section>
      </div>
      <div className="jp-next-row">
        <p>
          These choices are not a connection check.
          <br />
          The review starts with simulated capture off.
        </p>
        <button
          className="button primary"
          onClick={() => dispatch({ type: "next" })}
        >
          Continue to introduction <ArrowRight size={17} aria-hidden="true" />
        </button>
      </div>
    </div>
  );
}
function IntroductionStep({ dispatch }: Pick<StepProps, "dispatch">) {
  return (
    <div className="jp-introduction-layout">
      <div>
        <SectionIntro
          eyebrow="Human introduction / First 15 minutes"
          title="A conversation before the questions."
        >
          <p>
            Your facilitator starts with you: what the work is, where it begins,
            and which example is safe to show.
          </p>
        </SectionIntro>
        <ol className="jp-intro-agenda">
          <li>
            <span>01</span>
            <div>
              <h2>Choose one real workflow</h2>
              <p>
                Keep it specific. For this sample, it is a purchase invoice
                waiting for approval.
              </p>
            </div>
          </li>
          <li>
            <span>02</span>
            <div>
              <h2>Set the boundaries</h2>
              <p>
                Confirm what to share, what to leave out, and how to pause or
                ask for help.
              </p>
            </div>
          </li>
          <li>
            <span>03</span>
            <div>
              <h2>Hand over to BEEP</h2>
              <p>
                After the introduction, the AI-led review would follow your
                explanation and the selected screen.
              </p>
            </div>
          </li>
        </ol>
        <button
          className="button primary"
          onClick={() => dispatch({ type: "next" })}
        >
          Preview the handover <ArrowRight size={17} aria-hidden="true" />
        </button>
        <p className="jp-note jp-under-button">
          Skip the introduction in this sample. No meeting, timer or AI
          connection is running.
        </p>
      </div>
      <aside className="jp-intro-aside">
        <img src="/brand/amulet-3d.png" alt="" className="jp-intro-art" />
        <div className="jp-handover-card">
          <span className="eyebrow">Sample handover brief</span>
          <h2>Purchase to payment</h2>
          <p>
            “Walk me through what happens when an invoice arrives, but the
            receipt has not been confirmed.”
          </p>
          <div>
            <Users size={17} aria-hidden="true" />
            <span>Human introduction → AI-led review</span>
          </div>
        </div>
      </aside>
    </div>
  );
}

const BUSINESS_TABS = ["Invoice", "Approval", "History"] as const;
type BusinessTab = (typeof BUSINESS_TABS)[number];
const QUESTIONS: Record<BusinessTab, string> = {
  Invoice:
    "The invoice has a purchase order, but no receipt attached. What do you check before it can move forward?",
  Approval:
    "This invoice is waiting for receipt confirmation. Who confirms the goods arrived before finance can approve payment?",
  History:
    "The history shows an open request to operations. How do you follow up when the confirmation has not come back?",
};
function PreviewTabs({
  items,
  value,
  onChange,
  label,
  panelId,
}: {
  items: readonly string[];
  value: string;
  onChange: (value: string) => void;
  label: string;
  panelId: string;
}) {
  const id = useId();
  return (
    <div className="jp-tabs" role="tablist" aria-label={label}>
      {items.map((item, index) => (
        <button
          key={item}
          type="button"
          id={`${id}-${index}`}
          role="tab"
          aria-selected={value === item}
          aria-controls={panelId}
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
function BusinessScreen({
  tab,
  setTab,
  screen,
}: {
  tab: BusinessTab;
  setTab: (tab: BusinessTab) => void;
  screen: boolean;
}) {
  return (
    <section
      className="jp-business-screen"
      aria-label="Fictional Harbour Services sample business screen"
    >
      <div className="jp-screen-chrome">
        <div className="jp-window-dots" aria-hidden="true">
          <i />
          <i />
          <i />
        </div>
        <span>Harbour Services / Purchase register</span>
        <strong>SAMPLE</strong>
      </div>
      <div className="jp-business-heading">
        <div className="jp-business-initial" aria-hidden="true">
          H
        </div>
        <div>
          <strong>Harbour Services</strong>
          <span>Fictional company · Synthetic purchase workspace</span>
        </div>
        <span className="jp-business-area">Purchases</span>
      </div>
      <div className="jp-business-body">
        <div className="jp-screen-breadcrumb">
          Purchases <span>/</span> Invoices <span>/</span> INV-2048
        </div>
        <div className="jp-invoice-heading">
          <div>
            <h2>Invoice INV-2048</h2>
            <p>Bay Office Supplies · Fictional supplier</p>
          </div>
          <span className="jp-waiting-tag">Awaiting receipt</span>
        </div>
        <PreviewTabs
          items={BUSINESS_TABS}
          value={tab}
          onChange={(value) => setTab(value as BusinessTab)}
          label="Sample invoice sections"
          panelId="preview-business-panel"
        />
        <div
          className="jp-business-panel"
          id="preview-business-panel"
          role="tabpanel"
          aria-label={`Sample ${tab.toLowerCase()}`}
        >
          {tab === "Invoice" && (
            <>
              <dl className="jp-invoice-details">
                <div>
                  <dt>Purchase order</dt>
                  <dd>PO-1086</dd>
                </div>
                <div>
                  <dt>Sample amount</dt>
                  <dd>A$4,280.00</dd>
                </div>
                <div>
                  <dt>Owner</dt>
                  <dd>Finance team</dd>
                </div>
              </dl>
              <div className="jp-line-item">
                <span>Office equipment · Sample line item</span>
                <strong>A$4,280.00</strong>
              </div>
              <div className="jp-evidence-gap">
                <FileText size={20} aria-hidden="true" />
                <div>
                  <strong>Receipt attachment missing</strong>
                  <p>
                    The purchase order is linked. The sample record contains no
                    confirmation that the goods arrived.
                  </p>
                </div>
              </div>
            </>
          )}
          {tab === "Approval" && (
            <>
              <p className="jp-document-label">
                Approval route · Sample evidence
              </p>
              <ol className="jp-approval-route">
                <li className="jp-approval-complete">
                  <span className="jp-route-icon">
                    <Check size={15} aria-hidden="true" />
                  </span>
                  <div>
                    <strong>Invoice matched to purchase order</strong>
                    <p>Finance · PO-1086 linked</p>
                  </div>
                  <span className="jp-route-state">Matched</span>
                </li>
                <li className="jp-approval-waiting">
                  <span className="jp-route-icon">2</span>
                  <div>
                    <strong>Confirm goods were received</strong>
                    <p>Operations manager · Confirmation requested</p>
                    <span className="jp-missing-evidence">
                      No receipt evidence attached
                    </span>
                  </div>
                  <span className="jp-route-state">Waiting</span>
                </li>
                <li>
                  <span className="jp-route-icon">3</span>
                  <div>
                    <strong>Review for payment</strong>
                    <p>Finance · Reported next step, not completed</p>
                  </div>
                  <span className="jp-route-state">On hold</span>
                </li>
              </ol>
              <div className="jp-business-comment">
                <span className="jp-text-avatar" aria-hidden="true">
                  F
                </span>
                <div>
                  <strong>Finance · Sample note</strong>
                  <p>
                    Please confirm receipt before we release this invoice for
                    payment.
                  </p>
                </div>
              </div>
            </>
          )}
          {tab === "History" && (
            <>
              <p className="jp-document-label">
                Activity log · Scripted sequence, not live events
              </p>
              <ol className="jp-history">
                <li>
                  <span>01</span>
                  <div>
                    <strong>Invoice added to purchase register</strong>
                    <p>Purchase order PO-1086 linked by finance.</p>
                  </div>
                </li>
                <li>
                  <span>02</span>
                  <div>
                    <strong>Receipt confirmation requested</strong>
                    <p>
                      Finance asked the operations manager to confirm delivery.
                    </p>
                  </div>
                </li>
                <li>
                  <span>03</span>
                  <div>
                    <strong>Request remains open</strong>
                    <p>
                      No response is included in this sample history. Elapsed
                      time is not established.
                    </p>
                  </div>
                </li>
              </ol>
            </>
          )}
        </div>
      </div>
      <div className="jp-screen-footer">
        <Monitor size={14} aria-hidden="true" />
        <span>
          {screen
            ? "Simulated sharing on · Only this fixture is shown"
            : "Reference fixture · Simulated sharing off"}
        </span>
        <span>No integration</span>
      </div>
    </section>
  );
}
function ReviewStep({ state, dispatch }: StepProps) {
  const [tab, setTab] = useState<BusinessTab>("Approval");
  const [expanded, setExpanded] = useState(() => window.innerWidth > 820);
  const [reply, setReply] = useState(false);
  const [help, setHelp] = useState(false);
  const helpButton = useRef<HTMLButtonElement>(null);
  const helpHeading = useRef<HTMLHeadingElement>(null);
  const active = state.status === "active";
  useEffect(() => {
    if (help) helpHeading.current?.focus({ preventScroll: true });
  }, [help]);
  function closeHelp() {
    setHelp(false);
    helpButton.current?.focus();
  }
  return (
    <div className="jp-review-workspace">
      <div className="jp-room-heading">
        <div>
          <p className="eyebrow">Sample review / Purchase to payment</p>
          <h1>Show the work as it happens.</h1>
        </div>
        <span className="jp-room-boundary">
          Scripted questions. No audio or screen capture.
        </span>
      </div>
      <div className={`jp-room-layout ${expanded ? "" : "jp-room-compact"}`}>
        <div className="jp-stage">
          <BusinessScreen
            tab={tab}
            setTab={(value) => {
              setTab(value);
              setReply(false);
            }}
            screen={state.screen}
          />
          <p className="jp-stage-hint">
            Explore the invoice tabs. The companion’s sample question follows
            the evidence on this screen.
          </p>
        </div>
        <aside
          className={`jp-companion ${expanded ? "" : "jp-companion-compact"}`}
          aria-label="BEEP sample companion"
        >
          <div className="jp-companion-header">
            <span className="jp-companion-mark">
              <img
                src="/brand/amulet-monogram.svg"
                alt=""
                width="27"
                height="27"
              />
            </span>
            <div>
              <strong>BEEP companion</strong>
              <span>
                {active
                  ? "Scripted preview · Not connected"
                  : state.status === "paused"
                    ? "Preview paused · Capture off"
                    : "Disconnected · Sample state"}
              </span>
            </div>
            <button
              className="jp-icon-button"
              aria-label={expanded ? "Compact companion" : "Expand companion"}
              aria-expanded={expanded}
              aria-controls="preview-companion-content"
              onClick={() => setExpanded(!expanded)}
            >
              {expanded ? (
                <ChevronDown size={18} aria-hidden="true" />
              ) : (
                <ChevronUp size={18} aria-hidden="true" />
              )}
            </button>
          </div>
          <div className="jp-companion-content" id="preview-companion-content">
            {state.status !== "active" ? (
              <div className="jp-room-state" role="status">
                {state.status === "paused" ? (
                  <Pause size={22} aria-hidden="true" />
                ) : (
                  <WifiOff size={22} aria-hidden="true" />
                )}
                <h2>
                  {state.status === "paused"
                    ? "Take a moment."
                    : "Connection interrupted · sample state"}
                </h2>
                <p>
                  {state.status === "paused"
                    ? "Simulated microphone and screen sharing are off. Resume when you are ready, then turn each on yourself."
                    : "This is a designed recovery state, not a real outage. Retry leaves microphone and screen sharing off."}
                </p>
                {state.status === "disconnected" && (
                  <button
                    className="button"
                    onClick={() => dispatch({ type: "resume" })}
                  >
                    Retry simulated connection
                  </button>
                )}
              </div>
            ) : (
              <div className="jp-active-question">
                <p className="eyebrow">Current question · Scripted</p>
                <h2>
                  {reply
                    ? "Where would you look for that confirmation if it is not attached here?"
                    : QUESTIONS[tab]}
                </h2>
                <p className="jp-question-context">
                  <Link2 size={13} aria-hidden="true" />
                  Based on sample {tab.toLowerCase()} evidence
                </p>
              </div>
            )}
            {expanded && (
              <>
                <div className="jp-observations">
                  <p className="jp-document-label">
                    Screen context · Sample observations
                  </p>
                  <div>
                    <span>
                      <FileText size={12} aria-hidden="true" />
                      INV-2048
                    </span>
                    <span>
                      {tab === "History" ? "Open request" : "Receipt missing"}
                    </span>
                    <span>Approval on hold</span>
                  </div>
                </div>
                <section
                  className="jp-transcript"
                  aria-label="Scripted transcript"
                >
                  <div className="jp-transcript-heading">
                    <h3>Conversation</h3>
                    <span>Sample, not transcribed</span>
                  </div>
                  {reply && (
                    <p>
                      <strong>BEEP</strong>
                      <span>{QUESTIONS[tab]}</span>
                    </p>
                  )}
                  {reply && (
                    <p className="jp-sample-reply">
                      <strong>Sample participant</strong>
                      <span>
                        I ask the operations manager to confirm the goods
                        arrived. Finance holds payment until that confirmation
                        comes back.
                      </span>
                    </p>
                  )}
                  <button
                    className="jp-text-action"
                    disabled={!active}
                    onClick={() => setReply(!reply)}
                  >
                    {reply ? "Reset sample reply" : "Show sample reply"}
                    <ArrowRight size={14} aria-hidden="true" />
                  </button>
                </section>
              </>
            )}
            {help && (
              <section
                className="jp-help"
                aria-label="Preview help"
                onKeyDown={(event) => {
                  if (event.key === "Escape") closeHelp();
                }}
              >
                <div>
                  <h3 ref={helpHeading} tabIndex={-1}>
                    You can stop and ask.
                  </h3>
                  <button
                    className="jp-icon-button"
                    aria-label="Close help"
                    onClick={closeHelp}
                  >
                    <X size={17} aria-hidden="true" />
                  </button>
                </div>
                <p>
                  In a real review, pause before discussing anything sensitive
                  with your facilitator. This sample does not contact a person
                  or support service.
                </p>
                <strong>No message has been sent.</strong>
              </section>
            )}
          </div>
          <div className="jp-capture-summary">
            <span>{state.mic ? "Mic: simulated on" : "Mic: off"}</span>
            <span>{state.screen ? "Screen: simulated on" : "Screen: off"}</span>
            <span>Recording: none</span>
          </div>
          <div className="jp-companion-controls">
            <button
              className="jp-control"
              aria-label="Simulate microphone"
              aria-pressed={state.mic}
              disabled={!active}
              onClick={() => dispatch({ type: "toggle-mic" })}
            >
              {state.mic ? (
                <Mic size={19} aria-hidden="true" />
              ) : (
                <MicOff size={19} aria-hidden="true" />
              )}
              <span>{state.mic ? "Mic on*" : "Mic off"}</span>
            </button>
            <button
              className="jp-control"
              aria-label="Simulate screen sharing"
              aria-pressed={state.screen}
              disabled={!active}
              onClick={() => dispatch({ type: "toggle-screen" })}
            >
              {state.screen ? (
                <Monitor size={19} aria-hidden="true" />
              ) : (
                <MonitorOff size={19} aria-hidden="true" />
              )}
              <span>{state.screen ? "Screen on*" : "Screen off"}</span>
            </button>
            <button
              className="jp-control"
              aria-label={
                state.status === "paused" ? "Resume preview" : "Pause preview"
              }
              disabled={state.status === "disconnected"}
              onClick={() =>
                dispatch({
                  type: state.status === "paused" ? "resume" : "pause",
                })
              }
            >
              {state.status === "paused" ? (
                <Play size={19} aria-hidden="true" />
              ) : (
                <Pause size={19} aria-hidden="true" />
              )}
              <span>{state.status === "paused" ? "Resume" : "Pause"}</span>
            </button>
            <button
              ref={helpButton}
              className="jp-control"
              aria-label="Preview help"
              aria-expanded={help}
              onClick={() => (help ? closeHelp() : setHelp(true))}
            >
              <CircleHelp size={19} aria-hidden="true" />
              <span>Help</span>
            </button>
          </div>
          <button
            className="jp-finish"
            onClick={() => dispatch({ type: "next" })}
          >
            <Square size={12} aria-hidden="true" />
            Finish sample review <ArrowRight size={15} aria-hidden="true" />
          </button>
        </aside>
      </div>
      <div className="jp-state-tools">
        <span>Designer controls</span>
        <button
          className="jp-text-action"
          disabled={state.status === "disconnected"}
          onClick={() => dispatch({ type: "disconnect" })}
        >
          <WifiOff size={13} aria-hidden="true" />
          Simulate disconnection
        </button>
        <span>* Controls change local sample state only.</span>
      </div>
    </div>
  );
}
function RecapStep({ state, dispatch }: StepProps) {
  return (
    <div className="jp-recap-layout">
      <div>
        <SectionIntro
          eyebrow="Wrap-up / Check the story"
          title="Does this sound like the work?"
        >
          <p>
            Before a report, there is a chance to put the facts right. Try
            editing this sample note.
          </p>
        </SectionIntro>
        <div className="jp-recap-marker">
          <CheckCheck size={20} aria-hidden="true" />
          <div>
            <strong>Sample review finished</strong>
            <p>No recording was made. No transcript or report was sent.</p>
          </div>
        </div>
        <form
          className="jp-fact-form"
          onSubmit={(event) => {
            event.preventDefault();
            dispatch({ type: "save-fact" });
          }}
        >
          <label htmlFor="preview-key-fact">Key fact to check</label>
          <p className="jp-note">
            From the scripted participant response · sample-E02
          </p>
          <textarea
            id="preview-key-fact"
            value={state.factDraft}
            maxLength={2000}
            onChange={(event) =>
              dispatch({ type: "fact-draft", value: event.target.value })
            }
          />
          <button
            className="button"
            disabled={
              !state.factDraft.trim() ||
              state.factDraft.trim() === state.keyFact
            }
            type="submit"
          >
            Save sample fact
          </button>
          <p className="jp-note">
            Saved in memory for this preview only. Restart, a journey link or
            refresh resets it.
          </p>
        </form>
        {state.notice && (
          <p className="jp-local-notice" role="status">
            {state.notice}
          </p>
        )}
        <button
          className="button primary"
          disabled={state.factDraft.trim() !== state.keyFact}
          onClick={() => dispatch({ type: "next" })}
        >
          Open sample report <ArrowRight size={17} aria-hidden="true" />
        </button>
        {state.factDraft.trim() !== state.keyFact && (
          <p className="jp-note">
            Save your sample fact before opening the report.
          </p>
        )}
      </div>
      <aside className="jp-recap-aside">
        <p className="eyebrow">Still to establish</p>
        <h2>Some things need more evidence.</h2>
        <ul>
          {SAMPLE_UNKNOWNS.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
        <p>Unknowns belong in the report, not in a savings estimate.</p>
        <div className="jp-recap-outcome">
          <FileText size={24} aria-hidden="true" />
          <span>
            Next: an evidence-linked
            <br />
            <strong>sample workflow report</strong>
          </span>
        </div>
      </aside>
    </div>
  );
}
const REPORT_TABS = ["Overview", "Workflow map", "Evidence"] as const;
function SampleReport({ state, dispatch }: StepProps) {
  const [tab, setTab] = useState<string>("Overview");
  const [selectedEvidence, setSelectedEvidence] = useState<string[] | null>(
    null,
  );
  const [downloadNotice, setDownloadNotice] = useState("");
  const evidencePanel = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (selectedEvidence && tab === "Evidence")
      evidencePanel.current?.focus({ preventScroll: true });
  }, [tab, selectedEvidence]);
  function openEvidence(ids: string[]) {
    setSelectedEvidence(ids);
    setTab("Evidence");
  }
  function download() {
    try {
      downloadSampleReport(state);
      setDownloadNotice(
        "Sample HTML download prepared locally. No data was sent.",
      );
    } catch {
      setDownloadNotice(
        "The browser could not prepare the sample download. Your local notes are still here; try again.",
      );
    }
  }
  return (
    <article className="jp-report" aria-label="Sample workflow report">
      <header className="jp-report-header">
        <div>
          <p className="eyebrow">Amulet BEEP / Sample workflow review</p>
          <h1>Where the approval waits.</h1>
          <p>Harbour Services · Fictional purchase-to-payment workflow</p>
        </div>
        <button className="button" onClick={download}>
          <Download size={17} aria-hidden="true" />
          Download sample HTML
        </button>
      </header>
      <div className="jp-report-disclosure">
        <strong>SAMPLE REPORT</strong>
        <span>
          Synthetic evidence. Not a client assessment, live analysis or verified
          operating result.
        </span>
      </div>
      {downloadNotice && (
        <p className="jp-local-notice" role="status">
          {downloadNotice}
        </p>
      )}
      <PreviewTabs
        items={REPORT_TABS}
        value={tab}
        onChange={(value) => {
          setTab(value);
          setSelectedEvidence(null);
        }}
        label="Sample report sections"
        panelId="preview-report-panel"
      />
      <div
        id="preview-report-panel"
        className="jp-report-panel"
        role="tabpanel"
        aria-label={tab}
      >
        {tab === "Overview" && (
          <>
            <div className="jp-report-overview">
              <div className="jp-report-summary">
                <p className="eyebrow">The short version</p>
                <h2>
                  The invoice is visible.
                  <br />
                  The receipt is not.
                </h2>
                <p>
                  In this fictional example, finance has the invoice and its
                  purchase order. The record is waiting on operations to confirm
                  receipt before the reported payment-review step can proceed.
                </p>
                <p>
                  This is one example, not evidence of a recurring bottleneck.
                  The next useful step would be to check the handoff against
                  more cases.
                </p>
                <div className="jp-working-fact">
                  <span className="jp-document-label">
                    Working fact ·{" "}
                    {state.keyFact === SAMPLE_KEY_FACT
                      ? "Sample participant account"
                      : "Locally edited preview text"}
                  </span>
                  <p>{state.keyFact}</p>
                  {state.keyFact !== SAMPLE_KEY_FACT && (
                    <small>
                      The original synthetic findings and evidence have not been
                      regenerated.
                    </small>
                  )}
                </div>
              </div>
              <aside className="jp-report-scope">
                <h3>In this sample</h3>
                <dl>
                  <div>
                    <dt>Workflow</dt>
                    <dd>Purchase to payment</dd>
                  </div>
                  <div>
                    <dt>Evidence</dt>
                    <dd>Scripted screen + conversation</dd>
                  </div>
                  <div>
                    <dt>Decision boundary</dt>
                    <dd>Human confirmation before payment review</dd>
                  </div>
                  <div>
                    <dt>Not performed</dt>
                    <dd>No approval, payment or integration</dd>
                  </div>
                </dl>
              </aside>
            </div>
            <section className="jp-findings">
              <div className="jp-report-section-heading">
                <span>01 / What the sample shows</span>
                <h2>Findings, with their evidence.</h2>
              </div>
              <ol>
                {SAMPLE_FINDINGS.map((finding, index) => (
                  <li key={finding.id}>
                    <span className="jp-finding-number">0{index + 1}</span>
                    <div>
                      <span
                        className={`jp-claim-status jp-claim-${finding.status}`}
                      >
                        {finding.status} · Sample
                      </span>
                      <h3>{finding.text}</h3>
                      <div className="jp-evidence-links">
                        {finding.evidence_ids.map((id) => (
                          <button
                            key={id}
                            onClick={() => openEvidence([id])}
                            aria-label={`View sample evidence ${id}`}
                          >
                            <Link2 size={13} aria-hidden="true" />
                            {id}
                          </button>
                        ))}
                      </div>
                    </div>
                  </li>
                ))}
              </ol>
            </section>
            <section className="jp-report-unknowns">
              <div>
                <p className="eyebrow">02 / Not established</p>
                <h2>Evidence before a business case.</h2>
                <p>
                  No savings, cost or ROI estimate can be supported by this
                  sample.
                </p>
              </div>
              <ul>
                {SAMPLE_UNKNOWNS.map((item) => (
                  <li key={item}>
                    <span>{item}</span>
                    <strong>Unknown</strong>
                  </li>
                ))}
              </ul>
            </section>
            <section className="jp-next-questions">
              <div className="jp-report-section-heading">
                <span>03 / Suggested questions, not approved actions</span>
                <h2>What to check next.</h2>
              </div>
              <ol>
                <li>
                  Where should receipt confirmation be recorded, and who owns
                  it?
                </li>
                <li>
                  How often does an invoice reach this point without that
                  confirmation?
                </li>
                <li>
                  What approval controls must remain in place if the handoff
                  changes?
                </li>
              </ol>
            </section>
            <section className="jp-correction">
              <div>
                <p className="eyebrow">Your notes / Local sample copy</p>
                <h2>Something needs correcting?</h2>
                <p>
                  Try the correction experience. This stores a note in memory,
                  not a real report revision.
                </p>
              </div>
              <div>
                <form
                  onSubmit={(event) => {
                    event.preventDefault();
                    setDownloadNotice("");
                    dispatch({ type: "save-correction" });
                  }}
                >
                  <label htmlFor="preview-correction">
                    Add a sample correction
                  </label>
                  <textarea
                    id="preview-correction"
                    maxLength={2000}
                    value={state.correctionDraft}
                    onChange={(event) =>
                      dispatch({
                        type: "correction-draft",
                        value: event.target.value,
                      })
                    }
                    placeholder="For example: the warehouse team confirms receipt, not the operations manager."
                  />
                  <button
                    className="button"
                    disabled={!state.correctionDraft.trim()}
                    type="submit"
                  >
                    Save local correction
                  </button>
                  <p className="jp-note">
                    Nothing is sent. Restart, journey links and refresh clear
                    this note.
                  </p>
                </form>
                {state.notice && (
                  <p className="jp-local-notice" role="status">
                    {state.notice}
                  </p>
                )}
                {state.correction && (
                  <div className="jp-saved-correction">
                    <span className="jp-document-label">
                      Local sample correction · Not verified
                    </span>
                    <p>{state.correction}</p>
                  </div>
                )}
              </div>
            </section>
          </>
        )}
        {tab === "Workflow map" && (
          <section className="jp-sample-map">
            <h2>The route through the work.</h2>
            <p className="jp-note">
              SAMPLE map. The payment-review step is reported, not observed. The
              magenta rework route is a hypothesis to test.
            </p>
            <Suspense
              fallback={
                <div className="jp-map-fallback">
                  <p>
                    Opening the local sample map. You can also read the sequence
                    below.
                  </p>
                  <ol>
                    {SAMPLE_STEPS.map((step) => (
                      <li key={step.id}>
                        {step.title} · {step.actor}
                      </li>
                    ))}
                  </ol>
                </div>
              }
            >
              <WorkflowMap
                steps={SAMPLE_STEPS}
                edges={SAMPLE_EDGES}
                evidence={SAMPLE_EVIDENCE}
              />
            </Suspense>
          </section>
        )}
        {tab === "Evidence" && (
          <div className="jp-report-evidence" ref={evidencePanel} tabIndex={-1}>
            <div className="jp-report-section-heading">
              <span>
                Synthetic source material / Not captured from a session
              </span>
              <h2>
                {selectedEvidence
                  ? "The evidence behind this finding."
                  : "Read the sample evidence."}
              </h2>
            </div>
            {selectedEvidence && (
              <button
                className="jp-text-action"
                onClick={() => setSelectedEvidence(null)}
              >
                Show all sample evidence{" "}
                <ArrowRight size={14} aria-hidden="true" />
              </button>
            )}
            <ol>
              {SAMPLE_EVIDENCE.filter(
                (item) =>
                  !selectedEvidence || selectedEvidence.includes(item.id),
              ).map((item) => (
                <li key={item.id} id={`preview-${item.id}`}>
                  <div className="jp-evidence-meta">
                    <strong>{item.id}</strong>
                    <span>
                      {item.kind === "transcript"
                        ? "Scripted participant response"
                        : "Synthetic screen observation"}
                    </span>
                    <span>SAMPLE</span>
                  </div>
                  <p>{item.text}</p>
                  <small>{item.source_ref}</small>
                </li>
              ))}
            </ol>
          </div>
        )}
      </div>
      <footer className="jp-report-end">
        <FileText size={18} aria-hidden="true" />
        <span>End of sample report. No operational action has been taken.</span>
        <button
          className="jp-text-action"
          onClick={() => dispatch({ type: "back" })}
        >
          Return to wrap-up <ArrowLeft size={14} aria-hidden="true" />
        </button>
      </footer>
    </article>
  );
}
