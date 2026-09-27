import {
  Track,
  TrackEvent,
  type LocalParticipant,
  type LocalTrack,
} from "livekit-client";

type Source = "microphone" | "screen";
export interface CaptureState {
  microphone: boolean;
  screen: boolean;
  microphoneBusy: boolean;
  screenBusy: boolean;
  error: string;
}
/** Owns even unpublished tracks so pause also fences a pending picker/publish. */
export class CaptureController {
  private epoch = 0;
  private allowed = false;
  private acquisitionReady = true;
  private owned = new Map<LocalTrack, Source>();
  private pending = new Set<LocalTrack>();
  private listeners = new Set<() => void>();
  private state: CaptureState = {
    microphone: false,
    screen: false,
    microphoneBusy: false,
    screenBusy: false,
    error: "",
  };
  constructor(private participant: LocalParticipant) {}
  getSnapshot = () => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private update(patch: Partial<CaptureState>) {
    this.state = { ...this.state, ...patch };
    this.listeners.forEach((fn) => fn());
  }
  allow() {
    this.allowed = true;
  }
  setAcquisitionReady(ready: boolean) {
    if (this.acquisitionReady === ready) return;
    this.acquisitionReady = ready;
    if (!ready) {
      // Revoke pending work without changing the policy for existing capture.
      this.epoch += 1;
      this.pending.forEach((track) => this.release(track));
      this.update({ microphoneBusy: false, screenBusy: false });
    }
  }
  clearError() {
    this.update({ error: "" });
  }
  private release(track: LocalTrack) {
    // stop() is synchronous; unpublish signaling must never delay privacy controls.
    track.stop();
    this.owned.delete(track);
    this.pending.delete(track);
    void this.participant.unpublishTrack(track, true).catch(() => {
      this.update({
        error:
          "Local capture stopped. The room could not confirm track removal; disconnect and reconnect before sharing again.",
      });
    });
  }
  stopNow() {
    this.allowed = false;
    this.epoch += 1;
    const tracks = new Set(this.owned.keys());
    this.participant.trackPublications.forEach((pub) => {
      if (pub.track) tracks.add(pub.track);
    });
    tracks.forEach((track) => this.release(track));
    this.update({
      microphone: false,
      screen: false,
      microphoneBusy: false,
      screenBusy: false,
    });
  }
  disable(source: Source) {
    // A source-level stop also revokes pending operations; no late track is permitted.
    this.epoch += 1;
    Array.from(this.owned).forEach(([track, kind]) => {
      if (kind === source) this.release(track);
    });
    this.update({ [source]: false, microphoneBusy: false, screenBusy: false });
  }
  async enable(source: Source) {
    if (!this.allowed) {
      this.update({
        error:
          "Connect after both participants consent before enabling capture.",
      });
      return;
    }
    if (
      !this.acquisitionReady ||
      this.state[source] ||
      this.state[`${source}Busy`]
    )
      return;
    const epoch = this.epoch;
    this.update({ [`${source}Busy`]: true, error: "" });
    let tracks: LocalTrack[] = [];
    const current = () =>
      this.allowed && this.acquisitionReady && epoch === this.epoch;
    try {
      // Called from the user's click; never first await a token/network request before picker.
      tracks =
        source === "screen"
          ? await this.participant.createScreenTracks({
              audio: false,
              video: true,
            })
          : await this.participant.createTracks({
              audio: { echoCancellation: true, noiseSuppression: true },
              video: false,
            });
      if (!current()) {
        tracks.forEach((track) => track.stop());
        return;
      }
      tracks.forEach((track) => {
        this.owned.set(track, source);
        this.pending.add(track);
      });
      for (const track of tracks) {
        if (!current()) {
          this.release(track);
          continue;
        }
        await this.participant.publishTrack(track, {
          source:
            source === "screen"
              ? Track.Source.ScreenShare
              : Track.Source.Microphone,
        });
        if (!current()) {
          this.release(track);
          continue;
        }
        this.pending.delete(track);
        track.on(TrackEvent.Ended, () => {
          this.release(track);
          this.update({ [source]: false });
        });
      }
      if (current()) this.update({ [source]: tracks.length > 0 });
    } catch (error) {
      tracks.forEach((track) => this.release(track));
      if (current()) {
        const name = error instanceof Error ? error.name : "";
        this.update({
          [source]: false,
          error:
            name === "NotAllowedError"
              ? `${source === "screen" ? "Screen sharing" : "Microphone access"} was cancelled or denied. Nothing new is being captured. You can try again.`
              : `Could not start ${source === "screen" ? "screen sharing" : "the microphone"}. Check your browser permissions and room connection.`,
        });
      }
    } finally {
      if (current()) this.update({ [`${source}Busy`]: false });
    }
  }
}
