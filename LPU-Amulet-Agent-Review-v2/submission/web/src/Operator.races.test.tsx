import { describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { Operator } from "./Operator";
import { testView } from "./test-fixtures";

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}
function operatorHarness() {
  const a = testView({ id: "session-A", title: "Workflow A" }).session;
  const b = testView({ id: "session-B", title: "Workflow B" }).session;
  const created = (session: typeof a) => ({
    session,
    invitations: {
      client: `http://localhost/review/${session.id}#invite=synthetic-client`,
      facilitator: `http://localhost/review/${session.id}#invite=synthetic-facilitator`,
    },
  });
  const readbacks: Record<string, ReturnType<typeof deferred<Response>>[]> = {
    [a.id]: [],
    [b.id]: [],
  };
  const posts: ReturnType<typeof deferred<Response>>[] = [];
  const fetcher = vi.fn(async (url: string, options: RequestInit) => {
    if (url === "/api/sessions" && options.method === "POST") {
      const request = deferred<Response>();
      posts.push(request);
      return request.promise;
    }
    const id = url.slice("/api/sessions/".length);
    if (url.startsWith("/api/sessions/") && readbacks[id]) {
      const request = deferred<Response>();
      readbacks[id].push(request);
      // Deliberately ignore abort: stale completions must also be harmless.
      return request.promise;
    }
    return new Response(
      JSON.stringify(
        url === "/api/packs"
          ? { packs: [{ id: "general", title: "General workflow" }] }
          : { sessions: [] },
      ),
    );
  });
  vi.stubGlobal("fetch", fetcher);
  const user = userEvent.setup();
  const ui = render(<Operator />);
  const submit = async (title: string) => {
    await screen.findByRole("option", { name: "General workflow" });
    const input = screen.getByLabelText("Which workflow will you review?");
    await waitFor(() => expect(input).toBeEnabled());
    await user.clear(input);
    await user.type(input, title);
    await user.click(screen.getByLabelText(/Sponsored/));
    await user.click(
      screen.getByRole("button", { name: "Create session & invitations" }),
    );
  };
  const confirm = (session: typeof a) =>
    new Response(JSON.stringify({ session, role: "operator", snapshot: {} }));
  const completePost = async (index: number, session: typeof a) => {
    await act(async () =>
      posts[index].resolve(new Response(JSON.stringify(created(session)))),
    );
    await waitFor(() => expect(readbacks[session.id]).toHaveLength(1));
  };
  return {
    a,
    b,
    readbacks,
    posts,
    fetcher,
    user,
    ui,
    submit,
    confirm,
    created,
    completePost,
  };
}
const copyClient = () =>
  screen.queryByRole("button", { name: "Copy client invite" });

