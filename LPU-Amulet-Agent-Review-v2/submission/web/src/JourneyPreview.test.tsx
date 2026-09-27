import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { JourneyPreview } from "./JourneyPreview";
import {
  createPreviewState,
  previewReducer,
  buildSampleDownloadHtml,
  downloadSampleReport,
  SAMPLE_EVIDENCE,
  SAMPLE_KEY_FACT,
} from "./preview-data";

describe("interactive journey", () => {
  beforeEach(() => history.replaceState({}, "", "/preview"));
  afterEach(() => history.replaceState({}, "", "/"));
  it("walks through explicit consent, preparation, handover, review and a locally corrected report without network or devices", async () => {
    const fetch = vi.fn();
    const media = vi.fn();
    vi.stubGlobal("fetch", fetch);
    Object.defineProperty(navigator, "mediaDevices", {
      configurable: true,
      value: {
        getUserMedia: media,
        getDisplayMedia: media,
        enumerateDevices: media,
      },
    });
    const user = userEvent.setup();
    render(<JourneyPreview />);
    expect(
      screen.getByText("Interactive preview · Sample workflow · No recording"),
    ).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Prepare for my review" }),
    );
    const checkboxes = screen.getAllByRole("checkbox");
    checkboxes.forEach((box) => expect(box).not.toBeChecked());
    const consent = screen.getByRole("button", { name: "Confirm my consent" });
    expect(consent).toBeDisabled();
    await user.click(checkboxes[0]);
    expect(consent).toBeDisabled();
    await user.click(checkboxes[1]);
    await user.click(consent);
    expect(
      screen.getByRole("heading", { name: "Make yourself comfortable." }),
    ).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Demonstrate speaker check" }),
    );
    expect(screen.getByText(/No audio played/)).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Continue to introduction" }),
    );
    await user.click(
      screen.getByRole("button", { name: "Preview the handover" }),
    );
    const mic = screen.getByRole("button", { name: "Simulate microphone" });
    const share = screen.getByRole("button", {
      name: "Simulate screen sharing",
    });
    await user.click(mic);
    await user.click(share);
    expect(mic).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("button", { name: "Pause preview" }));
    expect(mic).toHaveAttribute("aria-pressed", "false");
    expect(share).toHaveAttribute("aria-pressed", "false");
    await user.click(screen.getByRole("button", { name: "Resume preview" }));
    expect(mic).toHaveAttribute("aria-pressed", "false");
    await user.click(screen.getByRole("button", { name: "Preview help" }));
    expect(screen.getByText("No message has been sent.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Close help" }));
    await user.click(screen.getByRole("button", { name: "Compact companion" }));
    await user.click(screen.getByRole("button", { name: "Expand companion" }));
    await user.click(screen.getByRole("button", { name: "Show sample reply" }));
    expect(screen.getByText(/I ask the operations manager/)).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Finish sample review" }),
    );
    const fact = screen.getByRole("textbox", { name: "Key fact to check" });
    await user.clear(fact);
    await user.type(fact, "Operations lead confirms receipt.");
    await user.click(screen.getByRole("button", { name: "Save sample fact" }));
    await user.click(
      screen.getByRole("button", { name: "Open sample report" }),
    );
    expect(screen.getByText("Operations lead confirms receipt.")).toBeVisible();
    await user.click(
      screen.getAllByRole("button", { name: /View sample evidence/ })[0],
    );
    expect(screen.getByRole("tab", { name: "Evidence" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByText(SAMPLE_EVIDENCE[0].text)).toBeVisible();
    await user.click(screen.getByRole("tab", { name: "Overview" }));
    await user.type(
      screen.getByRole("textbox", { name: "Add a sample correction" }),
      "Receipt comes from the warehouse.",
    );
    await user.click(
      screen.getByRole("button", { name: "Save local correction" }),
    );
    expect(screen.getByRole("status")).toHaveTextContent("not regenerated");
    expect(screen.getByText("Receipt comes from the warehouse.")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Restart preview" }));
    await user.click(screen.getByRole("button", { name: "Explore Report" }));
    expect(
      screen.queryByText("Receipt comes from the warehouse."),
    ).not.toBeInTheDocument();
    expect(screen.getByText(SAMPLE_KEY_FACT)).toBeVisible();
    expect(fetch).not.toHaveBeenCalled();
    expect(media).not.toHaveBeenCalled();
  });
  it("labels designer skips without granting consent and resets permissions", async () => {
    const user = userEvent.setup();
    render(<JourneyPreview />);
    await user.click(screen.getByRole("button", { name: "Explore Review" }));
    expect(
      screen.getByText(/Permission step skipped for exploration/),
    ).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Simulate disconnection" }),
    );
    expect(
      screen.getByRole("heading", {
        name: "Connection interrupted · sample state",
      }),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Simulate microphone" }),
    ).toBeDisabled();
    await user.click(
      screen.getByRole("button", { name: "Retry simulated connection" }),
    );
    expect(
      screen.getByRole("button", { name: "Simulate microphone" }),
    ).toHaveAttribute("aria-pressed", "false");
    await user.click(
      screen.getByRole("button", { name: "Explore Permission" }),
    );
    within(screen.getByRole("group", { name: "Required review permissions" }))
      .getAllByRole("checkbox")
      .forEach((box) => expect(box).not.toBeChecked());
  });
});

