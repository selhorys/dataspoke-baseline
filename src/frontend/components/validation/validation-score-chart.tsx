"use client";

/**
 * ValidationScoreChart — Recharts line chart for a dataset's quality score over time.
 *
 * Props:
 *   results  — ValidationResultRow[] from GET .../attr/validation/result
 *   height?  — chart height in px (default 200)
 */

import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { ValidationResultRow } from "@/types/validation";
import { formatDate } from "@/lib/format-time";
import { useDisplayTz } from "@/lib/preferences/timezone";

interface ValidationScoreChartProps {
  results: ValidationResultRow[];
  height?: number;
}

export function ValidationScoreChart({
  results,
  height = 200,
}: ValidationScoreChartProps) {
  const tz = useDisplayTz();

  if (results.length === 0) {
    return (
      <div
        className="flex items-center justify-center text-sm text-muted-foreground"
        style={{ height }}
      >
        No score data for this period.
      </div>
    );
  }

  // Sort ascending by data_time for the chart.
  const sorted = [...results].sort(
    (a, b) => new Date(a.data_time).getTime() - new Date(b.data_time).getTime(),
  );

  const data = sorted.map((r) => ({
    date: formatDate(r.data_time, tz),
    score: r.score,
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
        <YAxis
          domain={[0, 1]}
          tick={{ fontSize: 11 }}
          tickLine={false}
          axisLine={false}
          width={40}
        />
        <Tooltip
          contentStyle={{ fontSize: 12 }}
          labelFormatter={(label) => `Date: ${label}`}
          formatter={(value) => [typeof value === "number" ? value.toFixed(4) : value, "score"]}
        />
        <Line
          type="linear"
          dataKey="score"
          stroke="hsl(var(--brand))"
          dot={false}
          strokeWidth={2}
          connectNulls
        />
      </LineChart>
    </ResponsiveContainer>
  );
}
