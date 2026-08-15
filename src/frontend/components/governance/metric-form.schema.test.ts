/**
 * Tests for MetricForm Zod schema and pure helpers.
 *
 * Spec traces:
 *   - spec/API.md §Metric (/spoke/governance/metric) — metric_id pattern
 *     (^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$|^[a-z0-9]$)
 *   - src/api/schemas/metrics.py CreateMetricConfigRequest.metric_id pattern
 *   - spec/feature/FRONTEND_GOVERNANCE.md §Metrics (/governance/metrics) — "`metric_id`
 *     text input — **create-only**"
 *   - src/api/schemas/metrics.py _check_metric_conf_for_type (F2 invariant):
 *     ingestion-freshness and validation-score require a positive int time_window_sec;
 *     doc-health takes {} (no window)
 *   - spec/feature/BACKEND.md §Metrics Service §Window bounds / spec/API.md §Metric —
 *     time_window_sec is an integer in [1, 315_360_000] (ten years); the bound is read
 *     from types/governance.ts METRIC_TIME_WINDOW_SEC_MAX, never retyped here
 *   - src/api/schemas/metrics.py _check_metrics_series (F3 invariant):
 *     metrics[] names ⊆ METRIC_EMITTED_KEYS[type], #RRGGBB color, positive
 *     unique idx
 *   - types/governance.ts METRIC_EMITTED_KEYS and METRIC_TYPES_WITH_TIME_WINDOW
 */

import { describe, it, expect } from "vitest";
import {
  createSchema,
  baseSchema,
  METRIC_ID_PATTERN,
  seriesRowsForType,
  toSeries,
  fromInternal,
  toInternal,
} from "./metric-form.schema";
import type { InternalFormValues, MetricSeriesRow } from "./metric-form.schema";
import { METRIC_TIME_WINDOW_SEC_MAX } from "@/types/governance";
import type { MetricFormValues } from "@/types/governance";

// ── Shared valid base payload (edit schema — no metric_id validation) ──────────

const VALID_BASE = {
  mode: "active" as const,
  metric_type: "doc-health" as const,
  title: "Doc Health (DEV)",
  description: "Daily documentation-completeness check",
  metrics: [
    { name: "total", selected: true, color: "#64748B", idx: 1 },
    { name: "doc_health", selected: false, color: "#A855F7", idx: 2 },
  ],
  time_window_sec: undefined,
  schedule_tier: "daily" as const,
  is_enabled: true,
  dataset_filter: "origin = 'DEV'",
  metric_id: "",
};

interface TimeWindowPayload {
  mode?: string;
  metric_type?: string;
  title?: string;
  description?: string;
  metrics?: MetricSeriesRow[];
  time_window_sec?: number;
  schedule_tier?: string | null;
  is_enabled?: boolean;
  dataset_filter?: string;
  metric_id?: string;
}

