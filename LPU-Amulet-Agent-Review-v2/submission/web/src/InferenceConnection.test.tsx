import { expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Operator } from "./Operator";
import { InferenceConnection } from "./InferenceConnection";

// Explicitly fake OAuth metadata; no real account, model or voice access.
const fakeConnected = {
  reasoning: {
    provider: "codex",
    model: "gpt-5.5",
    configured: true,
    authenticated: true,
    state: "connected",
    inference_verified: false,
  },
  native_voice: {
    provider: "openai",
    model: "gpt-realtime",
    api_key_configured: false,
    live_verified: false,
    oauth_state: "experimental_unqualified",
  },
};

it("shows checked OAuth reasoning separately from the native voice key gap in the operator workspace", async () => {
  const calls: RequestInit[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, options: RequestInit) => {
      if (url === "/api/operator/inference") calls.push(options);
      return new Response(
        JSON.stringify(
          url === "/api/operator/inference"
            ? fakeConnected
            : url === "/api/packs"
              ? { packs: [] }
              : { sessions: [] },
        ),
      );
    }),
  );
  render(<Operator />);
  const panel = await screen.findByRole("region", {
    name: "Inference connection",
  });
  expect(await within(panel).findByText("ChatGPT / Codex")).toBeInTheDocument();
  expect(within(panel).getByText("gpt-5.5")).toBeInTheDocument();
  expect(within(panel).getByText("OAuth sign-in checked")).toBeInTheDocument();
  expect(
    within(panel).getByText(/Native voice requires a separate OpenAI API key/),
  ).toBeInTheDocument();
  expect(
    within(panel).getByText(
      /Native voice via OAuth remains experimental and unqualified/,
    ),
  ).toBeInTheDocument();
  expect(
    within(panel).getByText(/No inference or voice test was made/),
  ).toBeInTheDocument();
  expect(panel.querySelector("input, select, textarea")).toBeNull();
  await userEvent.click(
    within(panel).getByRole("button", { name: "Check connection status" }),
  );
  expect(calls).toHaveLength(2);
  expect(
    calls.every((call) => call.method === "GET" && call.body === undefined),
  ).toBe(true);
});

it("does not claim checked authentication from inconsistent configured metadata", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            ...fakeConnected,
            reasoning: { ...fakeConnected.reasoning, configured: false },
          }),
        ),
    ),
  );
  render(<InferenceConnection />);
  await screen.findByText("ChatGPT / Codex");
  expect(screen.queryByText("OAuth sign-in checked")).not.toBeInTheDocument();
  expect(screen.getByText("OAuth status unavailable")).toBeInTheDocument();
});

it("shows local API-key setup without claiming live verification", async () => {
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(
          JSON.stringify({
            ...fakeConnected,
            reasoning: {
              ...fakeConnected.reasoning,
              provider: "openai",
              model: "gpt-5.4-mini",
              authenticated: null,
              state: "locally_configured",
            },
            native_voice: {
              ...fakeConnected.native_voice,
              api_key_configured: true,
            },
          }),
        ),
    ),
  );
  render(<InferenceConnection />);
  expect(await screen.findByText("OpenAI API")).toBeInTheDocument();
  expect(
    screen.getByText("API key configured locally; not live verified"),
  ).toBeInTheDocument();
  expect(
    screen.getByText(/voice has not been live verified/),
  ).toBeInTheDocument();
  expect(screen.queryByText("OAuth sign-in checked")).not.toBeInTheDocument();
});

it("clears a checked status after a failed read without echoing provider errors", async () => {
  const fetcher = vi
    .fn()
    .mockResolvedValueOnce(new Response(JSON.stringify(fakeConnected)))
    .mockResolvedValueOnce(
      new Response(JSON.stringify({ detail: "SYNTHETIC-PRIVATE-DETAIL" }), {
        status: 503,
      }),
    );
  vi.stubGlobal("fetch", fetcher);
  render(<InferenceConnection />);
  await screen.findByText("OAuth sign-in checked");
  await userEvent.click(
    screen.getByRole("button", { name: "Check connection status" }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Connection status unavailable",
  );
  expect(screen.queryByText("OAuth sign-in checked")).not.toBeInTheDocument();
  expect(document.body.textContent).not.toContain("SYNTHETIC-PRIVATE-DETAIL");
});
