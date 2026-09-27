import type { Actor } from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}
/** Cookie-only auth. No automatic mutation retries or persisted credentials. */
export async function api<T>(
  path: string,
  options: { body?: unknown; signal?: AbortSignal; method?: string } = {},
): Promise<T> {
  const timeout = AbortSignal.timeout(20_000);
  const signal = options.signal
    ? AbortSignal.any([options.signal, timeout])
    : timeout;
  let response: Response;
  try {
    response = await fetch(`/api${path}`, {
      method: options.method ?? (options.body === undefined ? "GET" : "POST"),
      credentials: "same-origin",
      cache: "no-store",
      redirect: "error",
      signal,
      headers: {
        Accept: "application/json",
        ...(options.body !== undefined
          ? { "Content-Type": "application/json" }
          : {}),
      },
      ...(options.body !== undefined
        ? { body: JSON.stringify(options.body) }
        : {}),
    });
  } catch (error) {
    if (options.signal?.aborted) throw error;
    throw new ApiError(
      0,
      timeout.aborted
        ? "The request timed out. Its outcome may be unknown; refresh before trying again."
        : "Cannot reach the service. Check your connection and try again.",
    );
  }
  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail =
      payload && typeof payload === "object" && "detail" in payload
        ? payload.detail
        : null;
    throw new ApiError(
      response.status,
      typeof detail === "string"
        ? detail.slice(0, 800)
        : `The service could not complete this request (${response.status}).`,
    );
  }
  if (!payload || typeof payload !== "object")
    throw new ApiError(
      502,
      "The service returned an unreadable response. Please refresh.",
    );
  return payload as T;
}

export function consumeInvitation(): string | null {
  const fragment = window.location.hash;
  // Erase synchronously, including malformed fragments; never put it in logs or storage.
  if (fragment)
    history.replaceState(
      history.state,
      "",
      location.pathname + location.search,
    );
  return new URLSearchParams(fragment.slice(1)).get("invite") || null;
}
export async function bootstrapSession(): Promise<Actor | null> {
  const token = consumeInvitation();
  try {
    const actor = await api<Actor>(
      token ? "/invitations/exchange" : "/me",
      token ? { body: { token } } : {},
    );
    if (!["operator", "client", "facilitator"].includes(actor.role))
      throw new ApiError(502, "The service returned an invalid role.");
    if (actor.role !== "operator" && !actor.session_id)
      throw new ApiError(502, "The invitation is not bound to a review.");
    return actor;
  } catch (error) {
    if (!token && error instanceof ApiError && error.status === 401)
      return null;
    throw error;
  }
}
export const sessionPath = (id: string) =>
  `/sessions/${encodeURIComponent(id)}`;
export const errorMessage = (error: unknown) =>
  error instanceof ApiError
    ? error.message
    : "Something interrupted this action. Please try again.";