function withTimeWindow(overrides: TimeWindowPayload): TimeWindowPayload {
  return {
    ...VALID_BASE,
    metric_type: "ingestion-freshness",
    metrics: [{ name: "total", selected: true, color: "#64748B", idx: 1 }],
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
      withTimeWindow({
        metric_type: "validation-score",
        metrics: [{ name: "total", selected: true, color: "#64748B", idx: 1 }],
        time_window_sec: undefined,
      }),
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

  it("declares the ceiling the spec names — 315_360_000 (ten years)", () => {
    // Every other assertion in this block reads the bound through
    // METRIC_TIME_WINDOW_SEC_MAX, so they would hold for any ceiling. This is the one
    // place the literal is written out, pinning the constant to the spec rather than to
    // itself — and, together with
    // tests/unit/shared/test_metric_conf.py::test_max_time_window_sec_is_the_spec_ceiling,
    // pinning the TypeScript↔Python mirror that
    // spec/feature/FRONTEND_GOVERNANCE.md §Metrics (/governance/metrics) requires ("the
    // bound is declared once, beside the other backend-mirroring constants").
    // spec/API.md §Metric — "An integer in [1, 315360000] (ten years)".
    // spec/feature/BACKEND.md §Metrics Service — Window bounds — "an integer in
    // [1, 315_360_000] — one second to ten years".
    expect(METRIC_TIME_WINDOW_SEC_MAX).toBe(315_360_000);
    expect(METRIC_TIME_WINDOW_SEC_MAX).toBe(3650 * 24 * 60 * 60);
  });

  it("passes at METRIC_TIME_WINDOW_SEC_MAX (the ceiling is admissible)", () => {
    // spec/API.md §Metric — "An integer in [1, 315360000] (ten years)"; the interval is
    // closed, so ten years exactly is a legal window.
    const result = baseSchema.safeParse(
      withTimeWindow({
        metric_type: "ingestion-freshness",
        time_window_sec: METRIC_TIME_WINDOW_SEC_MAX,
      }),
    );
    expect(result.success).toBe(true);
  });

  it("fails one second above METRIC_TIME_WINDOW_SEC_MAX", () => {
    // spec/feature/BACKEND.md §Metrics Service §Window bounds — "time_window_sec is an
    // integer in [1, 315_360_000] — one second to ten years"; out of range is rejected
    // (API returns 422 INVALID_PARAMETER), so the form must not offer such a value.
    const result = baseSchema.safeParse(
      withTimeWindow({
        metric_type: "ingestion-freshness",
        time_window_sec: METRIC_TIME_WINDOW_SEC_MAX + 1,
      }),
    );
    expect(result.success).toBe(false);
    if (!result.success) {
      expect(result.error.issues.some((i) => i.path[0] === "time_window_sec")).toBe(true);
    }
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

// ── 3. F3 — series rows (metrics.py _check_metrics_series) ────────────────────

describe("seriesRowsForType — one row per emitted key, reseeded on type change", () => {
  it("renders one row per emitted key of the type", () => {
    const rows = seriesRowsForType("doc-health", []);
    expect(rows.map((r) => r.name)).toEqual(["total", "doc_health"]);
    expect(rows.every((r) => !r.selected)).toBe(true);
  });

  it("keeps color and order of keys the new type still emits, and drops the rest", () => {
    const previous = [
      { name: "total", selected: true, color: "#111111", idx: 2 },
      { name: "doc_health", selected: true, color: "#222222", idx: 1 },
    ];
    const rows = seriesRowsForType("ingestion-freshness", previous);
    expect(rows.map((r) => r.name)).toEqual(["total", "ingested_in_time"]);
    expect(rows[0]).toEqual({ name: "total", selected: true, color: "#111111", idx: 2 });
    // The key the new type adds arrives unchecked, on a free order slot.
    expect(rows[1].selected).toBe(false);
    expect(rows[1].idx).not.toBe(2);
  });

  it("seeds unchecked rows with the backend's factory default color", () => {
    const rows = seriesRowsForType("validation-score", []);
    expect(rows.find((r) => r.name === "validation_score_sum")?.color).toBe("#3B82F6");
  });

  it("accepts API series descriptors as the seed", () => {
    const rows = seriesRowsForType("doc-health", [
      { name: "doc_health", color: "#A855F7", idx: 1 },
    ]);
    expect(rows.find((r) => r.name === "doc_health")).toEqual({
      name: "doc_health",
      selected: true,
      color: "#A855F7",
      idx: 1,
    });
    expect(rows.find((r) => r.name === "total")?.selected).toBe(false);
  });
});

describe("toSeries — only checked rows are submitted, in idx order", () => {
  it("drops unchecked rows and the row-only `selected` flag", () => {
    const series = toSeries([
      { name: "doc_health", selected: true, color: "#A855F7", idx: 2 },
      { name: "total", selected: false, color: "#64748B", idx: 3 },
      { name: "other", selected: true, color: "#111111", idx: 1 },
    ]);
    expect(series).toEqual([
      { name: "other", color: "#111111", idx: 1 },
      { name: "doc_health", color: "#A855F7", idx: 2 },
    ]);
  });
});

describe("baseSchema — series rules mirror _check_metrics_series", () => {
  it("rejects a selection with no checked key", () => {
    const result = baseSchema.safeParse({
      ...VALID_BASE,
      metrics: [{ name: "total", selected: false, color: "#64748B", idx: 1 }],
    });
    expect(result.success).toBe(false);
  });

  it("rejects a malformed hex color on a checked row", () => {
    const result = baseSchema.safeParse({
      ...VALID_BASE,
      metrics: [{ name: "total", selected: true, color: "#12345", idx: 1 }],
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      expect(
        result.error.issues.some((i) => i.path.join(".") === "metrics.0.color"),
      ).toBe(true);
    }
  });

  it("ignores a malformed color on an unchecked row", () => {
    const result = baseSchema.safeParse({
      ...VALID_BASE,
      metrics: [
        { name: "total", selected: true, color: "#64748B", idx: 1 },
        { name: "doc_health", selected: false, color: "not-a-color", idx: 2 },
      ],
    });
    expect(result.success).toBe(true);
  });

  it("rejects a non-positive idx", () => {
    const result = baseSchema.safeParse({
      ...VALID_BASE,
      metrics: [{ name: "total", selected: true, color: "#64748B", idx: 0 }],
    });
    expect(result.success).toBe(false);
  });

  it("rejects duplicate idx values among checked rows", () => {
    const result = baseSchema.safeParse({
      ...VALID_BASE,
      metrics: [
        { name: "total", selected: true, color: "#64748B", idx: 1 },
        { name: "doc_health", selected: true, color: "#A855F7", idx: 1 },
      ],
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      expect(result.error.issues.some((i) => i.path[0] === "metrics")).toBe(true);
    }
  });
});

describe("baseSchema — dataset_filter is a capped string", () => {
  it("accepts an empty clause (all registered datasets)", () => {
    const result = baseSchema.safeParse({ ...VALID_BASE, dataset_filter: "" });
    expect(result.success).toBe(true);
  });

  it("rejects a clause over the 8,000-character payload cap", () => {
    const result = baseSchema.safeParse({
      ...VALID_BASE,
      dataset_filter: "x".repeat(8001),
    });
    expect(result.success).toBe(false);
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
    metrics: [{ name: "total", selected: true, color: "#64748B", idx: 1 }],
    time_window_sec: undefined,
    schedule_tier: "daily",
    is_enabled: true,
    dataset_filter: "origin = 'DEV'",
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
    metrics: [{ name: "total", selected: true, color: "#64748B", idx: 1 }],
    time_window_sec: 172800,
    schedule_tier: "daily",
    is_enabled: true,
    dataset_filter: "",
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
      metrics: [{ name: "total", selected: true, color: "#64748B", idx: 1 }],
      time_window_sec: 604800,
      schedule_tier: "weekly",
      is_enabled: true,
      dataset_filter: "",
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
      metrics: [
        { name: "total", color: "#64748B", idx: 1 },
        { name: "doc_health", color: "#A855F7", idx: 2 },
      ],
      metric_conf: {},
      schedule_tier: "daily",
      is_enabled: true,
      dataset_filter: "origin = 'DEV'",
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
      metrics: [{ name: "total", color: "#64748B", idx: 1 }],
      metric_conf: { time_window_sec: 172800 },
      schedule_tier: null,
      is_enabled: true,
      dataset_filter: "",
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
      metrics: [
        { name: "total", color: "#64748B", idx: 1 },
        { name: "validation_score_sum", color: "#3B82F6", idx: 2 },
      ],
      metric_conf: { time_window_sec: 3600 },
      schedule_tier: "hourly",
      is_enabled: false,
      dataset_filter: "",
    };
    const result = fromInternal(toInternal(original));
    expect(result.metric_conf).toStrictEqual({ time_window_sec: 3600 });
    expect(result.metric_type).toBe("validation-score");
    expect(result.is_enabled).toBe(false);
  });
});
