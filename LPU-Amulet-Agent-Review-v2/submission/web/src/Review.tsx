import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import {
  LiveKitRoom,
  RoomAudioRenderer,
  VideoTrack,
  useParticipants,
  useTracks,
} from "@livekit/components-react";
import {
  Room,
  RoomEvent,
  Track,
  ConnectionState,
  type RemoteParticipant,
  type RemoteTrackPublication,
} from "livekit-client";
import {
  ArrowRight,
  ChevronDown,
  ChevronUp,
  Clock3,
  Headphones,
  Mic,
  MicOff,
  Monitor,
  MonitorOff,
  Pause,
  Play,
  Square,
} from "lucide-react";
import type {
  Actor,
  ControlAction,
  Evidence,
  RoomCredentials,
  Session,
  SessionView,
} from "./types";
import { api, ApiError, errorMessage, sessionPath } from "./api";
import { useResource } from "./useResource";
import { CaptureController } from "./capture";
import {
  captureEligible,
  clocks,
  duration,
  hasConsent,
  statusLabel,
} from "./session";
import { ConsentForm } from "./ConsentForm";
import { ClientWelcome } from "./ClientWelcome";
import { ReportView } from "./ReportView";
import { ErrorNotice, Loading, Tabs } from "./ui";
import { runtimeReadiness } from "./readiness";

type NextStep = {
  title: string;
  detail: string;
  label?: string;
  act?: () => void;
  blocked?: boolean;
  href?: string;
};

