"use client";

import { apiFetch } from "@/lib/api/client";
import { usePoll } from "@/lib/hooks/use-poll";
import type {
  DatasetEventListResponse,
  EventMajorType,
} from "@/types/data";

// ── Unified per-dataset event timeline ───────────────────────────────────────────

interface DatasetEventParams {
  offset?: number;
  limit?: number;
  from?: string;
  to?: string;
  /**
   * Repeatable major-type filter. Each value is emitted as its own
   * `event_major_type` query pair; an empty/omitted list returns all types.
   */
  eventMajorTypes?: EventMajorType[];
}

function buildDatasetEventUrl(
  datasetUrn: string,
  params: DatasetEventParams,
): string {
  const sp = new URLSearchParams();
  if (params.offset !== undefined) sp.set("offset", String(params.offset));
  sp.set("limit", String(params.limit ?? 20));
  if (params.from) sp.set("from", params.from);
  if (params.to) sp.set("to", params.to);
  for (const t of params.eventMajorTypes ?? []) {
    sp.append("event_major_type", t);
  }
  sp.set("sort", "occurred_at_desc");
  return `/spoke/common/data/${encodeURIComponent(datasetUrn)}/event?${sp.toString()}`;
}

/**
 * GET /spoke/common/data/{urn}/event — the complete per-dataset event timeline
 * (covering source's ingestion runs ∪ dataset validation + metagen events),
 * newest first, polled.
 */
export function useDatasetEvents(
  datasetUrn: string,
  params: DatasetEventParams = {},
) {
  return usePoll<DatasetEventListResponse>({
    queryKey: ["data", "events", datasetUrn, params],
    queryFn: () =>
      apiFetch<DatasetEventListResponse>(
        buildDatasetEventUrl(datasetUrn, params),
      ),
    enabled: !!datasetUrn,
    meta: { handledInline: true },
  });
}
