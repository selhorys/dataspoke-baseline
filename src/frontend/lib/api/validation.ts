"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api/client";
import { usePoll } from "@/lib/hooks/use-poll";
import type {
  ValidationCoverage,
  ValidationListResponse,
  ValidationConfResponse,
  ValidationResultListResponse,
} from "@/types/validation";

// ── Cross-dataset list ─────────────────────────────────────────────────────────

interface ValidationListParams {
  offset?: number;
  limit?: number;
  sort?: string;
  /** covered (default) | uncovered | both — server-side coverage filter. */
  coverage?: ValidationCoverage;
}

function buildListUrl(params: ValidationListParams): string {
  const sp = new URLSearchParams();
  if (params.offset !== undefined) sp.set("offset", String(params.offset));
  if (params.limit !== undefined) sp.set("limit", String(params.limit));
  if (params.sort) sp.set("sort", params.sort);
  if (params.coverage) sp.set("coverage", params.coverage);
  const qs = sp.toString();
  return `/spoke/validation${qs ? `?${qs}` : ""}`;
}

export function useValidationList(
  params: ValidationListParams = {},
  options: { enabled?: boolean } = {},
) {
  return usePoll<ValidationListResponse>({
    queryKey: ["validation", "list", params],
    queryFn: () => apiFetch<ValidationListResponse>(buildListUrl(params)),
    enabled: options.enabled ?? true,
    meta: { handledInline: true },
  });
}

// ── Per-dataset conf ───────────────────────────────────────────────────────────

export function useValidationConf(datasetUrn: string) {
  return useQuery<ValidationConfResponse>({
    queryKey: ["validation", "conf", datasetUrn],
    queryFn: () =>
      apiFetch<ValidationConfResponse>(
        `/spoke/common/data/${encodeURIComponent(datasetUrn)}/attr/validation/conf`,
      ),
    enabled: !!datasetUrn,
    meta: { handledInline: true },
  });
}

// ── Upsert (PUT) conf ──────────────────────────────────────────────────────────

export function useUpsertValidationConf(datasetUrn: string) {
  const qc = useQueryClient();
  return useMutation<ValidationConfResponse, Error, Record<string, unknown>>({
    mutationFn: (body) =>
      apiFetch<ValidationConfResponse>(
        `/spoke/common/data/${encodeURIComponent(datasetUrn)}/attr/validation/conf`,
        {
          method: "PUT",
          body: JSON.stringify(body),
        },
      ),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["validation", "conf", datasetUrn] });
      void qc.invalidateQueries({ queryKey: ["validation", "list"] });
    },
  });
}

// ── Delete conf ────────────────────────────────────────────────────────────────

export function useDeleteValidationConf(datasetUrn: string) {
  const qc = useQueryClient();
  return useMutation<void, Error, void>({
    mutationFn: () =>
      apiFetch<void>(
        `/spoke/common/data/${encodeURIComponent(datasetUrn)}/attr/validation/conf`,
        { method: "DELETE" },
      ),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["validation", "conf", datasetUrn] });
      void qc.invalidateQueries({ queryKey: ["validation", "list"] });
    },
  });
}

// ── Historical results ─────────────────────────────────────────────────────────

interface ValidationResultsParams {
  from?: string;
  until?: string;
  limit?: number;
}

function buildResultUrl(datasetUrn: string, params: ValidationResultsParams): string {
  const sp = new URLSearchParams();
  if (params.from) sp.set("from", params.from);
  if (params.until) sp.set("until", params.until);
  if (params.limit !== undefined) sp.set("limit", String(params.limit));
  const qs = sp.toString();
  return `/spoke/common/data/${encodeURIComponent(datasetUrn)}/attr/validation/result${qs ? `?${qs}` : ""}`;
}

export function useValidationResults(
  datasetUrn: string,
  params: ValidationResultsParams = {},
) {
  return usePoll<ValidationResultListResponse>({
    queryKey: ["validation", "results", datasetUrn, params],
    queryFn: () =>
      apiFetch<ValidationResultListResponse>(buildResultUrl(datasetUrn, params)),
    enabled: !!datasetUrn,
    meta: { handledInline: true },
  });
}
