import { describe, it, expect, vi } from "vitest";
import { Track, type LocalParticipant, type LocalTrack } from "livekit-client";
import { CaptureController } from "./capture";
function harness() {
  const order: string[] = [];
  const track = {
    source: Track.Source.Microphone,
    stop: vi.fn(() => order.push("stop")),
    mediaStreamTrack: { readyState: "live" },
    on: vi.fn(),
    off: vi.fn(),
  } as unknown as LocalTrack;
  const participant = {
    createTracks: vi.fn().mockResolvedValue([track]),
    createScreenTracks: vi.fn().mockResolvedValue([track]),
    publishTrack: vi.fn().mockResolvedValue({ track }),
    unpublishTrack: vi.fn(() => {
      order.push("unpublish");
      return Promise.resolve();
    }),
    trackPublications: new Map(),
  } as unknown as LocalParticipant;
  const capture = new CaptureController(participant);
  return { capture, participant, track, order };
}
describe("consent-bound browser capture", () => {
  it("cannot request permissions before it has an explicit capture gate", async () => {
    const { capture, participant } = harness();
    await capture.enable("microphone");
    expect(participant.createTracks).not.toHaveBeenCalled();
    expect(capture.getSnapshot().error).toMatch(/consent|connect/i);
  });
  it("stops physical tracks and initiates unpublish before a pause request can start", async () => {
    const { capture, participant, track, order } = harness();
    capture.allow();
    await capture.enable("microphone");
    expect(participant.publishTrack).toHaveBeenCalled();
    capture.stopNow();
    order.push("server-pause");
    expect(track.stop).toHaveBeenCalled();
    expect(order).toEqual(["stop", "unpublish", "server-pause"]);
    expect(capture.getSnapshot().microphone).toBe(false);
  });
  it("discards a screen picker resolved after pause and requires a new explicit picker after resume", async () => {
    const { capture, participant, track } = harness();
    let resolve!: (tracks: LocalTrack[]) => void;
    vi.mocked(participant.createScreenTracks).mockReturnValue(
      new Promise((r) => {
        resolve = r;
      }),
    );
    capture.allow();
    const selecting = capture.enable("screen");
    capture.stopNow();
    capture.allow();
    resolve([track]);
    await selecting;
    expect(track.stop).toHaveBeenCalled();
    expect(participant.publishTrack).not.toHaveBeenCalled();
    expect(participant.createScreenTracks).toHaveBeenCalledTimes(1);
    expect(capture.getSnapshot().screen).toBe(false);
  });
  it("stops a publishing track immediately and removes late publication after finish", async () => {
    const { capture, participant, track } = harness();
    let resolve!: () => void;
    vi.mocked(participant.publishTrack).mockReturnValue(
      new Promise((r) => {
        resolve = () => r({ track } as never);
      }),
    );
    capture.allow();
    const pending = capture.enable("microphone");
    await Promise.resolve();
    capture.stopNow();
    expect(track.stop).toHaveBeenCalled();
    resolve();
    await pending;
    expect(participant.unpublishTrack).toHaveBeenCalled();
    expect(capture.getSnapshot().microphone).toBe(false);
  });
});
