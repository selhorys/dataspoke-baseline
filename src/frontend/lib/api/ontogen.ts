"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api/client";
import { usePoll } from "@/lib/hooks/use-poll";
import type {
  EdgeListResponse,
  NodeListResponse,
  OntogenConf,
  OntogenConfPatchBody,
  OntogenConfPutBody,
  OntogenEdge,
  OntogenEventListResponse,
  OntogenNode,
  OntogenRunResponse,
  OntogenTriple,
  ReviewRequest,
  SeedCreateResponse,
  SeedListResponse,
  TripleListResponse,
} from "@/types/ontogen";

// ── Singleton conf ────────────────────────────────────────────────────────────

export function useOntogenConf() {
  return useQuery<OntogenConf>({
    queryKey: ["ontogen", "conf"],
    queryFn: () => apiFetch<OntogenConf>("/spoke/ontogen/attr/conf"),
    meta: { handledInline: true },
  });
}

export function useUpsertOntogenConf() {
  const qc = useQueryClient();
  return useMutation<OntogenConf, Error, OntogenConfPutBody>({
    mutationFn: (body) =>
      apiFetch<OntogenConf>("/spoke/ontogen/attr/conf", {
        method: "PUT",
        body: JSON.stringify(body),
      }),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["ontogen", "conf"] });
    },
  });
}

export function usePatchOntogenConf() {
  const qc = useQueryClient();
  return useMutation<OntogenConf, Error, OntogenConfPatchBody>({
    mutationFn: (body) =>
      apiFetch<OntogenConf>("/spoke/ontogen/attr/conf", {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["ontogen", "conf"] });
    },
  });
}

export function useDeleteOntogenConf() {
  const qc = useQueryClient();
  return useMutation<void, Error, void>({
    mutationFn: () =>
      apiFetch<void>("/spoke/ontogen/attr/conf", { method: "DELETE" }),
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["ontogen", "conf"] });
    },
  });
}

// ── Seeds ─────────────────────────────────────────────────────────────────────

interface ListSeedsParams {
  offset?: number;
  limit?: number;
}

function buildSeedsUrl(params: ListSeedsParams): string {
  const sp = new URLSearchParams();
  if (params.offset !== undefined) sp.set("offset", String(params.offset));
  if (params.limit !== undefined) sp.set("limit", String(params.limit));
  const qs = sp.toString();
  return `/spoke/ontogen/attr/seed${qs ? `?${qs}` : ""}`;
}

export function useOntogenSeeds(params: ListSeedsParams = {}) {
  return useQuery<SeedListResponse>({
    queryKey: ["ontogen", "seeds", params],
    queryFn: () => apiFetch<SeedListResponse>(buildSeedsUrl(params)),
    meta: { handledInline: true },
  });
}

/** Fetches the raw Markdown body of a single seed. Returns the text content. */
export function useOntogenSeed(seedId: string) {
  return useQuery<string>({
    queryKey: ["ontogen", "seed", seedId],
    queryFn: () =>
      apiFetch<string>(`/spoke/ontogen/attr/seed/${seedId}`, {
        responseType: "text",
        headers: { accept: "text/markdown" },
      }),
    enabled: !!seedId,
    meta: { handledInline: true },
  });
}

export function useCreateSeed() {
  const qc = useQueryClient();
  return useMutation<SeedCreateResponse, Error, string>({
    mutationFn: (markdownBody) =>
      apiFetch<SeedCreateResponse>("/spoke/ontogen/attr/seed", {
        method: "POST",
        headers: { "content-type": "text/markdown" },
        body: markdownBody,
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["ontogen", "seeds"] });
    },
  });
}

export function useUpdateSeed(seedId: string) {
  const qc = useQueryClient();
  return useMutation<SeedCreateResponse, Error, string>({
    mutationFn: (markdownBody) =>
      apiFetch<SeedCreateResponse>(`/spoke/ontogen/attr/seed/${seedId}`, {
        method: "PATCH",
        headers: { "content-type": "text/markdown" },
        body: markdownBody,
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["ontogen", "seeds"] });
      void qc.invalidateQueries({ queryKey: ["ontogen", "seed", seedId] });
    },
  });
}

