"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch, ApiError } from "@/lib/api/client";
import { usePoll } from "@/lib/hooks/use-poll";
import type {
  ValidationListResponse,
  ValidationConfResponse,
  ValidationResultListResponse,
  ValidationEventListResponse,
} from "@/types/validation";

// ── Cross-dataset list ─────────────────────────────────────────────────────────

interface ValidationListParams {
  offset?: number;
  limit?: number;
  sort?: string;
  // When set, filters by soft-delete state. Omit to return active and removed
  // slots. The list page sends `false` by default and omits it to show deleted.
  removed?: boolean;
}

function buildListUrl(params: ValidationListParams): string {
  const sp = new URLSearchParams();
  if (params.offset !== undefined) sp.set("offset", String(params.offset));
  if (params.limit !== undefined) sp.set("limit", String(params.limit));
  if (params.sort) sp.set("sort", params.sort);
  if (params.removed !== undefined) sp.set("removed", String(params.removed));
  const qs = sp.toString();
  return `/spoke/validation${qs ? `?${qs}` : ""}`;
}

export function useValidationList(params: ValidationListParams = {}) {
  return usePoll<ValidationListResponse>({
    queryKey: ["validation", "list", params],
    queryFn: () => apiFetch<ValidationListResponse>(buildListUrl(params)),
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

// ── Restore (undelete) conf ──────────────────────────────────────────────────────

// Restores a soft-deleted conf as-is (preserving frozen variables/description and
// the result history). On success re-fetches the now-active conf so the detail
// page leaves the deleted state.
export function useRestoreValidationConf(datasetUrn: string) {
  const qc = useQueryClient();
  return useMutation<ValidationConfResponse, Error, void>({
    mutationFn: () =>
      apiFetch<ValidationConfResponse>(
        `/spoke/common/data/${encodeURIComponent(datasetUrn)}/attr/validation/conf/method/restore`,
        { method: "POST" },
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

// ── Event log ──────────────────────────────────────────────────────────────────

interface ValidationEventParams {
  offset?: number;
  limit?: number;
  from?: string;
  to?: string;
}

function buildEventUrl(datasetUrn: string, params: ValidationEventParams): string {
  const sp = new URLSearchParams();
  if (params.offset !== undefined) sp.set("offset", String(params.offset));
  sp.set("limit", String(params.limit ?? 20));
  if (params.from) sp.set("from", params.from);
  if (params.to) sp.set("to", params.to);
  sp.set("sort", "occurred_at_desc");
  return `/spoke/common/data/${encodeURIComponent(datasetUrn)}/event/validation?${sp.toString()}`;
}

export function useValidationEvents(
  datasetUrn: string,
  params: ValidationEventParams = {},
) {
  return usePoll<ValidationEventListResponse>({
    queryKey: ["validation", "events", datasetUrn, params],
    queryFn: () =>
      apiFetch<ValidationEventListResponse>(buildEventUrl(datasetUrn, params)),
    enabled: !!datasetUrn,
    meta: { handledInline: true },
  });
}
