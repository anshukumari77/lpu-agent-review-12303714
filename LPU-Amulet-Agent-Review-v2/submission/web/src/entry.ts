import type { Actor } from "./types";

type Entry =
  { kind: "preview" } | { kind: "live"; bootstrap: Promise<Actor | null> };

/** An explicit design route, never a fallback for a failed live service. */
export function bootstrapEntry(
  pathname: string,
  bootstrap: () => Promise<Actor | null>,
): Entry {
  if (pathname === "/preview" || pathname === "/preview/")
    return { kind: "preview" };
  return { kind: "live", bootstrap: bootstrap() };
}
