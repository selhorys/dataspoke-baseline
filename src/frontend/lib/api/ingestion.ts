"use client";

import {
  useMutation,
  useQueries,
  useQueryClient,
} from "@tanstack/react-query";
import { apiFetch, ApiError } from "@/lib/api/client";
import { defaultQueryRetry } from "@/lib/api/error-policy";
import { usePoll } from "@/lib/hooks/use-poll";
import type {
  IngestionSource,
  IngestionSourceBody,
  IngestionSourceListResponse,
  IngestionSourcePatchBody,
  IngestionRunResponse,
  IngestionSourceDatasetsResponse,
  IngestionEvent,
  IngestionEventListResponse,
  IngestionUnmanagedResponse,
  SecretRefListResponse,
  IngestionReverseLookupResponse,
  IngestionMode,
} from "@/types/ingestion";

// ── Source list ──────────────────────────────────────────────────────────────────

interface SourceListParams {
  offset?: number;
  limit?: number;
  mode?: IngestionMode;
}

function buildSourceListUrl(params: SourceListParams): string {
  const sp = new URLSearchParams();
  if (params.offset !== undefined) sp.set("offset", String(params.offset));
  if (params.limit !== undefined) sp.set("limit", String(params.limit));
  if (params.mode) sp.set("mode", params.mode);
  const qs = sp.toString();
  return `/spoke/ingestion/sources${qs ? `?${qs}` : ""}`;
}

/** GET /spoke/ingestion/sources — polled. */
export function useIngestionSources(params: SourceListParams = {}) {
  return usePoll<IngestionSourceListResponse>({
    queryKey: ["ingestion", "sources", params],
    queryFn: () =>
      apiFetch<IngestionSourceListResponse>(buildSourceListUrl(params)),
    meta: { handledInline: true },
  });
}

/**
 * Per-row covered-dataset count: fires `datasets?limit=1` per source id and
 * exposes each row's total_count. Used by the list view's coverage column.
 */
export function useIngestionSourceDatasetCounts(ids: string[]) {
  return useQueries({
    queries: ids.map((id) => ({
      queryKey: ["ingestion", "datasets", id, { offset: 0, limit: 1 }],
      queryFn: () =>
        apiFetch<IngestionSourceDatasetsResponse>(
          `/spoke/ingestion/sources/${encodeURIComponent(id)}/datasets?offset=0&limit=1`,
        ),
      meta: { handledInline: true },
    })),
  });
}

/**
 * Maximum page size accepted by `GET /spoke/ingestion/sources/{id}/event`
 * (`limit` is `le=1000`). The latest-run probe requests the whole page rather
 * than one event, because the feed also carries per-dataset observations and
 * source-lifecycle events — either can sit above the run outcome the status
 * column reports.
 */
const EVENT_PAGE_MAX = 1000;

/**
 * Event types that record a run outcome. Lifecycle events
 * (`INGESTION.SOURCE_CREATE` / `SOURCE_UPDATE` / `SOURCE_DELETE`) and any
 * future non-run `INGESTION.*` are excluded.
 */
const RUN_OUTCOME_EVENT_TYPES: ReadonlySet<string> = new Set([
  "INGESTION.COMPLETE",
  "INGESTION.FAIL",
]);

/**
 * `detail.source` wire values of the per-dataset observation producers
 * (spec/feature/BACKEND.md §Event Catalogue). They book `INGESTION.COMPLETE`
 * per mapped dataset with `status="success"`, so they are not run outcomes.
 */
const OBSERVATION_PRODUCERS: ReadonlySet<string> = new Set([
  "passive_observation",
  "last_ingested_observation",
]);

/**
 * Mirror of the server-side `latest_run` predicate, applied in the backend's
 * order. Both terms are required and neither is sufficient alone: the
 * whitelist alone lets a per-dataset observation outrank an older failure, the
 * blacklist alone lets a newer `SOURCE_UPDATE` (`status="success"`) do the
 * same.
 */
function isRunOutcomeEvent(event: IngestionEvent): boolean {
  // 1. Event-type whitelist.
  if (!RUN_OUTCOME_EVENT_TYPES.has(event.event_type)) return false;
  // 2. Producer blacklist, null-safe in the same direction as the backend's
  //    `detail->>'source' IS NULL OR ... NOT IN (...)`: an event carrying no
  //    `detail.source` is the inline ACM run record and must be kept.
  const producer = event.detail?.source;
  if (typeof producer !== "string") return true;
  return !OBSERVATION_PRODUCERS.has(producer);
}

/**
 * Newest run outcome on a page of source events, or `undefined` when the page
 * holds none. The page is requested newest-first, so the first surviving row
 * wins. Page-bounded by construction: unlike the unbounded server-side
 * `latest_run`, a run outcome pushed off the newest page reads as "no status".
 */
export function selectLatestRunEvent(
  page: IngestionEventListResponse,
): IngestionEvent | undefined {
  return page.events.find(isRunOutcomeEvent);
}