function SelectedScreen({
  room,
  sessionId,
  role,
  connected,
}: {
  room: Room;
  sessionId: string;
  role: Actor["role"];
  connected: boolean;
}) {
  const screens = useTracks([Track.Source.ScreenShare], {
    room,
    onlySubscribed: false,
  });
  const selected = screens.find(
    (ref) =>
      ref.participant.identity === `client:${sessionId}` &&
      ref.publication.track &&
      !ref.publication.isMuted,
  );
  return (
    <section className="screen-stage" aria-label="Selected screen">
      <div className="stage-toolbar">
        <span>
          <Monitor size={15} /> Selected screen
        </span>
        <span>
          {selected
            ? role === "client"
              ? "Local preview"
              : "Client screen"
            : "Not shared"}
        </span>
      </div>
      <div className={`stage-content ${selected ? "has-screen" : ""}`}>
        {selected ? (
          <VideoTrack
            trackRef={selected}
            className="selected-video"
            muted={true}
          />
        ) : (
          <div className="stage-empty">
            <Monitor size={38} strokeWidth={1.25} aria-hidden="true" />
            <h2>
              {role === "client"
                ? "Your example will appear here."
                : "The client’s example will appear here."}
            </h2>
            <p>
              {role !== "client"
                ? "Only the client can share a window or browser tab. Follow Your next step above; this empty space does not mean the call or recording is ready."
                : connected
                  ? "Follow Your next step above before showing the example. When asked, choose one window or browser tab. You can stop sharing at any time."
                  : "Follow Your next step above. Consent, joining the call and devices are separate actions. Nothing starts automatically."}
            </p>
            <p>
              Shared voice and screen are sent to AI and recording providers.
              Stopping capture does not delete material already received; ask
              your facilitator about retention.
            </p>
          </div>
        )}
      </div>
      <div className="stage-caption">
        <span className="status-dot" />
        {selected
          ? "A screen preview is not proof of server recording or AI observation."
          : "The camera is never requested. Screen audio is not requested."}
      </div>
    </section>
  );
}
function Conversation({
  room,
  session,
  events,
  eventError,
}: {
  room: Room;
  session: Session;
  events: Evidence[];
  eventError?: string;
}) {
  const [expanded, setExpanded] = useState(true);
  const logId = useId();
  const participants = useParticipants({ room });
  const agentPresent = participants.some((participant) => participant.isAgent);
  const facilitatorPresent = participants.some(
    (participant) => participant.identity === `facilitator:${session.id}`,
  );
  const transcripts = events.filter(
    (event) => event.kind === "transcript" || event.kind === "correction",
  );
  const observations = events.filter(
    (event) => event.kind === "screen_observation",
  );
  const lastObservation = observations.at(-1);
  return (
    <aside
      className={`conversation-panel beep-companion ${expanded ? "" : "is-collapsed"}`}
      aria-label="BEEP companion"
    >
      <div className="conversation-heading">
        <div className="companion-identity">
          <img
            src="/brand/amulet-monogram.svg"
            alt="Amulet"
            width="32"
            height="32"
          />
          <div>
            <div className="eyebrow">BEEP companion</div>
            <h2>Conversation</h2>
          </div>
        </div>
        <div className="speaker-presence">
          <span
            className={`status-dot ${session.status === "active" && agentPresent ? "live" : ""}`}
          />
          {session.status === "introduction"
            ? facilitatorPresent
              ? "Facilitator connected"
              : "Waiting for your facilitator"
            : session.status === "active"
              ? agentPresent
                ? "AI participant connected"
                : "Waiting for the AI participant"
              : "Review conversation"}
        </div>
        <button
          type="button"
          className="text-button companion-toggle"
          aria-expanded={expanded}
          aria-controls={logId}
          onClick={() => setExpanded((value) => !value)}
        >
          {expanded ? "Hide conversation" : "Show conversation"}
          {expanded ? (
            <ChevronUp size={16} aria-hidden="true" />
          ) : (
            <ChevronDown size={16} aria-hidden="true" />
          )}
        </button>
      </div>
      {eventError && <ErrorNotice>{eventError}</ErrorNotice>}
      <div
        id={logId}
        hidden={!expanded}
        className="conversation-log"
        role="log"
        aria-label="Saved conversation"
        aria-live="polite"
        aria-relevant="additions"
      >
        {transcripts.length ? (
          transcripts.map((event) => (
            <article className={`utterance ${event.actor}`} key={event.id}>
              <div>
                <strong>
                  {event.actor === "agent"
                    ? "BEEP"
                    : event.actor === "client"
                      ? "Client"
                      : event.actor === "facilitator"
                        ? "Facilitator"
                        : event.actor}
                </strong>
                <time>{duration(event.at_ms / 1000)}</time>
              </div>
              <p>{event.text}</p>
              {event.kind === "correction" && (
                <span className="fine-print">Client correction</span>
              )}
            </article>
          ))
        ) : (
          <div className="conversation-empty">
            <Headphones size={22} aria-hidden="true" />
            <p>
              {session.status === "introduction"
                ? "This is your time with the facilitator. AI interviewing begins only after handover."
                : "Saved transcript turns will appear here when the service receives them."}
            </p>
            <p className="fine-print">
              Silence here does not confirm that capture or transcription is
              working.
            </p>
          </div>
        )}
      </div>
      <div className="capture-status">
        <div>
          <span>Recording service</span>
          <strong>{session.recording_status.replace(/_/g, " ")}</strong>
        </div>
        <p>Server-reported recorder state, independent of this preview.</p>
        <div>
          <span>Last saved screen observation</span>
          <strong>
            {lastObservation
              ? duration(lastObservation.at_ms / 1000)
              : "None yet"}
          </strong>
        </div>
        <p>
          Saved observations are evidence, not a guarantee of current screen
          reception.
        </p>
      </div>
    </aside>
  );
}
function useSessionEvents(sessionId: string, enabled: boolean) {
  const [events, setEvents] = useState<Evidence[]>([]);
  const [cursor, setCursor] = useState(0);
  const resource = useResource<{ events: Evidence[] }>(
    enabled ? `${sessionPath(sessionId)}/events?after_seq=${cursor}` : null,
    2000,
  );
  useEffect(() => {
    if (!resource.data?.events.length) return;
    const incoming = resource.data.events;
    setEvents((previous) =>
      Array.from(
        new Map(
          [...previous, ...incoming].map((event) => [event.id, event]),
        ).values(),
      )
        .sort((a, b) => a.seq - b.seq)
        .slice(-200),
    );
    const seq = Math.max(...incoming.map((event) => event.seq));
    setCursor((previous) => Math.max(previous, seq));
  }, [resource.data]);
  return { events, error: resource.error };
}

