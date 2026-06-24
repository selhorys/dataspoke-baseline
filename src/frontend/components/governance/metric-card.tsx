"use client";

/**
 * MetricCard — combined dashboard card for a single enabled metric.
 *
 * Stacks, top to bottom: the metric title, a metric_type outline badge, the
 * latest `values` dict with its measured-at date, and the metric's per-metric
 * trend chart over the shared dashboard range.
 *
 * Reads:
 *   GET /spoke/governance/metric/{id}/attr/result?limit=1            (latest stat)
 *   GET /spoke/governance/metric/{id}/attr/result?from=…&to=…&limit  (trend)
 */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import { useLatestMetricResult, useMetricResults } from "@/lib/api/governance";
import { MetricTimeseriesChart } from "@/components/governance/metric-timeseries-chart";
import { formatDate } from "@/lib/format-time";
import { useDisplayTz } from "@/lib/preferences/timezone";
import type { RangeValue } from "@/lib/range";
import type { MetricDefinition } from "@/types/governance";

interface MetricCardProps {
  metric: MetricDefinition;
  range: RangeValue;
}

export function MetricCard({ metric, range }: MetricCardProps) {
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
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium">{metric.title}</CardTitle>
        <Badge variant="outline" className="w-fit text-xs">
          {metric.metric_type}
        </Badge>
      </CardHeader>
      <CardContent className="space-y-3">
        {isLoading ? (
          <div className="space-y-1.5">
            <Skeleton className="h-4 w-24" />
            <Skeleton className="h-4 w-20" />
            <Skeleton className="h-3 w-28" />
          </div>
        ) : latest ? (
          <div className="space-y-1">
            {Object.entries(latest.values).map(([key, val]) => (
              <p key={key} className="text-sm">
                <span className="font-medium">{key}</span>
                <span className="ml-2 tabular-nums">{val}</span>
              </p>
            ))}
            <p className="mt-1 text-xs text-muted-foreground">{formatDate(latest.measured_at, tz)}</p>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">No results yet.</p>
        )}

        <MetricTimeseriesChart results={rangedData?.results ?? []} height={160} />
      </CardContent>
    </Card>
  );
}