export function useDeleteSeed(seedId: string) {
  const qc = useQueryClient();
  return useMutation<void, Error, void>({
    mutationFn: () =>
      apiFetch<void>(`/spoke/ontogen/attr/seed/${seedId}`, { method: "DELETE" }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["ontogen", "seeds"] });
      void qc.invalidateQueries({ queryKey: ["ontogen", "seed", seedId] });
    },
  });
}

// ── Result lists ──────────────────────────────────────────────────────────────

interface ResultListParams {
  offset?: number;
  limit?: number;
  status?: string;
}

function buildResultUrl(kind: "node" | "edge" | "triple", params: ResultListParams): string {
  const sp = new URLSearchParams();
  if (params.offset !== undefined) sp.set("offset", String(params.offset));
  if (params.limit !== undefined) sp.set("limit", String(params.limit));
  if (params.status) sp.set("status", params.status);
  const qs = sp.toString();
  return `/spoke/ontogen/result/${kind}${qs ? `?${qs}` : ""}`;
}

export function useOntogenNodes(params: ResultListParams = {}) {
  return usePoll<NodeListResponse>({
    queryKey: ["ontogen", "nodes", params],
    queryFn: () => apiFetch<NodeListResponse>(buildResultUrl("node", params)),
    meta: { handledInline: true },
  });
}

export function useOntogenEdges(params: ResultListParams = {}) {
  return usePoll<EdgeListResponse>({
    queryKey: ["ontogen", "edges", params],
    queryFn: () => apiFetch<EdgeListResponse>(buildResultUrl("edge", params)),
    meta: { handledInline: true },
  });
}

export function useOntogenTriples(params: ResultListParams = {}) {
  return usePoll<TripleListResponse>({
    queryKey: ["ontogen", "triples", params],
    queryFn: () => apiFetch<TripleListResponse>(buildResultUrl("triple", params)),
    meta: { handledInline: true },
  });
}

// ── Review ────────────────────────────────────────────────────────────────────

export type ReviewKind = "node" | "edge" | "triple";

interface ReviewItemVars {
  kind: ReviewKind;
  id: string;
  body: ReviewRequest;
}

export function useReviewOntogenItem() {
  const qc = useQueryClient();
  return useMutation<OntogenNode | OntogenEdge | OntogenTriple, Error, ReviewItemVars>({
    mutationFn: ({ kind, id, body }) =>
      apiFetch<OntogenNode | OntogenEdge | OntogenTriple>(
        `/spoke/ontogen/result/${kind}/${id}/method/review`,
        {
          method: "POST",
          body: JSON.stringify(body),
        },
      ),
    meta: { handledInline: true },
    onSuccess: (_data, vars) => {
      void qc.invalidateQueries({ queryKey: ["ontogen", `${vars.kind}s`] });
      // Triples depend on nodes and edges, so invalidate all result lists when
      // a node or edge status changes.
      if (vars.kind !== "triple") {
        void qc.invalidateQueries({ queryKey: ["ontogen", "triples"] });
      }
    },
  });
}

// ── Global events ─────────────────────────────────────────────────────────────

/** GET /spoke/ontogen/event — global ontogen run event history, polled. */
export function useOntogenEvents(limit = 10) {
  return usePoll<OntogenEventListResponse>({
    queryKey: ["ontogen", "events", limit],
    queryFn: () =>
      apiFetch<OntogenEventListResponse>(`/spoke/ontogen/event?limit=${limit}`),
    meta: { handledInline: true },
  });
}

// ── Run ───────────────────────────────────────────────────────────────────────

interface RunOntogenVars {
  promptMd?: string;
  dry_run?: boolean;
}

export function useRunOntogen() {
  const qc = useQueryClient();
  return useMutation<OntogenRunResponse, Error, RunOntogenVars>({
    mutationFn: ({ promptMd, dry_run = false }) => {
      const url = `/spoke/ontogen/method/run${dry_run ? "?dry_run=true" : ""}`;
      if (promptMd) {
        return apiFetch<OntogenRunResponse>(url, {
          method: "POST",
          headers: { "content-type": "text/markdown" },
          body: promptMd,
        });
      }
      return apiFetch<OntogenRunResponse>(url, { method: "POST" });
    },
    meta: { handledInline: true },
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ["ontogen", "nodes"] });
      void qc.invalidateQueries({ queryKey: ["ontogen", "edges"] });
      void qc.invalidateQueries({ queryKey: ["ontogen", "triples"] });
    },
  });
}
