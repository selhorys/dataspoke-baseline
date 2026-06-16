"use client";

/**
 * MetricTimeseriesChart — Recharts line/area chart for a single metric's results.
 *
 * Props:
 *   results    — MetricResult[] from GET .../attr/result
 *   valueKeys  — which keys from `values` to plot (defaults to all keys in results)
 *   height?    — chart height in px (default 220)
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
import { formatDate } from "@/lib/format-time";
import { useDisplayTz } from "@/lib/preferences/timezone";

interface MetricTimeseriesChartProps {
  results: MetricResult[];
  valueKeys?: string[];
  height?: number;
}

export function MetricTimeseriesChart({
  results,
  valueKeys,
  height = 220,
}: MetricTimeseriesChartProps) {
  const tz = useDisplayTz();

  if (results.length === 0) {
    return (
      <div className="flex items-center justify-center text-sm text-muted-foreground" style={{ height }}>
        No measurement data for this period.
      </div>
    );
  }

  // Determine all value keys across results if not supplied.
  const allKeys =
    valueKeys ??
    Array.from(new Set(results.flatMap((r) => Object.keys(r.values)))).sort();

  // Sort ascending by measured_at for the chart.
  const sorted = [...results].sort(
    (a, b) => new Date(a.measured_at).getTime() - new Date(b.measured_at).getTime(),
  );

  const data = sorted.map((r) => ({
    date: formatDate(r.measured_at, tz),
    ...r.values,
  }));

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
          labelFormatter={(label) => `Date: ${label}`}
        />
        <Legend wrapperStyle={{ fontSize: 12 }} />
        {allKeys.map((key) => (
          <Line
            key={key}
            type="monotone"
            dataKey={key}
            stroke={colorForKey(key, allKeys)}
            dot={false}
            strokeWidth={2}
            connectNulls
          />
        ))}
      </LineChart>
    </ResponsiveContainer>
  );
}
