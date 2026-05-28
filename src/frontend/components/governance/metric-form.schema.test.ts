/**
 * Tests for MetricForm Zod schema and pure helpers.
 *
 * Spec traces:
 *   - spec/API.md §Metric (/spoke/governance/metric) — metric_id pattern
 *     (^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$)
 *   - src/api/schemas/metrics.py CreateMetricConfigRequest.metric_id pattern
 *   - spec/feature/FRONTEND_GOVERNANCE.md §Metrics create form — metric_id create-only field
 *   - src/api/schemas/metrics.py _check_metric_conf_for_type (F2 invariant):
 *     ingestion-freshness and validation-score require a positive int time_window_sec;
 *     doc-health takes {} (no window)
 *   - src/api/schemas/metrics.py _check_metrics_subset (F3 invariant):
 *     metrics[] must be a subset of METRIC_EMITTED_KEYS[type]
 *   - types/governance.ts METRIC_EMITTED_KEYS and METRIC_TYPES_WITH_TIME_WINDOW
 */

import { describe, it, expect } from "vitest";
import {
  createSchema,
  baseSchema,
  METRIC_ID_PATTERN,
  pruneMetricKeys,
  fromInternal,
  toInternal,
} from "./metric-form.schema";
import type { InternalFormValues } from "./metric-form.schema";
import type { MetricFormValues } from "@/types/governance";

// ── Shared valid base payload (edit schema — no metric_id validation) ──────────

const VALID_BASE = {
  mode: "active" as const,
  metric_type: "doc-health" as const,
  title: "Doc Health (DEV)",
  description: "Daily documentation-completeness check",
  metrics: ["total"],
  time_window_sec: undefined,
  schedule_tier: "daily" as const,
  is_enabled: true,
  dataset_filter: { origin: "DEV" },
  metric_id: "",
};

interface TimeWindowPayload {
  mode?: string;
  metric_type?: string;
  title?: string;
  description?: string;
  metrics?: string[];
  time_window_sec?: number;
  schedule_tier?: string | null;
  is_enabled?: boolean;
  dataset_filter?: Record<string, unknown>;
  metric_id?: string;
}

function withTimeWindow(overrides: TimeWindowPayload): TimeWindowPayload {
  return {
    ...VALID_BASE,
    metric_type: "ingestion-freshness",
    metrics: ["total"],
    ...overrides,
  };
}

// ── 1. metric_id regex ─────────────────────────────────────────────────────────

describe("METRIC_ID_PATTERN — valid identifiers (spec/API.md §Metric metric_id)", () => {
  const valid = [
    "a",
    "z",
    "0",
    "a1",
    "ingestion-freshness",
    "abc-123",
    "doc-health-dev",
    "ab",
    // exactly 64 chars: 1 lead + 62 middle + 1 trail
    "a" + "b".repeat(62) + "c",
  ];

  valid.forEach((id) => {
    it(`accepts "${id}"`, () => {
      expect(METRIC_ID_PATTERN.test(id)).toBe(true);
    });
  });
});

describe("METRIC_ID_PATTERN — invalid identifiers (spec/API.md §Metric metric_id)", () => {
  const invalid = [
    "",             // empty
    "A",            // uppercase
    "Abc",          // uppercase
    "ABC",          // uppercase
    "a_b",          // underscore not allowed
    "a b",          // space not allowed
    "-abc",         // leading hyphen
    "abc-",         // trailing hyphen
    "-",            // hyphen only
    "a-",           // trailing hyphen single
    // 65 chars: 1 lead + 63 middle + 1 trail → middle exceeds {0,62}
    "a" + "b".repeat(63) + "c",
  ];

  invalid.forEach((id) => {
    it(`rejects "${id}"`, () => {
      expect(METRIC_ID_PATTERN.test(id)).toBe(false);
    });
  });
});

