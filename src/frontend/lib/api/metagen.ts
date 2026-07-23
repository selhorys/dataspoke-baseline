"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api/client";
import { usePoll } from "@/lib/hooks/use-poll";
import type {
  MetagenBoundary,
  MetagenBoundaryPutBody,
  MetagenCandidate,
  MetagenConf,
  MetagenCoveredDatasetResponse,
  MetagenDatasetListResponse,
  MetagenConfCreateBody,
  MetagenConfListResponse,
  MetagenConfPatchBody,
  MetagenConfPutBody,
  MetagenEventListResponse,
  MetagenItemDetail,
  MetagenItemListResponse,
  MetagenReviewBody,
  MetagenRunBody,
  MetagenRunResponse,
  MetagenUncoveredResponse,
} from "@/types/metagen";

// ── Conf collection ───────────────────────────────────────────────────────────

interface ConfListParams {
  offset?: number;
  limit?: number;
}

function buildConfListUrl(params: ConfListParams): string {
  const sp = new URLSearchParams();
  if (params.offset !== undefined) sp.set("offset", String(params.offset));
  if (params.limit !== undefined) sp.set("limit", String(params.limit));
  const qs = sp.toString();
  return `/spoke/metagen/conf${qs ? `?${qs}` : ""}`;
}

/** GET /spoke/metagen/conf — paginated conf list, polled. */
export function useMetagenConfList(params: ConfListParams = {}) {
  return usePoll<MetagenConfListResponse>({
    queryKey: ["metagen", "confs", params],
    queryFn: () => apiFetch<MetagenConfListResponse>(buildConfListUrl(params)),
    meta: { handledInline: true },
  });
}

/** GET /spoke/metagen/conf/{conf_id}. */
export function useMetagenConf(confId: string) {
  return useQuery<MetagenConf>({
    queryKey: ["metagen", "conf", confId],
    queryFn: () =>
      apiFetch<MetagenConf>(
        `/spoke/metagen/conf/${encodeURIComponent(confId)}`,
      ),
    enabled: !!confId,
    meta: { handledInline: true },
  });
}

/** POST /spoke/metagen/conf — create a conf (409 METAGEN_CONF_EXISTS on dup name). */
export function useCreateMetagenConf() {
  const qc = useQueryClient();
  return useMutation<MetagenConf, Error, MetagenConfCreateBody>({
    mutationFn: (body) =>
      apiFetch<MetagenConf>("/spoke/metagen/conf", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["metagen", "confs"] });
      void qc.invalidateQueries({ queryKey: ["metagen", "uncovered"] });
    },
  });
}