describe("local preview state", () => {
  it("downloads a marked HTML sample with user text escaped and no remote resources", async () => {
    const state = {
      ...createPreviewState(),
      keyFact: '<img src=x onerror="alert(1)">',
      correction: "<script>alert('x')</script> & 'quoted'",
    };
    const html = buildSampleDownloadHtml(state);
    expect(html).toContain("SAMPLE");
    expect(html).toContain("&lt;img");
    expect(html).toContain("&lt;script&gt;");
    expect(html).toContain("&amp;");
    expect(html).not.toContain("<script>");
    expect(html).not.toContain("<img");
    expect(html).not.toMatch(/https?:\/\//);
    const createObjectURL = vi.fn((_blob: Blob) => "blob:local-sample");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", { createObjectURL, revokeObjectURL });
    let link: HTMLAnchorElement | undefined;
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      link = this;
    });
    downloadSampleReport(state);
    const blob = createObjectURL.mock.calls[0][0] as Blob;
    expect(blob.type).toBe("text/html;charset=utf-8");
    const text = await new Promise<string>((resolve) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.readAsText(blob);
    });
    expect(text).toBe(html);
    expect(link?.download).toBe("amulet-beep-SAMPLE-report.html");
    expect(link?.href).toBe("blob:local-sample");
    expect(document.querySelector("a[download]")).toBeNull();
    await vi.waitFor(() =>
      expect(revokeObjectURL).toHaveBeenCalledWith("blob:local-sample"),
    );
  });
  it("keeps corrections local and resets all sample state on restart or designer jumps", () => {
    let state = previewReducer(createPreviewState(), {
      type: "explore",
      step: "recap",
    });
    state = previewReducer(state, {
      type: "fact-draft",
      value: "Operations lead, not finance",
    });
    state = previewReducer(state, { type: "save-fact" });
    expect(state.keyFact).toBe("Operations lead, not finance");
    expect(state.notice).toMatch(/locally/i);
    state = previewReducer(state, { type: "next" });
    expect(state.keyFact).toBe("Operations lead, not finance");
    state = previewReducer(state, {
      type: "correction-draft",
      value: "Check the approval threshold.",
    });
    state = previewReducer(state, { type: "save-correction" });
    expect(state.correction).toBe("Check the approval threshold.");
    expect(state.correctionDraft).toBe("");
    expect(state.notice).toMatch(/not regenerated/i);
    expect(previewReducer(state, { type: "restart" })).toEqual(
      createPreviewState(),
    );
    expect(previewReducer(state, { type: "explore", step: "review" })).toEqual({
      ...createPreviewState(),
      step: "review",
    });
    const permission = previewReducer(state, {
      type: "explore",
      step: "permissions",
    });
    expect(permission.consent).toBe(false);
    const back = previewReducer(
      { ...state, step: "review", mic: true, screen: true },
      { type: "back" },
    );
    expect(back.step).toBe("introduction");
    expect(back.mic).toBe(false);
    expect(back.screen).toBe(false);
  });
  it("pause clears simulated capture and resume leaves it off", () => {
    let state = previewReducer(createPreviewState(), {
      type: "explore",
      step: "review",
    });
    state = previewReducer(state, { type: "toggle-mic" });
    state = previewReducer(state, { type: "toggle-screen" });
    expect(state.mic).toBe(true);
    expect(state.screen).toBe(true);
    state = previewReducer(state, { type: "pause" });
    expect(state.status).toBe("paused");
    expect(state.mic).toBe(false);
    expect(state.screen).toBe(false);
    expect(previewReducer(state, { type: "toggle-mic" }).mic).toBe(false);
    state = previewReducer(state, { type: "resume" });
    expect(state.status).toBe("active");
    expect(state.mic).toBe(false);
    expect(state.screen).toBe(false);
    state = previewReducer(state, { type: "toggle-screen" });
    expect(state.screen).toBe(true);
    state = previewReducer(state, { type: "disconnect" });
    expect(state.status).toBe("disconnected");
    expect(state.screen).toBe(false);
    expect(previewReducer(state, { type: "resume" }).screen).toBe(false);
  });
  it("requires explicit sample consent for sequential progression", () => {
    const welcome = createPreviewState();
    expect(welcome.consent).toBe(false);
    const permissions = previewReducer(welcome, { type: "next" });
    expect(permissions.step).toBe("permissions");
    expect(previewReducer(permissions, { type: "next" }).step).toBe(
      "permissions",
    );
    const accepted = previewReducer(permissions, { type: "consent" });
    expect(accepted.consent).toBe(true);
    expect(accepted.step).toBe("setup");
  });
});
