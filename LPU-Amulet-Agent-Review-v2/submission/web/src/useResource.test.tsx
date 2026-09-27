import { describe, it, expect, vi } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";
import { useResource } from "./useResource";
describe("session-scoped reads", () => {
  it("returns the verified readback to mutation callers", async () => {
    let revision = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ revision: ++revision }))),
    );
    const { result } = renderHook(() =>
      useResource<{ revision: number }>("/sessions/a", 0),
    );
    await waitFor(() => expect(result.current.data?.revision).toBe(1));
    let verified: unknown;
    await act(async () => {
      verified = await result.current.refresh();
    });
    expect(verified).toEqual({ revision: 2 });
  });
  it("clears the previous scope immediately and ignores late requests after navigation", async () => {
    const pending = new Map<string, (response: Response) => void>();
    vi.stubGlobal(
      "fetch",
      vi.fn(
        (url: string) =>
          new Promise<Response>((resolve) => {
            pending.set(url, resolve);
          }),
      ),
    );
    const { result, rerender } = renderHook(
      ({ path }) => useResource<{ title: string }>(path, 0),
      { initialProps: { path: "/sessions/a" } },
    );
    rerender({ path: "/sessions/b" });
    expect(result.current.data).toBeUndefined();
    expect(pending.has("/api/sessions/b")).toBe(true);
    await act(async () => {
      pending.get("/api/sessions/b")!(
        new Response(JSON.stringify({ title: "B" })),
      );
    });
    await waitFor(() => expect(result.current.data?.title).toBe("B"));
    await act(async () => {
      pending.get("/api/sessions/a")!(
        new Response(JSON.stringify({ title: "Private A" })),
      );
    });
    expect(result.current.data?.title).toBe("B");
  });
});
