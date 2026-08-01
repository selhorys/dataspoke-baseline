"use client";

/**
 * MetricTimeseriesChart — Recharts line/area chart for a single metric's results.
 *
 * Props:
 *   results    — MetricResult[] from GET .../attr/result
 *   valueKeys  — which keys from `values` to plot (defaults to all keys in results)
 *   height?    — chart height in px (default 220)
 *   grain?     — display grain; collapses results to one point per window
 *                (that window's last measurement). Display-only — it never
 *                changes what was fetched. Default: daily.
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
import type { MetricResult } from "@/types/governance";
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
  valueKeys?: string[];
  height?: number;
  grain?: ChartGrain;
}

export function MetricTimeseriesChart({
  results,
  valueKeys,
  height = 220,
  grain = DEFAULT_CHART_GRAIN,
}: MetricTimeseriesChartProps) {
  const tz = useDisplayTz();

  // Determine all value keys across results if not supplied. `date` is the x
  // key, so a value key of that name is shadowed by the bucket label and must
  // never become a series — plotting a string would poison the auto Y domain.
  const allKeys = (
    valueKeys ??
    Array.from(new Set(results.flatMap((r) => Object.keys(r.values)))).sort()
  ).filter((k) => k !== "date");

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
            stroke={colorForKey(key, allKeys)}
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
