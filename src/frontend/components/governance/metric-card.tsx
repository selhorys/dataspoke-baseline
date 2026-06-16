"use client";

/**
 * MetricCard — shows a metric's latest values on the dashboard.
 * Fetches GET /spoke/governance/metric/{id}/attr/result?limit=1 for the latest result.
 */

import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Badge } from "@/components/ui/badge";
import { useLatestMetricResult } from "@/lib/api/governance";
import { formatDate } from "@/lib/format-time";
import { useDisplayTz } from "@/lib/preferences/timezone";
import type { MetricDefinition } from "@/types/governance";

interface MetricCardProps {
  metric: MetricDefinition;
}

export function MetricCard({ metric }: MetricCardProps) {
  const tz = useDisplayTz();
  const { data, isLoading } = useLatestMetricResult(metric.id);
  const latest = data?.results[0] ?? null;

  return (
    <Card className="min-w-[180px]">
      <CardHeader className="pb-2">
        <CardTitle className="text-sm font-medium">{metric.title}</CardTitle>
        <Badge variant="outline" className="w-fit text-xs">
          {metric.metric_type}
        </Badge>
      </CardHeader>
      <CardContent>
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
      </CardContent>
    </Card>
  );
}
