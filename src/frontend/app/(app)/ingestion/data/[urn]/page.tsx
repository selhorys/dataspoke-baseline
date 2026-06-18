"use client";

import { use, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/ui/error-state";
import { Pagination, DEFAULT_PAGE_SIZE } from "@/components/pagination";
import { RangePicker } from "@/components/range-picker";
import { EventDetailCell } from "@/components/ingestion/event-detail-cell";
import { resolveRange } from "@/lib/range";
import {
  usePersistedRangeState,
  RANGE_KEYS,
} from "@/lib/hooks/use-range-selection";
import {
  useIngestionReverseLookup,
  useIngestionDatasetEvents,
} from "@/lib/api/ingestion";
import { modeBadgeVariant, modeLabel } from "@/lib/ingestion-mode-variant";
import { eventStatusVariant } from "@/lib/event-status-variant";
import { formatDateTime } from "@/lib/format-time";
import { useDisplayTz } from "@/lib/preferences/timezone";

export default function IngestionDatasetDetailPage({
  params,
}: {
  params: Promise<{ urn: string }>;
}) {
  // Next.js returns the [urn] segment URL-decoded on server render but still
  // encoded after client-side navigation. Normalize to the raw URN so the API
  // client encodes exactly once — double-encoding yields a 422.
  const { urn: rawUrn } = use(params);
  const datasetUrn = rawUrn.startsWith("urn:") ? rawUrn : decodeURIComponent(rawUrn);

  // Persisted selection; resolving via useMemo keeps the events query key
  // stable until the selection changes.
  const tz = useDisplayTz();
  const [eventOffset, setEventOffset] = useState(0);
  const [eventLimit, setEventLimit] = useState(DEFAULT_PAGE_SIZE);
  const { selection: eventSel, setSelection: setEventSel } =
    usePersistedRangeState(RANGE_KEYS.ingestionDatasetEvents);
  const eventRange = useMemo(
    () => resolveRange(eventSel, "datetime", tz),
    [eventSel, tz],
  );

  // Reset event pagination when the time filter changes.
  useEffect(() => {
    setEventOffset(0);
  }, [eventSel]);

  const {
    data: lookup,
    isLoading,
    error,
  } = useIngestionReverseLookup(datasetUrn);
  const { data: events } = useIngestionDatasetEvents(datasetUrn, {
    offset: eventOffset,
    limit: eventLimit,
    from: eventRange.from,
    to: eventRange.to,
  });

  if (isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-96" />
        <Skeleton className="h-32 w-full" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="space-y-2">
        <ErrorState message={`Failed to load ingestion info: ${error.message}`} />
        <Button variant="outline" size="sm" asChild>
          <Link href="/ingestion/conf">Back to ingestion</Link>
        </Button>
      </div>
    );
  }

  const unmapped = !lookup || lookup.source_id === null;

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex flex-wrap items-center gap-3">
        <Link
          href="/ingestion/conf"
          className="text-muted-foreground hover:text-foreground"
          aria-label="Back to ingestion list"
        >
          <ArrowLeft className="h-4 w-4" />
        </Link>
        <h1 className="truncate font-mono text-lg font-semibold tracking-tight">
          {datasetUrn}
        </h1>
      </div>

      {/* Ingestion panel */}
      <section className="rounded-lg border p-5">
        <h2 className="mb-3 text-sm font-medium">Ingestion</h2>
        {unmapped ? (
          <p className="text-sm text-muted-foreground">
            No source covers this dataset — it is in the{" "}
            <Link href="/ingestion/unmanaged" className="underline">
              unmanaged bucket
            </Link>
            .
          </p>
        ) : (
          <dl className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <dt className="text-xs font-medium text-muted-foreground">source</dt>
              <dd className="text-sm">
                <Link
                  href={`/ingestion/sources/${encodeURIComponent(lookup!.source_id!)}`}
                  className="font-medium hover:underline"
                >
                  {lookup!.name ?? lookup!.source_id}
                </Link>
              </dd>
              {lookup!.mode && (
                <Badge variant={modeBadgeVariant(lookup!.mode)} className="text-xs">
                  {modeLabel(lookup!.mode)}
                </Badge>
              )}
            </div>

            <div>
              <dt className="text-xs font-medium text-muted-foreground">
                latest run
              </dt>
              <dd className="mt-1 text-sm">
                {lookup!.latest_run ? (
                  <span className="flex flex-wrap items-center gap-2">
                    <Badge
                      variant={eventStatusVariant(lookup!.latest_run.status)}
                      className="text-xs"
                    >
                      {lookup!.latest_run.status}
                    </Badge>
                    <span className="text-muted-foreground">
                      {formatDateTime(lookup!.latest_run.occurred_at, tz)}
                    </span>
                    {lookup!.latest_run.run_id && (
                      <span className="font-mono text-xs text-muted-foreground">
                        run_id {lookup!.latest_run.run_id}
                      </span>
                    )}
                  </span>
                ) : (
                  <span className="text-muted-foreground">
                    No run recorded yet.
                  </span>
                )}
              </dd>
            </div>
          </dl>
        )}
      </section>

      {/* Events panel */}
      <section className="space-y-3 rounded-lg border p-5">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-medium">event/ingestion</h2>
          <RangePicker
            value={eventSel}
            onChange={setEventSel}
            tz={tz}
            granularity="datetime"
          />
        </div>
        {!events && (
          <p className="text-sm text-muted-foreground">Loading events…</p>
        )}
        {events && events.events.length === 0 && (
          <p className="text-sm text-muted-foreground">No ingestion events yet.</p>
        )}
        {events && events.events.length > 0 && (
          <ul className="space-y-2">
            {events.events.map((e) => (
              <li key={e.id} className="flex flex-wrap items-start gap-3 text-sm">
                <span className="shrink-0 text-muted-foreground">
                  {formatDateTime(e.occurred_at, tz)}
                </span>
                <Badge variant={eventStatusVariant(e.status)} className="text-xs">
                  {e.status}
                </Badge>
                {e.wrapper && (
                  <Badge variant="outline" className="text-xs">
                    wrapper
                  </Badge>
                )}
                <span>{e.event_type}</span>
                <EventDetailCell detail={e.detail} />
              </li>
            ))}
          </ul>
        )}
        <Pagination
          offset={eventOffset}
          limit={eventLimit}
          total={events?.total_count ?? 0}
          onOffset={setEventOffset}
          onLimit={setEventLimit}
        />
      </section>
    </div>
  );
}
