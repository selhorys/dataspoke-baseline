"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch, ApiError } from "@/lib/api/client";
import { usePoll } from "@/lib/hooks/use-poll";
import type {
  IngestionConfigListResponse,
  IngestionConfigResponse,
  IngestionEventListResponse,
  RunResultResponse,
} from "@/types/ingestion";

// ── List configs (cross-dataset) ───────────────────────────────────────────────

interface ListIngestionParams {
  offset?: number;
  limit?: number;
  status?: string;
  sort?: string;
}

function buildListUrl(params: ListIngestionParams): string {
  const sp = new URLSearchParams();
  if (params.offset !== undefined) sp.set("offset", String(params.offset));
  if (params.limit !== undefined) sp.set("limit", String(params.limit));
  if (params.status) sp.set("status", params.status);
  if (params.sort) sp.set("sort", params.sort);
  const qs = sp.toString();
  return `/spoke/ingestion${qs ? `?${qs}` : ""}`;
}

export function useIngestionList(params: ListIngestionParams = {}) {
  return usePoll<IngestionConfigListResponse>({
    queryKey: ["ingestion", "list", params],
    queryFn: () => apiFetch<IngestionConfigListResponse>(buildListUrl(params)),
    meta: { handledInline: true },
  });
}

// ── Per-dataset conf ───────────────────────────────────────────────────────────

export function useIngestionConf(datasetUrn: string) {
  return useQuery<IngestionConfigResponse>({
    queryKey: ["ingestion", "conf", datasetUrn],
    queryFn: () =>
      apiFetch<IngestionConfigResponse>(
        `/spoke/common/data/${encodeURIComponent(datasetUrn)}/attr/ingestion/conf`,
      ),
    enabled: !!datasetUrn,
    meta: { handledInline: true },
    retry: (failureCount, error) => {
      // Do not retry on 404 — the config may not exist yet (create state).
      if (error instanceof ApiError && error.status === 404) {
        return false;
      }
      return failureCount < 2;
    },
  });
}

// ── Upsert (PUT) conf ──────────────────────────────────────────────────────────

export function useUpsertIngestionConf(datasetUrn: string) {
  const qc = useQueryClient();
  return useMutation<IngestionConfigResponse, Error, Record<string, unknown>>({
    mutationFn: (body) =>
      apiFetch<IngestionConfigResponse>(
        `/spoke/common/data/${encodeURIComponent(datasetUrn)}/attr/ingestion/conf`,
        {
          method: "PUT",
          body: JSON.stringify(body),
        },
      ),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["ingestion", "conf", datasetUrn] });
      void qc.invalidateQueries({ queryKey: ["ingestion", "list"] });
    },
  });
}

// ── Delete conf ────────────────────────────────────────────────────────────────

export function useDeleteIngestionConf(datasetUrn: string) {
  const qc = useQueryClient();
  return useMutation<void, Error, void>({
    mutationFn: () =>
      apiFetch<void>(
        `/spoke/common/data/${encodeURIComponent(datasetUrn)}/attr/ingestion/conf`,
        { method: "DELETE" },
      ),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["ingestion", "conf", datasetUrn] });
      void qc.invalidateQueries({ queryKey: ["ingestion", "list"] });
    },
  });
}

// ── Run ingestion ──────────────────────────────────────────────────────────────

export function useRunIngestion(datasetUrn: string) {
  const qc = useQueryClient();
  return useMutation<RunResultResponse, Error, { dry_run?: boolean }>({
    mutationFn: ({ dry_run = false }) =>
      apiFetch<RunResultResponse>(
        `/spoke/common/data/${encodeURIComponent(datasetUrn)}/method/ingestion/run`,
        {
          method: "POST",
          body: JSON.stringify({ dry_run }),
        },
      ),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["ingestion", "events", datasetUrn] });
    },
  });
}

// ── Event history ──────────────────────────────────────────────────────────────

export function useIngestionEvents(datasetUrn: string, limit = 5) {
  return usePoll<IngestionEventListResponse>({
    queryKey: ["ingestion", "events", datasetUrn, limit],
    queryFn: () =>
      apiFetch<IngestionEventListResponse>(
        `/spoke/common/data/${encodeURIComponent(datasetUrn)}/event/ingestion?limit=${limit}&sort=occurred_at_desc`,
      ),
    enabled: !!datasetUrn,
    meta: { handledInline: true },
  });
}
