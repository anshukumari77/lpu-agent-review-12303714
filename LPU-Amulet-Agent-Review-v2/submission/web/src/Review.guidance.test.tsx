import { describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import {
  ConnectionState,
  RemoteParticipant,
  RemoteTrackPublication,
  Room,
  RoomEvent,
  Track,
  type LocalTrack,
} from "livekit-client";
import { Review } from "./Review";
import { testView } from "./test-fixtures";
import { TrackInfo, TrackSource } from "@livekit/protocol";

function mediaHarness(role: "client" | "facilitator" = "client") {
  const room = new Room();
  vi.spyOn(room, "name", "get").mockReturnValue("unit-room");
  vi.spyOn(room, "canPlaybackAudio", "get").mockReturnValue(true);
  const connect = vi.spyOn(room, "connect").mockImplementation(async () => {
    room.localParticipant.identity = `${role}:review-one`;
    room.state = ConnectionState.Connected;
    room.emit(RoomEvent.Connected);
  });
  vi.spyOn(room, "disconnect").mockImplementation(async () => {
    room.state = ConnectionState.Disconnected;
  });
  const track = (source: Track.Source) =>
    ({
      source,
      stop: vi.fn(),
      on: vi.fn(),
      off: vi.fn(),
      mediaStreamTrack: { readyState: "live" },
    }) as unknown as LocalTrack;
  const microphone = track(Track.Source.Microphone);
  const screenTrack = track(Track.Source.ScreenShare);
  const mic = vi
    .spyOn(room.localParticipant, "createTracks")
    .mockResolvedValue([microphone]);
  const share = vi
    .spyOn(room.localParticipant, "createScreenTracks")
    .mockResolvedValue([screenTrack]);
  const publish = vi
    .spyOn(room.localParticipant, "publishTrack")
    .mockImplementation(async (track) => ({ track }) as never);
  vi.spyOn(room.localParticipant, "unpublishTrack").mockResolvedValue(
    undefined,
  );
  return { room, connect, mic, share, publish, microphone, screenTrack };
}
const readyRuntime = { ready: true, missing: [], message: "" };
const next = () =>
  within(screen.getByRole("region", { name: "Your next step" }));
function participant(room: Room, identity: string, isAgent = false) {
  const person = new RemoteParticipant(
    {} as never,
    `synthetic-${identity}`,
    identity,
  );
  if (isAgent) vi.spyOn(person, "isAgent", "get").mockReturnValue(true);
  act(() => {
    room.remoteParticipants.set(identity, person);
    room.emit(RoomEvent.ParticipantConnected, person);
  });
  return person;
}
async function activeCaptureHarness() {
  let view = testView({
    status: "active",
    client_consent: true,
    facilitator_consent: true,
    recording_status: "recording",
    egress_id: "synthetic-egress",
    started_at: new Date().toISOString(),
  });
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async (url: string) =>
        new Response(
          JSON.stringify(
            url.endsWith("/room-token")
              ? {
                  server_url: "ws://localhost:7880",
                  participant_token: "synthetic-token",
                  participant_identity: "client:review-one",
                  room_name: "unit-room",
                }
              : url.includes("/events")
                ? { events: [] }
                : view,
          ),
        ),
    ),
  );
  const h = mediaHarness();
  render(
    <Review
      sessionId="review-one"
      actor={{ role: "client", session_id: "review-one" }}
      roomInstance={h.room}
      runtime={readyRuntime}
    />,
  );
  await userEvent.click(
    await screen.findByRole("button", { name: "Join the call" }),
  );
  await next().findByRole("heading", { name: "Wait for BEEP’s voice to join" });
  const agent = participant(h.room, "synthetic-beep-agent", true);
  await next().findByRole("button", { name: "Turn on my microphone" });
  const loseAgent = async () => {
    act(() => {
      h.room.remoteParticipants.delete(agent.identity);
      h.room.emit(RoomEvent.ParticipantDisconnected, agent);
    });
    await next().findByRole("heading", {
      name: "Wait for BEEP’s voice to join",
    });
  };
  return {
    ...h,
    agent,
    loseAgent,
    loseRecorder: () => {
      view = { ...view, session: { ...view.session, egress_id: null } };
    },
  };
}
/** Synthetic server responses; Review, request hook and Room are the actual components. */
describe("real review next-action guidance", () => {
  it("RS-02 retains the recorder-loss pending-picker stop and room-disconnect control", async () => {
    const h = await activeCaptureHarness();
    await userEvent.click(
      next().getByRole("button", { name: "Turn on my microphone" }),
    );
    let resolve!: (tracks: LocalTrack[]) => void;
    h.share.mockReturnValueOnce(
      new Promise((done) => {
        resolve = done;
      }),
    );
    await userEvent.click(
      await next().findByRole("button", { name: "Share one window" }),
    );
    h.loseRecorder();
    // Exercise the actual serial scoped-readback poll, not a mocked controller.
    await next().findByRole(
      "heading",
      { name: "Ask your operator to fix recording" },
      { timeout: 3500 },
    );
    expect(h.microphone.stop).toHaveBeenCalled();
    expect(h.room.state).toBe(ConnectionState.Disconnected);
    await act(async () => resolve([h.screenTrack]));
    expect(h.screenTrack.stop).toHaveBeenCalled();
    expect(h.publish).not.toHaveBeenCalledWith(
      h.screenTrack,
      expect.anything(),
    );
    expect(screen.getByRole("button", { name: "Share screen" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Pause review" })).toBeEnabled();
  });
  it.each(["microphone", "screen"] as const)(
    "RS-02 requires a new explicit %s picker after voice returns and ignores the old completion while it is pending",
    async (source) => {
      const h = await activeCaptureHarness();
      const acquire = source === "screen" ? h.share : h.mic;
      const oldTrack = source === "screen" ? h.screenTrack : h.microphone;
      const freshTrack = {
        ...oldTrack,
        stop: vi.fn(),
        on: vi.fn(),
      } as unknown as LocalTrack;
      const button =
        source === "screen" ? "Share screen" : "Turn microphone on";
      const offButton =
        source === "screen" ? "Stop sharing screen" : "Turn microphone off";
      let old!: (tracks: LocalTrack[]) => void;
      let fresh!: (tracks: LocalTrack[]) => void;
      acquire.mockReturnValueOnce(
        new Promise((done) => {
          old = done;
        }),
      );
      await userEvent.click(screen.getByRole("button", { name: button }));
      await h.loseAgent();
      participant(h.room, h.agent.identity, true);
      await waitFor(() =>
        expect(screen.getByRole("button", { name: button })).toBeEnabled(),
      );
      expect(acquire).toHaveBeenCalledTimes(1);
      acquire.mockReturnValueOnce(
        new Promise((done) => {
          fresh = done;
        }),
      );
      await userEvent.click(screen.getByRole("button", { name: button }));
      await act(async () => old([oldTrack]));
      expect(oldTrack.stop).toHaveBeenCalled();
      expect(h.publish).not.toHaveBeenCalled();
      expect(screen.getByRole("button", { name: button })).toBeDisabled();
      await act(async () => fresh([freshTrack]));
      expect(h.publish).toHaveBeenCalledExactlyOnceWith(freshTrack, {
        source: freshTrack.source,
      });
      expect(screen.getByRole("button", { name: offButton })).toBeEnabled();
      expect(acquire).toHaveBeenCalledTimes(2);
    },
  );
  it.each(["microphone", "screen"] as const)(
    "RS-02 does not revive an old %s picker when the agent leaves and returns in the same React batch",
    async (source) => {
      const h = await activeCaptureHarness();
      const acquire = source === "screen" ? h.share : h.mic;
      const lateTrack = source === "screen" ? h.screenTrack : h.microphone;
      const button =
        source === "screen" ? "Share screen" : "Turn microphone on";
      let resolve!: (tracks: LocalTrack[]) => void;
      acquire.mockReturnValueOnce(
        new Promise((done) => {
          resolve = done;
        }),
      );
      await userEvent.click(screen.getByRole("button", { name: button }));
      act(() => {
        h.room.remoteParticipants.delete(h.agent.identity);
        h.room.emit(RoomEvent.ParticipantDisconnected, h.agent);
        h.room.remoteParticipants.set(h.agent.identity, h.agent);
        h.room.emit(RoomEvent.ParticipantConnected, h.agent);
      });
      await act(async () => resolve([lateTrack]));
      expect(h.publish).not.toHaveBeenCalled();
      expect(lateTrack.stop).toHaveBeenCalled();
      expect(acquire).toHaveBeenCalledTimes(1);
      expect(screen.getByRole("button", { name: button })).toBeEnabled();
      expect(screen.getByRole("button", { name: button })).toHaveAttribute(
        "aria-pressed",
        "false",
      );
    },
  );
  it.each(["microphone", "screen"] as const)(
    "RS-02 stops pending %s publication on voice loss and removes its late completion after agent return",
    async (source) => {
      const h = await activeCaptureHarness();
      const pendingTrack = source === "screen" ? h.screenTrack : h.microphone;
      const existingTrack = source === "screen" ? h.microphone : h.screenTrack;
      const deviceButton =
        source === "screen" ? "Share screen" : "Turn microphone on";
      const existingButton =
        source === "screen" ? "Turn microphone on" : "Share screen";
      await userEvent.click(
        screen.getByRole("button", { name: existingButton }),
      );
      let resolve!: () => void;
      h.publish.mockImplementationOnce(
        () =>
          new Promise((done) => {
            resolve = () => done({ track: pendingTrack } as never);
          }),
      );
      await userEvent.click(screen.getByRole("button", { name: deviceButton }));
      await waitFor(() =>
        expect(h.publish).toHaveBeenCalledWith(pendingTrack, expect.anything()),
      );
      vi.mocked(h.room.disconnect).mockClear();
      await h.loseAgent();
      expect(pendingTrack.stop).toHaveBeenCalled();
      expect(existingTrack.stop).not.toHaveBeenCalled();
      expect(h.room.disconnect).not.toHaveBeenCalled();
      const unpublish = vi.mocked(h.room.localParticipant.unpublishTrack);
      expect(unpublish).toHaveBeenCalledWith(pendingTrack, true);
      unpublish.mockClear();
      participant(h.room, h.agent.identity, true);
      await waitFor(() =>
        expect(
          screen.getByRole("button", { name: deviceButton }),
        ).toBeEnabled(),
      );
      await act(async () => resolve());
      // Completion must unpublish again, even if the SDK published after the stop.
      expect(unpublish).toHaveBeenCalledWith(pendingTrack, true);
      expect(unpublish).not.toHaveBeenCalledWith(existingTrack, true);
      expect(
        screen.getByRole("button", { name: deviceButton }),
      ).toHaveAttribute("aria-pressed", "false");
      expect(h.publish).toHaveBeenCalledTimes(2);
      expect(existingTrack.stop).not.toHaveBeenCalled();
      await userEvent.click(
        screen.getByRole("button", { name: "Pause review" }),
      );
      expect(existingTrack.stop).toHaveBeenCalled();
    },
  );
  it("RS-02 stops a late screen picker on active voice loss without stopping the published microphone or disconnecting", async () => {
    const view = testView({
      status: "active",
      client_consent: true,
      facilitator_consent: true,
      recording_status: "recording",
      egress_id: "synthetic-egress",
      started_at: new Date().toISOString(),
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (url: string) =>
          new Response(
            JSON.stringify(
              url.endsWith("/room-token")
                ? {
                    server_url: "ws://localhost:7880",
                    participant_token: "synthetic-token",
                    participant_identity: "client:review-one",
                    room_name: "unit-room",
                  }
                : url.includes("/events")
                  ? { events: [] }
                  : view,
            ),
          ),
      ),
    );
    const h = mediaHarness();
    render(
      <Review
        sessionId="review-one"
        actor={{ role: "client", session_id: "review-one" }}
        roomInstance={h.room}
        runtime={readyRuntime}
      />,
    );
    // Room admission must not depend on agent presence: voice waits for the client.
    await userEvent.click(
      await screen.findByRole("button", { name: "Join the call" }),
    );
    await next().findByRole("heading", {
      name: "Wait for BEEP’s voice to join",
    });
    const agent = participant(h.room, "synthetic-beep-agent", true);
    await userEvent.click(
      await next().findByRole("button", { name: "Turn on my microphone" }),
    );
    let resolve!: (tracks: LocalTrack[]) => void;
    h.share.mockReturnValueOnce(
      new Promise((done) => {
        resolve = done;
      }),
    );
    await userEvent.click(
      await next().findByRole("button", { name: "Share one window" }),
    );
    expect(h.share).toHaveBeenCalledTimes(1);
    vi.mocked(h.room.disconnect).mockClear();
    act(() => {
      h.room.remoteParticipants.delete(agent.identity);
      h.room.emit(RoomEvent.ParticipantDisconnected, agent);
    });
    await next().findByRole("heading", {
      name: "Wait for BEEP’s voice to join",
    });
    expect(screen.getByRole("button", { name: "Share screen" })).toBeDisabled();
    await act(async () => resolve([h.screenTrack]));
    expect(h.publish).not.toHaveBeenCalledWith(
      h.screenTrack,
      expect.anything(),
    );
    expect(h.screenTrack.stop).toHaveBeenCalled();
    expect(h.microphone.stop).not.toHaveBeenCalled();
    expect(h.room.disconnect).not.toHaveBeenCalled();
    expect(h.room.state).toBe(ConnectionState.Connected);
    expect(
      screen.getByRole("button", { name: "Turn microphone off" }),
    ).toBeEnabled();
    await userEvent.click(
      next().getByRole("button", { name: "Pause while BEEP is unavailable" }),
    );
    expect(h.microphone.stop).toHaveBeenCalled();
    expect(h.room.localParticipant.unpublishTrack).toHaveBeenCalledWith(
      h.microphone,
      true,
    );
  });
  it("guides the facilitator to the client’s screen and hands over without leaving facilitator capture running", async () => {
    let view = {
      ...testView({
        status: "introduction",
        client_consent: true,
        facilitator_consent: true,
        recording_status: "recording",
        egress_id: "synthetic-egress",
        started_at: new Date(Date.now() - 901_000).toISOString(),
      }),
      role: "facilitator" as const,
    };
    const actions: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, options: RequestInit) => {
        if (url.endsWith("/control")) {
          actions.push(JSON.parse(options.body as string).action);
          view = { ...view, session: { ...view.session, status: "active" } };
        }
        return new Response(
          JSON.stringify(
            url.endsWith("/room-token")
              ? {
                  server_url: "ws://localhost:7880",
                  participant_token: "synthetic-token",
                  participant_identity: "facilitator:review-one",
                  room_name: "unit-room",
                }
              : url.includes("/events")
                ? { events: [] }
                : view,
          ),
        );
      }),
    );
    const h = mediaHarness("facilitator");
    render(
      <Review
        sessionId="review-one"
        actor={{ role: "facilitator", session_id: "review-one" }}
        roomInstance={h.room}
        runtime={readyRuntime}
      />,
    );
    await userEvent.click(
      await screen.findByRole("button", { name: "Join the call" }),
    );
    await userEvent.click(
      await next().findByRole("button", { name: "Turn on my microphone" }),
    );
    const client = participant(h.room, "client:review-one");
    await waitFor(() =>
      expect(
        next().getByRole("heading", {
          name: "Ask the client to share one window",
        }),
      ).toBeVisible(),
    );
    expect(screen.getByRole("button", { name: "Share screen" })).toBeDisabled();
    expect(
      within(screen.getByRole("region", { name: "Selected screen" })).getByText(
        /Only the client can share/,
      ),
    ).toBeVisible();
    // Readiness comes from a real remote publication, not a checkbox or elapsed time.
    const publication = new RemoteTrackPublication(
      Track.Kind.Video,
      new TrackInfo({
        sid: "synthetic-screen",
        name: "screen",
        source: TrackSource.SCREEN_SHARE,
      }),
      false,
    );
    publication.source = Track.Source.ScreenShare;
    vi.spyOn(publication, "setSubscribed").mockImplementation(() => {});
    const track = {
      kind: Track.Kind.Video,
      attach: vi.fn(),
      detach: vi.fn(),
      on: vi.fn(),
      off: vi.fn(),
      mediaStreamTrack: { readyState: "live" },
    };
    publication.track = track as never;
    act(() => {
      client.trackPublications.set(publication.trackSid, publication);
      client.videoTrackPublications.set(publication.trackSid, publication);
      h.room.emit(RoomEvent.TrackPublished, publication, client);
      h.room.emit(
        RoomEvent.TrackSubscribed,
        track as never,
        publication,
        client,
      );
    });
    await waitFor(() =>
      expect(
        next().getByRole("button", { name: "Hand over to BEEP" }),
      ).toBeEnabled(),
    );
    await userEvent.click(
      next().getByRole("button", { name: "Hand over to BEEP" }),
    );
    expect(h.microphone.stop).toHaveBeenCalled();
    await waitFor(() =>
      expect(
        next().getByRole("heading", { name: "Check in with the client" }),
      ).toBeVisible(),
    );
    expect(actions).toEqual(["handover"]);
    expect(h.share).not.toHaveBeenCalled();
    expect(h.room.state).toBe(ConnectionState.Disconnected);
  });
  it("stops existing capture when the server loses recording confirmation and explains the blocker", async () => {
    let view = testView({
      status: "introduction",
      client_consent: true,
      facilitator_consent: true,
      recording_status: "recording",
      egress_id: "synthetic-egress",
      started_at: new Date().toISOString(),
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (url: string) =>
          new Response(
            JSON.stringify(
              url.endsWith("/room-token")
                ? {
                    server_url: "ws://localhost:7880",
                    participant_token: "synthetic-token",
                    participant_identity: "client:review-one",
                    room_name: "unit-room",
                  }
                : url.includes("/events")
                  ? { events: [] }
                  : view,
            ),
          ),
      ),
    );
    const h = mediaHarness();
    render(
      <Review
        sessionId="review-one"
        actor={{ role: "client", session_id: "review-one" }}
        roomInstance={h.room}
        runtime={readyRuntime}
      />,
    );
    await userEvent.click(
      await screen.findByRole("button", { name: "Join the call" }),
    );
    await userEvent.click(
      await next().findByRole("button", { name: "Turn on my microphone" }),
    );
    await userEvent.click(
      next().getByRole("button", { name: "Share one window" }),
    );
    view = {
      ...view,
      session: { ...view.session, recording_status: "failed" },
    };
    await userEvent.click(
      next().getByRole("button", { name: "Check session status" }),
    );
    await waitFor(() =>
      expect(
        next().getByRole("heading", {
          name: "Ask your operator to fix recording",
        }),
      ).toBeVisible(),
    );
    expect(h.microphone.stop).toHaveBeenCalled();
    expect(h.screenTrack.stop).toHaveBeenCalled();
    expect(next().getByText(/failed.*Local capture is stopped/)).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Turn microphone on" }),
    ).toBeDisabled();
    expect(
      next().queryByRole("button", { name: "Join the call" }),
    ).not.toBeInTheDocument();
  });
  it("does not suggest joining or handover after the real session cap", async () => {
    const view = testView({
      status: "introduction",
      client_consent: true,
      facilitator_consent: true,
      recording_status: "recording",
      egress_id: "synthetic-egress",
      started_at: new Date(Date.now() - 5_401_000).toISOString(),
    });
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async (url: string) =>
          new Response(
            JSON.stringify(url.includes("/events") ? { events: [] } : view),
          ),
      ),
    );
    render(
      <Review
        sessionId="review-one"
        actor={{ role: "client", session_id: "review-one" }}
        runtime={readyRuntime}
      />,
    );
    await screen.findByRole("heading", {
      name: "Check the end of your review",
    });
    expect(next().getByText(/time limit/)).toBeVisible();
    expect(
      next().getByRole("button", { name: "Open report & evidence" }),
    ).toBeEnabled();
    expect(
      screen.queryByRole("button", { name: /Join the call|Hand over to BEEP/ }),
    ).not.toBeInTheDocument();
  });
  it.each([
    [
      "operator",
      "awaiting_consent",
      "Send each participant their own invitation",
    ],
    ["facilitator", "active", "Check in with the client"],
    ["client", "paused", "Resume when you are ready"],
    ["client", "finalising", "Check report preparation"],
    ["client", "completed", "Review your workflow report"],
    ["client", "partial", "Review the partial report"],
    ["client", "failed", "Check what was saved"],
  ] as const)(
    "keeps %s guidance appropriate to %s rather than suggesting forbidden capture",
    async (role, status, heading) => {
      const view = {
        ...testView({
          status,
          client_consent: role !== "operator",
          facilitator_consent: role !== "operator",
          recording_status: status === "active" ? "recording" : "stopped",
          egress_id: status === "active" ? "synthetic-egress" : null,
        }),
        role,
      };
      vi.stubGlobal(
        "fetch",
        vi.fn(
          async (url: string) =>
            new Response(
              JSON.stringify(url.includes("/events") ? { events: [] } : view),
            ),
        ),
      );
      const h = mediaHarness(role === "facilitator" ? role : "client");
      render(
        <Review
          sessionId="review-one"
          actor={{ role, session_id: "review-one" }}
          roomInstance={h.room}
          runtime={readyRuntime}
        />,
      );
      await screen.findByRole("heading", { name: heading });
      expect(
        next().queryByRole("button", {
          name: /Join the call|Turn on my microphone|Share one window/,
        }),
      ).not.toBeInTheDocument();
      expect(h.connect).not.toHaveBeenCalled();
      expect(h.mic).not.toHaveBeenCalled();
      expect(h.share).not.toHaveBeenCalled();
      if (role === "operator") {
        expect(
          next().getByRole("link", { name: "Return to operator workspace" }),
        ).toHaveAttribute("href", "/");
        expect(
          screen.queryByText(/Your consent is saved/),
        ).not.toBeInTheDocument();
        expect(next().getByText(/read-only/)).toBeVisible();
      }
      if (["finalising", "completed", "partial", "failed"].includes(status))
        expect(
          next().getByRole("button", { name: "Open report & evidence" }),
        ).toBeEnabled();
    },
  );
  it("resumes using server readback but waits for recording without restarting devices", async () => {
    let view = testView({
      status: "paused",
      client_consent: true,
      facilitator_consent: true,
      recording_status: "stopped",
      consent_epoch: 1,
    });
    const actions: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, options: RequestInit) => {
        if (url.endsWith("/control")) {
          actions.push(JSON.parse(options.body as string).action);
          view = {
            ...view,
            session: {
              ...view.session,
              status: "introduction",
              recording_status: "queued",
            },
          };
        }
        return new Response(
          JSON.stringify(url.includes("/events") ? { events: [] } : view),
        );
      }),
    );
    const h = mediaHarness();
    render(
      <Review
        sessionId="review-one"
        actor={{ role: "client", session_id: "review-one" }}
        roomInstance={h.room}
        runtime={readyRuntime}
      />,
    );
    await screen.findByRole("heading", { name: "Resume when you are ready" });
    await userEvent.click(
      next().getByRole("button", { name: "Resume review" }),
    );
    await waitFor(() =>
      expect(
        next().getByRole("heading", { name: "Wait for recording to be ready" }),
      ).toBeVisible(),
    );
    expect(actions).toEqual(["resume"]);
    expect(h.connect).not.toHaveBeenCalled();
    expect(h.mic).not.toHaveBeenCalled();
    expect(h.share).not.toHaveBeenCalled();
  });
  it("blocks a missing voice participant, unlocks real playback, and requires devices again after reconnect before finishing to the real report", async () => {
    let view = testView({
      status: "active",
      client_consent: true,
      facilitator_consent: true,
      recording_status: "recording",
      egress_id: "synthetic-egress",
      started_at: new Date().toISOString(),
    });
    const actions: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, options: RequestInit) => {
        if (url.endsWith("/control")) {
          const action = JSON.parse(options.body as string).action;
          actions.push(action);
          if (action === "finish")
            view = {
              ...view,
              session: {
                ...view.session,
                status: "finalising",
                consent_epoch: 1,
              },
            };
        }
        if (url.endsWith("/report"))
          return new Response(
            JSON.stringify({ detail: "Report is not yet available" }),
            { status: 409 },
          );
        return new Response(
          JSON.stringify(
            url.endsWith("/room-token")
              ? {
                  server_url: "ws://localhost:7880",
                  participant_token: "synthetic-token",
                  participant_identity: "client:review-one",
                  room_name: "unit-room",
                }
              : url.includes("/events")
                ? { events: [] }
                : view,
          ),
        );
      }),
    );
    const h = mediaHarness();
    const playback = vi
      .spyOn(h.room, "canPlaybackAudio", "get")
      .mockReturnValue(false);
    const audio = vi
      .spyOn(h.room, "startAudio")
      .mockImplementation(async () => {
        playback.mockReturnValue(true);
        h.room.emit(RoomEvent.AudioPlaybackStatusChanged, true);
      });
    render(
      <Review
        sessionId="review-one"
        actor={{ role: "client", session_id: "review-one" }}
        roomInstance={h.room}
        runtime={readyRuntime}
      />,
    );
    await userEvent.click(
      await screen.findByRole("button", { name: "Join the call" }),
    );
    expect(
      await next().findByRole("heading", {
        name: "Wait for BEEP’s voice to join",
      }),
    ).toBeVisible();
    expect(next().getByText(/check the voice worker/)).toBeVisible();
    expect(
      next().getByRole("button", { name: "Pause while BEEP is unavailable" }),
    ).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "Turn microphone on" }),
    ).toBeDisabled();
    const agent = participant(h.room, "synthetic-beep-agent", true);
    await waitFor(() =>
      expect(
        next().getByRole("button", { name: "Enable room audio" }),
      ).toBeEnabled(),
    );
    await userEvent.click(
      next().getByRole("button", { name: "Enable room audio" }),
    );
    expect(audio).toHaveBeenCalledTimes(1);
    await userEvent.click(
      await next().findByRole("button", { name: "Turn on my microphone" }),
    );
    await userEvent.click(
      next().getByRole("button", { name: "Share one window" }),
    );
    expect(
      next().getByRole("heading", { name: "Show one real example to BEEP" }),
    ).toBeVisible();
    expect(next().getByText(/Finish review.*report/)).toBeVisible();
    expect(
      screen.queryByText(/BEEP is listening|Recording confirmed/),
    ).not.toBeInTheDocument();
    act(() => h.room.emit(RoomEvent.Reconnecting));
    expect(h.microphone.stop).toHaveBeenCalled();
    expect(h.screenTrack.stop).toHaveBeenCalled();
    await userEvent.click(
      next().getByRole("button", { name: "Rejoin the call" }),
    );
    expect(
      await next().findByRole("button", { name: "Turn on my microphone" }),
    ).toBeVisible();
    expect(h.mic).toHaveBeenCalledTimes(1);
    expect(h.share).toHaveBeenCalledTimes(1);
    await userEvent.click(
      await next().findByRole("button", { name: "Turn on my microphone" }),
    );
    await userEvent.click(
      next().getByRole("button", { name: "Share one window" }),
    );
    // Ending uses the existing finish handler and the real ReportView's unavailable state.
    await userEvent.click(
      screen.getByRole("button", { name: "Finish review" }),
    );
    await waitFor(() =>
      expect(
        screen.getByRole("tab", { name: "Report & evidence" }),
      ).toHaveAttribute("aria-selected", "true"),
    );
    expect(actions).toEqual(["finish"]);
    expect(
      await screen.findByText(
        /Report is not yet available|report.*being prepared|Preparing.*report/i,
      ),
    ).toBeVisible();
    expect(h.room.state).toBe(ConnectionState.Disconnected);
    expect(agent.isAgent).toBe(true);
  });
  it("joins explicitly, waits for microphone publication, shares one window and keeps the human introduction before server-gated handover", async () => {
    let view = testView({
      status: "introduction",
      client_consent: true,
      facilitator_consent: true,
      recording_status: "recording",
      egress_id: "synthetic-egress",
      started_at: new Date().toISOString(),
    });
    const requests: { url: string; body?: string }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, options: RequestInit) => {
        requests.push({ url, body: options.body as string });
        if (url.endsWith("/control")) {
          // The server rejects a handover even if a local clock says it is time.
          return new Response(
            JSON.stringify({ detail: "Human introduction has not finished" }),
            { status: 409 },
          );
        }
        return new Response(
          JSON.stringify(
            url.endsWith("/room-token")
              ? {
                  server_url: "ws://localhost:7880",
                  participant_token: "synthetic-token",
                  participant_identity: "client:review-one",
                  room_name: "unit-room",
                }
              : url.includes("/events")
                ? { events: [] }
                : view,
          ),
        );
      }),
    );
    const h = mediaHarness();
    render(
      <Review
        sessionId="review-one"
        actor={{ role: "client", session_id: "review-one" }}
        roomInstance={h.room}
        runtime={readyRuntime}
      />,
    );
    await userEvent.click(
      await screen.findByRole("button", { name: "Join the call" }),
    );
    await waitFor(() =>
      expect(
        next().getByRole("button", { name: "Turn on my microphone" }),
      ).toBeEnabled(),
    );
    expect(h.mic).not.toHaveBeenCalled();
    expect(h.share).not.toHaveBeenCalled();
    let publish!: () => void;
    h.publish.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          publish = () => resolve({ track: h.microphone } as never);
        }),
    );
    await userEvent.click(
      await next().findByRole("button", { name: "Turn on my microphone" }),
    );
    expect(
      next().getByRole("heading", {
        name: "Finish turning on your microphone",
      }),
    ).toBeVisible();
    expect(
      next().queryByRole("button", { name: "Share one window" }),
    ).not.toBeInTheDocument();
    await act(async () => publish());
    await userEvent.click(
      next().getByRole("button", { name: "Share one window" }),
    );
    expect(h.share).toHaveBeenCalledExactlyOnceWith({
      audio: false,
      video: true,
    });
    expect(
      next().getByRole("heading", {
        name: "Wait for your facilitator to join",
      }),
    ).toBeVisible();
    participant(h.room, "facilitator:review-one");
    await waitFor(() =>
      expect(
        next().getByRole("heading", { name: "Show one real example" }),
      ).toBeVisible(),
    );
    expect(next().getByText(/what starts the work/)).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Hand over to BEEP" }),
    ).not.toBeInTheDocument();
    expect(
      next().getByText(/server checks the introduction time/i),
    ).toBeVisible();
    view = {
      ...view,
      session: {
        ...view.session,
        started_at: new Date(Date.now() - 901_000).toISOString(),
      },
    };
    await waitFor(
      () =>
        expect(
          next().getByRole("button", { name: "Hand over to BEEP" }),
        ).toBeEnabled(),
      { timeout: 3500 },
    );
    await userEvent.click(
      next().getByRole("button", { name: "Hand over to BEEP" }),
    );
    await screen.findByText("Human introduction has not finished");
    expect(
      screen.getByText("Human introduction", { selector: ".pill" }),
    ).toBeVisible();
    expect(requests.filter((r) => r.url.endsWith("/control"))).toEqual([
      {
        url: "/api/sessions/review-one/control",
        body: JSON.stringify({ action: "handover" }),
      },
    ]);
    expect(h.connect).toHaveBeenCalledTimes(1);
  });
  it("walks from explicit permissions through the other consent and real setup/recording gates before joining", async () => {
    let view = testView();
    const requests: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        requests.push(url);
        if (url.endsWith("/consent-policy")) {
          const { consentPolicyFixture } =
            await import("./consent-test-fixture");
          return new Response(JSON.stringify({ policy: consentPolicyFixture }));
        }
        if (url.endsWith("/consent")) view = testView({ client_consent: true });
        return new Response(
          JSON.stringify(url.includes("/events") ? { events: [] } : view),
        );
      }),
    );
    const room = new Room();
    const connect = vi.spyOn(room, "connect");
    const mic = vi.spyOn(room.localParticipant, "createTracks");
    const share = vi.spyOn(room.localParticipant, "createScreenTracks");
    const props = {
      sessionId: "review-one",
      actor: { role: "client" as const, session_id: "review-one" },
      roomInstance: room,
      runtime: {
        ready: false,
        missing: ["OpenAI API key (native voice)"],
        message:
          "Setup needed: OpenAI API key (native voice). Contact your operator.",
      },
    };
    const { rerender } = render(<Review {...props} />);
    await userEvent.click(
      await screen.findByRole("button", { name: "Review my permissions" }),
    );
    const next = () =>
      within(screen.getByRole("region", { name: "Your next step" }));
    expect(
      next().getByRole("heading", { name: "Review your permissions" }),
    ).toBeVisible();
    for (const checkbox of screen.getAllByRole("checkbox"))
      await userEvent.click(checkbox);
    await userEvent.click(
      screen.getByRole("button", { name: "Confirm my consent" }),
    );
    await waitFor(() =>
      expect(
        next().getByRole("heading", { name: "Wait for your facilitator" }),
      ).toBeVisible(),
    );
    expect(next().getByText(/Your consent is saved/)).toBeVisible();
    view = testView({
      client_consent: true,
      facilitator_consent: true,
      status: "introduction",
      started_at: new Date().toISOString(),
    });
    await userEvent.click(
      next().getByRole("button", { name: "Check session status" }),
    );
    await waitFor(() =>
      expect(
        next().getByRole("heading", {
          name: "Ask your operator to complete setup",
        }),
      ).toBeVisible(),
    );
    expect(next().getByText(/OpenAI API key \(native voice\)/)).toBeVisible();
    expect(
      next().queryByRole("button", { name: /Join|Connect/ }),
    ).not.toBeInTheDocument();
    rerender(
      <Review {...props} runtime={{ ready: true, missing: [], message: "" }} />,
    );
    expect(
      next().getByRole("heading", { name: "Wait for recording to be ready" }),
    ).toBeVisible();
    expect(next().getByText(/not started/)).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Turn microphone on" }),
    ).toBeDisabled();
    view = {
      ...view,
      session: {
        ...view.session,
        recording_status: "recording",
        egress_id: "synthetic-egress",
      },
    };
    await userEvent.click(
      next().getByRole("button", { name: "Check session status" }),
    );
    await waitFor(() =>
      expect(
        next().getByRole("button", { name: "Join the call" }),
      ).toBeEnabled(),
    );
    expect(next().getByText(/Joining does not turn on/)).toBeVisible();
    expect(
      document.querySelectorAll(".review-root .button.primary"),
    ).toHaveLength(1);
    expect(requests.some((url) => url.endsWith("/room-token"))).toBe(false);
    expect(connect).not.toHaveBeenCalled();
    expect(mic).not.toHaveBeenCalled();
    expect(share).not.toHaveBeenCalled();
  });
});
