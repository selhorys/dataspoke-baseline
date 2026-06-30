"use client";

/**
 * Unified per-dataset hub — /data/[urn].
 *
 * Merges the formerly separate /ingestion/data, /validation/data and
 * /metagen/data surfaces into one page: a header (dataset URN + DataHub
 * deep-link), three summary Cards (Ingestion / Validation / MetaGen), and three
 * CollapsiblePanels — Validation, MetaGen (each the per-feature body, event list
 * removed) and Events (the unified timeline with a major-type filter). The
 * ingestion reverse-lookup folds into the Ingestion summary card.
 *
 * Spec: spec/feature/FRONTEND_BASIC.md §Per-dataset page.
 */

import { use } from "react";
import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { PageHeader } from "@/components/page-header";
import { CollapsiblePanel } from "@/components/collapsible-panel";
import { EventsPanel } from "@/components/events-panel";
import { DatahubDatasetLink } from "@/components/datahub-dataset-link";
import { ValidationDataPanel } from "@/components/validation/validation-data-panel";
import { MetagenDataPanel } from "@/components/metagen/metagen-data-panel";
import { useIngestionReverseLookup } from "@/lib/api/ingestion";
import { useValidationConf, useValidationResults } from "@/lib/api/validation";
import { useMetagenBoundary, useMetagenItems } from "@/lib/api/metagen";
import { ApiError } from "@/lib/api/client";
import { eventStatusVariant } from "@/lib/event-status-variant";
import { modeBadgeVariant, modeLabel } from "@/lib/ingestion-mode-variant";
import { scoreBadgeVariant, scoreLabel } from "@/lib/validation-score";
import { formatDateTime } from "@/lib/format-time";
import { useDisplayTz } from "@/lib/preferences/timezone";

// ── Summary cards ────────────────────────────────────────────────────────────────

