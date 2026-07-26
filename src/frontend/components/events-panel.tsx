"use client";

/**
 * EventsPanel — the unified per-dataset event timeline body: an
 * EventMajorTypeFilter (default all checked) + RangePicker + EventsTable +
 * Pagination, bound to GET /spoke/common/data/{urn}/event with
 * event_major_type / from / to / offset / limit. Renders the `wrapper` tag via
 * EventsTable. Meant to be hosted inside a CollapsiblePanel.
 *
 * Spec: spec/feature/FRONTEND_BASIC.md §Per-dataset page (Events panel).
 */

import { useEffect, useMemo, useState } from "react";
import { Skeleton } from "@/components/ui/skeleton";
import { Pagination, DEFAULT_PAGE_SIZE } from "@/components/pagination";
import { RangePicker } from "@/components/range-picker";
import { EventMajorTypeFilter } from "@/components/event-major-type-filter";
import { EventsTable } from "@/components/events-table";
import { resolveRange } from "@/lib/range";
import {
  usePersistedRangeState,
  RANGE_KEYS,
} from "@/lib/hooks/use-range-selection";
import { useDatasetEvents } from "@/lib/api/data";
import { useDisplayTz } from "@/lib/preferences/timezone";
import { EVENT_MAJOR_TYPES, type EventMajorType } from "@/types/data";

interface EventsPanelProps {
  datasetUrn: string;
}

export function EventsPanel({ datasetUrn }: EventsPanelProps) {
  const tz = useDisplayTz();

  // Default: every major type checked. An empty selection is mapped to "all"
  // when querying (omitting the filter) so the table never goes blank.
  const [majorTypes, setMajorTypes] = useState<EventMajorType[]>(() => [
    ...EVENT_MAJOR_TYPES,
  ]);
  const [offset, setOffset] = useState(0);
  const [limit, setLimit] = useState(DEFAULT_PAGE_SIZE);

  const { selection, setSelection } = usePersistedRangeState(
    RANGE_KEYS.dataEvents,
  );
  // A preset resolves open above (no `to`), so this 15 s-polled query keeps
  // reaching new events. The useMemo is still required — resolveRange reads the
  // clock for `from`, and an unmemoized call would mint a new query key every
  // render (cache miss → refetch → render → …).
  const range = useMemo(
    () => resolveRange(selection, "datetime", tz),
    [selection, tz],
  );

  // All types checked → omit the filter (server returns all). Some subset →
  // send exactly those. None checked → also omit so the table is not empty.
  const allChecked = majorTypes.length === EVENT_MAJOR_TYPES.length;
  const queryTypes =
    allChecked || majorTypes.length === 0 ? undefined : majorTypes;
  // Stable string key so the reset effect can be statically checked.
  const queryTypesKey = queryTypes?.join(",") ?? "";

  // Reset pagination when the filter or time window changes.
  useEffect(() => {
    setOffset(0);
  }, [selection, queryTypesKey]);

  const { data } = useDatasetEvents(datasetUrn, {
    offset,
    limit,
    from: range.from,
    to: range.to,
    eventMajorTypes: queryTypes,
  });

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <EventMajorTypeFilter value={majorTypes} onChange={setMajorTypes} />
        <RangePicker
          value={selection}
          onChange={setSelection}
          tz={tz}
          granularity="datetime"
        />
      </div>
      {!data ? (
        <Skeleton className="h-20 w-full" />
      ) : (
        <EventsTable
          events={data.events}
          emptyMessage="No events for this dataset in the selected window."
        />
      )}
      <Pagination
        offset={offset}
        limit={limit}
        total={data?.total_count ?? 0}
        onOffset={setOffset}
        onLimit={setLimit}
      />
    </div>
  );
}
