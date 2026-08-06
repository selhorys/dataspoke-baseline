"use client";

import { useMemo } from "react";
import { Skeleton } from "@/components/ui/skeleton";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { QueryErrorState } from "@/components/query-error-state";
import { EmptyState } from "@/components/ui/empty-state";
import { RangePicker } from "@/components/range-picker";
import { ChartGrainPicker } from "@/components/chart-grain-picker";
import { PageHeader } from "@/components/page-header";
import { resolveRange, type RangeValue } from "@/lib/range";
import {
  usePersistedRangeState,
  RANGE_KEYS,
} from "@/lib/hooks/use-range-selection";
import {
  usePersistedGrainState,
  GRAIN_KEYS,
} from "@/lib/hooks/use-grain-selection";
import {
  usePersistedMetricViewState,
  METRIC_VIEW_KEYS,
} from "@/lib/hooks/use-metric-view-selection";
import type { ChartGrain } from "@/lib/chart-grain";
import type { MetricSortDir } from "@/lib/metric-view";
import { MetricCard } from "@/components/governance/metric-card";
import { MetricTypeFilter } from "@/components/governance/metric-type-filter";
import { useEnabledMetrics } from "@/lib/api/governance";
import { useDisplayTz } from "@/lib/preferences/timezone";
import type { MetricDefinition } from "@/types/governance";

// ── Inner component (rendered after enabled metrics load) ─────────────────────

function DashboardContent({
  metrics,
  hasEnabledMetrics,
  range,
  grain,
}: {
  /** The metrics surviving the view controls, in display order. */
  metrics: MetricDefinition[];
  /** Whether the fetched enabled set held anything at all, before filtering. */
  hasEnabledMetrics: boolean;
  range: RangeValue;
  grain: ChartGrain;
}) {
  if (metrics.length === 0) {
    // Two distinct empty states: nothing enabled anywhere points at the
    // catalogue, while an empty result over a non-empty set points at the
    // reader's own view controls.
    return hasEnabledMetrics ? (
      <EmptyState message="No enabled metrics match the current type filter and title search. Adjust the controls above to see data here." />
    ) : (
      <EmptyState message="No enabled metrics. Enable a metric on the Metrics page to see data here." />
    );
  }

  return (
    <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(22rem,1fr))]">
      {metrics.map((m) => (
        // A stable key keeps card instances alive across reorders, so sorting
        // never remounts a card and never refetches its results.
        <MetricCard key={m.id} metric={m} range={range} grain={grain} />
      ))}
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function GovernanceDashboardPage() {
  const { data, isLoading, error } = useEnabledMetrics();
  // One shared, persisted range for every chart on the page; resolving via
  // useMemo keeps the per-card query keys stable until the selection changes.
  // Still required even though a preset now resolves open above — resolveRange
  // reads the clock for `from`.
  const tz = useDisplayTz();
  const { selection: sel, setSelection: setSel } = usePersistedRangeState(
    RANGE_KEYS.governanceDashboard,
  );
  const range = useMemo(() => resolveRange(sel, "date", tz), [sel, tz]);
  // One shared, persisted display grain for every card's chart. Display-only —
  // it collapses the rows already fetched and never enters a query key.
  const { grain, setGrain } = usePersistedGrainState(
    GRAIN_KEYS.governanceDashboard,
  );
  // Type filter + title search + title sort, persisted together.
  // Display-only as well: they narrow and order the already-fetched enabled set
  // and add no request parameter.
  const { view, setTypes, setSearch, setSortDir } = usePersistedMetricViewState(
    METRIC_VIEW_KEYS.governanceDashboard,
  );

  const metrics = data?.metrics;
  const visible = useMemo(() => {
    if (!metrics) return [];
    const needle = view.search.trim().toLowerCase();
    const kept = metrics.filter(
      (m) =>
        view.types.includes(m.metric_type) &&
        (needle === "" || m.title.toLowerCase().includes(needle)),
    );
    // Sort a copy — never mutate an array derived from the query cache.
    const dir = view.sortDir === "desc" ? -1 : 1;
    return [...kept].sort((a, b) => dir * a.title.localeCompare(b.title));
  }, [metrics, view.types, view.search, view.sortDir]);

  // The read is capped at limit=100; disclose when the catalogue is larger, so
  // an absent card reads as truncation rather than as a filter result.
  const capped = data !== undefined && data.total_count > data.metrics.length;

  return (
    <div className="space-y-4">
      <PageHeader
        title="Governance · Dashboard"
        actions={
          <>
            <RangePicker value={sel} onChange={setSel} tz={tz} granularity="date" />
            <ChartGrainPicker value={grain} onChange={setGrain} />
          </>
        }
      />

      {/* View controls (client-side over the fetched enabled set) */}
      <div className="flex flex-wrap items-center gap-2">
        <MetricTypeFilter value={view.types} onChange={setTypes} />
        <Input
          value={view.search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search titles…"
          aria-label="Search titles"
          className="h-9 w-[240px]"
        />
        <Select
          value={view.sortDir}
          onValueChange={(v) => setSortDir(v as MetricSortDir)}
        >
          <SelectTrigger aria-label="Sort metrics" className="h-9 w-[200px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="asc">Title A→Z</SelectItem>
            <SelectItem value="desc">Title Z→A</SelectItem>
          </SelectContent>
        </Select>
      </div>

      {isLoading && (
        <div className="grid gap-4 [grid-template-columns:repeat(auto-fit,minmax(22rem,1fr))]">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-[280px] w-full rounded-lg" />
          ))}
        </div>
      )}

      {error && (
        <QueryErrorState error={error} context="Failed to load metrics" />
      )}

      {data && capped && (
        <p className="text-sm text-muted-foreground">
          Showing the first {data.metrics.length} of {data.total_count} enabled
          metrics — filter and sort apply to these {data.metrics.length} only.
        </p>
      )}

      {data && (
        <DashboardContent
          metrics={visible}
          hasEnabledMetrics={data.metrics.length > 0}
          range={range}
          grain={grain}
        />
      )}
    </div>
  );
}
