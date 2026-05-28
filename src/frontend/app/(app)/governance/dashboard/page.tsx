"use client";

import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/ui/error-state";
import { EmptyState } from "@/components/ui/empty-state";
import { MetricCard } from "@/components/governance/metric-card";
import { MetricTimeseriesChart } from "@/components/governance/metric-timeseries-chart";
import { useEnabledMetrics, useMetricResults } from "@/lib/api/governance";
import type { MetricDefinition } from "@/types/governance";

// ── Date window helpers ────────────────────────────────────────────────────────

function daysAgoIso(days: number): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  return d.toISOString();
}

// ── Per-metric timeseries chart (one per enabled metric) ──────────────────────

function MetricChart({ metric }: { metric: MetricDefinition }) {
  // `from` is recomputed on each render/poll tick; no `to` — open-ended (backend defaults to now).
  const from = daysAgoIso(30);
  const { data } = useMetricResults(metric.id, { from, limit: 100 });

  return (
    <div className="rounded-lg border p-4">
      <h3 className="mb-2 text-sm font-medium">{metric.title}</h3>
      <MetricTimeseriesChart results={data?.results ?? []} height={180} />
    </div>
  );
}

// ── Inner component (rendered after enabled metrics load) ─────────────────────

function DashboardContent({ metrics }: { metrics: MetricDefinition[] }) {
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
        <h2 className="mb-3 text-sm font-medium text-muted-foreground">Daily trend (last 30 d)</h2>
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {metrics.map((m) => (
            <MetricChart key={m.id} metric={m} />
          ))}
        </div>
      </section>
    </>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function GovernanceDashboardPage() {
  const { data, isLoading, error } = useEnabledMetrics();

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold tracking-tight">Governance · Dashboard</h1>

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

      {data && <DashboardContent metrics={data.metrics} />}
    </div>
  );
}
