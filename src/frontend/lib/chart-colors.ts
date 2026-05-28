/**
 * Chart color tokens for Recharts.
 * All governance metric timeseries charts use this palette.
 * A design pass can restyle by editing this single file.
 */

export const CHART_COLORS = [
  "#6366f1", // indigo-500
  "#22c55e", // green-500
  "#f59e0b", // amber-500
  "#3b82f6", // blue-500
  "#ec4899", // pink-500
  "#14b8a6", // teal-500
  "#f97316", // orange-500
  "#8b5cf6", // violet-500
] as const;

/** Returns a deterministic color for a given key string. */
export function colorForKey(key: string, allKeys: string[]): string {
  const idx = allKeys.indexOf(key);
  return CHART_COLORS[idx >= 0 ? idx % CHART_COLORS.length : 0];
}
