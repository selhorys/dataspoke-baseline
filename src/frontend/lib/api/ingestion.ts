"use client";

import {
  useMutation,
  useQueries,
  useQueryClient,
} from "@tanstack/react-query";
import { apiFetch, ApiError } from "@/lib/api/client";
import { usePoll } from "@/lib/hooks/use-poll";
import type {
  IngestionSource,
  IngestionSourceBody,
  IngestionSourceListResponse,
  IngestionSourcePatchBody,
  IngestionRunResponse,
  IngestionSourceDatasetsResponse,
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
 * Per-row latest-run status: fires `event?limit=1&sort=occurred_at_desc` per
 * source id and exposes the newest event's status. Used by the list view's
 * status column.
 */
export function useIngestionSourceLatestRuns(ids: string[]) {
  return useQueries({
    queries: ids.map((id) => ({
      queryKey: ["ingestion", "events", id, { offset: 0, limit: 1 }],
      queryFn: () =>
        apiFetch<IngestionEventListResponse>(
          `/spoke/ingestion/sources/${encodeURIComponent(id)}/event?offset=0&limit=1&sort=occurred_at_desc`,
        ),
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
    retry: (failureCount, error) => {
      if (error instanceof ApiError && error.status === 404) return false;
      return failureCount < 2;
    },
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
 * is 403 READ_ONLY_ROLE for Readers). Never retried on 403/503.
 */
export function useIngestionSecrets(enabled: boolean) {
  return usePoll<SecretRefListResponse>({
    queryKey: ["ingestion", "secrets"],
    queryFn: () =>
      apiFetch<SecretRefListResponse>("/spoke/ingestion/secrets"),
    enabled,
    meta: { handledInline: true },
    retry: (failureCount, error) => {
      if (
        error instanceof ApiError &&
        (error.status === 403 || error.status === 503)
      ) {
        return false;
      }
      return failureCount < 2;
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
