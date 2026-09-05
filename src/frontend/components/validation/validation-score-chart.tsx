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

import { useCallback } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { TooltipContentProps } from "recharts";
import type { ValidationResultRow } from "@/types/validation";
import {
  DEFAULT_CHART_GRAIN,
  grainTooltipLabel,
  toGrainPoints,
  type ChartGrain,
  type GrainPoint,
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

  // Stable identity across renders — Recharts uses `content` as a component
  // type (React.createElement(content, props)), so a fresh inline arrow here
  // would make React unmount/remount the tooltip subtree on every render
  // (this page polls every 15s), including mid-hover.
  const renderScoreTooltip = useCallback(
    (props: TooltipContentProps) => {
      const { active, payload, label, accessibilityLayer } = props;
      if (!active || !payload || payload.length === 0) return null;
      const point = payload[0].payload as GrainPoint;
      const score = point.score;
      const note = point.score_note;
      return (
        <div
          className="max-w-[16rem] rounded-md border bg-popover px-2 py-1 text-xs text-popover-foreground shadow-md"
          {...(accessibilityLayer ? { role: "status", "aria-live": "assertive" as const } : {})}
        >
          <p>{`${grainTooltipLabel(grain)}: ${label}`}</p>
          <p>{`score: ${typeof score === "number" ? score.toFixed(4) : score}`}</p>
          {typeof note === "string" && note.length > 0 && (
            <p className="mt-0.5 break-words text-muted-foreground">{note}</p>
          )}
        </div>
      );
    },
    [grain],
  );

  // One point per grain window — that window's last result — ascending.
  const data = toGrainPoints(results, {
    grain,
    tz,
    timeOf: (r) => r.data_time,
    valuesOf: (r) => ({ score: r.score, score_note: r.score_note ?? "" }),
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
        <Tooltip content={renderScoreTooltip} />
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
