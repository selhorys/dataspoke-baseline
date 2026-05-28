/**
 * Runtime configuration injected by the root server layout into the page as
 * window.__DATASPOKE_RUNTIME_CONFIG__. On the client, this object is read
 * before any API call so Kubernetes ConfigMap values are honoured without
 * a rebuild. Falls back to NEXT_PUBLIC_* env vars for local dev (`pnpm dev`).
 */

export interface RuntimeConfig {
  apiBaseUrl: string;
  datahubUrl: string;
}

declare global {
  interface Window {
    __DATASPOKE_RUNTIME_CONFIG__?: Partial<RuntimeConfig>;
  }
}

/**
 * Returns the effective runtime configuration.
 *
 * Resolution order (highest priority first):
 *   1. window.__DATASPOKE_RUNTIME_CONFIG__ — set by the server layout at
 *      request time from DATASPOKE_API_BASE_URL / DATASPOKE_DATAHUB_URL
 *   2. NEXT_PUBLIC_API_BASE_URL / NEXT_PUBLIC_DATAHUB_URL — build-time env
 *      vars, useful in `pnpm dev` via .env.local
 *   3. Empty strings (same-origin API, no DataHub link)
 *
 * SSR-safe: the window branch is guarded by typeof window !== "undefined".
 */
export function getRuntimeConfig(): RuntimeConfig {
  if (typeof window !== "undefined" && window.__DATASPOKE_RUNTIME_CONFIG__) {
    const w = window.__DATASPOKE_RUNTIME_CONFIG__;
    return {
      apiBaseUrl: w.apiBaseUrl ?? process.env.NEXT_PUBLIC_API_BASE_URL ?? "",
      datahubUrl: w.datahubUrl ?? process.env.NEXT_PUBLIC_DATAHUB_URL ?? "",
    };
  }
  return {
    apiBaseUrl: process.env.NEXT_PUBLIC_API_BASE_URL ?? "",
    datahubUrl: process.env.NEXT_PUBLIC_DATAHUB_URL ?? "",
  };
}
