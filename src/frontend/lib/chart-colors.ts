/**
 * Chart color tokens for Recharts.
 * All governance metric timeseries charts use this palette.
 * A design pass can restyle by editing the CSS variables these reference.
 *
 * Colors are token-derived (`hsl(var(--chart-N))`) so charts track a dedicated
 * categorical palette across light / dark modes. The chart tokens are decoupled
 * from the `--feature-*` hub-and-spoke hues (which appear only in the panel
 * spine, summary-card tick, and sidebar icon); `--chart-1` anchors to the brand
 * indigo so single-series charts stay on-brand. Recharts resolves these var()
 * strings in the SVG stroke/fill at render time.
 */

export const CHART_COLORS = [
  "hsl(var(--chart-1))",
  "hsl(var(--chart-2))",
  "hsl(var(--chart-3))",
  "hsl(var(--chart-4))",
  "hsl(var(--chart-5))",
  "hsl(var(--chart-6))",
  "hsl(var(--chart-7))",
  "hsl(var(--chart-8))",
] as const;

/** Returns a deterministic color for a given key string. */
export function colorForKey(key: string, allKeys: string[]): string {
  const idx = allKeys.indexOf(key);
  return CHART_COLORS[idx >= 0 ? idx % CHART_COLORS.length : 0];
}