function IngestionSummaryCard({ datasetUrn }: { datasetUrn: string }) {
  const tz = useDisplayTz();
  const { data: lookup, isLoading } = useIngestionReverseLookup(datasetUrn);
  const unmapped = !isLoading && (!lookup || lookup.source_id === null);

  return (
    <Card className="border-t-2 border-t-feature-ingestion">
      <CardHeader className="p-4 pb-2">
        <CardTitle className="text-sm font-medium">Ingestion</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 p-4 pt-0 text-sm">
        {isLoading && (
          <span className="text-muted-foreground">Loading…</span>
        )}
        {!isLoading && unmapped && (
          <span className="text-muted-foreground">Unmanaged</span>
        )}
        {!isLoading && !unmapped && lookup && (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <Link
                href={`/ingestion/sources/${encodeURIComponent(lookup.source_id!)}`}
                className="truncate font-medium hover:underline"
              >
                {lookup.name ?? lookup.source_id}
              </Link>
              {lookup.mode && (
                <Badge variant={modeBadgeVariant(lookup.mode)} className="text-xs">
                  {modeLabel(lookup.mode)}
                </Badge>
              )}
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {lookup.latest_run ? (
                <>
                  <Badge
                    variant={eventStatusVariant(lookup.latest_run.status)}
                    className="text-xs"
                  >
                    last run {lookup.latest_run.status}
                  </Badge>
                  <span className="text-xs text-muted-foreground">
                    {formatDateTime(lookup.latest_run.occurred_at, tz)}
                  </span>
                </>
              ) : (
                <span className="text-xs text-muted-foreground">
                  No run recorded
                </span>
              )}
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

function ValidationSummaryCard({ datasetUrn }: { datasetUrn: string }) {
  const tz = useDisplayTz();
  const { data: conf, isLoading, error } = useValidationConf(datasetUrn);
  const { data: resultsData } = useValidationResults(datasetUrn, { limit: 1 });
  const is404 = error instanceof ApiError && error.status === 404;
  const showConf = conf && !is404;
  const latest = resultsData?.results?.[0] ?? null;
  const latestScore = latest?.score ?? null;

  return (
    <Card className="border-t-2 border-t-feature-validation">
      <CardHeader className="p-4 pb-2">
        <CardTitle className="text-sm font-medium">Validation</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 p-4 pt-0 text-sm">
        {isLoading && <span className="text-muted-foreground">Loading…</span>}
        {!isLoading && (
          <>
            {showConf ? (
              <span className="text-muted-foreground">
                {conf.variables.length} variable
                {conf.variables.length === 1 ? "" : "s"}
              </span>
            ) : (
              <span className="text-muted-foreground">No config</span>
            )}
            <div className="flex flex-wrap items-center gap-2">
              {latestScore !== null ? (
                <>
                  <Badge variant={scoreBadgeVariant(latestScore)} className="text-xs">
                    Latest score {scoreLabel(latestScore)}
                  </Badge>
                  {latest?.data_time && (
                    <span className="text-xs text-muted-foreground">
                      {formatDateTime(latest.data_time, tz)}
                    </span>
                  )}
                </>
              ) : (
                <span className="text-xs text-muted-foreground">No score yet</span>
              )}
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

function MetagenSummaryCard({ datasetUrn }: { datasetUrn: string }) {
  const { data: boundary, isLoading } = useMetagenBoundary(datasetUrn);
  const { data: itemsData } = useMetagenItems(datasetUrn);
  const itemCount = itemsData?.total_count ?? itemsData?.items.length ?? 0;

  return (
    <Card className="border-t-2 border-t-feature-metagen">
      <CardHeader className="p-4 pb-2">
        <CardTitle className="text-sm font-medium">MetaGen</CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 p-4 pt-0 text-sm">
        {isLoading && <span className="text-muted-foreground">Loading…</span>}
        {!isLoading && (
          <>
            <div>
              {boundary ? (
                <Badge
                  variant={boundary.is_enabled ? "default" : "secondary"}
                  className="text-xs"
                >
                  {boundary.is_enabled ? "enabled" : "disabled"}
                </Badge>
              ) : (
                <span className="text-muted-foreground">No boundary</span>
              )}
            </div>
            <span className="text-xs text-muted-foreground">
              {itemCount} candidate item{itemCount === 1 ? "" : "s"}
            </span>
          </>
        )}
      </CardContent>
    </Card>
  );
}

// ── Page ─────────────────────────────────────────────────────────────────────────

export default function DatasetHubPage({
  params,
}: {
  params: Promise<{ urn: string }>;
}) {
  // Next.js returns the [urn] segment URL-decoded on server render but still
  // encoded after client-side navigation. Normalize to the raw URN so the API
  // client encodes exactly once — double-encoding yields a 422.
  const { urn: rawUrn } = use(params);
  const datasetUrn = rawUrn.startsWith("urn:")
    ? rawUrn
    : decodeURIComponent(rawUrn);

  return (
    <div className="space-y-6">
      {/* Header */}
      <PageHeader
        title={datasetUrn}
        titleClassName="font-mono text-lg"
        backHref="/governance/dashboard"
        backLabel="Back to dashboard"
        actions={<DatahubDatasetLink urn={datasetUrn} />}
      />

      {/* Summary cards */}
      <div className="grid gap-4 sm:grid-cols-3">
        <IngestionSummaryCard datasetUrn={datasetUrn} />
        <ValidationSummaryCard datasetUrn={datasetUrn} />
        <MetagenSummaryCard datasetUrn={datasetUrn} />
      </div>

      {/* Foldable feature panels */}
      <CollapsiblePanel title="Validation" accent="validation">
        <ValidationDataPanel datasetUrn={datasetUrn} />
      </CollapsiblePanel>

      <CollapsiblePanel title="MetaGen" accent="metagen">
        <MetagenDataPanel datasetUrn={datasetUrn} />
      </CollapsiblePanel>

      <CollapsiblePanel title="Events">
        <EventsPanel datasetUrn={datasetUrn} />
      </CollapsiblePanel>
    </div>
  );
}
