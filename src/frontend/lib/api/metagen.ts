"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api/client";
import { usePoll } from "@/lib/hooks/use-poll";
import type {
  MetagenBoundary,
  MetagenBoundaryPutBody,
  MetagenCandidate,
  MetagenEventListResponse,
  MetagenGlobalConf,
  MetagenGlobalConfPatchBody,
  MetagenGlobalConfPutBody,
  MetagenItemDetail,
  MetagenItemListResponse,
  MetagenReviewBody,
  MetagenRunBody,
  MetagenRunResponse,
} from "@/types/metagen";

// ── Global conf ───────────────────────────────────────────────────────────────

/**
 * GET /spoke/metagen/attr/conf
 * Returns null when no conf has been created yet (fresh install).
 */
export function useMetagenConf() {
  return useQuery<MetagenGlobalConf | null>({
    queryKey: ["metagen", "conf"],
    queryFn: () => apiFetch<MetagenGlobalConf | null>("/spoke/metagen/attr/conf"),
    meta: { handledInline: true },
  });
}

/** PUT /spoke/metagen/attr/conf — full replacement / initial creation. */
export function useUpsertMetagenConf() {
  const qc = useQueryClient();
  return useMutation<MetagenGlobalConf, Error, MetagenGlobalConfPutBody>({
    mutationFn: (body) =>
      apiFetch<MetagenGlobalConf>("/spoke/metagen/attr/conf", {
        method: "PUT",
        body: JSON.stringify(body),
      }),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["metagen", "conf"] });
    },
  });
}

/** PATCH /spoke/metagen/attr/conf — partial update. */
export function usePatchMetagenConf() {
  const qc = useQueryClient();
  return useMutation<MetagenGlobalConf, Error, MetagenGlobalConfPatchBody>({
    mutationFn: (body) =>
      apiFetch<MetagenGlobalConf>("/spoke/metagen/attr/conf", {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["metagen", "conf"] });
    },
  });
}

/** DELETE /spoke/metagen/attr/conf */
export function useDeleteMetagenConf() {
  const qc = useQueryClient();
  return useMutation<void, Error, void>({
    mutationFn: () =>
      apiFetch<void>("/spoke/metagen/attr/conf", { method: "DELETE" }),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["metagen", "conf"] });
    },
  });
}

// ── Cross-dataset item queue ───────────────────────────────────────────────────

interface MetagenQueueParams {
  dataset_urn?: string;
  kind?: string;
  status?: string;
  offset?: number;
  limit?: number;
}

function buildQueueUrl(params: MetagenQueueParams): string {
  const sp = new URLSearchParams();
  if (params.dataset_urn) sp.set("dataset_urn", params.dataset_urn);
  if (params.kind) sp.set("kind", params.kind);
  if (params.status) sp.set("status", params.status);
  if (params.offset !== undefined) sp.set("offset", String(params.offset));
  if (params.limit !== undefined) sp.set("limit", String(params.limit));
  const qs = sp.toString();
  return `/spoke/metagen/item${qs ? `?${qs}` : ""}`;
}

/** GET /spoke/metagen/item — cross-dataset queue, polled. */
export function useMetagenQueue(params: MetagenQueueParams = {}) {
  return usePoll<MetagenItemListResponse>({
    queryKey: ["metagen", "queue", params],
    queryFn: () => apiFetch<MetagenItemListResponse>(buildQueueUrl(params)),
    meta: { handledInline: true },
  });
}

// ── Global run ─────────────────────────────────────────────────────────────────

/** POST /spoke/metagen/method/run?dry_run=true */
export function useRunMetagen() {
  const qc = useQueryClient();
  return useMutation<MetagenRunResponse, Error, MetagenRunBody>({
    mutationFn: ({ dataset_urns, dry_run = false }) => {
      const url = `/spoke/metagen/method/run${dry_run ? "?dry_run=true" : ""}`;
      if (dataset_urns && dataset_urns.length > 0) {
        return apiFetch<MetagenRunResponse>(url, {
          method: "POST",
          body: JSON.stringify({ dataset_urns }),
        });
      }
      return apiFetch<MetagenRunResponse>(url, { method: "POST" });
    },
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["metagen", "queue"] });
      void qc.invalidateQueries({ queryKey: ["metagen", "events"] });
    },
  });
}

// ── Global events ──────────────────────────────────────────────────────────────

/** GET /spoke/metagen/event — polled. */
export function useMetagenEvents(limit = 10) {
  return usePoll<MetagenEventListResponse>({
    queryKey: ["metagen", "events", limit],
    queryFn: () =>
      apiFetch<MetagenEventListResponse>(`/spoke/metagen/event?limit=${limit}`),
    meta: { handledInline: true },
  });
}

// ── Per-dataset boundary ───────────────────────────────────────────────────────

/**
 * GET /spoke/common/data/{urn}/attr/metagen/conf
 * Returns null when no boundary has been set for this dataset.
 */
