import type { ApiErrorPayload } from "./types";
import { getRuntimeConfig } from "@/lib/runtime-config";

/** Resolved at call time so runtime config (set before first call) is honoured. */
function apiBase(): string {
  return getRuntimeConfig().apiBaseUrl + "/api/v1";
}

export class ApiError extends Error {
  readonly error_code: string;
  readonly trace_id: string;
  readonly status: number;

  constructor(payload: ApiErrorPayload, status: number) {
    super(payload.message);
    this.name = "ApiError";
    this.error_code = payload.error_code;
    this.trace_id = payload.trace_id;
    this.status = status;
  }
}

/**
 * Lazily reads from the Zustand auth store without a top-level import so that
 * this module can be used before the store is mounted (e.g. during SSR where
 * `window` is absent). The dynamic import is kept inside a function to avoid
 * circular module resolution at bundle time.
 */
async function getAuthStoreState() {
  if (typeof window === "undefined") return null;
  const { useAuthStore } = await import("@/lib/auth/store");
  return useAuthStore.getState();
}

async function getAccessToken(): Promise<string | null> {
  const state = await getAuthStoreState();
  return state?.accessToken ?? null;
}

async function clearAuthStore(): Promise<void> {
  const state = await getAuthStoreState();
  state?.clear();
}

async function setAccessToken(token: string): Promise<void> {
  const state = await getAuthStoreState();
  state?.setToken(token);
}

async function parseError(response: Response): Promise<ApiError> {
  let payload: ApiErrorPayload;
  try {
    payload = (await response.json()) as ApiErrorPayload;
  } catch {
    payload = {
      error_code: "UNKNOWN_ERROR",
      message: response.statusText || "An unexpected error occurred",
      trace_id: "00000000-0000-0000-0000-000000000000",
      resp_time: new Date().toISOString(),
    };
  }
  return new ApiError(payload, response.status);
}

// Deduplicates concurrent refresh calls: all callers await the same promise.
let refreshInFlight: Promise<boolean> | null = null;

/**
 * Attempts a token refresh via the httpOnly cookie. Deduplicates concurrent
 * calls so exactly one POST /auth/token/refresh is in flight at a time.
 * Exported for use by the silent-refresh probe in providers.tsx.
 */
export async function ensureFreshToken(): Promise<boolean> {
  if (refreshInFlight) return refreshInFlight;

  refreshInFlight = (async () => {
    try {
      const response = await fetch(`${apiBase()}/auth/token/refresh`, {
        method: "POST",
        credentials: "include",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
        },
      });
      if (!response.ok) return false;
      const data: unknown = await response.json();
      if (typeof (data as Record<string, unknown>)?.access_token !== "string") return false;
      await setAccessToken((data as { access_token: string }).access_token);
      return true;
    } catch {
      return false;
    }
  })();

  try {
    return await refreshInFlight;
  } finally {
    refreshInFlight = null;
  }
}

// Internal alias used by the 401 retry path below.
const attemptRefresh = ensureFreshToken;

function normalizeHeaders(source: RequestInit["headers"]): Record<string, string> {
  const result: Record<string, string> = {};
  if (!source) return result;

  const normalized = new Headers(source);
  normalized.forEach((value, key) => {
    result[key] = value;
  });
  return result;
}

export interface ApiFetchInit extends RequestInit {
  /** Controls how the response body is parsed on success. Default: "json". */
  responseType?: "json" | "text";
}

export async function apiFetch<T>(path: string, init: ApiFetchInit = {}): Promise<T> {
  const { responseType = "json", ...fetchInit } = init;

  const traceId =
    typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : undefined;

  const token = await getAccessToken();

  const headers: Record<string, string> = {
    ...(responseType === "json" ? { "content-type": "application/json", accept: "application/json" } : {}),
    ...(traceId ? { "x-trace-id": traceId } : {}),
    ...normalizeHeaders(fetchInit.headers),
    ...(token ? { authorization: `Bearer ${token}` } : {}),
  };

  const url = `${apiBase()}${path}`;

  const response = await fetch(url, {
    ...fetchInit,
    headers,
    credentials: "include",
  });

  if (response.status === 401 && token) {
    const refreshed = await attemptRefresh();
    if (refreshed) {
      const newToken = await getAccessToken();
      const retryHeaders: Record<string, string> = {
        ...headers,
        ...(newToken ? { authorization: `Bearer ${newToken}` } : {}),
      };
      const retryResponse = await fetch(url, {
        ...fetchInit,
        headers: retryHeaders,
        credentials: "include",
      });
      if (!retryResponse.ok) {
        throw await parseError(retryResponse);
      }
      if (retryResponse.status === 204) return undefined as T;
      if (responseType === "text") return retryResponse.text() as Promise<T>;
      return retryResponse.json() as Promise<T>;
    } else {
      await clearAuthStore();
      throw await parseError(response);
    }
  }

  if (!response.ok) {
    throw await parseError(response);
  }

  if (response.status === 204) return undefined as T;
  if (responseType === "text") return response.text() as Promise<T>;
  return response.json() as Promise<T>;
}
