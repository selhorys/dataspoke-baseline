"use client";

/**
 * MetricDatasetTable — the metric detail page's Datasets panel: which datasets
 * the metric's `dataset_filter` covers, and whether each met the criterion on
 * the latest non-dry run.
 *
 * Modelled on the Ingestion SourceDatasetTable. Columns: `dataset_urn` (linked
 * to /data/[urn]), `datahub` (the shared deep-link), a `met` badge
 * (true / false / unknown), and `last check time` (shared tz helper; em dash
 * when the row has no verdict).
 *
 * The three-way toggle group drives the repeatable `met` query param and resets
 * `offset` on change. With **zero** toggles selected the panel renders its empty
 * state and issues **no request**: an omitted repeatable param and an empty one
 * are the same HTTP request, which the API reads as "all three", so the
 * no-selection case cannot be expressed on the wire and is resolved client-side.
 *
 * Beneath the table a muted line states the envelope's `attrs_synced_at`, so an
 * empty or unexpectedly small table reads as a pending attribute sync rather
 * than as a filter that matches nothing.
 *
 * Read: GET /spoke/governance/metric/{id}/dataset?met&offset&limit&sort=dataset_urn
 *
 * Spec: spec/feature/FRONTEND_GOVERNANCE.md §Metric detail (Datasets panel).
 */

import { useState } from "react";
import Link from "next/link";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { DatahubDatasetLink } from "@/components/datahub-dataset-link";
import { QueryErrorState } from "@/components/query-error-state";
import { DEFAULT_PAGE_SIZE, Pagination } from "@/components/pagination";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useMetricDatasets } from "@/lib/api/governance";
import { formatDateTime } from "@/lib/format-time";
import { useDisplayTz } from "@/lib/preferences/timezone";
import { METRIC_VERDICTS, type MetricVerdict } from "@/types/governance";

const EM_DASH = "—";

const VERDICT_VARIANT: Record<MetricVerdict, "success" | "destructive" | "secondary"> = {
  true: "success",
  false: "destructive",
  unknown: "secondary",
};

interface MetricDatasetTableProps {
  metricId: string;
}

export function MetricDatasetTable({ metricId }: MetricDatasetTableProps) {
  const tz = useDisplayTz();
  const [verdicts, setVerdicts] = useState<MetricVerdict[]>([...METRIC_VERDICTS]);
  const [offset, setOffset] = useState(0);
  const [limit, setLimit] = useState(DEFAULT_PAGE_SIZE);

  // Zero toggles is a client-side empty state, never a request.
  const hasSelection = verdicts.length > 0;

  const { data, isLoading, error } = useMetricDatasets(
    metricId,
    { met: verdicts, offset, limit, sort: "dataset_urn" },
    { enabled: hasSelection },
  );

  function toggleVerdict(verdict: MetricVerdict, checked: boolean) {
    setOffset(0);
    setVerdicts((prev) =>
      checked
        ? METRIC_VERDICTS.filter((v) => v === verdict || prev.includes(v))
        : prev.filter((v) => v !== verdict),
    );
  }

  const rows = data?.datasets ?? [];

  return (
    <div className="space-y-3">
      <div
        className="flex flex-wrap items-center gap-4"
        role="group"
        aria-label="Filter datasets by criterion verdict"
      >
        {METRIC_VERDICTS.map((verdict) => (
          <label
            key={verdict}
            className="flex cursor-pointer items-center gap-1.5 text-xs font-medium"
          >
            <Checkbox
              checked={verdicts.includes(verdict)}
              onCheckedChange={(c) => toggleVerdict(verdict, c === true)}
              aria-label={verdict}
            />
            {verdict}
          </label>
        ))}
      </div>

      {error && <QueryErrorState error={error} context="Failed to load covered datasets" />}

      {!hasSelection ? (
        <EmptyState message="No verdict selected. Check at least one of true / false / unknown to list covered datasets." />
      ) : isLoading ? (
        <Skeleton className="h-32 w-full" />
      ) : rows.length === 0 ? (
        <EmptyState message="No dataset in this metric's scope matches the selected verdicts." />
      ) : (
        <div className="rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>dataset_urn</TableHead>
                <TableHead>datahub</TableHead>
                <TableHead>met criterion</TableHead>
                <TableHead>last check time</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row) => (
                <TableRow key={row.dataset_urn}>
                  <TableCell>
                    <Link
                      href={`/data/${encodeURIComponent(row.dataset_urn)}`}
                      className="font-mono text-xs hover:underline"
                    >
                      {row.dataset_urn}
                    </Link>
                  </TableCell>
                  <TableCell>
                    <DatahubDatasetLink
                      urn={row.dataset_urn}
                      fallback={<span className="text-muted-foreground">{EM_DASH}</span>}
                    />
                  </TableCell>
                  <TableCell>
                    <Badge variant={VERDICT_VARIANT[row.met]} className="text-xs">
                      {row.met}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-sm text-muted-foreground">
                    {row.last_check_at ? formatDateTime(row.last_check_at, tz) : EM_DASH}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <p className="text-xs text-muted-foreground">
        {data?.attrs_synced_at
          ? `Scope synced ${formatDateTime(data.attrs_synced_at, tz)}`
          : "Scope never synced — dataset attributes have not been mirrored from DataHub yet."}
      </p>

      {hasSelection && (
        <Pagination
          offset={offset}
          limit={limit}
          total={data?.total_count ?? 0}
          onOffset={setOffset}
          onLimit={(next) => {
            setLimit(next);
            setOffset(0);
          }}
        />
      )}
    </div>
  );
}
