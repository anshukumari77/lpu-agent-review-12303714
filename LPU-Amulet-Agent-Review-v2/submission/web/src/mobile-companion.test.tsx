import { expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { JourneyPreview } from "./JourneyPreview";
it("opens a compact companion on mobile with controls and expansion available", async () => {
  vi.stubGlobal("innerWidth", 390);
  const user = userEvent.setup();
  render(<JourneyPreview />);
  await user.click(screen.getByRole("button", { name: "Explore Review" }));
  expect(
    screen.getByRole("button", { name: "Expand companion" }),
  ).toBeVisible();
  expect(
    screen.getByRole("button", { name: "Finish sample review" }),
  ).toBeVisible();
  expect(screen.getByRole("button", { name: "Pause preview" })).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Expand companion" }));
  expect(
    screen.getByRole("button", { name: "Show sample reply" }),
  ).toBeVisible();
});
