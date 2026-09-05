/**
 * Tests for lib/chart-colors.ts — colorForKey determinism and palette stability.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_GOVERNANCE.md §Dashboard / §Metrics detail:
 *     "One line per values key per metric" — implies consistent key→color mapping
 *     across renders (dashboard and timeseries chart both use colorForKey).
 *   - components/governance/metric-timeseries-chart.tsx: allKeys is recomputed
 *     each render from results; colorForKey must be stable given the same key
 *     position so chart lines do not flicker between renders.
 */

import { describe, it, expect } from "vitest";
import { colorForKey, CHART_COLORS } from "./chart-colors";

// ── 1. Determinism ─────────────────────────────────────────────────────────────

describe("colorForKey — determinism", () => {
  it("returns the same color for the same key and allKeys on repeated calls", () => {
    const allKeys = ["total", "ingested_in_time"];
    const first = colorForKey("total", allKeys);
    const second = colorForKey("total", allKeys);
    expect(first).toBe(second);
  });

  it("returns the same color regardless of call order (call order must not affect result)", () => {
    const allKeys = ["total", "doc_health"];
    // Call in reverse order; result must still be deterministic given the same allKeys.
    colorForKey("doc_health", allKeys);
    const color = colorForKey("total", allKeys);
    // Determinism: calling doc_health first must not alter total's color.
    expect(color).toBe(colorForKey("total", allKeys));
    // The first key must also be a valid palette entry.
    expect(CHART_COLORS as readonly string[]).toContain(color);
  });
});

// ── 2. Index-based assignment — valid palette entries ─────────────────────────
//
// Contract: each key maps to a palette entry derived from its position in allKeys.
// Tests verify membership in CHART_COLORS (contract), not specific indices (impl).
// A restyle that reorders the palette will not break these tests.

describe("colorForKey — each key receives a valid palette color", () => {
  it("first key in allKeys receives a valid palette entry", () => {
    const allKeys = ["total", "doc_health"];
    expect(CHART_COLORS as readonly string[]).toContain(colorForKey("total", allKeys));
  });

  it("second key in allKeys receives a valid palette entry", () => {
    const allKeys = ["total", "doc_health"];
    expect(CHART_COLORS as readonly string[]).toContain(colorForKey("doc_health", allKeys));
  });

  it("third key in allKeys receives a valid palette entry", () => {
    const allKeys = ["total", "ingested_in_time", "doc_health"];
    expect(CHART_COLORS as readonly string[]).toContain(colorForKey("doc_health", allKeys));
  });

  it("distinct keys in the same allKeys set receive distinct colors (within palette size)", () => {
    // Within palette size, index-based assignment guarantees distinct colors
    // as long as the palette entries themselves are unique (verified in §3).
    const allKeys = ["total", "doc_health"];
    expect(colorForKey("total", allKeys)).not.toBe(colorForKey("doc_health", allKeys));
  });
});

// ── 3. Distinct keys get distinct colors within palette size ───────────────────

describe("colorForKey — distinct colors within palette", () => {
  it("all CHART_COLORS palette entries are unique strings", () => {
    const unique = new Set(CHART_COLORS);
    expect(unique.size).toBe(CHART_COLORS.length);
  });

  it("distinct keys within palette size get distinct colors", () => {
    // Use as many keys as there are palette entries
    const allKeys = CHART_COLORS.map((_, i) => `key${i}`);
    const colors = allKeys.map((k) => colorForKey(k, allKeys));
    const unique = new Set(colors);
    expect(unique.size).toBe(CHART_COLORS.length);
  });

  it("ingestion-freshness metric keys get distinct colors", () => {
    const allKeys = ["total", "ingested_in_time"];
    const c0 = colorForKey("total", allKeys);
    const c1 = colorForKey("ingested_in_time", allKeys);
    expect(c0).not.toBe(c1);
  });

  it("doc-health metric keys get distinct colors", () => {
    const allKeys = ["total", "doc_health"];
    const c0 = colorForKey("total", allKeys);
    const c1 = colorForKey("doc_health", allKeys);
    expect(c0).not.toBe(c1);
  });

  it("validation-score metric keys get distinct colors", () => {
    const allKeys = ["valid_confd", "valid_in_time"];
    const c0 = colorForKey("valid_confd", allKeys);
    const c1 = colorForKey("valid_in_time", allKeys);
    expect(new Set([c0, c1]).size).toBe(2);
  });
});

// ── 4. Palette wrap-around (>= palette.length keys) ───────────────────────────

describe("colorForKey — palette wrap-around", () => {
  it("wraps around when the key index exceeds palette size — returns a valid palette entry", () => {
    // palette has 8 entries; key at index 8 must still resolve to a palette member.
    const allKeys = CHART_COLORS.map((_, i) => `key${i}`).concat(["overflow-key"]);
    const wrappedColor = colorForKey("overflow-key", allKeys);
    expect(CHART_COLORS as readonly string[]).toContain(wrappedColor);
  });
});

// ── 5. Key not in allKeys — fallback ──────────────────────────────────────────

describe("colorForKey — key absent from allKeys", () => {
  it("returns a valid palette entry when the key is not found in allKeys", () => {
    // Contract: never returns undefined or throws; always a valid palette entry.
    const color = colorForKey("unknown-key", ["total", "doc_health"]);
    expect(CHART_COLORS as readonly string[]).toContain(color);
  });
});

// ── 6. Stability across allKeys order changes ──────────────────────────────────
//
// The dashboard and detail page recompute allKeys each render. If allKeys order
// changes (e.g. via Set iteration), a key may receive a different color between
// renders. The spec intent is stability per render (colorForKey is pure given the
// same allKeys array). We verify the function is purely index-based — order matters.

describe("colorForKey — order sensitivity (caller's responsibility)", () => {
  it("returns different colors for the same key when allKeys order changes", () => {
    // This is expected behavior — callers must supply a stable sorted allKeys.
    // The timeseries chart sorts allKeys with .sort() for this reason.
    // Contract: if position differs, color must differ (palette entries are distinct per §3).
    const color1 = colorForKey("doc_health", ["total", "doc_health"]);    // index 1
    const color2 = colorForKey("doc_health", ["doc_health", "total"]);    // index 0
    // Both must be valid palette entries.
    expect(CHART_COLORS as readonly string[]).toContain(color1);
    expect(CHART_COLORS as readonly string[]).toContain(color2);
    // Different positions → different colors (because palette entries are distinct).
    expect(color1).not.toBe(color2);
  });
});
