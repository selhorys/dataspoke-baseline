"use client";

/**
 * ValidationVariablesChart — small multiples of per-variable values over time.
 *
 * Renders one auto-scaled line chart per declared variable, stacked in a
 * single full-width column (one chart per row). Each chart is captioned with
 * the variable's name and description so differing value scales do not flatten
 * each other on a shared Y-axis.
 *
 * Props:
 *   results   — ValidationResultRow[] from GET .../attr/validation/result
 *   variables — declared variables from the conf ({ name, description }).
 *               Determines chart ordering and captions. If empty, falls back
 *               to all variable names observed across the results.
 *   height?   — per-chart height in px (default 160)
 *   grain?    — display grain; collapses results to one point per window (that
 *               window's last result). Display-only — it never changes what was
 *               fetched, and it is shared with the Quality Score chart so both
 *               stay in lockstep. Default: daily.
 *
 * Spec: spec/feature/FRONTEND_VALIDATION.md §Page contracts (small multiples).
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
import type { ValidationResultRow, ValidationVariable } from "@/types/validation";
import { colorForKey } from "@/lib/chart-colors";
import {
  DEFAULT_CHART_GRAIN,
  grainTooltipLabel,
  toGrainPoints,
  type ChartGrain,
} from "@/lib/chart-grain";
import { useDisplayTz } from "@/lib/preferences/timezone";

interface ValidationVariablesChartProps {
  results: ValidationResultRow[];
  variables?: ValidationVariable[];
  height?: number;
  grain?: ChartGrain;
}

export function ValidationVariablesChart({
  results,
  variables,
  height = 160,
  grain = DEFAULT_CHART_GRAIN,
}: ValidationVariablesChartProps) {
  const tz = useDisplayTz();

  // Resolve the ordered list of variables to chart. Prefer the declared conf
  // variables; otherwise synthesize bare entries from observed result keys.
  const charted: ValidationVariable[] =
    variables && variables.length > 0
      ? variables
      : Array.from(new Set(results.flatMap((r) => Object.keys(r.variables))))
          .sort()
          .map((name) => ({ name, description: "" }));

  const keys = charted.map((v) => v.name);

  // Bucket ONCE over every result, then project each small multiple from that
  // shared point set — so all stacked charts carry the identical x category
  // set and a given window sits at the same horizontal position in each. A
  // window whose last result omits a variable leaves that series undefined
  // there; connectNulls bridges the gap.
  const points = toGrainPoints(results, {
    grain,
    tz,
    timeOf: (r) => r.data_time,
    valuesOf: (r) => r.variables,
  });

  // Empty covers both "nothing fetched" and "nothing plottable" (every row's
  // timestamp unparseable), so the user never sees bare axes.
  if (points.length === 0) {
    return (
      <div
        className="flex items-center justify-center text-sm text-muted-foreground"
        style={{ height }}
      >
        No variable data for this period.
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 gap-6">
      {charted.map((variable) => {
        const { name, description } = variable;
        const color = colorForKey(name, keys);
        const data = points.map((p) => ({ date: p.date, value: p[name] }));
        const hasData = data.some((d) => d.value !== undefined);

        return (
          <div key={name} className="space-y-1">
            <div className="min-w-0">
              <p className="truncate font-mono text-xs font-medium" title={name}>
                {name}
              </p>
              {description && (
                <p
                  className="truncate text-xs text-muted-foreground"
                  title={description}
                >
                  {description}
                </p>
              )}
            </div>
            {!hasData ? (
              <div
                className="flex items-center justify-center text-xs text-muted-foreground"
                style={{ height }}
              >
                No data
              </div>
            ) : (
              <ResponsiveContainer width="100%" height={height}>
                <LineChart
                  data={data}
                  margin={{ top: 4, right: 12, bottom: 0, left: 0 }}
                >
                  <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                  <XAxis
                    dataKey="date"
                    tick={{ fontSize: 10 }}
                    tickLine={false}
                    axisLine={false}
                  />
                  <YAxis
                    tick={{ fontSize: 10 }}
                    tickLine={false}
                    axisLine={false}
                    width={44}
                    domain={["auto", "auto"]}
                  />
                  <Tooltip
                    contentStyle={{ fontSize: 12 }}
                    labelFormatter={(label) => `${grainTooltipLabel(grain)}: ${label}`}
                    formatter={(value) => [value, name]}
                  />
                  <Line
                    type="linear"
                    dataKey="value"
                    stroke={color}
                    dot={{ r: 3 }}
                    activeDot={{ r: 5 }}
                    strokeWidth={2}
                    connectNulls
                  />
                </LineChart>
              </ResponsiveContainer>
            )}
          </div>
        );
      })}
    </div>
  );
}
