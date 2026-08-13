"use client";

/**
 * MetricTimeseriesChart — Recharts line/area chart for a single metric's results.
 *
 * Props:
 *   results    — MetricResult[] from GET .../attr/result
 *   series     — the metric's `metrics[]` series descriptors. When given, they
 *                decide both which keys are drawn and in what order (`idx`) and
 *                color (`color`).
 *   valueKeys  — which keys from `values` to plot when no descriptors are given
 *                (defaults to all keys present in results, colored by colorForKey)
 *   height?    — chart height in px (default 220)
 *   grain?     — display grain; collapses results to one point per window
 *                (that window's last measurement). Display-only — it never
 *                changes what was fetched. Default: daily.
 *
 * Spec: spec/feature/FRONTEND_GOVERNANCE.md §Dashboard / §Metric detail.
 */

import {
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { MetricResult, MetricSeries } from "@/types/governance";
import { colorForKey } from "@/lib/chart-colors";
import {
  DEFAULT_CHART_GRAIN,
  grainTooltipLabel,
  toGrainPoints,
  type ChartGrain,
} from "@/lib/chart-grain";
import { useDisplayTz } from "@/lib/preferences/timezone";

interface MetricTimeseriesChartProps {
  results: MetricResult[];
  series?: MetricSeries[];
  valueKeys?: string[];
  height?: number;
  grain?: ChartGrain;
}

export function MetricTimeseriesChart({
  results,
  series,
  valueKeys,
  height = 220,
  grain = DEFAULT_CHART_GRAIN,
}: MetricTimeseriesChartProps) {
  const tz = useDisplayTz();

  // Series descriptors, in display order — sort a copy, never the prop's array.
  const ordered = [...(series ?? [])].sort((a, b) => a.idx - b.idx);

  // Determine all value keys across results if neither descriptors nor an
  // explicit key list is supplied. `date` is the x key, so a value key of that
  // name is shadowed by the bucket label and must never become a series —
  // plotting a string would poison the auto Y domain.
  const allKeys = (
    ordered.length > 0
      ? ordered.map((s) => s.name)
      : (valueKeys ??
        Array.from(new Set(results.flatMap((r) => Object.keys(r.values)))).sort())
  ).filter((k) => k !== "date");

  // colorForKey stays the fallback for a chart drawn without descriptors.
  const colorByName = new Map(ordered.map((s) => [s.name, s.color]));
  const strokeFor = (key: string): string =>
    colorByName.get(key) ?? colorForKey(key, allKeys);

  // One point per grain window — that window's last measurement — ascending.
  const data = toGrainPoints(results, {
    grain,
    tz,
    timeOf: (r) => r.measured_at,
    valuesOf: (r) => r.values,
  });

  // Empty covers both "nothing fetched" and "nothing plottable" (every row's
  // timestamp unparseable), so the user never sees bare axes.
  if (data.length === 0) {
    return (
      <div className="flex items-center justify-center text-sm text-muted-foreground" style={{ height }}>
        No measurement data for this period.
      </div>
    );
  }

  return (
    <ResponsiveContainer width="100%" height={height}>
      <LineChart data={data} margin={{ top: 4, right: 16, bottom: 0, left: 0 }}>
        <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
        <XAxis
          dataKey="date"
          tick={{ fontSize: 11 }}
          tickLine={false}
          axisLine={false}
        />
        <YAxis tick={{ fontSize: 11 }} tickLine={false} axisLine={false} width={40} />
        <Tooltip
          contentStyle={{ fontSize: 12 }}
          labelFormatter={(label) => `${grainTooltipLabel(grain)}: ${label}`}
        />
        <Legend wrapperStyle={{ fontSize: 12 }} />
        {allKeys.map((key) => (
          <Line
            key={key}
            type="monotone"
            dataKey={key}
            stroke={strokeFor(key)}
            dot={{ r: 3 }}
            activeDot={{ r: 5 }}
            strokeWidth={2}
            connectNulls
          />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}
