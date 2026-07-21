/**
 * Runtime configuration injected by the root server layout into the page as
 * window.__DATASPOKE_RUNTIME_CONFIG__. On the client, this object is read
 * before any API call so Kubernetes ConfigMap values are honoured without
 * a rebuild. Falls back to NEXT_PUBLIC_* env vars for local dev (`pnpm dev`).
 */

/**
 * Deployment-local wiring only. The DataHub and Langfuse links are externally
 * wired peripherals and resolve solely from `GET /spoke/common/peripheral-links`
 * (see `lib/api/peripheral-links.ts`), so they have no representation here.
 */
export interface RuntimeConfig {
  apiBaseUrl: string;
  airflowUrl: string;
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
 *      request time from DATASPOKE_API_BASE_URL / DATASPOKE_AIRFLOW_URL
 *   2. NEXT_PUBLIC_API_BASE_URL / NEXT_PUBLIC_AIRFLOW_URL — build-time env
 *      vars, useful in `pnpm dev` via .env.local
 *   3. Empty strings (same-origin API, no Airflow link)
 *
 * SSR-safe: the window branch is guarded by typeof window !== "undefined".
 */
export function getRuntimeConfig(): RuntimeConfig {
  if (typeof window !== "undefined" && window.__DATASPOKE_RUNTIME_CONFIG__) {
    const w = window.__DATASPOKE_RUNTIME_CONFIG__;
    return {
      // `||` (not `??`) so an injected empty string (e.g. the server layout
      // running without DATASPOKE_API_BASE_URL set, as in host `pnpm dev`)
      // falls back to the NEXT_PUBLIC build-time value.
      apiBaseUrl: w.apiBaseUrl || process.env.NEXT_PUBLIC_API_BASE_URL || "",
      airflowUrl: w.airflowUrl || process.env.NEXT_PUBLIC_AIRFLOW_URL || "",
    };
  }
  return {
    apiBaseUrl: process.env.NEXT_PUBLIC_API_BASE_URL ?? "",
    airflowUrl: process.env.NEXT_PUBLIC_AIRFLOW_URL ?? "",
  };
}
