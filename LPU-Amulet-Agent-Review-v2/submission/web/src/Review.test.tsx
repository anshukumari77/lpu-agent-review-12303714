import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Review } from "./Review";
import { CaptureController } from "./capture";
import { testView } from "./test-fixtures";
import { Room } from "livekit-client";
import { consentPolicyFixture } from "./consent-test-fixture";
import type { SessionStatus } from "./types";
describe("real room admission and fail-closed controls", () => {
  it.each([true, { ai: true, recording: true }])(
    "bypasses welcome when the client already consented (%j)",
    async (client_consent) => {
      const view = testView({ client_consent });
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
        />,
      );
      expect(await screen.findByText(/Your consent is saved/)).toBeVisible();
      expect(
        screen.queryByRole("button", { name: "Review my permissions" }),
      ).not.toBeInTheDocument();
    },
  );

  it.each<SessionStatus>([
    "introduction",
    "active",
    "paused",
    "finalising",
    "completed",
    "partial",
    "failed",
  ])("does not interpose welcome over a %s session", async (status) => {
    const view = testView({
      status,
      client_consent: true,
      facilitator_consent: true,
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
      />,
    );
    await screen.findByRole("heading", { name: "Unit test workflow" });
    expect(
      screen.queryByRole("button", { name: "Review my permissions" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("contentinfo", { name: "Review capture controls" }),
    ).toBeVisible();
  });

  it.each(["facilitator", "operator"] as const)(
    "does not show the client welcome to a %s",
    async (role) => {
      const view = { ...testView(), role };
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
          actor={{ role, session_id: "review-one" }}
        />,
      );
      await screen.findByRole("heading", { name: "Unit test workflow" });
      expect(
        screen.queryByRole("button", { name: "Review my permissions" }),
      ).not.toBeInTheDocument();
    },
  );

  it("cannot use the invitation to bypass an unverified actor scope", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify(testView()))),
    );
    render(
      <Review
        sessionId="review-one"
        actor={{ role: "client", session_id: "another-review" }}
      />,
    );
    await screen.findByText(/This review could not be verified for your role/);
    expect(
      screen.queryByRole("button", { name: "Review my permissions" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("does not persist invitation continuation or request media permissions", async () => {
    const storage = vi.spyOn(Storage.prototype, "setItem");
    const roomInstance = new Room();
    const microphone = vi.spyOn(
      roomInstance.localParticipant,
      "setMicrophoneEnabled",
    );
    const sharing = vi.spyOn(
      roomInstance.localParticipant,
      "setScreenShareEnabled",
    );
    const urls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string) => {
        urls.push(url);
        return new Response(
          JSON.stringify(
            url.includes("/events")
              ? { events: [] }
              : testView({ client_consent: { ai: true, recording: false } }),
          ),
        );
      }),
    );
    const props = {
      roomInstance,
      sessionId: "review-one",
      actor: { role: "client" as const, session_id: "review-one" },
    };
    const { unmount } = render(<Review {...props} />);
    await userEvent.click(
      await screen.findByRole("button", { name: "Review my permissions" }),
    );
    expect(
      await screen.findByRole("form", { name: "A review you control." }),
    ).toBeVisible();
    expect(
      screen
        .getAllByRole("checkbox")
        .every((box) => !(box as HTMLInputElement).checked),
    ).toBe(true);
    expect(storage).not.toHaveBeenCalled();
    expect(microphone).not.toHaveBeenCalledWith(true);
    expect(sharing).not.toHaveBeenCalledWith(true);
    expect(urls.some((url) => /\/(consent|room-token)$/.test(url))).toBe(false);
    unmount();
    render(<Review {...props} />);
    expect(
      await screen.findByRole("button", { name: "Review my permissions" }),
    ).toBeVisible();
  });
  it("keeps truthful companion status and capture controls visible when conversation is collapsed by keyboard", async () => {
    const view = testView({
      status: "active",
      client_consent: true,
      facilitator_consent: true,
      recording_status: "not_started",
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
      />,
    );
    const companion = await screen.findByRole("complementary", {
      name: "BEEP companion",
    });
    expect(within(companion).getByAltText("Amulet")).toHaveAttribute(
      "src",
      "/brand/amulet-monogram.svg",
    );
    const toggle = within(companion).getByRole("button", {
      name: "Hide conversation",
    });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(
      screen.getByRole("log", { name: "Saved conversation" }),
    ).toBeVisible();
    toggle.focus();
    await userEvent.keyboard("{Enter}");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveAccessibleName("Show conversation");
    expect(
      screen.queryByRole("log", { name: "Saved conversation" }),
    ).not.toBeInTheDocument();
    expect(within(companion).getByText("Recording service")).toBeVisible();
    expect(within(companion).getByText("not started")).toBeVisible();
    expect(
      within(companion).getByText("Waiting for the AI participant"),
    ).toBeVisible();
    expect(screen.getByText("Not connected")).toBeVisible();
    expect(screen.getByText("No screen shared")).toBeVisible();
    expect(screen.getByRole("button", { name: "Pause review" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Finish review" })).toBeEnabled();
    expect(
      screen.queryByText(/BEEP is listening|Recording confirmed/i),
    ).not.toBeInTheDocument();
    await userEvent.keyboard(" ");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(
      screen.getByRole("log", { name: "Saved conversation" }),
    ).toBeVisible();
  });
  it("does not request a room or devices before consent and explains missing providers", async () => {
    let view = testView();
    const urls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, options: RequestInit) => {
        urls.push(url);
        if (url.endsWith("/consent-policy"))
          return new Response(JSON.stringify({ policy: consentPolicyFixture }));
        if (url.endsWith("/consent")) {
          view = testView({
            client_consent: true,
            facilitator_consent: true,
            status: "introduction",
            recording_status: "recording",
            egress_id: "synthetic-egress",
            started_at: new Date().toISOString(),
          });
          return new Response(JSON.stringify({ session: view.session }));
        }
        if (url.endsWith("/room-token"))
          return new Response(
            JSON.stringify({ detail: "Room providers are not configured" }),
            { status: 503 },
          );
        return new Response(
          JSON.stringify(url.includes("/events") ? { events: [] } : view),
        );
      }),
    );
    render(
      <Review
        runtime={{ ready: true, missing: [], message: "" }}
        sessionId="review-one"
        actor={{ role: "client", session_id: "review-one" }}
      />,
    );
    await screen.findByRole("heading", {
      name: "Let’s look at how the work gets done.",
    });
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
    expect(urls.some((url) => url.endsWith("/consent"))).toBe(false);
    expect(urls.some((url) => url.endsWith("/room-token"))).toBe(false);
    const user = userEvent.setup();
    await user.click(
      screen.getByRole("button", { name: "Review my permissions" }),
    );
    await screen.findByText("A review you control.");
    expect(urls.some((url) => url.endsWith("/room-token"))).toBe(false);
    expect(
      screen.getByRole("button", { name: "Turn microphone on" }),
    ).toBeDisabled();
    for (const box of screen.getAllByRole("checkbox")) await user.click(box);
    await user.click(
      screen.getByRole("button", { name: "Confirm my consent" }),
    );
    await user.click(
      await screen.findByRole("button", { name: "Join the call" }),
    );
    expect(
      await screen.findByText("Room providers are not configured"),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Turn microphone on" }),
    ).toBeDisabled();
    expect(screen.queryByText("Recording confirmed")).not.toBeInTheDocument();
  });
  it("does not repeat LiveKit connect on unrelated React renders", async () => {
    const roomInstance = new Room();
    const connect = vi
      .spyOn(roomInstance, "connect")
      .mockResolvedValue(undefined);
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
                    participant_token: "unit-test-token",
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
    const props = {
      roomInstance,
      runtime: { ready: true, missing: [], message: "" },
      sessionId: "review-one",
      actor: { role: "client" as const, session_id: "review-one" },
    };
    const { rerender } = render(<Review {...props} />);
    await userEvent.click(
      await screen.findByRole("button", { name: "Join the call" }),
    );
    await waitFor(() => expect(connect).toHaveBeenCalledTimes(1));
    rerender(<Review {...props} />);
    expect(connect).toHaveBeenCalledTimes(1);
  });
  it("stops local capture before submitting pause and verifies server state", async () => {
    let view = testView({
      status: "active",
      client_consent: true,
      facilitator_consent: true,
      recording_status: "recording",
      egress_id: "synthetic-egress",
      started_at: new Date().toISOString(),
    });
    const order: string[] = [];
    const original = CaptureController.prototype.stopNow;
    // Instrument the real controller at the UI boundary; its physical stop is tested separately.
    vi.spyOn(CaptureController.prototype, "stopNow").mockImplementation(
      function (this: CaptureController) {
        order.push("local-stop");
        original.call(this);
      },
    );
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, options: RequestInit) => {
        if (url.endsWith("/control")) {
          order.push("server-pause");
          view = {
            ...view,
            session: { ...view.session, status: "paused", consent_epoch: 1 },
          };
          return new Response(JSON.stringify({ session: view.session }));
        }
        return new Response(
          JSON.stringify(url.includes("/events") ? { events: [] } : view),
        );
      }),
    );
    render(
      <Review
        runtime={{ ready: true, missing: [], message: "" }}
        sessionId="review-one"
        actor={{ role: "client", session_id: "review-one" }}
      />,
    );
    await screen.findByText("Unit test workflow");
    order.length = 0;
    await userEvent.click(screen.getByRole("button", { name: "Pause review" }));
    await waitFor(() => expect(order).toContain("server-pause"));
    expect(order.indexOf("local-stop")).toBeLessThan(
      order.indexOf("server-pause"),
    );
    expect(
      await within(
        screen.getByRole("contentinfo", { name: "Review capture controls" }),
      ).findByRole("button", { name: "Resume review" }),
    ).toBeInTheDocument();
  });
});
