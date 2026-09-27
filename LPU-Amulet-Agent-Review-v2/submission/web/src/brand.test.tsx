import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { Header } from "./ui";

describe("branded header", () => {
  it("labels the walkthrough without claiming a private live review", () => {
    render(<Header preview />);
    expect(screen.getByText("Journey preview")).toBeInTheDocument();
    expect(screen.queryByText("Private review")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "BEEP home" })).toHaveAttribute(
      "href",
      "/preview",
    );
    expect(screen.getByRole("img", { name: "Amulet" })).toBeInTheDocument();
  });
  it("keeps the live app's private role and sign-out", () => {
    render(<Header role="client" onLogout={() => {}} />);
    expect(screen.getByText("Private review")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Sign out" }),
    ).toBeInTheDocument();
  });
});
