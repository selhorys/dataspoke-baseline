"use client";

import { useMemo } from "react";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/ui/error-state";
import { EmptyState } from "@/components/ui/empty-state";
import { RangePicker } from "@/components/range-picker";
import { resolveRange, type RangeValue } from "@/lib/range";
import {
  usePersistedRangeState,
  RANGE_KEYS,
} from "@/lib/hooks/use-range-selection";
import { MetricCard } from "@/components/governance/metric-card";
import { MetricTimeseriesChart } from "@/components/governance/metric-timeseries-chart";
import { useEnabledMetrics, useMetricResults } from "@/lib/api/governance";
import { useDisplayTz } from "@/lib/preferences/timezone";
import type { MetricDefinition } from "@/types/governance";

// ── Per-metric timeseries chart (one per enabled metric) ──────────────────────

function MetricChart({ metric, range }: { metric: MetricDefinition; range: RangeValue }) {
  const { data } = useMetricResults(metric.id, {
    from: range.from,
    to: range.to,
    limit: 100,
  });

  return (
    <div className="rounded-lg border p-4">
      <h3 className="mb-2 text-sm font-medium">{metric.title}</h3>
      <MetricTimeseriesChart results={data?.results ?? []} height={180} />
    </div>
  );
}

// ── Inner component (rendered after enabled metrics load) ─────────────────────

function DashboardContent({
  metrics,
  range,
}: {
  metrics: MetricDefinition[];
  range: RangeValue;
}) {
  if (metrics.length === 0) {
    return (
      <EmptyState message="No enabled metrics. Enable a metric on the Metrics page to see data here." />
    );
  }

  return (
    <>
      {/* Metric cards */}
      <div className="flex flex-wrap gap-4">
        {metrics.map((m) => (
          <MetricCard key={m.id} metric={m} />
        ))}
      </div>

      {/* Small multiples — one chart per metric */}
      <section className="mt-6">
        <h2 className="mb-3 text-sm font-medium text-muted-foreground">Daily trend</h2>
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {metrics.map((m) => (
            <MetricChart key={m.id} metric={m} range={range} />
          ))}
        </div>
      </section>
    </>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function GovernanceDashboardPage() {
  const { data, isLoading, error } = useEnabledMetrics();
  // One shared, persisted range for every chart on the page; resolving via
  // useMemo keeps the per-card query keys stable until the selection changes.
  const tz = useDisplayTz();
  const { selection: sel, setSelection: setSel } = usePersistedRangeState(
    RANGE_KEYS.governanceDashboard,
  );
  const range = useMemo(() => resolveRange(sel, "date", tz), [sel, tz]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">Governance · Dashboard</h1>
        <RangePicker value={sel} onChange={setSel} tz={tz} granularity="date" />
      </div>

      {isLoading && (
        <div className="flex flex-wrap gap-4">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-[120px] w-[180px] rounded-lg" />
          ))}
        </div>
      )}

      {error && (
        <ErrorState message={`Failed to load metrics: ${error.message}`} />
      )}

      {data && <DashboardContent metrics={data.metrics} range={range} />}
    </div>
  );
}
