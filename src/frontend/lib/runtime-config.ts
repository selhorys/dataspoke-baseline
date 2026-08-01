/**
 * Runtime configuration injected by the root server layout into the page as
 * window.__DATASPOKE_RUNTIME_CONFIG__. On the client, this object is read
 * before any API call so Kubernetes ConfigMap values are honoured without
 * a rebuild. On the server the same values are read from the DATASPOKE_* process
 * environment, so server-rendered markup carries the deployed URLs. Both sides
 * fall back to NEXT_PUBLIC_* env vars for local dev (`pnpm dev`).
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
 * Resolution order (highest priority first), per field:
 *   1. On the client: window.__DATASPOKE_RUNTIME_CONFIG__ — set by the server
 *      layout at request time
 *      On the server (SSR, Server Components): DATASPOKE_API_BASE_URL /
 *      DATASPOKE_AIRFLOW_URL read directly from the process environment
 *   2. NEXT_PUBLIC_API_BASE_URL / NEXT_PUBLIC_AIRFLOW_URL — build-time env
 *      vars, useful in `pnpm dev` via .env.local
 *   3. Empty strings (same-origin API, no Airflow link)
 *
 * SSR-safe: the window branch is guarded by typeof window !== "undefined", and
 * the server branch resolves the same values so markup rendered during SSR
 * (e.g. absolute hrefs) matches what the client resolves after hydration.
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
  // Server side (SSR / Server Components): the window global does not exist,
  // so read the non-public runtime vars straight from the process environment.
  // `||` (not `??`) so a set-but-empty DATASPOKE_* value falls through to the
  // NEXT_PUBLIC build-time value, matching the window branch's convention that
  // an empty string means unset.
  return {
    apiBaseUrl:
      process.env.DATASPOKE_API_BASE_URL || process.env.NEXT_PUBLIC_API_BASE_URL || "",
    airflowUrl:
      process.env.DATASPOKE_AIRFLOW_URL || process.env.NEXT_PUBLIC_AIRFLOW_URL || "",
  };
}