describe("RS-01 exact-session invitation verification", () => {
  it.each([
    ["creation", "success"],
    ["creation", "error"],
    ["automatic", "success"],
    ["automatic", "error"],
    ["manual", "success"],
    ["manual", "error"],
  ] as const)(
    "aborts %s on unmount and ignores its late %s while the next workspace verifies B",
    async (stage, outcome) => {
      const h = operatorHarness();
      await h.submit(h.a.title);
      if (stage !== "creation") await h.completePost(0, h.a);
      if (stage === "manual") {
        await act(async () =>
          h.readbacks[h.a.id][0].resolve(
            new Response(JSON.stringify({ detail: "Retry verification" }), {
              status: 503,
            }),
          ),
        );
        await h.user.click(
          await screen.findByRole("button", { name: "Verify created review" }),
        );
      }
      const pending =
        stage === "creation" ? h.posts[0] : h.readbacks[h.a.id].at(-1)!;
      const options = h.fetcher.mock.calls
        .filter(([url, options]) =>
          stage === "creation"
            ? url === "/api/sessions" && options.method === "POST"
            : url === `/api/sessions/${h.a.id}`,
        )
        .at(-1)![1];
      h.ui.unmount();
      expect(options.signal?.aborted).toBe(true);
      const next = operatorHarness();
      await next.submit(next.b.title);
      await next.completePost(0, next.b);
      const calls = next.fetcher.mock.calls.length;
      await act(async () => {
        if (outcome === "error") pending.reject(new Error("Late A failure"));
        else
          pending.resolve(
            stage === "creation"
              ? new Response(JSON.stringify(h.created(h.a)))
              : h.confirm(h.a),
          );
      });
      expect(next.fetcher).toHaveBeenCalledTimes(calls);
      expect(copyClient()).not.toBeInTheDocument();
      expect(
        screen.getByLabelText("Which workflow will you review?"),
      ).toBeDisabled();
      expect(document.querySelector(".create-panel [role=alert]")).toBeNull();
      await act(async () =>
        next.readbacks[next.b.id][0].resolve(next.confirm(next.b)),
      );
      expect(
        await screen.findByRole("button", { name: "Copy client invite" }),
      ).toBeVisible();
    },
  );
  it("hides verified A as soon as B creation starts and unlocks B only on its valid operator readback", async () => {
    const h = operatorHarness();
    await h.submit(h.a.title);
    await h.completePost(0, h.a);
    await act(async () => h.readbacks[h.a.id][0].resolve(h.confirm(h.a)));
    await screen.findByRole("button", { name: "Copy client invite" });
    await h.submit(h.b.title);
    // The B POST itself is still pending: A must already be invalidated.
    expect(copyClient()).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText("client invitation URL"),
    ).not.toBeInTheDocument();
    await h.completePost(1, h.b);
    const verify = screen.getByRole("button", {
      name: "Verify created review",
    });
    expect(verify).toBeDisabled();
    await act(async () => h.readbacks[h.b.id][0].resolve(h.confirm(h.a)));
    await screen.findByText(/could not be verified in this workspace/);
    expect(copyClient()).not.toBeInTheDocument();
    expect(verify).toBeEnabled();
    await h.user.click(verify);
    expect(verify).toBeDisabled();
    await h.user.click(verify);
    expect(h.readbacks[h.b.id]).toHaveLength(2);
    await act(async () =>
      h.readbacks[h.b.id][1].resolve(
        new Response(
          JSON.stringify({
            session: h.b,
            role: "client",
            snapshot: {},
          }),
        ),
      ),
    );
    await screen.findByText(/could not be verified in this workspace/);
    expect(copyClient()).not.toBeInTheDocument();
    await h.user.click(verify);
    await act(async () => h.readbacks[h.b.id][2].resolve(h.confirm(h.b)));
    expect(
      await screen.findByRole("button", { name: "Copy client invite" }),
    ).toBeVisible();
    expect(screen.getByLabelText("client invitation URL")).toHaveValue(
      `http://localhost/review/${h.b.id}#invite=synthetic-client`,
    );
  });
  it("never exposes B from an old A readback after the A/A/B verification schedule", async () => {
    const h = operatorHarness();
    await h.submit(h.a.title);
    await h.completePost(0, h.a);
    await h.user.click(
      screen.getByRole("button", { name: "Verify created review" }),
    );
    await act(async () => h.readbacks[h.a.id][0].resolve(h.confirm(h.a)));
    await screen.findByRole("button", { name: "Copy client invite" });
    await h.submit(h.b.title);
    await h.completePost(1, h.b);
    expect(copyClient()).not.toBeInTheDocument();
    // Drain any duplicate A request issued by the old UI. The fixed UI
    // serializes it instead; the safety assertion is identical either way.
    await act(async () => {
      for (const stale of h.readbacks[h.a.id].slice(1))
        stale.resolve(h.confirm(h.a));
    });
    expect(copyClient()).not.toBeInTheDocument();
    expect(h.readbacks[h.a.id]).toHaveLength(1);
    await act(async () => h.readbacks[h.b.id][0].resolve(h.confirm(h.b)));
    expect(
      await screen.findByRole("button", { name: "Copy client invite" }),
    ).toBeVisible();
    expect(screen.getByLabelText("client invitation URL")).toHaveValue(
      `http://localhost/review/${h.b.id}#invite=synthetic-client`,
    );
  });
});