describe("createSchema — metric_id validation", () => {
  it("accepts a valid metric_id in the create schema", () => {
    const result = createSchema.safeParse({
      ...VALID_BASE,
      metric_id: "doc-health-dev",
    });
    expect(result.success).toBe(true);
  });

  it("rejects uppercase metric_id", () => {
    const result = createSchema.safeParse({
      ...VALID_BASE,
      metric_id: "DocHealth",
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      const err = result.error.issues.find((i) => i.path[0] === "metric_id");
      expect(err).toBeDefined();
    }
  });

  it("rejects metric_id with leading hyphen", () => {
    const result = createSchema.safeParse({
      ...VALID_BASE,
      metric_id: "-doc-health",
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      expect(result.error.issues.some((i) => i.path[0] === "metric_id")).toBe(true);
    }
  });

  it("rejects metric_id with trailing hyphen", () => {
    const result = createSchema.safeParse({
      ...VALID_BASE,
      metric_id: "doc-health-",
    });
    expect(result.success).toBe(false);
  });

  it("rejects empty metric_id", () => {
    const result = createSchema.safeParse({
      ...VALID_BASE,
      metric_id: "",
    });
    expect(result.success).toBe(false);
  });

  it("rejects metric_id with spaces", () => {
    const result = createSchema.safeParse({
      ...VALID_BASE,
      metric_id: "doc health",
    });
    expect(result.success).toBe(false);
  });

  it("rejects metric_id with underscore", () => {
    const result = createSchema.safeParse({
      ...VALID_BASE,
      metric_id: "doc_health",
    });
    expect(result.success).toBe(false);
  });

  it("rejects metric_id longer than 64 characters", () => {
    // 65 chars — the pattern middle segment is {0,62}, so total max is 64
    const id = "a" + "b".repeat(63) + "c"; // 65 chars
    const result = createSchema.safeParse({
      ...VALID_BASE,
      metric_id: id,
    });
    expect(result.success).toBe(false);
  });

  it("accepts metric_id of exactly 64 characters", () => {
    const id = "a" + "b".repeat(62) + "c"; // 64 chars
    const result = createSchema.safeParse({
      ...VALID_BASE,
      metric_id: id,
    });
    expect(result.success).toBe(true);
  });
});

// ── 2. F2 — conditional time_window_sec (spec/API.md §Metric, metrics.py) ──────

describe("F2 invariant — time_window_sec required for ingestion-freshness", () => {
  it("fails when time_window_sec is absent for ingestion-freshness", () => {
    const result = baseSchema.safeParse(
      withTimeWindow({ metric_type: "ingestion-freshness", time_window_sec: undefined }),
    );
    expect(result.success).toBe(false);
    if (!result.success) {
      const err = result.error.issues.find((i) => i.path[0] === "time_window_sec");
      expect(err).toBeDefined();
      expect(err?.message).toContain("required");
    }
  });

  it("fails when time_window_sec is absent for validation-score", () => {
    const result = baseSchema.safeParse(
      withTimeWindow({ metric_type: "validation-score", metrics: ["total"], time_window_sec: undefined }),
    );
    expect(result.success).toBe(false);
    if (!result.success) {
      const err = result.error.issues.find((i) => i.path[0] === "time_window_sec");
      expect(err).toBeDefined();
    }
  });

  it("passes when time_window_sec is a positive integer for ingestion-freshness", () => {
    const result = baseSchema.safeParse(
      withTimeWindow({ metric_type: "ingestion-freshness", time_window_sec: 86400 }),
    );
    expect(result.success).toBe(true);
  });

  it("passes when time_window_sec is 1 (minimum positive integer)", () => {
    const result = baseSchema.safeParse(
      withTimeWindow({ metric_type: "ingestion-freshness", time_window_sec: 1 }),
    );
    expect(result.success).toBe(true);
  });

  it("fails when time_window_sec is 0 (not positive) for ingestion-freshness", () => {
    // z.coerce.number().int().positive() — 0 is not positive
    const result = baseSchema.safeParse(
      withTimeWindow({ metric_type: "ingestion-freshness", time_window_sec: 0 }),
    );
    expect(result.success).toBe(false);
    if (!result.success) {
      expect(result.error.issues.some((i) => i.path[0] === "time_window_sec")).toBe(true);
    }
  });

  it("fails when time_window_sec is negative for ingestion-freshness", () => {
    const result = baseSchema.safeParse(
      withTimeWindow({ metric_type: "ingestion-freshness", time_window_sec: -100 }),
    );
    expect(result.success).toBe(false);
  });
});

describe("F2 invariant — doc-health passes without time_window_sec", () => {
  it("passes for doc-health when time_window_sec is absent", () => {
    const result = baseSchema.safeParse({
      ...VALID_BASE,
      metric_type: "doc-health",
      time_window_sec: undefined,
    });
    expect(result.success).toBe(true);
  });

  it("passes for doc-health even when time_window_sec is provided (schema does not forbid extra)", () => {
    // The frontend schema does not actively reject time_window_sec for doc-health;
    // it simply does not require it. The backend enforces metric_conf={} separately.
    const result = baseSchema.safeParse({
      ...VALID_BASE,
      metric_type: "doc-health",
      time_window_sec: 86400,
    });
    // This is acceptable at the frontend schema layer; the backend will reject metric_conf if needed.
    expect(result.success).toBe(true);
  });
});

// ── 3. F3 — pruneMetricKeys pure helper (metrics.py _check_metrics_subset) ─────

describe("pruneMetricKeys — F3 metrics subset invariant (src/api/schemas/metrics.py _check_metrics_subset)", () => {
  it("returns only keys valid for ingestion-freshness from a prior selection", () => {
    // doc-health has ['total', 'doc_health']; ingestion-freshness has ['total', 'ingested_in_time']
    // 'doc_health' must be pruned when switching to ingestion-freshness
    const pruned = pruneMetricKeys("ingestion-freshness", ["total", "doc_health"]);
    expect(pruned).toEqual(["total"]);
  });

  it("returns only keys valid for doc-health, dropping ingestion-freshness-only keys", () => {
    const pruned = pruneMetricKeys("doc-health", ["total", "ingested_in_time"]);
    expect(pruned).toEqual(["total"]);
  });

  it("returns only keys valid for validation-score", () => {
    const pruned = pruneMetricKeys("validation-score", ["total", "doc_health", "ingested_in_time"]);
    // validation-score emits: total, validation_score_sum
    expect(pruned).toContain("total");
    expect(pruned).not.toContain("doc_health");
    expect(pruned).not.toContain("ingested_in_time");
  });

  it("keeps all keys when they are all valid for the new type", () => {
    const pruned = pruneMetricKeys("ingestion-freshness", ["total", "ingested_in_time"]);
    expect(pruned).toHaveLength(2);
    expect(pruned).toContain("total");
    expect(pruned).toContain("ingested_in_time");
  });

  it("returns empty array when none of the prior keys are valid for the new type", () => {
    const pruned = pruneMetricKeys("doc-health", ["ingested_in_time", "validation_score_sum"]);
    expect(pruned).toHaveLength(0);
  });

  it("returns empty array when prior selection is empty", () => {
    const pruned = pruneMetricKeys("ingestion-freshness", []);
    expect(pruned).toHaveLength(0);
  });
});

// ── 4. F2 — fromInternal serialization (src/api/schemas/metrics.py _check_metric_conf_for_type) ──
//
// The backend's _check_metric_conf_for_type raises 422 when:
//   - ingestion-freshness / validation-score: metric_conf.time_window_sec absent or not a positive int
//   - doc-health: metric_conf !== {}  (any key present causes rejection)
//
// fromInternal is the path that builds the payload sent to the API.

describe("fromInternal — doc-health serializes metric_conf as exactly {} (backend 422 guard)", () => {
  const docHealthInternal: InternalFormValues = {
    mode: "active",
    metric_type: "doc-health",
    title: "Doc Health",
    description: "Checks documentation completeness",
    metrics: ["total"],
    time_window_sec: undefined,
    schedule_tier: "daily",
    is_enabled: true,
    dataset_filter: { origin: "DEV" },
    metric_id: "",
  };

  it("produces metric_conf === {} for doc-health when time_window_sec is absent", () => {
    const result = fromInternal(docHealthInternal);
    expect(result.metric_conf).toStrictEqual({});
  });

  it("produces metric_conf === {} for doc-health even when time_window_sec is somehow present in internal state", () => {
    // This is the bug the test guards: if a stale time_window_sec value survives
    // a type-switch, fromInternal must still drop it for doc-health.
    // Backend _check_metric_conf_for_type returns 422 if metric_conf !== {} for doc-health.
    const withStaleWindow: InternalFormValues = { ...docHealthInternal, time_window_sec: 86400 };
    const result = fromInternal(withStaleWindow);
    expect(result.metric_conf).toStrictEqual({});
    expect(Object.keys(result.metric_conf)).toHaveLength(0);
  });
});

describe("fromInternal — ingestion-freshness serializes metric_conf as { time_window_sec: N }", () => {
  const freshnessInternal: InternalFormValues = {
    mode: "active",
    metric_type: "ingestion-freshness",
    title: "Freshness",
    description: "Checks ingestion freshness",
    metrics: ["total"],
    time_window_sec: 172800,
    schedule_tier: "daily",
    is_enabled: true,
    dataset_filter: {},
    metric_id: "",
  };

  it("produces metric_conf === { time_window_sec: 172800 } for ingestion-freshness", () => {
    const result = fromInternal(freshnessInternal);
    expect(result.metric_conf).toStrictEqual({ time_window_sec: 172800 });
  });

  it("preserves the exact integer value of time_window_sec", () => {
    const result = fromInternal({ ...freshnessInternal, time_window_sec: 86400 });
    expect(result.metric_conf).toStrictEqual({ time_window_sec: 86400 });
  });
});

describe("fromInternal — validation-score serializes metric_conf as { time_window_sec: N }", () => {
  it("produces metric_conf === { time_window_sec: N } for validation-score", () => {
    const internal: InternalFormValues = {
      mode: "active",
      metric_type: "validation-score",
      title: "Validation Score",
      description: "Tracks validation pass rate",
      metrics: ["total"],
      time_window_sec: 604800,
      schedule_tier: "weekly",
      is_enabled: true,
      dataset_filter: {},
      metric_id: "",
    };
    const result = fromInternal(internal);
    expect(result.metric_conf).toStrictEqual({ time_window_sec: 604800 });
  });
});

// ── 5. F2 — round-trip toInternal(fromInternal(x)) preserves meaningful fields ─

describe("round-trip toInternal → fromInternal (API payload field preservation)", () => {
  it("round-trips doc-health: metric_conf stays {}, time_window_sec stays absent", () => {
    const original: MetricFormValues = {
      mode: "active",
      metric_type: "doc-health",
      title: "Doc Health",
      description: "Completeness check",
      metrics: ["total", "doc_health"],
      metric_conf: {},
      schedule_tier: "daily",
      is_enabled: true,
      dataset_filter: { origin: "DEV" },
    };
    const result = fromInternal(toInternal(original));
    expect(result.metric_conf).toStrictEqual({});
    expect(result.metric_type).toBe("doc-health");
    expect(result.metrics).toEqual(original.metrics);
    expect(result.title).toBe(original.title);
    expect(result.is_enabled).toBe(original.is_enabled);
  });

  it("round-trips ingestion-freshness: time_window_sec preserved in metric_conf", () => {
    const original: MetricFormValues = {
      mode: "active",
      metric_type: "ingestion-freshness",
      title: "Freshness",
      description: "Freshness check",
      metrics: ["total"],
      metric_conf: { time_window_sec: 172800 },
      schedule_tier: null,
      is_enabled: true,
      dataset_filter: {},
    };
    const result = fromInternal(toInternal(original));
    expect(result.metric_conf).toStrictEqual({ time_window_sec: 172800 });
    expect(result.metric_type).toBe("ingestion-freshness");
  });

  it("round-trips validation-score: time_window_sec preserved in metric_conf", () => {
    const original: MetricFormValues = {
      mode: "active",
      metric_type: "validation-score",
      title: "Val Score",
      description: "Validation score metric",
      metrics: ["total", "validation_score_sum"],
      metric_conf: { time_window_sec: 3600 },
      schedule_tier: "hourly",
      is_enabled: false,
      dataset_filter: {},
    };
    const result = fromInternal(toInternal(original));
    expect(result.metric_conf).toStrictEqual({ time_window_sec: 3600 });
    expect(result.metric_type).toBe("validation-score");
    expect(result.is_enabled).toBe(false);
  });
});
