import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ClientWelcome } from "./ClientWelcome";

describe("shared client invitation", () => {
  it("makes the real review a question-led map, not an automation builder, with one next action", async () => {
    const onContinue = vi.fn();
    render(<ClientWelcome title="Invoice approvals" onContinue={onContinue} />);
    expect(
      screen.getByText(/BEEP will ask questions to map your existing workflow/),
    ).toBeVisible();
    expect(
      screen.getByText(/does not automate your work or build integrations/),
    ).toBeVisible();
    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(onContinue).not.toHaveBeenCalled();
    await userEvent.click(
      screen.getByRole("button", { name: "Review my permissions" }),
    );
    expect(onContinue).toHaveBeenCalledExactlyOnceWith();
  });
  it("explains the workflow, real-example preparation, time and screen boundaries", () => {
    const { container } = render(
      <ClientWelcome title="Invoice approvals" onContinue={vi.fn()} />,
    );
    expect(
      screen.getByRole("heading", {
        level: 1,
        name: "Let’s look at how the work gets done.",
      }),
    ).toBeVisible();
    expect(screen.getByText("Invoice approvals")).toBeVisible();
    expect(
      screen.getByText(
        /Up to 90 minutes, including 15 minutes with your facilitator/,
      ),
    ).toBeVisible();
    expect(
      screen.getByText(/follow one invoice from arrival to approval/),
    ).toBeVisible();
    expect(screen.getByText(/Share only the window or tab/)).toHaveTextContent(
      "Your camera is never requested",
    );
    expect(
      screen.getByText(
        /Shared voice and screen go to AI and recording providers/,
      ),
    ).toBeVisible();
    expect(container.querySelector(".welcome-artwork img")).toHaveAttribute(
      "src",
      "/brand/amulet-3d.png",
    );
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });

  it("continues by keyboard without fetching, saving browser state or requesting devices", async () => {
    const onContinue = vi.fn();
    const fetch = vi.fn();
    const getUserMedia = vi.fn();
    const getDisplayMedia = vi.fn();
    const storage = vi.spyOn(Storage.prototype, "setItem");
    vi.stubGlobal("fetch", fetch);
    vi.stubGlobal("navigator", {
      ...navigator,
      mediaDevices: { getUserMedia, getDisplayMedia },
    });
    render(<ClientWelcome title="Invoice approvals" onContinue={onContinue} />);
    const user = userEvent.setup();
    await user.tab();
    expect(
      screen.getByRole("button", { name: "Review my permissions" }),
    ).toHaveFocus();
    await user.keyboard("{Enter}");
    expect(onContinue).toHaveBeenCalledExactlyOnceWith();
    expect(fetch).not.toHaveBeenCalled();
    expect(storage).not.toHaveBeenCalled();
    expect(getUserMedia).not.toHaveBeenCalled();
    expect(getDisplayMedia).not.toHaveBeenCalled();
  });

  it("labels the preview invitation and keeps the same continuation contract", async () => {
    const onContinue = vi.fn();
    render(
      <ClientWelcome
        title="Sample invoice workflow"
        onContinue={onContinue}
        preview
      />,
    );
    expect(screen.getByText("BEEP / Preview invitation")).toBeVisible();
    await userEvent.click(
      screen.getByRole("button", { name: "Prepare for my review" }),
    );
    expect(onContinue).toHaveBeenCalledExactlyOnceWith();
  });
});