/** PUT/PATCH /spoke/metagen/conf/{conf_id}. */
export function useUpdateMetagenConf(confId: string) {
  const qc = useQueryClient();

  function invalidate() {
    void qc.invalidateQueries({ queryKey: ["metagen", "confs"] });
    void qc.invalidateQueries({ queryKey: ["metagen", "conf", confId] });
    void qc.invalidateQueries({ queryKey: ["metagen", "uncovered"] });
  }

  const put = useMutation<MetagenConf, Error, MetagenConfPutBody>({
    mutationFn: (body) =>
      apiFetch<MetagenConf>(`/spoke/metagen/conf/${encodeURIComponent(confId)}`, {
        method: "PUT",
        body: JSON.stringify(body),
      }),
    meta: { handledInline: true },
    onSuccess: invalidate,
  });

  const patch = useMutation<MetagenConf, Error, MetagenConfPatchBody>({
    mutationFn: (body) =>
      apiFetch<MetagenConf>(`/spoke/metagen/conf/${encodeURIComponent(confId)}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    meta: { handledInline: true },
    onSuccess: invalidate,
  });

  return { put, patch };
}

/** DELETE /spoke/metagen/conf/{conf_id}. */
export function useDeleteMetagenConf(confId: string) {
  const qc = useQueryClient();
  return useMutation<void, Error, void>({
    mutationFn: () =>
      apiFetch<void>(`/spoke/metagen/conf/${encodeURIComponent(confId)}`, {
        method: "DELETE",
      }),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["metagen", "confs"] });
      void qc.invalidateQueries({ queryKey: ["metagen", "conf", confId] });
      void qc.invalidateQueries({ queryKey: ["metagen", "items"] });
      void qc.invalidateQueries({ queryKey: ["metagen", "datasets"] });
      void qc.invalidateQueries({ queryKey: ["metagen", "uncovered"] });
    },
  });
}

/** POST /spoke/metagen/conf/{conf_id}/method/run?dry_run=true. */
export function useRunMetagenConf(confId: string) {
  const qc = useQueryClient();
  return useMutation<MetagenRunResponse, Error, MetagenRunBody>({
    mutationFn: ({ dataset_urns, dry_run = false }) => {
      const url = `/spoke/metagen/conf/${encodeURIComponent(confId)}/method/run${
        dry_run ? "?dry_run=true" : ""
      }`;
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
      void qc.invalidateQueries({ queryKey: ["metagen", "datasets"] });
      void qc.invalidateQueries({ queryKey: ["metagen", "events"] });
      void qc.invalidateQueries({ queryKey: ["metagen", "conf-events", confId] });
    },
  });
}

/** GET /spoke/metagen/conf/{conf_id}/event — per-conf run history, polled. */
export function useMetagenConfEvents(
  confId: string,
  params: { from?: string; to?: string; offset?: number; limit?: number } = {},
) {
  function buildUrl(): string {
    const sp = new URLSearchParams();
    if (params.from) sp.set("from", params.from);
    if (params.to) sp.set("to", params.to);
    if (params.offset !== undefined) sp.set("offset", String(params.offset));
    if (params.limit !== undefined) sp.set("limit", String(params.limit));
    const qs = sp.toString();
    return `/spoke/metagen/conf/${encodeURIComponent(confId)}/event${qs ? `?${qs}` : ""}`;
  }

  return usePoll<MetagenEventListResponse>({
    queryKey: ["metagen", "conf-events", confId, params],
    queryFn: () => apiFetch<MetagenEventListResponse>(buildUrl()),
    enabled: !!confId,
    meta: { handledInline: true },
  });
}

// ── Covered datasets (per-conf) ─────────────────────────────────────────────────

/** GET /spoke/metagen/conf/{conf_id}/dataset?include_disallowed=<bool> — polled. */
export function useMetagenCoveredDatasets(
  confId: string,
  includeDisallowed: boolean,
  params: { offset?: number; limit?: number } = {},
) {
  function buildUrl(): string {
    const sp = new URLSearchParams();
    if (includeDisallowed) sp.set("include_disallowed", "true");
    if (params.offset !== undefined) sp.set("offset", String(params.offset));
    if (params.limit !== undefined) sp.set("limit", String(params.limit));
    const qs = sp.toString();
    return `/spoke/metagen/conf/${encodeURIComponent(confId)}/dataset${qs ? `?${qs}` : ""}`;
  }

  return usePoll<MetagenCoveredDatasetResponse>({
    queryKey: ["metagen", "covered", confId, includeDisallowed, params],
    queryFn: () => apiFetch<MetagenCoveredDatasetResponse>(buildUrl()),
    enabled: !!confId,
    meta: { handledInline: true },
  });
}

// ── Uncovered ──────────────────────────────────────────────────────────────────

/** GET /spoke/metagen/uncovered?include_disallowed=<bool> — polled. */
export function useMetagenUncovered(
  includeDisallowed: boolean,
  params: { offset?: number; limit?: number } = {},
) {
  function buildUrl(): string {
    const sp = new URLSearchParams();
    if (includeDisallowed) sp.set("include_disallowed", "true");
    if (params.offset !== undefined) sp.set("offset", String(params.offset));
    if (params.limit !== undefined) sp.set("limit", String(params.limit));
    const qs = sp.toString();
    return `/spoke/metagen/uncovered${qs ? `?${qs}` : ""}`;
  }

  return usePoll<MetagenUncoveredResponse>({
    queryKey: ["metagen", "uncovered", includeDisallowed, params],
    queryFn: () => apiFetch<MetagenUncoveredResponse>(buildUrl()),
    meta: { handledInline: true },
  });
}

// ── Per-dataset result rollup (cross-conf) ──────────────────────────────────────

interface MetagenDatasetParams {
  dataset_urn?: string;
  conf_id?: string;
  offset?: number;
  limit?: number;
}

function buildDatasetUrl(params: MetagenDatasetParams): string {
  const sp = new URLSearchParams();
  if (params.dataset_urn) sp.set("dataset_urn", params.dataset_urn);
  if (params.conf_id) sp.set("conf_id", params.conf_id);
  if (params.offset !== undefined) sp.set("offset", String(params.offset));
  if (params.limit !== undefined) sp.set("limit", String(params.limit));
  const qs = sp.toString();
  return `/spoke/metagen/dataset${qs ? `?${qs}` : ""}`;
}

/** GET /spoke/metagen/dataset — per-dataset result rollup, polled. */
export function useMetagenDatasets(params: MetagenDatasetParams = {}) {
  return usePoll<MetagenDatasetListResponse>({
    queryKey: ["metagen", "datasets", params],
    queryFn: () => apiFetch<MetagenDatasetListResponse>(buildDatasetUrl(params)),
    meta: { handledInline: true },
  });
}

// ── Global events ──────────────────────────────────────────────────────────────

/** GET /spoke/metagen/event — cross-conf event feed, polled. */
export function useMetagenEvents(
  params: { from?: string; to?: string; offset?: number; limit?: number } = {},
) {
  function buildUrl(): string {
    const sp = new URLSearchParams();
    if (params.from) sp.set("from", params.from);
    if (params.to) sp.set("to", params.to);
    if (params.offset !== undefined) sp.set("offset", String(params.offset));
    if (params.limit !== undefined) sp.set("limit", String(params.limit));
    const qs = sp.toString();
    return `/spoke/metagen/event${qs ? `?${qs}` : ""}`;
  }

  return usePoll<MetagenEventListResponse>({
    queryKey: ["metagen", "events", params],
    queryFn: () => apiFetch<MetagenEventListResponse>(buildUrl()),
    meta: { handledInline: true },
  });
}

// ── Per-dataset boundary (attr/metagen/boundary) ────────────────────────────────

/**
 * GET /spoke/common/data/{urn}/attr/metagen/boundary
 * Returns null when no boundary has been set for this dataset.
 */
export function useMetagenBoundary(datasetUrn: string) {
  return useQuery<MetagenBoundary | null>({
    queryKey: ["metagen", "boundary", datasetUrn],
    queryFn: () =>
      apiFetch<MetagenBoundary | null>(
        `/spoke/common/data/${encodeURIComponent(datasetUrn)}/attr/metagen/boundary`,
      ),
    enabled: !!datasetUrn,
    meta: { handledInline: true },
  });
}

/** PUT /spoke/common/data/{urn}/attr/metagen/boundary */
export function useUpsertMetagenBoundary(datasetUrn: string) {
  const qc = useQueryClient();
  return useMutation<MetagenBoundary, Error, MetagenBoundaryPutBody>({
    mutationFn: (body) =>
      apiFetch<MetagenBoundary>(
        `/spoke/common/data/${encodeURIComponent(datasetUrn)}/attr/metagen/boundary`,
        { method: "PUT", body: JSON.stringify(body) },
      ),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["metagen", "boundary", datasetUrn] });
      void qc.invalidateQueries({ queryKey: ["metagen", "uncovered"] });
    },
  });
}

/** DELETE /spoke/common/data/{urn}/attr/metagen/boundary */
export function useDeleteMetagenBoundary(datasetUrn: string) {
  const qc = useQueryClient();
  return useMutation<void, Error, void>({
    mutationFn: () =>
      apiFetch<void>(
        `/spoke/common/data/${encodeURIComponent(datasetUrn)}/attr/metagen/boundary`,
        { method: "DELETE" },
      ),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["metagen", "boundary", datasetUrn] });
      void qc.invalidateQueries({ queryKey: ["metagen", "uncovered"] });
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
        `/spoke/common/data/${encodeURIComponent(datasetUrn)}/attr/metagen/item/${encodeURIComponent(itemId)}`,
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
 * Returns the updated candidate. Reject is valid on both llm_approved and
 * approved candidates; rejecting an approved candidate flips it to rejected and
 * removes the editable DataHub description it had written.
 * 422 METAGEN_DATASET_NOT_IN_BOUNDARY when the dataset has no is_enabled boundary.
 */
export function useReviewCandidate() {
  const qc = useQueryClient();
  return useMutation<MetagenCandidate, Error, ReviewCandidateVars>({
    mutationFn: ({ datasetUrn, itemId, candidateId, body }) =>
      apiFetch<MetagenCandidate>(
        `/spoke/common/data/${encodeURIComponent(datasetUrn)}/attr/metagen/item/${encodeURIComponent(itemId)}/candidate/${encodeURIComponent(candidateId)}/method/review`,
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
      void qc.invalidateQueries({ queryKey: ["metagen", "datasets"] });
      void qc.invalidateQueries({
        queryKey: ["metagen", "dataset-events", vars.datasetUrn],
      });
    },
  });
}