export function Review({
  sessionId,
  actor,
  onStopAvailable,
  roomInstance,
  runtime = runtimeReadiness(),
}: {
  sessionId: string;
  actor: Actor;
  onStopAvailable?: (stop: (() => void) | null) => void;
  roomInstance?: Room;
  runtime?: ReturnType<typeof runtimeReadiness>;
}) {
  const resource = useResource<SessionView>(sessionPath(sessionId), 2000);
  const [room] = useState(
    () =>
      roomInstance ??
      new Room({
        adaptiveStream: true,
        dynacast: true,
        stopLocalTrackOnUnpublish: true,
      }),
  );
  const participants = useParticipants({ room });
  const facilitatorPresent = participants.some(
    (person) => person.identity === `facilitator:${sessionId}`,
  );
  const clientPresent = participants.some(
    (person) => person.identity === `client:${sessionId}`,
  );
  const agentPresent = participants.some((person) => person.isAgent);
  const clientScreenPublished = useTracks([Track.Source.ScreenShare], {
    room,
    onlySubscribed: false,
  }).some(
    (ref) =>
      ref.participant.identity === `client:${sessionId}` &&
      !!ref.publication.track &&
      !ref.publication.isMuted,
  );
  const [capture] = useState(
    () => new CaptureController(room.localParticipant),
  );
  const media = useSyncExternalStore(
    capture.subscribe,
    capture.getSnapshot,
    capture.getSnapshot,
  );
  const [credentials, setCredentials] = useState<RoomCredentials | null>(null);
  const credentialsRef = useRef<RoomCredentials | null>(null);
  const generation = useRef(0);
  const joinRequest = useRef<AbortController | null>(null);
  const [connection, setConnection] = useState<
    "offline" | "joining" | "connected" | "interrupted"
  >("offline");
  const [busy, setBusy] = useState("");
  const busyRef = useRef(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [tab, setTab] = useState("Review room");
  const [preparedSessionId, setPreparedSessionId] = useState<string | null>(
    null,
  );
  const [now, setNow] = useState(Date.now());
  const [audioBlocked, setAudioBlocked] = useState(false);
  const alive = useRef(true);
  const mutation = useRef<AbortController | null>(null);
  const view = resource.data;
  const verified =
    view?.session.id === sessionId &&
    view.role === actor.role &&
    (actor.role === "operator" || actor.session_id === sessionId);
  const session = verified ? view.session : undefined;
  const eligible =
    !!session &&
    captureEligible(session) &&
    actor.role !== "operator" &&
    (actor.role === "client" || session.status === "introduction");
  // Configuration, recorder readback and RTC presence are separate checks.
  const recordingReady =
    session?.recording_status === "recording" && !!session.egress_id;
  const admissionReady =
    eligible &&
    runtime.ready &&
    recordingReady &&
    !!session &&
    clocks(session, now).totalRemaining > 0;
  const voiceReady = session?.status !== "active" || agentPresent;
  const latest = useRef({ session, eligible: admissionReady });
  latest.current = { session, eligible: admissionReady };
  const eventFeed = useSessionEvents(sessionId, !!session);
  const stopLocal = useCallback(() => {
    generation.current += 1;
    joinRequest.current?.abort();
    capture.stopNow();
    room.remoteParticipants.forEach((participant) =>
      participant.trackPublications.forEach((publication) => {
        publication.track?.detach();
        publication.setSubscribed(false);
      }),
    );
    credentialsRef.current = null;
    void room.disconnect(true).catch(() => {});
    if (alive.current) {
      setCredentials(null);
      setConnection("offline");
    }
  }, [capture, room]);
  const onRoomError = useCallback(() => {
    stopLocal();
    if (alive.current)
      setError(
        "The media room could not connect. Check the connection and runtime configuration, then try again.",
      );
  }, [stopLocal]);
  const previousEpoch = useRef<number | undefined>(undefined);
  useLayoutEffect(() => {
    // Voice gates new/pending devices, never room admission or existing tracks.
    // Fence at the RTC event too: React may batch a leave and return together.
    const reconcileVoice = () =>
      capture.setAcquisitionReady(
        latest.current.session?.status !== "active" ||
          Array.from(room.remoteParticipants.values()).some(
            (person) => person.isAgent,
          ),
      );
    reconcileVoice();
    room
      .on(RoomEvent.ParticipantConnected, reconcileVoice)
      .on(RoomEvent.ParticipantDisconnected, reconcileVoice);
    return () => {
      room
        .off(RoomEvent.ParticipantConnected, reconcileVoice)
        .off(RoomEvent.ParticipantDisconnected, reconcileVoice);
    };
  }, [capture, room, session?.status]);
  useLayoutEffect(() => {
    if (
      !eligible ||
      !runtime.ready ||
      !recordingReady ||
      resource.error ||
      (previousEpoch.current !== undefined &&
        previousEpoch.current !== session?.consent_epoch)
    )
      stopLocal();
    previousEpoch.current = session?.consent_epoch;
  }, [
    eligible,
    runtime.ready,
    recordingReady,
    session?.consent_epoch,
    resource.error,
    stopLocal,
  ]);
  useEffect(() => {
    alive.current = true;
    const leave = () => stopLocal();
    onStopAvailable?.(stopLocal);
    window.addEventListener("pagehide", leave);
    window.addEventListener("offline", leave);
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => {
      alive.current = false;
      clearInterval(tick);
      mutation.current?.abort();
      stopLocal();
      window.removeEventListener("pagehide", leave);
      window.removeEventListener("offline", leave);
      onStopAvailable?.(null);
    };
  }, [onStopAvailable, stopLocal]);
  // Subscribe only to this session's allowed humans and server-granted agents. Never auto-subscribe.
  useEffect(() => {
    const subscribe = (
      publication: RemoteTrackPublication,
      participant: RemoteParticipant,
    ) => {
      const status = latest.current.session?.status;
      const allowed =
        !!credentialsRef.current &&
        ((participant.identity === `client:${sessionId}` &&
          [Track.Source.Microphone, Track.Source.ScreenShare].includes(
            publication.source,
          )) ||
          (!!credentialsRef.current &&
            participant.identity === `facilitator:${sessionId}` &&
            status === "introduction" &&
            publication.source === Track.Source.Microphone) ||
          (!!credentialsRef.current &&
            participant.isAgent &&
            status === "active" &&
            publication.kind === Track.Kind.Audio));
      publication.setSubscribed(allowed);
    };
    const reconcile = () =>
      room.remoteParticipants.forEach((participant) =>
        participant.trackPublications.forEach((publication) =>
          subscribe(publication, participant),
        ),
      );
    const connected = () => {
      const expected = credentialsRef.current;
      if (
        !expected ||
        !latest.current.eligible ||
        room.name !== expected.room_name ||
        room.localParticipant.identity !== expected.participant_identity
      ) {
        stopLocal();
        if (alive.current)
          setError(
            "The room identity could not be verified. Capture remains stopped.",
          );
        return;
      }
      capture.allow();
      reconcile();
      setConnection("connected");
      setAudioBlocked(!room.canPlaybackAudio);
    };
    const interrupted = () => {
      stopLocal();
      if (alive.current) {
        setConnection("interrupted");
        setNotice(
          "Connection interrupted. Local capture is stopped. Reconnect, then explicitly turn on your microphone and choose a screen again.",
        );
      }
    };
    const disconnected = () => {
      capture.stopNow();
      if (alive.current)
        setConnection((previous) =>
          previous === "connected" ? "interrupted" : previous,
        );
    };
    const audioChanged = () => setAudioBlocked(!room.canPlaybackAudio);
    room
      .on(RoomEvent.Connected, connected)
      .on(RoomEvent.Reconnecting, interrupted)
      .on(RoomEvent.SignalReconnecting, interrupted)
      .on(RoomEvent.Disconnected, disconnected)
      .on(RoomEvent.TrackPublished, subscribe)
      .on(RoomEvent.ParticipantConnected, reconcile)
      .on(RoomEvent.AudioPlaybackStatusChanged, audioChanged);
    reconcile();
    return () => {
      room
        .off(RoomEvent.Connected, connected)
        .off(RoomEvent.Reconnecting, interrupted)
        .off(RoomEvent.SignalReconnecting, interrupted)
        .off(RoomEvent.Disconnected, disconnected)
        .off(RoomEvent.TrackPublished, subscribe)
        .off(RoomEvent.ParticipantConnected, reconcile)
        .off(RoomEvent.AudioPlaybackStatusChanged, audioChanged);
    };
  }, [room, capture, sessionId, session?.status, stopLocal]);
  useEffect(() => {
    if (
      session?.started_at &&
      clocks(session, now).totalRemaining === 0 &&
      eligible
    ) {
      stopLocal();
      setNotice(
        "The session time limit has been reached. Capture is stopped; waiting for the server to close the review.",
      );
    }
  }, [now, session, eligible, stopLocal]);

  async function join() {
    if (!latest.current.eligible || connection === "joining" || busyRef.current)
      return;
    const expectedSession = latest.current.session!;
    const epoch = expectedSession.consent_epoch;
    const stamp = ++generation.current;
    const abort = new AbortController();
    joinRequest.current = abort;
    setConnection("joining");
    setError("");
    setNotice("");
    try {
      const token = await api<RoomCredentials>(
        `${sessionPath(sessionId)}/room-token`,
        { body: {}, signal: abort.signal },
      );
      if (
        !alive.current ||
        abort.signal.aborted ||
        stamp !== generation.current ||
        !latest.current.eligible ||
        latest.current.session?.consent_epoch !== epoch
      )
        return;
      if (
        token.participant_identity !== `${actor.role}:${sessionId}` ||
        token.room_name !== expectedSession.room_name ||
        !token.participant_token ||
        !/^wss?:\/\//.test(token.server_url)
      )
        throw new ApiError(
          403,
          "The room credentials do not match this review. Capture remains stopped.",
        );
      credentialsRef.current = token;
      setCredentials(token);
    } catch (error) {
      if (alive.current && stamp === generation.current) {
        setError(errorMessage(error));
        setConnection("offline");
      }
    }
  }
  async function mutate(suffix: string, body: unknown) {
    if (busyRef.current)
      throw new ApiError(
        409,
        "Another review action is pending. Wait for it to finish before trying again.",
      );
    busyRef.current = true;
    setBusy(suffix);
    setError("");
    resource.cancel();
    const abort = new AbortController();
    mutation.current = abort;
    try {
      await api(`${sessionPath(sessionId)}/${suffix}`, {
        body,
        signal: abort.signal,
      });
      if (alive.current) {
        const confirmed = await resource.refresh();
        if (
          !confirmed ||
          confirmed.session.id !== sessionId ||
          confirmed.role !== actor.role
        )
          throw new ApiError(
            0,
            "The action was submitted but its current state could not be verified. Refresh before trying it again.",
          );
      }
    } catch (error) {
      if (alive.current) {
        setError(errorMessage(error));
        await resource.refresh();
      }
      throw error;
    } finally {
      busyRef.current = false;
      if (alive.current) setBusy("");
    }
  }
  function control(action: ControlAction) {
    // Privacy happens synchronously, before any fetch, even if another request is busy.
    if (
      ["pause", "finish", "takeover"].includes(action) ||
      (action === "handover" && actor.role === "facilitator")
    )
      stopLocal();
    if (busyRef.current) {
      setNotice(
        "Capture is stopped locally. Wait for the pending request, then retry the review control.",
      );
      return;
    }
    if (action === "pause" || action === "takeover")
      setNotice("Local capture stopped. The server pause is being checked.");
    if (action === "finish")
      setNotice(
        "Local capture stopped. The service will prepare a report from the evidence received.",
      );
    if (action === "resume")
      setNotice(
        "Resuming does not start devices. Reconnect, then turn on the microphone and choose your screen again.",
      );
    void mutate("control", { action })
      .then(() => {
        if (alive.current && action === "finish") setTab("Report & evidence");
      })
      .catch(() => {});
  }
  function toggle(source: "microphone" | "screen") {
    if (media[source]) capture.disable(source);
    else if (
      latest.current.eligible &&
      voiceReady &&
      connection === "connected" &&
      room.state === ConnectionState.Connected
    )
      void capture.enable(source);
  }
  if (!view && resource.loading)
    return (
      <main id="main" className="review-loading">
        <Loading />
      </main>
    );
  if (!session)
    return (
      <main id="main" className="review-loading">
        <ErrorNotice onRetry={() => void resource.refresh()}>
          {resource.error?.message ||
            "This review could not be verified for your role. Open the correct invitation or sign in again."}
        </ErrorNotice>
        <p className="muted">
          Local capture is stopped while access or session state is unverified.
        </p>
      </main>
    );
  const time = clocks(session, now);
  const isParticipant = actor.role !== "operator";
  const ownConsent =
    actor.role === "client"
      ? session.client_consent
      : session.facilitator_consent;
  const terminal = ["finalising", "completed", "partial", "failed"].includes(
    session.status,
  );
  const canControl =
    isParticipant &&
    ["introduction", "active", "paused"].includes(session.status);
  const connected = connection === "connected";
  const canCapture =
    admissionReady &&
    connected &&
    room.state === ConnectionState.Connected &&
    voiceReady;
  // Guidance describes current verified state; it never advances a stage itself.
  const nextStep: NextStep = (() => {
    const check = {
      label: "Check session status",
      act: () => void resource.refresh(),
    };
    if (terminal)
      return {
        title:
          session.status === "finalising"
            ? "Check report preparation"
            : session.status === "completed"
              ? "Review your workflow report"
              : session.status === "partial"
                ? "Review the partial report"
                : "Check what was saved",
        detail:
          session.status === "finalising"
            ? "Local capture is off. The service is preparing a report from the evidence it received. Open Report & evidence to check availability; a report is not ready until the service returns it."
            : session.status === "failed" || session.status === "partial"
              ? "This review ended without a complete session. Open Report & evidence to check the saved material and any available partial report. Ask your operator about gaps; do not assume the whole review was captured."
              : "Open Report & evidence to read the workflow map, check its sources and add corrections. The review documents the existing work; it has not installed automations.",
        label: "Open report & evidence",
        act: () => setTab("Report & evidence"),
      };
    if (actor.role === "operator")
      return {
        title: "Send each participant their own invitation",
        detail:
          "This operator view is read-only. Send the client link to the person showing the work and the facilitator link to the person leading the introduction. To facilitate yourself, open only that invitation in a separate browser profile; do not use it in this operator tab.",
        label: "Return to operator workspace",
        href: "/",
      };
    if (busy)
      return {
        title: "Wait for the service to confirm",
        detail:
          "Your action is being saved and checked against this session. No devices will start automatically. Pause and finish still stop local capture immediately.",
      };
    if (isParticipant && !hasConsent(ownConsent) && !terminal)
      return {
        title: "Review your permissions",
        detail: `Read the two permissions below, then confirm only if you agree. The ${actor.role === "client" ? "facilitator" : "client"} must consent separately. No microphone or screen starts here.`,
      };
    if (
      !hasConsent(session.client_consent) ||
      !hasConsent(session.facilitator_consent)
    )
      return {
        title:
          actor.role === "client"
            ? "Wait for your facilitator"
            : "Wait for the client",
        detail: `Your consent is saved. The ${actor.role === "client" ? "facilitator" : "client"} must open their own invitation and confirm both permissions before you can join.`,
        ...check,
      };
    if (session.status === "awaiting_consent")
      return {
        title: "Wait for the introduction to start",
        detail:
          "Both permissions are saved. Waiting for the server to open the human introduction; do not start your example yet.",
        ...check,
      };
    if (time.totalRemaining === 0)
      return {
        title: "Check the end of your review",
        detail:
          "The session time limit has been reached. Local capture is stopped. The server must close the review before its report can be prepared; check Report & evidence for the actual result.",
        label: "Open report & evidence",
        act: () => setTab("Report & evidence"),
      };
    if (!runtime.ready)
      return {
        title: "Ask your operator to complete setup",
        detail: `${runtime.message} Do not begin the voice-and-screen review until setup is ready.`,
        blocked: true,
      };
    if (session.status === "paused")
      return {
        title: "Resume when you are ready",
        detail:
          "Local capture is off. Agree with the other participant before resuming. The server restarts the review first; you must then rejoin, turn on your microphone and choose a window again.",
        label: "Resume review",
        act: () => control("resume"),
      };
    if (!recordingReady)
      return {
        title: ["not_started", "queued", "starting"].includes(
          session.recording_status,
        )
          ? "Wait for recording to be ready"
          : "Ask your operator to fix recording",
        detail: `The server reports recording: ${session.recording_status.replace(/_/g, " ")}${session.recording_status === "recording" && !session.egress_id ? " (recorder identity not confirmed)" : ""}. Local capture is stopped. Do not start your example yet. Ask your operator to check the recording worker and storage if this does not become ready.`,
        blocked: true,
        ...check,
      };
    if (actor.role === "facilitator" && session.status === "active")
      return {
        title: "Check in with the client",
        detail:
          "The session is in the AI-led phase. Your microphone and screen are off; this view cannot confirm what the client hears. If BEEP has not joined or the client needs help, pause the review and ask your operator to check the voice worker.",
        label: "Pause for human help",
        act: () => control("takeover"),
      };
    if (!connected)
      return connection === "joining"
        ? {
            title: "Wait for the call to connect",
            detail:
              "BEEP is checking your room access. No devices start while you wait.",
          }
        : {
            title:
              connection === "interrupted"
                ? "Rejoin the call"
                : "Join the call",
            detail:
              "Joining does not turn on your microphone or share your screen. You will choose each separately next.",
            label:
              connection === "interrupted"
                ? "Rejoin the call"
                : "Join the call",
            act: () => void join(),
          };
    if (session.status === "active" && !agentPresent)
      return {
        title: "Wait for BEEP’s voice to join",
        detail:
          "The AI voice participant is not connected. Pause before continuing your example and ask your operator to check the voice worker and room dispatch. A connected call is not proof that BEEP can hear you.",
        blocked: true,
        label: "Pause while BEEP is unavailable",
        act: () => control("pause"),
      };
    if (audioBlocked)
      return {
        title: "Enable the call’s audio",
        detail:
          "Your browser has blocked playback. Enable room audio so you can hear the other participant before continuing.",
        label: "Enable room audio",
        act: () =>
          void room
            .startAudio()
            .catch(() =>
              setError(
                "Audio playback is blocked. Check browser permissions and try again.",
              ),
            ),
      };
    if (!media.microphone)
      return media.microphoneBusy
        ? {
            title: "Finish turning on your microphone",
            detail:
              "Allow microphone access in the browser if asked. Waiting for the microphone track to publish; your example has not started.",
          }
        : {
            title: "Turn on your microphone",
            detail:
              media.error ||
              "Your call is connected, but your microphone is off. Turn it on when you are ready to speak.",
            label: "Turn on my microphone",
            act: () => toggle("microphone"),
          };
    if (actor.role === "client" && !media.screen)
      return media.screenBusy
        ? {
            title: "Choose one window in the browser",
            detail:
              "Select only the window or tab for your example, then confirm Share. Waiting for the selected screen to publish.",
          }
        : {
            title: "Share one window",
            detail:
              media.error ||
              "Close unrelated or sensitive material. Choose the window or tab with your example, not your entire desktop. Screen audio is not requested.",
            label: "Share one window",
            act: () => toggle("screen"),
          };
    if (session.status === "introduction") {
      if (actor.role === "client" ? !facilitatorPresent : !clientPresent)
        return {
          title:
            actor.role === "client"
              ? "Wait for your facilitator to join"
              : "Wait for the client to join",
          detail:
            "Both permissions are saved, but the other person is not connected to this call. Ask them to join using their own invitation before you begin.",
          ...check,
        };
      if (actor.role === "facilitator" && !clientScreenPublished)
        return {
          title: "Ask the client to share one window",
          detail:
            "Only the client shares the example. Ask them to choose Share one window and select the relevant window or tab. Their screen has not reached this call yet.",
        };
      if (time.introRemaining === 0)
        return {
          title: "Hand over to BEEP when you both agree",
          detail:
            "Confirm the workflow and that you are both ready. The server checks the introduction time before BEEP can ask questions. This does not build automations or integrations.",
          label: "Hand over to BEEP",
          act: () => control("handover"),
        };
      return {
        title:
          actor.role === "client"
            ? "Show one real example"
            : "Ask the client to show one example",
        detail: `Start with what starts the work. Walk through one recent example, including who approves it and what happens when a detail is missing. BEEP is not interviewing yet. Introduction remaining: ${duration(time.introRemaining)}. The server checks the introduction time before handover.`,
      };
    }
    return {
      title: "Show one real example to BEEP",
      detail:
        "Say what starts the work, then walk through the example and explain each decision. BEEP will ask about your existing workflow, not build integrations. When you have covered the example, choose Finish review to prepare the report from the evidence received.",
    };
  })();
  // Navigation only: server consent and all existing admission fences remain authoritative.
  if (
    actor.role === "client" &&
    session.status === "awaiting_consent" &&
    !hasConsent(ownConsent) &&
    preparedSessionId !== sessionId
  )
    return (
      <main id="main" className="client-entry">
        <ClientWelcome
          title={session.title}
          onContinue={() => setPreparedSessionId(sessionId)}
        />
      </main>
    );
  return (
    <LiveKitRoom
      className="review-root"
      room={room}
      serverUrl={credentials?.server_url}
      token={credentials?.participant_token}
      connect={!!credentials && admissionReady}
      connectOptions={{ autoSubscribe: false }}
      audio={false}
      video={false}
      screen={false}
      onError={onRoomError}
    >
      <RoomAudioRenderer room={room} muted={!connected || !eligible} />
      <main id="main" className="review-workspace">
        <div className="review-heading">
          <div>
            <div className="eyebrow">
              BEEP /{" "}
              {actor.role === "operator"
                ? "Operator review"
                : "Private review room"}
            </div>
            <h1>{session.title}</h1>
          </div>
          <div className="review-heading-status">
            <span className={`pill ${session.status}`}>
              {statusLabel(session.status)}
            </span>
            {session.started_at && !terminal && (
              <span className="session-time">
                <Clock3 size={15} />
                {duration(time.totalRemaining)} remaining
              </span>
            )}
          </div>
        </div>
        {tab === "Review room" && (
          <section
            className={`review-next-step ${nextStep.blocked ? "is-blocked" : ""}`}
            aria-label="Your next step"
          >
            <div aria-live="polite" aria-atomic="true">
              <p className="eyebrow">
                {nextStep.blocked ? "Review blocked" : "Your next step"}
              </p>
              <h2>{nextStep.title}</h2>
              <p id="review-next-detail">{nextStep.detail}</p>
            </div>
            {nextStep.href && (
              <a className="button primary" href={nextStep.href}>
                {nextStep.label}
                <ArrowRight size={17} aria-hidden="true" />
              </a>
            )}
            {nextStep.act && nextStep.label && (
              <button
                className="button primary"
                type="button"
                aria-describedby="review-next-detail"
                onClick={nextStep.act}
              >
                {nextStep.label}
                <ArrowRight size={17} aria-hidden="true" />
              </button>
            )}
          </section>
        )}
        <div className="review-nav">
          <Tabs
            items={["Review room", "Report & evidence"]}
            value={tab}
            onChange={setTab}
            label="Review workspace"
          />
          <span className="connection-label">
            <span className={`status-dot ${connected ? "live" : ""}`} />
            {connected
              ? "Room connected"
              : connection === "joining"
                ? "Connecting to room…"
                : connection === "interrupted"
                  ? "Connection interrupted"
                  : "Not connected"}
          </span>
        </div>
        {error && (
          <ErrorNotice onRetry={() => void resource.refresh()}>
            {error}
          </ErrorNotice>
        )}
        {notice && (
          <p className="notice" role="status">
            {notice}
          </p>
        )}
        {media.error && <ErrorNotice>{media.error}</ErrorNotice>}
        {tab === "Review room" ? (
          <>
            <div className="phase-strip">
              <div
                className={session.status === "introduction" ? "current" : ""}
              >
                <span className="phase-number">01</span>
                <span>Human introduction</span>
                {session.status === "introduction" && (
                  <strong>{duration(time.introRemaining)}</strong>
                )}
              </div>
              <span className="phase-route" />
              <div className={session.status === "active" ? "current" : ""}>
                <span className="phase-number">02</span>
                <span>AI-led review</span>
              </div>
              <span className="phase-route" />
              <div className={terminal ? "current" : ""}>
                <span className="phase-number">03</span>
                <span>Report & corrections</span>
              </div>
            </div>
            {isParticipant && !hasConsent(ownConsent) && !terminal ? (
              <div className="consent-layout">
                <ConsentForm
                  busy={!!busy}
                  onConsent={(value) =>
                    mutate("consent", value).catch(() => {})
                  }
                />
                <section className="consent-aside">
                  <div className="eyebrow">Before you start</div>
                  <h3>Keep a real example nearby.</h3>
                  <p>
                    Choose one workflow you know well. A recent example helps
                    explain the steps, handoffs and exceptions without guessing.
                  </p>
                  <ul className="preparation-list">
                    <li>Close personal and unrelated windows.</li>
                    <li>Check who is joining the review.</li>
                    <li>Ask about processing and retention.</li>
                    <li>Keep pause within reach.</li>
                  </ul>
                  <div className="consent-status">
                    <span>
                      <span className="status-dot" />
                      Client:{" "}
                      {hasConsent(session.client_consent)
                        ? "consented"
                        : "not yet consented"}
                    </span>
                    <span>
                      <span className="status-dot" />
                      Facilitator:{" "}
                      {hasConsent(session.facilitator_consent)
                        ? "consented"
                        : "not yet consented"}
                    </span>
                  </div>
                </section>
              </div>
            ) : (
              <>
                <div className="room-layout">
                  <SelectedScreen
                    room={room}
                    sessionId={sessionId}
                    role={actor.role}
                    connected={connected}
                  />
                  <Conversation
                    room={room}
                    session={session}
                    events={eventFeed.events}
                    eventError={eventFeed.error?.message}
                  />
                </div>
              </>
            )}
          </>
        ) : (
          <ReportView
            key={`${actor.role}:${sessionId}`}
            sessionId={sessionId}
            role={actor.role}
            onCorrect={(text) => mutate("corrections", { text })}
          />
        )}
      </main>
      <footer className="control-dock" aria-label="Review capture controls">
        <div className="dock-context">
          <span
            className={`status-dot ${media.microphone || media.screen ? "live" : ""}`}
          />
          <div>
            <strong>
              {media.microphone || media.screen
                ? "Local capture on"
                : "Local capture off"}
            </strong>
            <span>
              {media.screen ? "Selected screen shared" : "No screen shared"}
            </span>
          </div>
        </div>
        <div className="capture-controls">
          <button
            className={`device-button ${media.microphone ? "on" : ""}`}
            aria-label={
              media.microphone ? "Turn microphone off" : "Turn microphone on"
            }
            aria-pressed={media.microphone}
            disabled={
              (!canCapture && !media.microphone) || media.microphoneBusy
            }
            onClick={() => toggle("microphone")}
          >
            {media.microphone ? <Mic size={21} /> : <MicOff size={21} />}
            <span>
              {media.microphoneBusy
                ? "Starting…"
                : media.microphone
                  ? "Mic on"
                  : "Mic off"}
            </span>
          </button>
          <button
            className={`device-button ${media.screen ? "on" : ""}`}
            aria-label={media.screen ? "Stop sharing screen" : "Share screen"}
            aria-pressed={media.screen}
            disabled={
              actor.role !== "client" ||
              (!canCapture && !media.screen) ||
              media.screenBusy
            }
            onClick={() => toggle("screen")}
          >
            {media.screen ? <MonitorOff size={21} /> : <Monitor size={21} />}
            <span>
              {media.screenBusy
                ? "Choosing…"
                : media.screen
                  ? "Stop share"
                  : "Share screen"}
            </span>
          </button>
          <span className="dock-divider" />
          <button
            className="device-button"
            aria-label={
              session.status === "paused" ? "Resume review" : "Pause review"
            }
            disabled={!canControl}
            onClick={() =>
              control(session.status === "paused" ? "resume" : "pause")
            }
          >
            {session.status === "paused" ? (
              <Play size={21} />
            ) : (
              <Pause size={21} />
            )}
            <span>{session.status === "paused" ? "Resume" : "Pause"}</span>
          </button>
          <button
            className="button finish-button"
            disabled={!canControl}
            onClick={() => control("finish")}
          >
            <Square size={15} /> Finish review
          </button>
        </div>
        {actor.role === "facilitator" && session.status === "active" ? (
          <button
            className="text-button dock-help"
            onClick={() => control("takeover")}
          >
            Pause for human help
          </button>
        ) : (
          <span className="dock-help">
            Pause stops local capture immediately.
          </span>
        )}
      </footer>
    </LiveKitRoom>
  );
}