/**
 * Per-row latest-run status: fires `event?limit=1000&sort=occurred_at_desc`
 * per source id and derives the newest **run outcome** from that page. Each
 * result's `data` is the winning event (or `undefined`). Used by the list
 * view's status column.
 */
export function useIngestionSourceLatestRuns(ids: string[]) {
  return useQueries({
    queries: ids.map((id) => ({
      queryKey: ["ingestion", "events", id, { offset: 0, limit: EVENT_PAGE_MAX }],
      queryFn: () =>
        apiFetch<IngestionEventListResponse>(
          `/spoke/ingestion/sources/${encodeURIComponent(id)}/event?offset=0&limit=${EVENT_PAGE_MAX}&sort=occurred_at_desc`,
        ),
      select: selectLatestRunEvent,
      meta: { handledInline: true },
    })),
  });
}

// ── Single source ────────────────────────────────────────────────────────────────

/** GET /spoke/ingestion/sources/{id}. */
export function useIngestionSource(id: string) {
  return usePoll<IngestionSource>({
    queryKey: ["ingestion", "source", id],
    queryFn: () =>
      apiFetch<IngestionSource>(
        `/spoke/ingestion/sources/${encodeURIComponent(id)}`,
      ),
    enabled: !!id,
    meta: { handledInline: true },
  });
}

/** POST /spoke/ingestion/sources — create. */
export function useCreateIngestionSource() {
  const qc = useQueryClient();
  return useMutation<IngestionSource, Error, IngestionSourceBody>({
    mutationFn: (body) =>
      apiFetch<IngestionSource>("/spoke/ingestion/sources", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["ingestion", "sources"] });
      void qc.invalidateQueries({ queryKey: ["ingestion", "unmanaged"] });
    },
  });
}

/** PUT /spoke/ingestion/sources/{id} — full replacement. */
export function useReplaceIngestionSource(id: string) {
  const qc = useQueryClient();
  return useMutation<IngestionSource, Error, IngestionSourceBody>({
    mutationFn: (body) =>
      apiFetch<IngestionSource>(
        `/spoke/ingestion/sources/${encodeURIComponent(id)}`,
        { method: "PUT", body: JSON.stringify(body) },
      ),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["ingestion", "source", id] });
      void qc.invalidateQueries({ queryKey: ["ingestion", "sources"] });
    },
  });
}

/** PATCH /spoke/ingestion/sources/{id} — partial update. */
export function usePatchIngestionSource(id: string) {
  const qc = useQueryClient();
  return useMutation<IngestionSource, Error, IngestionSourcePatchBody>({
    mutationFn: (body) =>
      apiFetch<IngestionSource>(
        `/spoke/ingestion/sources/${encodeURIComponent(id)}`,
        { method: "PATCH", body: JSON.stringify(body) },
      ),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["ingestion", "source", id] });
      void qc.invalidateQueries({ queryKey: ["ingestion", "sources"] });
    },
  });
}

/** DELETE /spoke/ingestion/sources/{id}. */
export function useDeleteIngestionSource(id: string) {
  const qc = useQueryClient();
  return useMutation<void, Error, void>({
    mutationFn: () =>
      apiFetch<void>(`/spoke/ingestion/sources/${encodeURIComponent(id)}`, {
        method: "DELETE",
      }),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["ingestion", "sources"] });
      void qc.invalidateQueries({ queryKey: ["ingestion", "unmanaged"] });
    },
  });
}

// ── Run ──────────────────────────────────────────────────────────────────────────

interface RunVars {
  dry_run?: boolean;
}

/**
 * POST /spoke/ingestion/sources/{id}/method/run — execute the extractor.
 * `dry_run` is a query param (no body), mirroring the metagen run hook.
 */
export function useRunIngestionSource(id: string) {
  const qc = useQueryClient();
  return useMutation<IngestionRunResponse, Error, RunVars>({
    mutationFn: ({ dry_run = false } = {}) => {
      const url = `/spoke/ingestion/sources/${encodeURIComponent(id)}/method/run${
        dry_run ? "?dry_run=true" : ""
      }`;
      return apiFetch<IngestionRunResponse>(url, { method: "POST" });
    },
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["ingestion", "source", id] });
      void qc.invalidateQueries({ queryKey: ["ingestion", "events", id] });
      void qc.invalidateQueries({ queryKey: ["ingestion", "datasets", id] });
    },
  });
}

// ── Source datasets ──────────────────────────────────────────────────────────────

interface PageParams {
  offset?: number;
  limit?: number;
}

function buildPageQuery(params: PageParams): string {
  const sp = new URLSearchParams();
  if (params.offset !== undefined) sp.set("offset", String(params.offset));
  if (params.limit !== undefined) sp.set("limit", String(params.limit));
  const qs = sp.toString();
  return qs ? `?${qs}` : "";
}

