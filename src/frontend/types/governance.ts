/**
 * Governance domain types — derived from src/api/schemas/metrics.py.
 */

export type MetricType = "ingestion-freshness" | "validation-score" | "doc-health";
export type MetricMode = "active" | "passive";
export type ScheduleTier = "hourly" | "daily" | "weekly";

/** Canonical metric-type order for type-list controls. */
export const METRIC_TYPES: readonly MetricType[] = [
  "ingestion-freshness",
  "validation-score",
  "doc-health",
] as const;

/** Keys emitted by each built-in metric type (mirrors _EMITTED_KEYS in the backend). */
export const METRIC_EMITTED_KEYS: Record<MetricType, string[]> = {
  "ingestion-freshness": ["total", "ingested_in_time"],
  "validation-score": ["total", "validation_score_sum"],
  "doc-health": ["total", "doc_health"],
};

/** Metric types that require time_window_sec in metric_conf. */
export const METRIC_TYPES_WITH_TIME_WINDOW: MetricType[] = [
  "ingestion-freshness",
  "validation-score",
];

/**
 * One chart series of a metric — which emitted key to draw, in what color, at
 * what display position. The dashboard draws one line per descriptor, in `idx`
 * order, stroked with `color` (spec/API.md §Metric — Definition body).
 */
export interface MetricSeries {
  /** One of the metric type's emitted keys; unique within the metric. */
  name: string;
  /** Line color as a `#RRGGBB` hex string. */
  color: string;
  /** 1-based display order; unique within the metric. */
  idx: number;
}

/**
 * Default series colors, mirroring the backend's factory defaults
 * (src/backend/metrics/bootstrap.py): the shared `total` baseline is slate and
 * each type's own key takes a distinct hue.
 */
export const METRIC_SERIES_DEFAULT_COLORS: Record<string, string> = {
  total: "#64748B",
  ingested_in_time: "#22C55E",
  validation_score_sum: "#3B82F6",
  doc_health: "#A855F7",
};

/** Fallback palette for an emitted key with no factory default. */
export const METRIC_SERIES_FALLBACK_COLOR = "#64748B";

export function defaultSeriesColor(name: string): string {
  return METRIC_SERIES_DEFAULT_COLORS[name] ?? METRIC_SERIES_FALLBACK_COLOR;
}

export interface MetricDefinition {
  id: string;
  mode: MetricMode;
  is_enabled: boolean;
  metric_type: MetricType;
  title: string;
  description: string;
  metrics: MetricSeries[];
  metric_conf: Record<string, unknown>;
  schedule_tier: ScheduleTier | null;
  /** SQL `WHERE`-clause string; empty matches every registered dataset. */
  dataset_filter: string;
  created_at: string;
  updated_at: string;
}

/**
 * List-only row shape. Adds `last_run_at` (derived from the latest
 * METRIC.RUN_COMPLETE event) — present only on the list response, not on the
 * single-GET / CRUD MetricDefinition.
 */
export interface MetricDefinitionListItem extends MetricDefinition {
  last_run_at: string | null;
}

export interface MetricDefinitionListResponse {
  offset: number;
  limit: number;
  total_count: number;
  metrics: MetricDefinitionListItem[];
}

export interface MetricResult {
  id: string;
  metric_id: string;
  values: Record<string, number>;
  measured_at: string;
}

export interface MetricResultListResponse {
  offset: number;
  limit: number;
  total_count: number;
  results: MetricResult[];
}

export interface MetricRunResult {
  run_id: string;
  status: string;
  detail: Record<string, unknown>;
}

export interface MetricEvent {
  id: string;
  entity_type: string;
  entity_id: string;
  event_type: string;
  status: string;
  detail: Record<string, unknown>;
  occurred_at: string;
}

export interface MetricEventListResponse {
  offset: number;
  limit: number;
  total_count: number;
  events: MetricEvent[];
}

// ── Covered datasets (GET .../metric/{id}/dataset) ─────────────────────────────

/**
 * Per-dataset verdict. `"unknown"` means the dataset is in the filter's scope
 * but carries no verdict — the metric has never run, or the dataset entered
 * scope after the last run.
 */
export type MetricVerdict = "true" | "false" | "unknown";

/** Canonical toggle order for the Datasets panel's verdict filter. */
export const METRIC_VERDICTS: readonly MetricVerdict[] = ["true", "false", "unknown"] as const;

export interface MetricDatasetRow {
  dataset_urn: string;
  met: MetricVerdict;
  /** Per-dataset evidence time, falling back to the run's measured_at. */
  last_check_at: string | null;
  detail: Record<string, unknown> | null;
}

export interface MetricDatasetListResponse {
  offset: number;
  limit: number;
  total_count: number;
  datasets: MetricDatasetRow[];
  /**
   * Newest `dataset_registry.attrs_synced_at` over the datasets in scope — how
   * fresh the attributes the scope was filtered against are. Scope-relative and
   * unaffected by `met` filtering or paging.
   */
  attrs_synced_at: string | null;
}

// ── Form types ─────────────────────────────────────────────────────────────────

export interface MetricFormValues {
  mode: MetricMode;
  metric_type: MetricType;
  title: string;
  description: string;
  metrics: MetricSeries[];
  metric_conf: Record<string, unknown>;
  schedule_tier: ScheduleTier | null;
  is_enabled: boolean;
  dataset_filter: string;
}

export interface CreateMetricFormValues extends MetricFormValues {
  metric_id: string;
}
