import { it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { App } from "./App";
import { testView } from "./test-fixtures";
import { Review } from "./Review";
import { runtimeReadiness } from "./readiness";
import { writeFileSync, mkdirSync } from "node:fs";

it("keeps the sharing and retention boundary visible after consent", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async (url: string) =>
        new Response(
          JSON.stringify(
            url.includes("/events")
              ? { events: [] }
              : testView({
                  client_consent: true,
                  facilitator_consent: true,
                  status: "introduction",
                }),
          ),
        ),
    ),
  );
  render(
    <Review
      sessionId="review-one"
      actor={{ role: "client", session_id: "review-one" }}
    />,
  );
  expect(
    await screen.findByRole("heading", {
      name: "Your example will appear here.",
    }),
  ).toBeInTheDocument();
  expect(
    screen.getByText(
      /Shared voice and screen are sent to AI and recording providers/,
    ),
  ).toBeInTheDocument();
  expect(
    screen.queryByText("Your work stays with you."),
  ).not.toBeInTheDocument();
  // Explicit offline render fixture for geometry only, not API/provider proof.
  mkdirSync("review-fix-artifacts", { recursive: true });
  writeFileSync(
    "review-fix-artifacts/room-fixture.html",
    document.body.innerHTML,
  );
});

it("explains the missing provider and prevents room admission after consent", async () => {
  history.replaceState(null, "", "/review/review-one");
  const urls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      urls.push(url);
      return new Response(
        JSON.stringify(
          url.endsWith("/health")
            ? {
                status: "ok",
                version: "test",
                readiness: {
                  ready: false,
                  configured: { openai_api_key: false },
                  blockers: ["openai_api_key", "unrecognised-sensitive-value"],
                },
              }
            : url.includes("/events")
              ? { events: [] }
              : testView({
                  status: "introduction",
                  client_consent: true,
                  facilitator_consent: true,
                  started_at: new Date().toISOString(),
                }),
        ),
      );
    }),
  );
  render(
    <App
      bootstrap={Promise.resolve({ role: "client", session_id: "review-one" })}
    />,
  );
  expect(
    await screen.findByRole("heading", {
      name: "Ask your operator to complete setup",
    }),
  ).toBeVisible();
  expect(
    screen.queryByRole("button", { name: "Join the call" }),
  ).not.toBeInTheDocument();
  expect(
    screen.getAllByText(/OpenAI API key \(native voice\)/).length,
  ).toBeGreaterThan(0);
  expect(
    screen.queryByText("Consent is in place. Connect when ready."),
  ).not.toBeInTheDocument();
  expect(document.body.textContent).not.toContain(
    "unrecognised-sensitive-value",
  );
  expect(urls.some((url) => url.endsWith("/room-token"))).toBe(false);
  expect(
    screen.getByRole("button", { name: "Turn microphone on" }),
  ).toBeDisabled();
});

it("labels OAuth configuration separately from voice without exposing unknown blocker values", () => {
  const status = runtimeReadiness({
    status: "ok",
    version: "synthetic",
    readiness: {
      ready: false,
      blockers: [
        "codex_home",
        "codex_executable",
        "codex_model",
        "openai_api_key",
        "recording_enabled",
        "SYNTHETIC-SENSITIVE-DETAIL",
      ],
    },
  });
  expect(status.ready).toBe(false);
  expect(status.missing).toEqual([
    "Codex OAuth home selection",
    "Codex executable",
    "Codex reasoning model",
    "OpenAI API key (native voice)",
    "Recording must remain enabled",
    "Service configuration",
  ]);
  expect(status.message).not.toContain("SYNTHETIC-SENSITIVE-DETAIL");
});
