"use client";

/**
 * ValidationVariablesChart — Recharts line chart for per-variable values over time.
 *
 * Features:
 *   - One line per declared/observed variable.
 *   - Checkbox legend allows toggling visibility of each variable's series.
 *   - Colors are deterministic via colorForKey().
 *
 * Props:
 *   results      — ValidationResultRow[] from GET .../attr/validation/result
 *   allVariables — variable names from the conf (determines ordering and legend entries).
 *                  If empty, falls back to all keys observed in results.
 *   height?      — chart height in px (default 220)
 */

import { useEffect, useState } from "react";
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
import { colorForKey } from "@/lib/chart-colors";
import { toggleVisibleKey, syncVisibleKeys } from "@/lib/validation-chart-toggle";

interface ValidationVariablesChartProps {
  results: ValidationResultRow[];
  allVariables?: string[];
  height?: number;
}

function formatDate(iso: string): string {
  const d = new Date(iso);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export function ValidationVariablesChart({
  results,
  allVariables,
  height = 220,
}: ValidationVariablesChartProps) {
  // Derive the full key set from conf variables or observed keys.
  const derivedKeys =
    allVariables && allVariables.length > 0
      ? allVariables
      : Array.from(
          new Set(results.flatMap((r) => Object.keys(r.variables))),
        ).sort();

  // Visibility state: all visible by default.
  const [visibleKeys, setVisibleKeys] = useState<Set<string>>(
    () => new Set(derivedKeys),
  );

  // Sync: when derivedKeys gains new entries (e.g. after a conf edit + refetch),
  // add them to visibleKeys. Existing user toggles for keys already tracked are preserved.
  useEffect(() => {
    setVisibleKeys((prev) => syncVisibleKeys(prev, derivedKeys));
  }, [derivedKeys.join(",")]); // eslint-disable-line react-hooks/exhaustive-deps

  const toggleKey = (key: string) => {
    setVisibleKeys((prev) => toggleVisibleKey(prev, key));
  };

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

  // Sort ascending by data_time.
  const sorted = [...results].sort(
    (a, b) => new Date(a.data_time).getTime() - new Date(b.data_time).getTime(),
  );

  const data = sorted.map((r) => {
    const row: Record<string, string | number> = { date: formatDate(r.data_time) };
    derivedKeys.forEach((key) => {
      const val = r.variables[key];
      if (val !== undefined) {
        row[key] = val;
      }
    });
    return row;
  });

  const activeKeys = derivedKeys.filter((k) => visibleKeys.has(k));

  return (
    <div className="space-y-3">
      {/* Toggleable legend */}
      <div className="flex flex-wrap gap-3">
        {derivedKeys.map((key) => {
          const color = colorForKey(key, derivedKeys);
          const active = visibleKeys.has(key);
          return (
            <button
              key={key}
              type="button"
              onClick={() => toggleKey(key)}
              className={`inline-flex items-center gap-1.5 rounded px-2 py-0.5 text-xs transition-opacity ${
                active ? "opacity-100" : "opacity-40"
              }`}
              aria-pressed={active}
              title={active ? `Hide ${key}` : `Show ${key}`}
            >
              <span
                className="inline-block h-2.5 w-2.5 shrink-0 rounded-full"
                style={{ backgroundColor: color }}
              />
              <span className="font-mono">{key}</span>
            </button>
          );
        })}
      </div>

      {/* Chart */}
      <ResponsiveContainer width="100%" height={height}>
        <LineChart data={data} margin={{ top: 4, right: 16, bottom: 0, left: 0 }}>
          <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
          <XAxis
            dataKey="date"
            tick={{ fontSize: 11 }}
            tickLine={false}
            axisLine={false}
          />
          <YAxis tick={{ fontSize: 11 }} tickLine={false} axisLine={false} width={48} />
          <Tooltip
            contentStyle={{ fontSize: 12 }}
            labelFormatter={(label) => `Date: ${label}`}
          />
          {activeKeys.map((key) => (
            <Line
              key={key}
              type="monotone"
              dataKey={key}
              stroke={colorForKey(key, derivedKeys)}
              dot={false}
              strokeWidth={2}
              connectNulls
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
