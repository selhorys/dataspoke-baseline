/**
 * Single source of truth for chart display grain — how rows a chart has already
 * fetched are collapsed before plotting. Pure functions — no React imports; safe
 * in any context.
 *
 * A grain partitions the selected range into fixed windows (hourly / daily /
 * weekly) and each window contributes exactly ONE point: that window's LAST
 * measurement (greatest timestamp), labelled by the truncated window start. This
 * is what keeps the x-axis honest — a categorical axis over raw `YYYY-MM-DD`
 * strings turns two runs seconds apart into two identical categories pinned to
 * opposite edges of the plot; bucketing yields one distinct label per window.
 *
 * Window boundaries are derived in the global display timezone (TzMode, from
 * useDisplayTz), the same preference the RangePicker's calendar reads, so
 * switching Local↔UTC re-derives the buckets. Weekly windows start on Monday.
 *
 * Grain is a CLIENT-SIDE DISPLAY CONCERN and adds no request parameter: it never
 * alters the `from` / `to` / `until` / `limit` a call site sends, and it must
 * never enter a react-query key.
 */

import { tzParts } from "@/lib/format-time";
import type { TzMode } from "@/lib/range";

export type ChartGrain = "hourly" | "daily" | "weekly";

/** Picker order — coarsening left to right. */
export const CHART_GRAINS: readonly ChartGrain[] = ["hourly", "daily", "weekly"];

export const DEFAULT_CHART_GRAIN: ChartGrain = "daily";

/** Shape guard for safe parsing of persisted (localStorage) grains. */
export function isChartGrain(x: unknown): x is ChartGrain {
  return typeof x === "string" && (CHART_GRAINS as readonly string[]).includes(x);
}

/** Tooltip label prefix for a point's x value at the given grain. */
export function grainTooltipLabel(grain: ChartGrain): string {
  if (grain === "hourly") return "Hour";
  if (grain === "weekly") return "Week of";
  return "Date";
}

/**
 * A collapsed chart point. The x key is always `date` (the bucket label),
 * whatever the grain; the remaining keys carry that window's measured values.
 * The index signature admits `string` so the `date` label itself conforms — a
 * value key colliding with `date` is overwritten by the label, so a chart must
 * not plot a series named `date`.
 */
export interface GrainPoint {
  date: string;
  [key: string]: string | number;
}

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

/** "YYYY-MM-DD" from 0-based-month calendar fields. */
function ymd(year: number, month: number, day: number): string {
  return `${year}-${pad(month + 1)}-${pad(day)}`;
}

/**
 * Bucket label for an instant at `grain`, in the display tz. Returns null when
 * the input is not a valid date, so such rows can be skipped rather than
 * collapsed into one bogus category.
 *
 *   hourly -> "YYYY-MM-DD HH:00"   (date component included — labels must stay
 *                                   unique across a multi-day range)
 *   daily  -> "YYYY-MM-DD"
 *   weekly -> "YYYY-MM-DD" of that week's Monday
 *
 * Every format is zero-padded and hierarchical, so lexicographic order over
 * labels of one grain is chronological order.
 */
export function grainBucket(
  iso: string,
  grain: ChartGrain,
  tz: TzMode,
): string | null {
  const p = tzParts(iso, tz);
  if (!p) return null;
  if (grain === "hourly") {
    return `${ymd(p.year, p.month, p.day)} ${pad(p.hours)}:00`;
  }
  if (grain === "daily") {
    return ymd(p.year, p.month, p.day);
  }
  // Weekly: step back to Monday as pure calendar math on the wall-clock date.
  // getDay()/getUTCDay() are 0=Sunday..6=Saturday, so Monday is `weekday + 6`
  // mod 7 days back. Date.UTC normalizes the month/year underflow. Subtracting
  // milliseconds from the original instant instead would be wrong in local tz
  // across a DST boundary (a "week" is not always 7 × 24h).
  const offset = (p.weekday + 6) % 7;
  const monday = new Date(Date.UTC(p.year, p.month, p.day - offset));
  return ymd(monday.getUTCFullYear(), monday.getUTCMonth(), monday.getUTCDate());
}

/**
 * Collapse rows to one point per grain window — that window's LAST measurement
 * — sorted ascending by window.
 *
 * Rows whose timestamp is not a valid date are skipped entirely. Within a
 * window the row with the greatest instant wins; ties resolve to the later row
 * in input order.
 */
export function toGrainPoints<T>(
  rows: T[],
  opts: {
    grain: ChartGrain;
    tz: TzMode;
    timeOf: (row: T) => string;
    valuesOf: (row: T) => Record<string, number>;
  },
): GrainPoint[] {
  const { grain, tz, timeOf, valuesOf } = opts;
  const latest = new Map<string, { t: number; row: T }>();

  for (const row of rows) {
    const iso = timeOf(row);
    const bucket = grainBucket(iso, grain, tz);
    if (bucket === null) continue;
    // grainBucket already rejected invalid dates, so this is a real instant.
    const t = new Date(iso).getTime();
    const held = latest.get(bucket);
    if (held === undefined || t >= held.t) latest.set(bucket, { t, row });
  }

  return Array.from(latest.entries())
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([date, { row }]) => ({ ...valuesOf(row), date }));
}