export function useMetagenBoundary(datasetUrn: string) {
  return useQuery<MetagenBoundary | null>({
    queryKey: ["metagen", "boundary", datasetUrn],
    queryFn: () =>
      apiFetch<MetagenBoundary | null>(
        `/spoke/common/data/${encodeURIComponent(datasetUrn)}/attr/metagen/conf`,
      ),
    enabled: !!datasetUrn,
    meta: { handledInline: true },
  });
}

/** PUT /spoke/common/data/{urn}/attr/metagen/conf */
export function useUpsertMetagenBoundary(datasetUrn: string) {
  const qc = useQueryClient();
  return useMutation<MetagenBoundary, Error, MetagenBoundaryPutBody>({
    mutationFn: (body) =>
      apiFetch<MetagenBoundary>(
        `/spoke/common/data/${encodeURIComponent(datasetUrn)}/attr/metagen/conf`,
        { method: "PUT", body: JSON.stringify(body) },
      ),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["metagen", "boundary", datasetUrn] });
    },
  });
}

/** DELETE /spoke/common/data/{urn}/attr/metagen/conf */
export function useDeleteMetagenBoundary(datasetUrn: string) {
  const qc = useQueryClient();
  return useMutation<void, Error, void>({
    mutationFn: () =>
      apiFetch<void>(
        `/spoke/common/data/${encodeURIComponent(datasetUrn)}/attr/metagen/conf`,
        { method: "DELETE" },
      ),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["metagen", "boundary", datasetUrn] });
    },
  });
}

// ── Per-dataset items ──────────────────────────────────────────────────────────

/** GET /spoke/common/data/{urn}/attr/metagen/item — polled. */
export function useMetagenItems(datasetUrn: string) {
  return usePoll<MetagenItemListResponse>({
    queryKey: ["metagen", "items", datasetUrn],
    queryFn: () =>
      apiFetch<MetagenItemListResponse>(
        `/spoke/common/data/${encodeURIComponent(datasetUrn)}/attr/metagen/item`,
      ),
    enabled: !!datasetUrn,
    meta: { handledInline: true },
  });
}

/** GET /spoke/common/data/{urn}/attr/metagen/item/{item_id} */
export function useMetagenItem(
  datasetUrn: string,
  itemId: string,
  options: { enabled?: boolean } = {},
) {
  return useQuery<MetagenItemDetail>({
    queryKey: ["metagen", "item", datasetUrn, itemId],
    queryFn: () =>
      apiFetch<MetagenItemDetail>(
        `/spoke/common/data/${encodeURIComponent(datasetUrn)}/attr/metagen/item/${itemId}`,
      ),
    enabled: !!datasetUrn && !!itemId && (options.enabled ?? true),
    meta: { handledInline: true },
  });
}

// ── Candidate review ───────────────────────────────────────────────────────────

interface ReviewCandidateVars {
  datasetUrn: string;
  itemId: string;
  candidateId: string;
  body: MetagenReviewBody;
}

/**
 * POST /spoke/common/data/{urn}/attr/metagen/item/{item_id}/candidate/{candidate_id}/method/review
 *
 * Returns the updated candidate.
 * 409 METAGEN_CANNOT_REJECT_APPROVED when trying to reject an approved candidate.
 */
export function useReviewCandidate() {
  const qc = useQueryClient();
  return useMutation<MetagenCandidate, Error, ReviewCandidateVars>({
    mutationFn: ({ datasetUrn, itemId, candidateId, body }) =>
      apiFetch<MetagenCandidate>(
        `/spoke/common/data/${encodeURIComponent(datasetUrn)}/attr/metagen/item/${itemId}/candidate/${candidateId}/method/review`,
        { method: "POST", body: JSON.stringify(body) },
      ),
    meta: { handledInline: true },
    onSuccess: (_data, vars) => {
      void qc.invalidateQueries({
        queryKey: ["metagen", "item", vars.datasetUrn, vars.itemId],
      });
      void qc.invalidateQueries({
        queryKey: ["metagen", "items", vars.datasetUrn],
      });
      void qc.invalidateQueries({ queryKey: ["metagen", "queue"] });
      void qc.invalidateQueries({
        queryKey: ["metagen", "dataset-events", vars.datasetUrn],
      });
    },
  });
}

// ── Per-dataset events ─────────────────────────────────────────────────────────

/** GET /spoke/common/data/{urn}/event/metagen — polled. */
export function useMetagenDatasetEvents(datasetUrn: string, limit = 10) {
  return usePoll<MetagenEventListResponse>({
    queryKey: ["metagen", "dataset-events", datasetUrn, limit],
    queryFn: () =>
      apiFetch<MetagenEventListResponse>(
        `/spoke/common/data/${encodeURIComponent(datasetUrn)}/event/metagen?limit=${limit}`,
      ),
    enabled: !!datasetUrn,
    meta: { handledInline: true },
  });
}
