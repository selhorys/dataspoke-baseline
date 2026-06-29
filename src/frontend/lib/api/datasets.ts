"use client";

import { apiFetch } from "@/lib/api/client";
import { usePoll } from "@/lib/hooks/use-poll";
import type { DatasetListResponse } from "@/types/dataset";

// ── Dataset catalog (collection root of /spoke/common/data) ──────────────────────

interface DatasetListParams {
  offset?: number;
  limit?: number;
  /** dataset_urn | dataset_urn_desc */
  sort?: string;
}

function buildListUrl(params: DatasetListParams): string {
  const sp = new URLSearchParams();
  if (params.offset !== undefined) sp.set("offset", String(params.offset));
  if (params.limit !== undefined) sp.set("limit", String(params.limit));
  if (params.sort) sp.set("sort", params.sort);
  const qs = sp.toString();
  return `/spoke/common/data${qs ? `?${qs}` : ""}`;
}

/** GET /spoke/common/data — paginated catalog of all registered datasets, polled. */
export function useDatasetList(params: DatasetListParams = {}) {
  return usePoll<DatasetListResponse>({
    queryKey: ["datasets", "list", params],
    queryFn: () => apiFetch<DatasetListResponse>(buildListUrl(params)),
    meta: { handledInline: true },
  });
}
