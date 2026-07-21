"use client";

/**
 * Peripheral display links — `GET /spoke/common/peripheral-links`.
 *
 * This endpoint serves the `peripheral_config` DB plane, the sole source of the
 * DataHub and Langfuse display links behind the app shell's infra icons and the
 * DataHub / Langfuse deep-links. The client carries no alternative for these
 * values, so wiring a peripheral through `PATCH /admin/peripherals/{datahub,langfuse}`
 * reaches the UI with no chart operation and no pod restart, and nothing can mask
 * what the DB holds.
 *
 * Airflow and ReDoc are deliberately absent: they are deployment-local (Airflow
 * ships in the umbrella chart, ReDoc is the API itself), not externally-wired
 * peripherals, so they resolve from `getRuntimeConfig()` instead.
 *
 * Spec: spec/feature/FRONTEND_BASIC.md §Shell; spec/API.md §Data Resource.
 */

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api/client";
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
    // This read gates every DataHub and Langfuse affordance in the app, so a
    // transient failure is worth one retry — but not the four-with-backoff
    // default, which would cost every page load a request storm.
    retry: 1,
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
 * Resolves the peripheral-sourced display links from the API response alone.
 *
 * Before the first successful response — and if that first read fails — every
 * value is `""`, which callers render as "no link" rather than a broken one.
 * Once a response has landed, TanStack Query holds `data` across refetches, so a
 * known link never flashes away and back, and a later failing refetch keeps
 * serving the last-known value.
 *
 * Every resolved value is re-checked with the shared safe-URL guard before it
 * reaches a caller that will make it an `href`; an unsafe value degrades to
 * `""`, the same state as "unconfigured".
 */
export function useDisplayLinks(): DisplayLinks {
  const { data } = usePeripheralLinks();

  const datahubUrl = data?.datahub_url ?? "";
  const langfuseUrl = data?.langfuse_url ?? "";
  const langfuseProjectId = data?.langfuse_project_id ?? "";

  return useMemo(
    () => ({
      datahubUrl: sanitizeDisplayUrl(datahubUrl),
      langfuseUrl: sanitizeDisplayUrl(langfuseUrl),
      langfuseProjectId: sanitizeProjectId(langfuseProjectId),
    }),
    [datahubUrl, langfuseUrl, langfuseProjectId],
  );
}
