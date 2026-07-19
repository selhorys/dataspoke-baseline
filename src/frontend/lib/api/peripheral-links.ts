"use client";

/**
 * Peripheral display links — `GET /spoke/common/peripheral-links`.
 *
 * The app shell's infra icons and the DataHub / Langfuse deep-links resolve
 * their base URLs from two planes: the chart-injected runtime config (env) and
 * the `peripheral_config` DB plane served by this endpoint. Wiring a peripheral
 * purely through `PATCH /admin/peripherals/{datahub,langfuse}` therefore reaches
 * the UI with no chart operation and no pod restart.
 *
 * Airflow and ReDoc are deliberately absent: they are deployment-local (Airflow
 * ships in the umbrella chart, ReDoc is the API itself), not externally-wired
 * peripherals, so they stay on `getRuntimeConfig()` alone.
 *
 * Spec: spec/feature/FRONTEND_BASIC.md §Shell; spec/API.md §Data Resource.
 */

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api/client";
import { getRuntimeConfig } from "@/lib/runtime-config";
import { sanitizeDisplayUrl, sanitizeProjectId } from "@/lib/safe-url";
import type { PeripheralLinks } from "@/lib/api/types";

/**
 * One module-level constant, shared by every caller.
 *
 * `DatahubDatasetLink` and `EvidenceLink` render once per table row, so a key
 * derived per-instance would defeat TanStack Query's request deduplication and
 * fan out one request per row. This key is stable and argument-free, so all
 * instances observe a single cache entry and a single in-flight fetch.
 */
export const PERIPHERAL_LINKS_QUERY_KEY = ["peripheral-links"] as const;

/**
 * Peripheral wiring changes at operator cadence, not request cadence, so the
 * result stays fresh for the lifetime of a typical session. Combined with the
 * shared key this means one request per page load at most, regardless of how
 * many rows mount the hook.
 */
const STALE_TIME_MS = 30 * 60 * 1000;

/** Raw query over the peripheral-links endpoint. */
export function usePeripheralLinks() {
  return useQuery<PeripheralLinks>({
    queryKey: PERIPHERAL_LINKS_QUERY_KEY,
    queryFn: () => apiFetch<PeripheralLinks>("/spoke/common/peripheral-links"),
    staleTime: STALE_TIME_MS,
    refetchOnWindowFocus: false,
    // This is a best-effort read that degrades to "render no link", so a
    // failure must not cost four requests with backoff on every page load.
    retry: false,
    // For the same reason it is not worth a global error toast on every page.
    meta: { handledInline: true },
  });
}

export interface DisplayLinks {
  datahubUrl: string;
  langfuseUrl: string;
  langfuseProjectId: string;
}

/**
 * Resolves the peripheral-sourced display links.
 *
 * Precedence — **env wins**: the runtime-config value is used whenever it is
 * non-empty, and the API value fills in only when it is unset. This keeps every
 * existing dev/prod chart install behaviourally unchanged while letting a
 * DB-plane-only deployment resolve its links from the API.
 *
 * While the query is in flight the env value is returned rather than an empty
 * string, so an already-known link never flashes away and back.
 *
 * Every resolved value is re-checked with the shared safe-URL guard before it
 * reaches a caller that will make it an `href`; an unsafe value degrades to
 * `""`, the same state as "unconfigured".
 */
export function useDisplayLinks(): DisplayLinks {
  const env = getRuntimeConfig();
  const { data } = usePeripheralLinks();

  const envDatahub = env.datahubUrl;
  const envLangfuse = env.langfuseUrl;
  const envProjectId = env.langfuseProjectId;
  const apiDatahub = data?.datahub_url ?? "";
  const apiLangfuse = data?.langfuse_url ?? "";
  const apiProjectId = data?.langfuse_project_id ?? "";

  return useMemo(
    () => ({
      datahubUrl: sanitizeDisplayUrl(envDatahub || apiDatahub),
      langfuseUrl: sanitizeDisplayUrl(envLangfuse || apiLangfuse),
      langfuseProjectId: sanitizeProjectId(envProjectId || apiProjectId),
    }),
    [envDatahub, envLangfuse, envProjectId, apiDatahub, apiLangfuse, apiProjectId],
  );
}
