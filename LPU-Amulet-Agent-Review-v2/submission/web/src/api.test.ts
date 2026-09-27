import { describe, it, expect, vi } from "vitest";
import { api, ApiError, consumeInvitation, bootstrapSession } from "./api";

describe("same-origin cookie authentication", () => {
  it("erases the invitation fragment before exchange sends the secret", async () => {
    history.replaceState(
      null,
      "",
      "/review/session-one?welcome=1#invite=secret%2Btoken",
    );
    const fetcher = vi.fn(() => {
      expect(location.hash).toBe("");
      expect(location.search).toBe("?welcome=1");
      return Promise.resolve(
        new Response(
          JSON.stringify({ role: "client", session_id: "session-one" }),
        ),
      );
    });
    vi.stubGlobal("fetch", fetcher);
    expect(await bootstrapSession()).toEqual({
      role: "client",
      session_id: "session-one",
    });
    expect(fetcher).toHaveBeenCalledWith(
      "/api/invitations/exchange",
      expect.objectContaining({
        credentials: "same-origin",
        cache: "no-store",
        body: JSON.stringify({ token: "secret+token" }),
        method: "POST",
      }),
    );
    expect(localStorage.length).toBe(0);
  });
  it("clears all fragments even when the invitation is malformed", () => {
    history.replaceState(null, "", "/review/session-one#unexpected=secret");
    expect(consumeInvitation()).toBeNull();
    expect(location.hash).toBe("");
  });
  it("returns an explicit unavailable error rather than a fabricated response", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response(
            JSON.stringify({ detail: "Recording service unavailable" }),
            { status: 503 },
          ),
        ),
    );
    await expect(api("/health")).rejects.toMatchObject({
      status: 503,
      message: "Recording service unavailable",
    } satisfies Partial<ApiError>);
  });
  it("does not expose non-JSON proxy error pages", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValue(
          new Response("<html>internal server dump</html>", { status: 502 }),
        ),
    );
    await expect(api("/health")).rejects.toThrow(
      "The service could not complete this request (502).",
    );
  });
});
