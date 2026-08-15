/**
 * MetricForm Zod schema and pure helpers — extracted for testability.
 *
 * Spec: spec/feature/FRONTEND_GOVERNANCE.md §Metrics create/edit form,
 *       spec/API.md §Metric (/spoke/governance/metric) — metric_id pattern,
 *       §`dataset_filter` grammar (payload caps),
 *       src/api/schemas/metrics.py — _check_metric_conf_for_type, _check_metrics_series.
 */

import { z } from "zod";
import {
  METRIC_EMITTED_KEYS,
  METRIC_TIME_WINDOW_SEC_MAX,
  METRIC_TIME_WINDOW_SEC_MIN,
  METRIC_TYPES_WITH_TIME_WINDOW,
  defaultSeriesColor,
} from "@/types/governance";
import type {
  MetricFormValues,
  MetricSeries,
  MetricType,
  ScheduleTier,
} from "@/types/governance";

/**
 * metric_id regex — mirrors CreateMetricConfigRequest.metric_id pattern in
 * src/api/schemas/metrics.py:
 *   ^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$
 */
export const METRIC_ID_PATTERN = /^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$/;

/** `#RRGGBB` — mirrors MetricSeries.color's pattern in src/api/schemas/metrics.py. */
export const SERIES_COLOR_PATTERN = /^#[0-9A-Fa-f]{6}$/;

/** dataset_filter payload cap (spec/API.md §`dataset_filter` grammar). */
export const DATASET_FILTER_MAX_CHARS = 8000;

/**
 * One row of the metrics control — every emitted key of the selected
 * metric_type gets a row; only checked rows are submitted as `{name, color, idx}`.
 */
export interface MetricSeriesRow {
  name: string;
  selected: boolean;
  color: string;
  idx: number;
}

// Shared object for both edit and create — `metric_id` is always present on the
// form (empty string in edit mode, populated and validated in create mode).
export const baseObject = z.object({
  mode: z.enum(["active", "passive"]),
  metric_type: z.enum(["ingestion-freshness", "validation-score", "doc-health"]),
  title: z.string().min(1, "Title is required"),
  description: z.string().min(1, "Description is required"),
  metrics: z.array(
    z.object({
      name: z.string(),
      selected: z.boolean(),
      color: z.string(),
      idx: z.coerce.number(),
    }),
  ),
  time_window_sec: z.coerce
    .number()
    .int()
    .min(METRIC_TIME_WINDOW_SEC_MIN, "Must be a positive integer")
    .max(
      METRIC_TIME_WINDOW_SEC_MAX,
      `Must be at most ${METRIC_TIME_WINDOW_SEC_MAX} seconds (ten years)`,
    )
    .optional(),
  schedule_tier: z.enum(["hourly", "daily", "weekly"]).nullable(),
  is_enabled: z.boolean(),
  // SQL WHERE clause; the backend owns the grammar, the client owns the cap.
  dataset_filter: z
    .string()
    .max(DATASET_FILTER_MAX_CHARS, `Filter text is capped at ${DATASET_FILTER_MAX_CHARS} characters`),
  // metric_id always present on the form; validated by pattern only in create mode.
  metric_id: z.string(),
});

type BaseShape = z.infer<typeof baseObject>;

/**
 * Series refinement — mirrors _check_metrics_series in src/api/schemas/metrics.py
 * for the checked rows only: at least one key selected, `#RRGGBB` color, `idx` a
 * positive integer, `idx` unique within the metric. (`name` uniqueness is
 * structural: the control renders exactly one row per emitted key.)
 */
function checkSeries(data: BaseShape, ctx: z.RefinementCtx): void {
  const selected = data.metrics
    .map((row, index) => ({ row, index }))
    .filter(({ row }) => row.selected);

  if (selected.length === 0) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "Select at least one metric key",
      path: ["metrics"],
    });
    return;
  }

  const idxCounts = new Map<number, number>();
  for (const { row } of selected) {
    idxCounts.set(row.idx, (idxCounts.get(row.idx) ?? 0) + 1);
  }

  for (const { row, index } of selected) {
    if (!SERIES_COLOR_PATTERN.test(row.color)) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Color must be a #RRGGBB hex string",
        path: ["metrics", index, "color"],
      });
    }
    if (!Number.isInteger(row.idx) || row.idx < 1) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Order must be a positive integer",
        path: ["metrics", index, "idx"],
      });
    } else if ((idxCounts.get(row.idx) ?? 0) > 1) {
      ctx.addIssue({
        code: z.ZodIssueCode.custom,
        message: "Order must be unique within the metric",
        path: ["metrics", index, "idx"],
      });
    }
  }
}

/**
 * Shared refinements:
 *   - time_window_sec (F2 invariant): metric_type ∈ {ingestion-freshness,
 *     validation-score} → required integer in [1, METRIC_TIME_WINDOW_SEC_MAX];
 *     doc-health → metric_conf is {}.
 *   - metrics series rules (see checkSeries).
 */
