import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Operator } from "./Operator";
import { Login } from "./Login";
describe("private operator actions", () => {
  it("starts with a workflow question and keeps separate invitations unconsumed after verified creation", async () => {
    const requests: { url: string; method?: string; body?: string }[] = [];
    const session = {
      id: "guidance-session",
      title: "Invoice approvals",
      status: "awaiting_consent",
      offer: "sponsored",
      created_at: "2026-09-11T00:00:00Z",
      pack_id: "general",
    };
    const invitations = {
      client:
        "http://localhost/review/guidance-session#invite=synthetic-client",
      facilitator:
        "http://localhost/review/guidance-session#invite=synthetic-facilitator",
    };
    let readback: (() => void) | undefined;
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, options: RequestInit) => {
        requests.push({
          url,
          method: options.method,
          body: options.body as string,
        });
        if (url === "/api/sessions/guidance-session") {
          await new Promise<void>((resolve) => {
            readback = resolve;
          });
          return new Response(
            JSON.stringify({ session, role: "operator", snapshot: {} }),
          );
        }
        return new Response(
          JSON.stringify(
            url === "/api/packs"
              ? {
                  packs: [
                    {
                      id: "general",
                      title: "General workflow",
                      description:
                        "Questions about steps, handoffs and exceptions.",
                    },
                  ],
                }
              : url === "/api/sessions" && options.method === "POST"
                ? { session, invitations }
                : { sessions: [] },
          ),
        );
      }),
    );
    const open = vi.spyOn(window, "open");
    const user = userEvent.setup();
    render(<Operator />);
    await screen.findByRole("option", { name: "General workflow" });
    const title = screen.getByRole("textbox", {
      name: "Which workflow will you review?",
    });
    expect(title).toHaveAccessibleDescription(/Name one existing process/);
    expect(
      screen.getByLabelText("Questions to guide the review"),
    ).toBeVisible();
    expect(
      screen
        .getAllByRole("radio")
        .every((radio) => !(radio as HTMLInputElement).checked),
    ).toBe(true);
    const create = screen.getByRole("button", {
      name: "Create session & invitations",
    });
    expect(create).toBeDisabled();
    expect(create).toHaveAccessibleDescription(/Name the workflow/);
    await user.type(title, "Invoice approvals");
    await user.click(screen.getByLabelText(/Sponsored/));
    await user.click(create);
    await waitFor(() => expect(readback).toBeDefined());
    expect(
      screen.queryByRole("button", { name: "Copy client invite" }),
    ).not.toBeInTheDocument();
    readback!();
    await screen.findByRole("heading", {
      name: "Send the two invitations separately.",
    });
    expect(
      screen.getByRole("region", { name: "Private invitations" }),
    ).toHaveFocus();
    expect(
      screen.getByText("Invoice approvals", { selector: "strong" }),
    ).toBeVisible();
    expect(create).toHaveAccessibleDescription(
      /Session created.*Send the client and facilitator/,
    );
    expect(
      screen.getByRole("heading", { name: "Client invitation" }),
    ).toBeVisible();
    expect(
      screen.getByRole("heading", { name: "Facilitator invitation" }),
    ).toBeVisible();
    expect(
      screen.getByText(/Copying a link does not open or use it/),
    ).toBeVisible();
    expect(
      screen.getByRole("link", { name: "Open read-only operator view" }),
    ).toHaveAttribute("href", "/review/guidance-session");
    expect(
      screen
        .queryAllByRole("link")
        .some((link) => link.getAttribute("href")?.includes("#invite=")),
    ).toBe(false);
    await user.click(
      screen.getByRole("button", { name: "Copy client invite" }),
    );
    expect(await navigator.clipboard.readText()).toBe(invitations.client);
    await user.click(
      screen.getByRole("button", { name: "Copy facilitator invite" }),
    );
    expect(await navigator.clipboard.readText()).toBe(invitations.facilitator);
    expect(open).not.toHaveBeenCalled();
    expect(requests.filter((request) => request.method === "POST")).toEqual([
      {
        url: "/api/sessions",
        method: "POST",
        body: JSON.stringify({
          title: "Invoice approvals",
          pack_id: "general",
          offer: "sponsored",
        }),
      },
    ]);
    expect(localStorage.length).toBe(0);
  });
  it("requires an explicit paid confirmation, creates via API, and verifies the created session", async () => {
    const requests: { url: string; body?: string }[] = [];
    const session = {
      id: "created-one",
      title: "Quote review",
      status: "awaiting_consent",
      offer: "paid",
      created_at: "2026-09-11T00:00:00Z",
      pack_id: "general",
    };
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, options: RequestInit) => {
        requests.push({ url, body: options.body as string });
        const data =
          url === "/api/packs"
            ? { packs: [{ id: "general", title: "Workflow review" }] }
            : url === "/api/sessions" && options.method === "POST"
              ? {
                  session,
                  invitations: {
                    client:
                      "http://localhost/review/created-one#invite=client-secret",
                    facilitator:
                      "http://localhost/review/created-one#invite=fac-secret",
                  },
                }
              : url === "/api/sessions/created-one"
                ? { session, snapshot: {}, role: "operator" }
                : { sessions: [] };
        return new Response(JSON.stringify(data));
      }),
    );
    render(<Operator />);
    const user = userEvent.setup();
    await screen.findByRole("option", { name: "Workflow review" });
    await user.type(
      screen.getByLabelText("Which workflow will you review?"),
      "Quote review",
    );
    await user.click(
      screen.getByLabelText("Paid · payment confirmed outside BEEP"),
    );
    expect(
      screen.getByRole("button", { name: "Create session & invitations" }),
    ).toBeDisabled();
    await user.click(screen.getByLabelText(/I have confirmed payment/));
    await user.click(
      screen.getByRole("button", { name: "Create session & invitations" }),
    );
    await screen.findByText("Send the two invitations separately.");
    expect(requests.some((x) => x.url === "/api/sessions/created-one")).toBe(
      true,
    );
    expect(requests.find((x) => x.body)?.body).toBe(
      JSON.stringify({
        title: "Quote review",
        pack_id: "general",
        offer: "paid",
      }),
    );
    expect(
      screen.getByRole("button", { name: "Copy client invite" }),
    ).toBeInTheDocument();
    expect(localStorage.length).toBe(0);
  });
  it("verifies the authenticated role after login and clears the password", async () => {
    const authenticated = vi.fn();
    const fetcher = vi.fn(
      async () => new Response(JSON.stringify({ role: "operator" })),
    );
    vi.stubGlobal("fetch", fetcher);
    const user = userEvent.setup();
    render(<Login onAuthenticated={authenticated} />);
    await user.type(
      screen.getByLabelText("Operator password"),
      "unit-test-password",
    );
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    await waitFor(() =>
      expect(authenticated).toHaveBeenCalledWith({ role: "operator" }),
    );
    expect(screen.getByLabelText("Operator password")).toHaveValue("");
    expect(fetcher).toHaveBeenLastCalledWith(
      "/api/me",
      expect.objectContaining({ method: "GET" }),
    );
  });
});
