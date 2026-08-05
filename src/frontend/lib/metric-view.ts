/**
 * Single source of truth for the governance dashboard's metric view state — how
 * the enabled metric set a page has already fetched is narrowed and ordered
 * before rendering. Pure functions — no React imports; safe in any context.
 *
 * The view state carries three parts: the metric types kept (`types`), a
 * case-insensitive substring matched against each metric's `description`
 * (`search`, inactive while blank), and the direction of the `description` sort
 * (`sortDir`).
 *
 * A metric view is a CLIENT-SIDE DISPLAY CONCERN and adds no request parameter:
 * it never alters the `is_enabled` / `limit` a call site sends, and it must
 * never enter a react-query key. An empty `types` selection means exactly that —
 * no metric survives — rather than falling back to every type.
 */

import { METRIC_TYPES, type MetricType } from "@/types/governance";

export type MetricSortDir = "asc" | "desc";

export interface MetricViewState {
  /** Metric types kept, in canonical METRIC_TYPES order. */
  types: MetricType[];
  /** Case-insensitive substring over `description`; blank means inactive. */
  search: string;
  /** Direction of the `description` sort. */
  sortDir: MetricSortDir;
}

export const DEFAULT_METRIC_VIEW: MetricViewState = {
  types: [...METRIC_TYPES],
  search: "",
  sortDir: "asc",
};

function isMetricType(x: unknown): x is MetricType {
  return typeof x === "string" && (METRIC_TYPES as readonly string[]).includes(x);
}

/** Shape guard for safe parsing of persisted (localStorage) view states. */
export function isMetricViewState(x: unknown): x is MetricViewState {
  if (typeof x !== "object" || x === null) return false;
  const v = x as Record<string, unknown>;
  if (!Array.isArray(v.types) || !v.types.every(isMetricType)) return false;
  if (typeof v.search !== "string") return false;
  return v.sortDir === "asc" || v.sortDir === "desc";
}
