"use client";

/**
 * MetricCard — combined dashboard card for a single enabled metric.
 *
 * The header carries the metric `title` on the left and, top-right at a smaller
 * size, the `metric_type` outline badge beside a `Details` button linking to
 * /governance/metrics/{id}; `description` sits under the title in small muted
 * text. The body stacks the latest `values` dict with its measured-at date and
 * the metric's trend chart — one line per `metrics[]` series descriptor, in
 * `idx` order, stroked with each descriptor's `color`.
 *
 * `description` and `metrics` both come from the list read that produced
 * `metric`, so the card needs no extra fetch for either.
 *
 * Reads:
 *   GET /spoke/governance/metric/{id}/attr/result?limit=1            (latest stat)
 *   GET /spoke/governance/metric/{id}/attr/result?from=…&to=…&limit  (trend)
 *
 * Spec: spec/feature/FRONTEND_GOVERNANCE.md §Dashboard.
 */

import Link from "next/link";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import { useLatestMetricResult, useMetricResults } from "@/lib/api/governance";
import { MetricTimeseriesChart } from "@/components/governance/metric-timeseries-chart";
import { formatDate } from "@/lib/format-time";
import { useDisplayTz } from "@/lib/preferences/timezone";
import type { RangeValue } from "@/lib/range";
import { DEFAULT_CHART_GRAIN, type ChartGrain } from "@/lib/chart-grain";
import type { MetricDefinition } from "@/types/governance";

interface MetricCardProps {
  metric: MetricDefinition;
  range: RangeValue;
  /** Display grain for the trend chart — passthrough, no effect on the reads. */
  grain?: ChartGrain;
}

export function MetricCard({
  metric,
  range,
  grain = DEFAULT_CHART_GRAIN,
}: MetricCardProps) {
  const tz = useDisplayTz();
  const { data, isLoading } = useLatestMetricResult(metric.id);
  const latest = data?.results[0] ?? null;

  const { data: rangedData } = useMetricResults(metric.id, {
    from: range.from,
    to: range.to,
    limit: 100,
  });

  return (
    <Card className="w-full" data-testid={`metric-card-${metric.id}`}>
      <CardHeader className="gap-1.5 pb-3">
        <div className="flex items-start justify-between gap-2">
          <CardTitle className="text-lg font-semibold leading-tight">
            {metric.title}
          </CardTitle>
          {/* Two controls share this slot; distinct keys keep React from
              reusing one node as the other across re-renders. */}
          <div className="flex shrink-0 items-center gap-1.5">
            <Badge
              key="metric-type-badge"
              variant="outline"
              className="px-1.5 py-0 text-[10px] font-medium"
            >
              {metric.metric_type}
            </Badge>
            <Button
              key="metric-details-link"
              asChild
              variant="outline"
              size="sm"
              className="h-6 px-2 text-xs"
            >
              <Link href={`/governance/metrics/${encodeURIComponent(metric.id)}`}>
                Details
              </Link>
            </Button>
          </div>
        </div>
        {metric.description && (
          <p className="text-xs text-muted-foreground">{metric.description}</p>
        )}
      </CardHeader>
      <CardContent className="space-y-4">
        {isLoading ? (
          <div className="space-y-2">
            <Skeleton className="h-5 w-28" />
            <Skeleton className="h-3 w-24" />
          </div>
        ) : latest ? (
          <div className="space-y-2">
            <div className="flex flex-wrap gap-x-6 gap-y-2">
              {Object.entries(latest.values).map(([key, val]) => (
                <div key={key} className="flex flex-col">
                  <span className="text-xs text-muted-foreground">{key}</span>
                  <span className="text-xl font-semibold tabular-nums leading-tight">
                    {val}
                  </span>
                </div>
              ))}
            </div>
            <p className="text-xs text-muted-foreground">
              {formatDate(latest.measured_at, tz)}
            </p>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">No results yet.</p>
        )}

        <MetricTimeseriesChart
          results={rangedData?.results ?? []}
          series={metric.metrics}
          height={160}
          grain={grain}
        />
      </CardContent>
    </Card>
  );
}
