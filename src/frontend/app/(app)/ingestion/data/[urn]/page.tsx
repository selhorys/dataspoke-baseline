"use client";

import { use } from "react";
import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/ui/error-state";
import {
  useIngestionReverseLookup,
  useIngestionDatasetEvents,
} from "@/lib/api/ingestion";
import { modeBadgeVariant, modeLabel } from "@/lib/ingestion-mode-variant";
import { eventStatusVariant } from "@/lib/event-status-variant";
import { formatDateTime } from "@/lib/format-time";

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

  const {
    data: lookup,
    isLoading,
    error,
  } = useIngestionReverseLookup(datasetUrn);
  const { data: events } = useIngestionDatasetEvents(datasetUrn, 10);

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
          <Link href="/ingestion">Back to ingestion</Link>
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
          href="/ingestion"
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
                      {formatDateTime(lookup!.latest_run.occurred_at)}
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
      <section className="rounded-lg border p-5">
        <h2 className="mb-3 text-sm font-medium">event/ingestion (latest 10)</h2>
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
                  {formatDateTime(e.occurred_at)}
                </span>
                <Badge variant={eventStatusVariant(e.status)} className="text-xs">
                  {e.status}
                </Badge>
                <span>{e.event_type}</span>
                {e.detail && Object.keys(e.detail).length > 0 && (
                  <span className="font-mono text-xs text-muted-foreground">
                    {JSON.stringify(e.detail)}
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
