import { describe, expect, it, vi } from "vitest";
import { bootstrapEntry } from "./entry";

describe("isolated journey entry", () => {
  it.each(["/preview", "/preview/"])(
    "never starts authentication on %s",
    (path) => {
      const bootstrap = vi.fn(() => Promise.resolve(null));
      expect(bootstrapEntry(path, bootstrap)).toEqual({ kind: "preview" });
      expect(bootstrap).not.toHaveBeenCalled();
    },
  );
  it.each(["/", "/review/real-session", "/preview-other", "/preview/nested"])(
    "preserves real authentication on %s",
    (path) => {
      const promise = Promise.resolve(null);
      const bootstrap = vi.fn(() => promise);
      expect(bootstrapEntry(path, bootstrap)).toEqual({
        kind: "live",
        bootstrap: promise,
      });
      expect(bootstrap).toHaveBeenCalledTimes(1);
    },
  );
});
