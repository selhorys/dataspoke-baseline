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
import { formatDate } from "@/lib/format-time";
import { useDisplayTz } from "@/lib/preferences/timezone";

interface ValidationVariablesChartProps {
  results: ValidationResultRow[];
  variables?: ValidationVariable[];
  height?: number;
}

export function ValidationVariablesChart({
  results,
  variables,
  height = 160,
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

  if (results.length === 0) {
    return (
      <div
        className="flex items-center justify-center text-sm text-muted-foreground"
        style={{ height }}
      >
        No variable data for this period.
      </div>
    );
  }

  // Sort ascending by data_time for the charts.
  const sorted = [...results].sort(
    (a, b) => new Date(a.data_time).getTime() - new Date(b.data_time).getTime(),
  );

  return (
    <div className="grid grid-cols-1 gap-6">
      {charted.map((variable) => {
        const { name, description } = variable;
        const color = colorForKey(name, keys);
        const data = sorted
          .filter((r) => r.variables[name] !== undefined)
          .map((r) => ({
            date: formatDate(r.data_time, tz),
            value: r.variables[name],
          }));

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
            {data.length === 0 ? (
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
                    labelFormatter={(label) => `Date: ${label}`}
                    formatter={(value) => [value, name]}
                  />
                  <Line
                    type="linear"
                    dataKey="value"
                    stroke={color}
                    dot={false}
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
