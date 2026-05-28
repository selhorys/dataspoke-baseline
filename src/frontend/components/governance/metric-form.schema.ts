/**
 * MetricForm Zod schema and pure helpers — extracted for testability.
 *
 * Spec: spec/feature/FRONTEND_GOVERNANCE.md §Metrics create/edit form,
 *       spec/API.md §Metric (/spoke/governance/metric) — metric_id pattern,
 *       src/api/schemas/metrics.py — _check_metric_conf_for_type, _check_metrics_subset.
 */

import { z } from "zod";
import {
  METRIC_EMITTED_KEYS,
  METRIC_TYPES_WITH_TIME_WINDOW,
} from "@/types/governance";
import type { MetricFormValues, MetricType, ScheduleTier } from "@/types/governance";

/**
 * metric_id regex — mirrors CreateMetricConfigRequest.metric_id pattern in
 * src/api/schemas/metrics.py:
 *   ^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$
 */
export const METRIC_ID_PATTERN = /^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$/;

// Shared object for both edit and create — `metric_id` is always present on the
// form (empty string in edit mode, populated and validated in create mode).
export const baseObject = z.object({
  mode: z.enum(["active", "passive"]),
  metric_type: z.enum(["ingestion-freshness", "validation-score", "doc-health"]),
  title: z.string().min(1, "Title is required"),
  description: z.string().min(1, "Description is required"),
  metrics: z.array(z.string()).min(1, "Select at least one metric key"),
  time_window_sec: z.coerce
    .number()
    .int()
    .positive("Must be a positive integer")
    .optional(),
  schedule_tier: z.enum(["hourly", "daily", "weekly"]).nullable(),
  is_enabled: z.boolean(),
  dataset_filter: z.object({
    origin: z.string().optional(),
    tags: z.array(z.string()).optional(),
    glossary_terms: z.array(z.string()).optional(),
    dataset_urns: z.array(z.string()).optional(),
  }),
  // metric_id always present on the form; validated by pattern only in create mode.
  metric_id: z.string(),
});

/**
 * Shared time_window_sec refinement (F2 invariant):
 *   metric_type ∈ {ingestion-freshness, validation-score} → time_window_sec required positive int.
 *   doc-health → time_window_sec must be absent (metric_conf must be {}).
 */
export function applyTimeWindowRefinement<
  T extends z.ZodObject<{ metric_type: z.ZodEnum<[string, ...string[]]>; time_window_sec: z.ZodOptional<z.ZodNumber> } & Record<string, z.ZodTypeAny>>,
>(obj: T) {
  return obj.superRefine((data, ctx) => {
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
  });
}

// Edit schema: time_window_sec required when applicable; metric_id accepted but not validated.
export const baseSchema = applyTimeWindowRefinement(baseObject);

// Create schema: additionally validates metric_id pattern.
export const createSchema = applyTimeWindowRefinement(
  baseObject.extend({
    metric_id: z
      .string()
      .regex(METRIC_ID_PATTERN, "Use lowercase letters, digits, and hyphens (e.g. doc-health-dev)"),
  }),
);

/**
 * F3 invariant: prune stale metric keys when metric_type changes.
 * Given the new type and the prior selected keys, returns only keys valid for the new type.
 * Pure function — testable without React.
 */
export function pruneMetricKeys(newType: MetricType, current: string[]): string[] {
  const allowed: string[] = METRIC_EMITTED_KEYS[newType] ?? [];
  return current.filter((k) => allowed.includes(k));
}

// ── Internal form value shape ─────────────────────────────────────────────────

/** Internal shape used by react-hook-form — time_window_sec is flat, metric_conf is absent. */
export type InternalFormValues = z.infer<typeof baseObject>;

/**
 * toInternal: flatten MetricFormValues for react-hook-form.
 * Extracts metric_conf.time_window_sec into a flat field; drops metric_conf.
 *
 * Spec: metric_conf is rebuilt by fromInternal before submission.
 */
export function toInternal(v: MetricFormValues): InternalFormValues {
  const tw = v.metric_conf?.time_window_sec;
  return {
    mode: v.mode,
    metric_type: v.metric_type,
    title: v.title,
    description: v.description,
    metrics: v.metrics,
    time_window_sec: typeof tw === "number" ? tw : undefined,
    schedule_tier: v.schedule_tier,
    is_enabled: v.is_enabled,
    dataset_filter: v.dataset_filter,
    metric_id: "",
  };
}

/**
 * fromInternal: rebuild MetricFormValues from the internal form shape.
 *
 * F2 serialization invariant (mirrors src/api/schemas/metrics.py _check_metric_conf_for_type):
 *   - doc-health             → metric_conf === {}         (no time_window_sec key)
 *   - ingestion-freshness    → metric_conf === { time_window_sec: N }  (positive int)
 *   - validation-score       → metric_conf === { time_window_sec: N }  (positive int)
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
    metrics: v.metrics,
    metric_conf,
    schedule_tier: v.schedule_tier as ScheduleTier | null,
    is_enabled: v.is_enabled,
    dataset_filter: v.dataset_filter,
  };
}
