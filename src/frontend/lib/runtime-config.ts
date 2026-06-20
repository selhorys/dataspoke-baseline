/**
 * Runtime configuration injected by the root server layout into the page as
 * window.__DATASPOKE_RUNTIME_CONFIG__. On the client, this object is read
 * before any API call so Kubernetes ConfigMap values are honoured without
 * a rebuild. Falls back to NEXT_PUBLIC_* env vars for local dev (`pnpm dev`).
 */

export interface RuntimeConfig {
  apiBaseUrl: string;
  datahubUrl: string;
  langfuseUrl: string;
  langfuseProjectId: string;
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
 *      request time from DATASPOKE_API_BASE_URL / DATASPOKE_DATAHUB_URL /
 *      DATASPOKE_LANGFUSE_URL / DATASPOKE_LANGFUSE_PROJECT_ID / DATASPOKE_AIRFLOW_URL
 *   2. NEXT_PUBLIC_API_BASE_URL / NEXT_PUBLIC_DATAHUB_URL /
 *      NEXT_PUBLIC_LANGFUSE_URL / NEXT_PUBLIC_LANGFUSE_PROJECT_ID /
 *      NEXT_PUBLIC_AIRFLOW_URL — build-time env vars, useful in `pnpm dev` via .env.local
 *   3. Empty strings (same-origin API, no DataHub link)
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
      datahubUrl: w.datahubUrl || process.env.NEXT_PUBLIC_DATAHUB_URL || "",
      langfuseUrl: w.langfuseUrl || process.env.NEXT_PUBLIC_LANGFUSE_URL || "",
      langfuseProjectId:
        w.langfuseProjectId || process.env.NEXT_PUBLIC_LANGFUSE_PROJECT_ID || "",
      airflowUrl: w.airflowUrl || process.env.NEXT_PUBLIC_AIRFLOW_URL || "",
    };
  }
  return {
    apiBaseUrl: process.env.NEXT_PUBLIC_API_BASE_URL ?? "",
    datahubUrl: process.env.NEXT_PUBLIC_DATAHUB_URL ?? "",
    langfuseUrl: process.env.NEXT_PUBLIC_LANGFUSE_URL ?? "",
    langfuseProjectId: process.env.NEXT_PUBLIC_LANGFUSE_PROJECT_ID ?? "",
    airflowUrl: process.env.NEXT_PUBLIC_AIRFLOW_URL ?? "",
  };
}
