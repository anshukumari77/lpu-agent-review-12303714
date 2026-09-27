import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { App } from "./App";
import { ApiError } from "./api";
describe("application entry", () => {
  it("shows expired invitation errors instead of an operator screen or report", async () => {
    history.replaceState(null, "", "/review/expired");
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              status: "ok",
              version: "test",
              readiness: { ready: false, blockers: ["openai_api_key"] },
            }),
          ),
      ),
    );
    const boot = Promise.reject(new Error("expired"));
    boot.catch(() => {});
    render(<App bootstrap={boot} />);
    expect(
      await screen.findByRole("heading", {
        name: "This invitation could not be opened.",
      }),
    ).toBeInTheDocument();
    expect(
      screen.queryByLabelText("Operator password"),
    ).not.toBeInTheDocument();
  });
  it("treats unavailable access checks as service failures rather than invalid invitations", async () => {
    history.replaceState(null, "", "/");
    vi.stubGlobal(
      "fetch",
      vi.fn(
        async () =>
          new Response(
            JSON.stringify({
              status: "ok",
              version: "test",
              readiness: { ready: false, blockers: [] },
            }),
          ),
      ),
    );
    const boot = Promise.reject(new ApiError(503, "Service unavailable"));
    boot.catch(() => {});
    render(<App bootstrap={boot} />);
    await screen.findByRole("heading", {
      name: "Access could not be checked.",
    });
    expect(
      screen.getByRole("link", { name: "Retry access check" }),
    ).toHaveAttribute("href", "/");
    expect(
      screen.queryByText(/Ask the operator for a valid invitation/),
    ).not.toBeInTheDocument();
  });
  it("does not load an unrelated review even if the browser path changes", async () => {
    history.replaceState(null, "", "/review/other");
    const fetcher = vi.fn(
      async (_url: string) =>
        new Response(
          JSON.stringify({
            status: "ok",
            version: "test",
            readiness: { ready: true, blockers: [] },
          }),
        ),
    );
    vi.stubGlobal("fetch", fetcher);
    render(
      <App
        bootstrap={Promise.resolve({ role: "client", session_id: "my-review" })}
      />,
    );
    expect(
      await screen.findByText("This invitation belongs to a different review."),
    ).toBeInTheDocument();
    expect(
      fetcher.mock.calls.some(([url]) =>
        String(url).includes("/sessions/other"),
      ),
    ).toBe(false);
  });
});