export function applyMetricRefinements(data: BaseShape, ctx: z.RefinementCtx): void {
  const needsWindow = (METRIC_TYPES_WITH_TIME_WINDOW as string[]).includes(
    data.metric_type as string,
  );
  if (needsWindow && (data.time_window_sec === undefined || data.time_window_sec === null)) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      message: "time_window_sec is required for this metric type",
      path: ["time_window_sec"],
    });
  }
  checkSeries(data, ctx);
}

// Edit schema: metric_id accepted but not validated.
export const baseSchema = baseObject.superRefine(applyMetricRefinements);

// Create schema: additionally validates metric_id pattern.
export const createSchema = baseObject
  .extend({
    metric_id: z
      .string()
      .regex(METRIC_ID_PATTERN, "Use lowercase letters, digits, and hyphens (e.g. doc-health-dev)"),
  })
  .superRefine(applyMetricRefinements);

// ── Series rows ────────────────────────────────────────────────────────────────

/**
 * Builds the control's rows for `type`: one row per emitted key, seeded from the
 * matching descriptor when there is one. Unselected rows carry a sensible
 * default color and the next free order, so checking a box needs no extra input.
 *
 * Used both to seed the form and to reseed it when metric_type changes — keys
 * the new type does not emit drop out, keys it adds appear unchecked.
 */
export function seriesRowsForType(
  type: MetricType,
  series: ReadonlyArray<MetricSeries | MetricSeriesRow>,
): MetricSeriesRow[] {
  const keys: string[] = METRIC_EMITTED_KEYS[type] ?? [];
  const bySelectedName = new Map<string, MetricSeries | MetricSeriesRow>();
  for (const entry of series) {
    // A row shape carries `selected`; a descriptor is always selected.
    const isSelected = !("selected" in entry) || entry.selected;
    if (isSelected) bySelectedName.set(entry.name, entry);
  }

  const takenIdx = new Set<number>();
  for (const key of keys) {
    const match = bySelectedName.get(key);
    if (match) takenIdx.add(match.idx);
  }

  let nextIdx = 1;
  const freeIdx = (): number => {
    while (takenIdx.has(nextIdx)) nextIdx += 1;
    takenIdx.add(nextIdx);
    return nextIdx;
  };

  return keys.map((name) => {
    const match = bySelectedName.get(name);
    if (match) {
      return { name, selected: true, color: match.color, idx: match.idx };
    }
    return { name, selected: false, color: defaultSeriesColor(name), idx: freeIdx() };
  });
}

/** The checked rows as API series descriptors, in display order. */
export function toSeries(rows: ReadonlyArray<MetricSeriesRow>): MetricSeries[] {
  return rows
    .filter((row) => row.selected)
    .map(({ name, color, idx }) => ({ name, color, idx }))
    .sort((a, b) => a.idx - b.idx);
}

// ── Internal form value shape ─────────────────────────────────────────────────

/** Internal shape used by react-hook-form — time_window_sec is flat, metric_conf is absent. */
export type InternalFormValues = z.infer<typeof baseObject>;

/**
 * toInternal: flatten MetricFormValues for react-hook-form.
 * Extracts metric_conf.time_window_sec into a flat field (metric_conf is rebuilt
 * by fromInternal) and expands `metrics` into one row per emitted key.
 */
export function toInternal(v: MetricFormValues): InternalFormValues {
  const tw = v.metric_conf?.time_window_sec;
  return {
    mode: v.mode,
    metric_type: v.metric_type,
    title: v.title,
    description: v.description,
    metrics: seriesRowsForType(v.metric_type, v.metrics ?? []),
    time_window_sec: typeof tw === "number" ? tw : undefined,
    schedule_tier: v.schedule_tier,
    is_enabled: v.is_enabled,
    dataset_filter: v.dataset_filter ?? "",
    metric_id: "",
  };
}

/**
 * fromInternal: rebuild MetricFormValues from the internal form shape.
 *
 * F2 serialization invariant (mirrors src/api/schemas/metrics.py _check_metric_conf_for_type):
 *   - doc-health             → metric_conf === {}         (no time_window_sec key)
 *   - ingestion-freshness    → metric_conf === { time_window_sec: N }  (int in [1, MAX])
 *   - validation-score       → metric_conf === { time_window_sec: N }  (int in [1, MAX])
 *
 * MAX is METRIC_TIME_WINDOW_SEC_MAX (spec/feature/BACKEND.md §Measurement window).
 *
 * Any time_window_sec value present in the internal state is silently dropped for doc-health
 * to prevent a backend 422 from _check_metric_conf_for_type.
 */
export function fromInternal(v: InternalFormValues): MetricFormValues {
  const needsWindow = METRIC_TYPES_WITH_TIME_WINDOW.includes(v.metric_type as MetricType);
  const metric_conf: Record<string, unknown> =
    needsWindow && v.time_window_sec ? { time_window_sec: v.time_window_sec } : {};
  return {
    mode: v.mode as MetricFormValues["mode"],
    metric_type: v.metric_type as MetricType,
    title: v.title,
    description: v.description,
    metrics: toSeries(v.metrics),
    metric_conf,
    schedule_tier: v.schedule_tier as ScheduleTier | null,
    is_enabled: v.is_enabled,
    dataset_filter: v.dataset_filter,
  };
}
