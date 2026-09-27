import { beforeEach, describe, it, expect, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ConsentForm } from "./ConsentForm";
import { consentPolicyFixture as policy } from "./consent-test-fixture";

describe("explicit consent", () => {
  beforeEach(() => {
    history.replaceState({}, "", "/");
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ policy }))),
    );
  });
  it("keeps the explicitly labelled preview offline and never supplies a valid acceptance binding", async () => {
    history.replaceState({}, "", "/preview");
    try {
      const fetch = vi
        .fn()
        .mockRejectedValue(new Error("No network in preview"));
      vi.stubGlobal("fetch", fetch);
      const onConsent = vi.fn().mockResolvedValue(undefined);
      render(<ConsentForm onConsent={onConsent} busy={false} />);
      expect(fetch).not.toHaveBeenCalled();
      expect(
        screen.getByText(/Preview only: no consent is recorded/),
      ).toBeVisible();
      for (const box of screen.getAllByRole("checkbox"))
        await userEvent.click(box);
      await userEvent.click(
        screen.getByRole("button", { name: "Confirm my consent" }),
      );
      expect(onConsent.mock.calls[0][0].policy_id).toBe("preview-only");
    } finally {
      history.replaceState({}, "", "/");
    }
  });
  it("renders the served named notice and submits its version and content binding", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ policy }))),
    );
    const onConsent = vi.fn().mockResolvedValue(undefined);
    render(<ConsentForm onConsent={onConsent} busy={false} />);
    expect(await screen.findByText(policy.notice.providers)).toBeVisible();
    for (const text of Object.values(policy.notice))
      expect(screen.getByText(text)).toBeVisible();
    for (const box of screen.getAllByRole("checkbox")) {
      expect(box).not.toBeChecked();
      await userEvent.click(box);
    }
    await userEvent.click(
      screen.getByRole("button", { name: "Confirm my consent" }),
    );
    expect(onConsent).toHaveBeenCalledExactlyOnceWith({
      ai: true,
      recording: true,
      notice_version: policy.notice_version,
      policy_id: policy.policy_id,
    });
  });
  it("blocks duplicate confirmation while saving without changing the callback contract", async () => {
    const user = userEvent.setup();
    const onConsent = vi.fn().mockResolvedValue(undefined);
    const { rerender } = render(
      <ConsentForm onConsent={onConsent} busy={false} />,
    );
    await screen.findByText(policy.notice.providers);
    for (const option of screen.getAllByRole("checkbox"))
      await user.click(option);
    rerender(<ConsentForm onConsent={onConsent} busy />);
    screen
      .getAllByRole("checkbox")
      .forEach((option) => expect(option).toBeDisabled());
    const confirm = screen.getByRole("button", { name: "Saving consent…" });
    expect(confirm).toBeDisabled();
    await user.click(confirm);
    expect(onConsent).not.toHaveBeenCalled();
  });
  it("names the permission form and explains device privacy before either opt-in", async () => {
    render(<ConsentForm onConsent={vi.fn()} busy={false} />);
    await screen.findByText(policy.notice.providers);
    expect(
      screen.getByRole("form", { name: "A review you control." }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Your camera is never requested/)).toBeVisible();
    expect(
      screen.getByRole("group", { name: "Required review permissions" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Deployment policy is UNAPPROVED/)).toBeVisible();
    expect(
      screen.getByText(
        /Connecting does not turn on your microphone or share your screen/,
      ),
    ).toBeVisible();
  });
  it("requires two unchecked opt-ins and sends both choices only after confirmation", async () => {
    const user = userEvent.setup();
    const onConsent = vi.fn().mockResolvedValue(undefined);
    render(<ConsentForm onConsent={onConsent} busy={false} />);
    await screen.findByText(policy.notice.providers);
    const options = screen.getAllByRole("checkbox");
    options.forEach((option) => expect(option).not.toBeChecked());
    expect(
      screen.getByRole("button", { name: "Confirm my consent" }),
    ).toBeDisabled();
    await user.click(options[0]);
    expect(
      screen.getByRole("button", { name: "Confirm my consent" }),
    ).toBeDisabled();
    await user.click(options[1]);
    await user.click(
      screen.getByRole("button", { name: "Confirm my consent" }),
    );
    expect(onConsent).toHaveBeenCalledExactlyOnceWith({
      ai: true,
      recording: true,
      notice_version: policy.notice_version,
      policy_id: policy.policy_id,
    });
  });
  it.each([null, { policy: {} }, { policy: { ...policy, notice: {} } }])(
    "fails closed for an unavailable or malformed notice (%j)",
    async (payload) => {
      vi.stubGlobal(
        "fetch",
        vi.fn(async () => new Response(JSON.stringify(payload))),
      );
      const onConsent = vi.fn();
      render(<ConsentForm onConsent={onConsent} busy={false} />);
      await screen.findByRole("alert");
      for (const box of screen.getAllByRole("checkbox")) {
        expect(box).toBeDisabled();
        expect(box).not.toBeChecked();
      }
      expect(
        screen.getByRole("button", { name: "Confirm my consent" }),
      ).toBeDisabled();
      expect(onConsent).not.toHaveBeenCalled();
    },
  );
  it("reload clears both choices and late responses cannot replace a newer notice", async () => {
    let resolveOld!: (value: Response) => void;
    const next = {
      ...policy,
      notice_version: "synthetic-v2",
      policy_id: "d".repeat(64),
      notice: {
        ...policy.notice,
        providers: "A newly selected synthetic provider configuration.",
      },
    };
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ policy })))
      .mockImplementationOnce(
        () =>
          new Promise<Response>((resolve) => {
            resolveOld = resolve;
          }),
      )
      .mockResolvedValueOnce(new Response(JSON.stringify({ policy: next })));
    vi.stubGlobal("fetch", fetch);
    const onConsent = vi.fn().mockResolvedValue(undefined);
    render(<ConsentForm onConsent={onConsent} busy={false} />);
    await screen.findByText(policy.notice.providers);
    for (const box of screen.getAllByRole("checkbox"))
      await userEvent.click(box);
    await userEvent.click(
      screen.getByRole("button", { name: "Reload consent notice" }),
    );
    for (const box of screen.getAllByRole("checkbox")) {
      expect(box).toBeDisabled();
      expect(box).not.toBeChecked();
    }
    await userEvent.click(
      screen.getByRole("button", { name: "Reload consent notice" }),
    );
    await screen.findByText(next.notice.providers);
    await act(async () => resolveOld(new Response(JSON.stringify({ policy }))));
    expect(screen.queryByText(policy.notice.providers)).not.toBeInTheDocument();
    for (const box of screen.getAllByRole("checkbox"))
      await userEvent.click(box);
    await userEvent.click(
      screen.getByRole("button", { name: "Confirm my consent" }),
    );
    expect(onConsent).toHaveBeenCalledExactlyOnceWith({
      ai: true,
      recording: true,
      notice_version: next.notice_version,
      policy_id: next.policy_id,
    });
  });
});
