import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError } from "./api";
interface Resource<T> {
  path: string | null;
  data?: T;
  error: ApiError | null;
  loading: boolean;
}
/** Serial polling, abort plus generation fencing, and no previous-scope render. */
export function useResource<T>(path: string | null, pollMs = 0) {
  const [state, setState] = useState<Resource<T>>({
    path,
    data: undefined,
    error: null,
    loading: !!path,
  });
  const generation = useRef(0);
  const active = useRef(true);
  const controller = useRef<AbortController | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const cancel = useCallback(() => {
    generation.current += 1;
    controller.current?.abort();
    clearTimeout(timer.current);
  }, []);
  const refresh = useCallback(async () => {
    cancel();
    if (!path || !active.current) return;
    const stamp = generation.current;
    const abort = new AbortController();
    controller.current = abort;
    setState((previous) => ({
      path,
      data: previous.path === path ? previous.data : undefined,
      error: null,
      loading: true,
    }));
    try {
      const data = await api<T>(path, { signal: abort.signal });
      if (
        active.current &&
        stamp === generation.current &&
        !abort.signal.aborted
      ) {
        setState({ path, data, error: null, loading: false });
        return data;
      }
    } catch (error) {
      if (
        active.current &&
        stamp === generation.current &&
        !abort.signal.aborted
      )
        setState({
          path,
          error:
            error instanceof ApiError
              ? error
              : new ApiError(0, "The service could not be reached."),
          loading: false,
        });
    } finally {
      if (active.current && stamp === generation.current && pollMs > 0)
        timer.current = setTimeout(() => {
          void refresh();
        }, pollMs);
    }
  }, [path, pollMs, cancel]);
  useEffect(() => {
    active.current = true;
    void refresh();
    return () => {
      active.current = false;
      cancel();
    };
  }, [refresh, cancel]);
  const scoped =
    state.path === path
      ? state
      : { data: undefined, error: null, loading: !!path };
  return { ...scoped, refresh, cancel };
}
