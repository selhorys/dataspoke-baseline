"use client";

/**
 * IngestionDataPanel — the reverse-lookup body for the unified /data/[urn] page:
 * the owning source link + mode badge + latest-run summary, or an unmanaged
 * notice. The per-dataset ingestion event list lives in the shared Events panel.
 *
 * Spec: spec/feature/FRONTEND_INGESTION.md §Per-dataset (moved to /data/[urn]).
 */

import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/ui/error-state";
import { useIngestionReverseLookup } from "@/lib/api/ingestion";
import { modeBadgeVariant, modeLabel } from "@/lib/ingestion-mode-variant";
import { eventStatusVariant } from "@/lib/event-status-variant";
import { formatDateTime } from "@/lib/format-time";
import { useDisplayTz } from "@/lib/preferences/timezone";

interface IngestionDataPanelProps {
  datasetUrn: string;
}

export function IngestionDataPanel({ datasetUrn }: IngestionDataPanelProps) {
  const tz = useDisplayTz();
  const { data: lookup, isLoading, error } =
    useIngestionReverseLookup(datasetUrn);

  if (isLoading) {
    return <Skeleton className="h-24 w-full" />;
  }

  if (error) {
    return (
      <ErrorState message={`Failed to load ingestion info: ${error.message}`} />
    );
  }

  const unmapped = !lookup || lookup.source_id === null;

  if (unmapped) {
    return (
      <p className="text-sm text-muted-foreground">
        No source covers this dataset — it is in the{" "}
        <Link href="/ingestion/unmanaged" className="underline">
          unmanaged bucket
        </Link>
        .
      </p>
    );
  }

  return (
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
        <dt className="text-xs font-medium text-muted-foreground">latest run</dt>
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
            <span className="text-muted-foreground">No run recorded yet.</span>
          )}
        </dd>
      </div>
    </dl>
  );
}
