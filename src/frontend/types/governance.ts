/**
 * Governance domain types — derived from src/api/schemas/metrics.py.
 */

export type MetricType = "ingestion-freshness" | "validation-score" | "doc-health";
export type MetricMode = "active" | "passive";
export type ScheduleTier = "hourly" | "daily" | "weekly";

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

export interface DatasetFilter {
  origin?: string;
  tags?: string[];
  glossary_terms?: string[];
  dataset_urns?: string[];
}

export interface MetricDefinition {
  id: string;
  mode: MetricMode;
  is_enabled: boolean;
  metric_type: MetricType;
  title: string;
  description: string;
  metrics: string[];
  metric_conf: Record<string, unknown>;
  schedule_tier: ScheduleTier | null;
  dataset_filter: DatasetFilter;
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

// ── Form types ─────────────────────────────────────────────────────────────────

export interface MetricFormValues {
  mode: MetricMode;
  metric_type: MetricType;
  title: string;
  description: string;
  metrics: string[];
  metric_conf: Record<string, unknown>;
  schedule_tier: ScheduleTier | null;
  is_enabled: boolean;
  dataset_filter: DatasetFilter;
}

export interface CreateMetricFormValues extends MetricFormValues {
  metric_id: string;
}
