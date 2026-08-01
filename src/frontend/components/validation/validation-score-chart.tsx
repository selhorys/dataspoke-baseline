"use client";

/**
 * ValidationScoreChart — Recharts line chart for a dataset's quality score over time.
 *
 * Props:
 *   results  — ValidationResultRow[] from GET .../attr/validation/result
 *   height?  — chart height in px (default 200)
 *   grain?   — display grain; collapses results to one point per window (that
 *              window's last result). Display-only — it never changes what was
 *              fetched. Default: daily.
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
import {
  DEFAULT_CHART_GRAIN,
  grainTooltipLabel,
  toGrainPoints,
  type ChartGrain,
} from "@/lib/chart-grain";
import { useDisplayTz } from "@/lib/preferences/timezone";

interface ValidationScoreChartProps {
  results: ValidationResultRow[];
  height?: number;
  grain?: ChartGrain;
}

export function ValidationScoreChart({
  results,
  height = 200,
  grain = DEFAULT_CHART_GRAIN,
}: ValidationScoreChartProps) {
  const tz = useDisplayTz();

  // One point per grain window — that window's last result — ascending.
  const data = toGrainPoints(results, {
    grain,
    tz,
    timeOf: (r) => r.data_time,
    valuesOf: (r) => ({ score: r.score }),
  });

  // Empty covers both "nothing fetched" and "nothing plottable" (every row's
  // timestamp unparseable), so the user never sees bare axes.
  if (data.length === 0) {
    return (
      <div
        className="flex items-center justify-center text-sm text-muted-foreground"
        style={{ height }}
      >
        No score data for this period.
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
        <YAxis
          domain={[0, 1]}
          tick={{ fontSize: 11 }}
          tickLine={false}
          axisLine={false}
          width={40}
        />
        <Tooltip
          contentStyle={{ fontSize: 12 }}
          labelFormatter={(label) => `${grainTooltipLabel(grain)}: ${label}`}
          formatter={(value) => [typeof value === "number" ? value.toFixed(4) : value, "score"]}
        />
        <Line
          type="linear"
          dataKey="score"
          stroke="hsl(var(--brand))"
          dot={{ r: 3 }}
          activeDot={{ r: 5 }}
          strokeWidth={2}
          connectNulls
        />
      </LineChart>
    </ResponsiveContainer>
  );
}
