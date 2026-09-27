import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ReportView } from "./ReportView";
const report = {
  schema_version: "1",
  session_id: "review-one",
  revision: 2,
  title: "Test workflow",
  summary: "Only from the supplied test evidence.",
  claims: [],
  steps: [],
  edges: [],
  unknowns: ["Approval not established"],
  recommendations: [],
  evidence: [
    {
      id: "e-1",
      kind: "transcript",
      actor: "client",
      text: "<img src=x onerror=alert(1)>",
      at_ms: 2000,
      consent_epoch: 1,
      seq: 1,
    },
  ],
  status: "partial",
  generated_at: "2026-09-11T00:00:00Z",
  internal_opportunity: { private: "Internal-only content" },
};
describe("scoped evidence-linked report", () => {
  it("renders evidence as text and never renders internal opportunity for a client", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ report }))),
    );
    const { container } = render(
      <ReportView
        sessionId="review-one"
        role="client"
        onCorrect={async () => {}}
      />,
    );
    await screen.findByText("Test workflow");
    await userEvent.click(screen.getByRole("tab", { name: "Evidence" }));
    expect(
      await screen.findByText("<img src=x onerror=alert(1)>"),
    ).toBeInTheDocument();
    expect(container.querySelector("img")).toBeNull();
    expect(screen.queryByText("Internal-only content")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Export JSON" })).toHaveAttribute(
      "href",
      "/api/sessions/review-one/export",
    );
  });
  it("does not offer operator-authored client corrections", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ report }))),
    );
    render(
      <ReportView
        sessionId="review-one"
        role="operator"
        onCorrect={async () => {}}
      />,
    );
    await screen.findByText("Test workflow");
    expect(
      screen.queryByRole("button", { name: "Submit correction" }),
    ).not.toBeInTheDocument();
  });
  it("refuses a report returned for a different session", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(
            JSON.stringify({ report: { ...report, session_id: "other" } }),
          ),
        ),
    );
    render(
      <ReportView
        sessionId="review-one"
        role="client"
        onCorrect={async () => {}}
      />,
    );
    await waitFor(() =>
      expect(
        screen.getByText(/report could not be verified for this review/i),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText("Test workflow")).not.toBeInTheDocument();
  });
});