/** GET /spoke/ingestion/sources/{id}/datasets — polled. */
export function useIngestionSourceDatasets(id: string, params: PageParams = {}) {
  return usePoll<IngestionSourceDatasetsResponse>({
    queryKey: ["ingestion", "datasets", id, params],
    queryFn: () =>
      apiFetch<IngestionSourceDatasetsResponse>(
        `/spoke/ingestion/sources/${encodeURIComponent(id)}/datasets${buildPageQuery(params)}`,
      ),
    enabled: !!id,
    meta: { handledInline: true },
  });
}

// ── Source events ────────────────────────────────────────────────────────────────

interface EventParams extends PageParams {
  from?: string;
  to?: string;
}

function buildEventQuery(params: EventParams): string {
  const sp = new URLSearchParams();
  if (params.offset !== undefined) sp.set("offset", String(params.offset));
  if (params.limit !== undefined) sp.set("limit", String(params.limit));
  if (params.from) sp.set("from", params.from);
  if (params.to) sp.set("to", params.to);
  sp.set("sort", "occurred_at_desc");
  return `?${sp.toString()}`;
}

/** GET /spoke/ingestion/sources/{id}/event — polled, newest first. */
export function useIngestionSourceEvents(id: string, params: EventParams = {}) {
  return usePoll<IngestionEventListResponse>({
    queryKey: ["ingestion", "events", id, params],
    queryFn: () =>
      apiFetch<IngestionEventListResponse>(
        `/spoke/ingestion/sources/${encodeURIComponent(id)}/event${buildEventQuery(params)}`,
      ),
    enabled: !!id,
    meta: { handledInline: true },
  });
}

// ── Unmanaged bucket ─────────────────────────────────────────────────────────────

/** GET /spoke/ingestion/unmanaged — polled. */
export function useIngestionUnmanaged(params: PageParams = {}) {
  return usePoll<IngestionUnmanagedResponse>({
    queryKey: ["ingestion", "unmanaged", params],
    queryFn: () =>
      apiFetch<IngestionUnmanagedResponse>(
        `/spoke/ingestion/unmanaged${buildPageQuery(params)}`,
      ),
    meta: { handledInline: true },
  });
}

// ── Secret references ────────────────────────────────────────────────────────────

/**
 * GET /spoke/ingestion/secrets — Editor/Admin only.
 * `enabled` should be the caller's canWrite so Readers never fire it (the route
 * is 403 READ_ONLY_ROLE for Readers).
 */
export function useIngestionSecrets(enabled: boolean) {
  return usePoll<SecretRefListResponse>({
    queryKey: ["ingestion", "secrets"],
    queryFn: () =>
      apiFetch<SecretRefListResponse>("/spoke/ingestion/secrets"),
    enabled,
    meta: { handledInline: true },
    // This read reports whether the secret resolver is reachable at all, so an
    // unavailable resolver is the answer rather than an obstacle to it. Every
    // other class defers to the global policy.
    retry: (failureCount, error) => {
      if (error instanceof ApiError && error.status === 503) return false;
      return defaultQueryRetry(failureCount, error);
    },
  });
}

// ── Per-dataset reverse-lookup ───────────────────────────────────────────────────

/** GET /spoke/common/data/{urn}/attr/ingestion — owning source for a dataset. */
export function useIngestionReverseLookup(datasetUrn: string) {
  return usePoll<IngestionReverseLookupResponse>({
    queryKey: ["ingestion", "reverse", datasetUrn],
    queryFn: () =>
      apiFetch<IngestionReverseLookupResponse>(
        `/spoke/common/data/${encodeURIComponent(datasetUrn)}/attr/ingestion`,
      ),
    enabled: !!datasetUrn,
    meta: { handledInline: true },
  });
}

interface DatasetEventParams {
  offset?: number;
  limit?: number;
  from?: string;
  to?: string;
}

function buildDatasetEventUrl(datasetUrn: string, params: DatasetEventParams): string {
  const sp = new URLSearchParams();
  if (params.offset !== undefined) sp.set("offset", String(params.offset));
  sp.set("limit", String(params.limit ?? 20));
  if (params.from) sp.set("from", params.from);
  if (params.to) sp.set("to", params.to);
  sp.set("sort", "occurred_at_desc");
  return `/spoke/common/data/${encodeURIComponent(datasetUrn)}/event/ingestion?${sp.toString()}`;
}

/** GET /spoke/common/data/{urn}/event/ingestion — per-dataset ingestion events. */
export function useIngestionDatasetEvents(
  datasetUrn: string,
  params: DatasetEventParams = {},
) {
  return usePoll<IngestionEventListResponse>({
    queryKey: ["ingestion", "dataset-events", datasetUrn, params],
    queryFn: () =>
      apiFetch<IngestionEventListResponse>(
        buildDatasetEventUrl(datasetUrn, params),
      ),
    enabled: !!datasetUrn,
    meta: { handledInline: true },
  });
}
