"use client";

import { useMemo } from "react";
import { Skeleton } from "@/components/ui/skeleton";
import { ErrorState } from "@/components/ui/error-state";
import { EmptyState } from "@/components/ui/empty-state";
import { RangePicker } from "@/components/range-picker";
import { PageHeader } from "@/components/page-header";
import { resolveRange, type RangeValue } from "@/lib/range";
import {
  usePersistedRangeState,
  RANGE_KEYS,
} from "@/lib/hooks/use-range-selection";
import { MetricCard } from "@/components/governance/metric-card";
import { useEnabledMetrics } from "@/lib/api/governance";
import { useDisplayTz } from "@/lib/preferences/timezone";
import type { MetricDefinition } from "@/types/governance";

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
    <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(22rem,1fr))]">
      {metrics.map((m) => (
        <MetricCard key={m.id} metric={m} range={range} />
      ))}
    </div>
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
      <PageHeader
        title="Governance · Dashboard"
        actions={
          <RangePicker value={sel} onChange={setSel} tz={tz} granularity="date" />
        }
      />

      {isLoading && (
        <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(22rem,1fr))]">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-[280px] w-full rounded-lg" />
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
