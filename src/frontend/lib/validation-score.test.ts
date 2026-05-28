/**
 * Tests for lib/validation-score.ts — scoreBadgeVariant and scoreLabel.
 *
 * Spec traces:
 *   - spec/feature/VALIDATION.md §Validation Result:
 *     "result.type: SUCCESS if score == 1.0 else FAILURE"
 *     "0.0 ≤ score ≤ 1.0; 1.0 = pass; 0.0 = fail; intermediate values reserved for
 *      partial-success semantics — currently treated as fail at the DataHub enum boundary"
 *   - spec/feature/FRONTEND_VALIDATION.md §Page contracts:
 *     List "Quality Score" column: "—" until first result row arrives.
 *     Detail header badge: shown only when latestScore !== null.
 *
 * scoreBadgeVariant mirrors the DataHub assertion result.type boundary exactly:
 *   score === 1.0  → "default"      (SUCCESS / pass)
 *   0 ≤ score < 1  → "destructive"  (FAILURE / fail)
 *   null/undefined → "outline"      (no data, display "—")
 */

import { describe, it, expect } from "vitest";
import { scoreBadgeVariant, scoreLabel } from "./validation-score";

// ── 1. scoreBadgeVariant — three branches ─────────────────────────────────────

describe("scoreBadgeVariant — maps score to Badge variant (VALIDATION.md §Validation Result boundary)", () => {
  // Branch 1: score === 1.0 → SUCCESS / "default"
  it("returns 'default' for score === 1.0 (SUCCESS boundary)", () => {
    expect(scoreBadgeVariant(1.0)).toBe("default");
  });

  // Branch 2: 0 ≤ score < 1 → FAILURE / "destructive"
  it("returns 'destructive' for score === 0.0 (full fail)", () => {
    expect(scoreBadgeVariant(0.0)).toBe("destructive");
  });

  it("returns 'destructive' for score === 0.9999 (near-pass — still FAILURE per spec boundary)", () => {
    // VALIDATION.md: "1.0 = pass; intermediate values treated as fail"
    expect(scoreBadgeVariant(0.9999)).toBe("destructive");
  });

  it("returns 'destructive' for score === 0.5 (mid-range partial)", () => {
    expect(scoreBadgeVariant(0.5)).toBe("destructive");
  });

  it("returns 'destructive' for score === 0.0001 (near-zero partial)", () => {
    expect(scoreBadgeVariant(0.0001)).toBe("destructive");
  });

  // Branch 3: null → "outline" (no data)
  it("returns 'outline' for null score (no result rows yet — list shows '—')", () => {
    expect(scoreBadgeVariant(null)).toBe("outline");
  });

  it("returns 'outline' for undefined score", () => {
    expect(scoreBadgeVariant(undefined)).toBe("outline");
  });

  // Boundary: ensure 1.0 is strictly equal, not ≥ 1.0
  it("does NOT return 'default' for 1.0001 (above 1.0 is out of spec range)", () => {
    // score > 1.0 is invalid per the spec but the UI must not misclassify it as pass.
    // The function treats any non-1.0 number as destructive.
    expect(scoreBadgeVariant(1.0001)).toBe("destructive");
  });
});

// ── 2. scoreLabel — display strings ──────────────────────────────────────────

describe("scoreLabel — formats score for display (FRONTEND_VALIDATION.md §Page contracts)", () => {
  it("returns '1.0000' for score 1.0 (4 decimal places, matches page.tsx .toFixed(4))", () => {
    expect(scoreLabel(1.0)).toBe("1.0000");
  });

  it("returns '0.0000' for score 0.0", () => {
    expect(scoreLabel(0.0)).toBe("0.0000");
  });

  it("returns '0.9500' for score 0.95", () => {
    expect(scoreLabel(0.95)).toBe("0.9500");
  });

  it("returns '0.1234' for score 0.1234", () => {
    expect(scoreLabel(0.1234)).toBe("0.1234");
  });

  it("returns '—' for null (no data)", () => {
    expect(scoreLabel(null)).toBe("—");
  });

  it("returns '—' for undefined", () => {
    expect(scoreLabel(undefined)).toBe("—");
  });
});
