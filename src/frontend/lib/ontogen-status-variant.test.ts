/**
 * Tests for lib/ontogen-status-variant.ts — ontogenStatusVariant and ontogenStatusLabel.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_ONTOGEN.md: nodes/edges/triples display with status badges.
 *   - src/api/schemas/ontogen.py NodeResponse/EdgeResponse/TripleResponse .status field:
 *       "llm_pending"  — LLM-created, awaiting review
 *       "llm_approved" — LLM-reviewer accepted + high confidence
 *       "approved"     — human-approved
 *       "rejected"     — human-rejected
 *   - spec/feature/FRONTEND_BASIC.md §Shared Component Notes:
 *       write actions rendered only when role ∈ {Editor, Admin}; status badges are
 *       read-only visual indicators for all roles.
 *
 * Invariants:
 *   "rejected"     → "destructive" variant (signals human rejection — must be visually distinct)
 *   "approved"     → "default" variant (human-approved success state)
 *   "llm_approved" → non-destructive variant (LLM acceptance, not yet human-reviewed)
 *   "llm_pending"  → non-destructive variant (awaiting any review)
 *   unknown status → safe fallback (does not throw, does not return "destructive")
 */

import { describe, it, expect } from "vitest";
import { ontogenStatusVariant, ontogenStatusLabel } from "./ontogen-status-variant";
import type { OntogenStatus } from "@/types/ontogen";

// ---------------------------------------------------------------------------
// 1. ontogenStatusVariant — exhaustive table covering all four OntogenStatus values
// ---------------------------------------------------------------------------

describe("ontogenStatusVariant — exhaustive OntogenStatus table", () => {
  // Derived from src/api/schemas/ontogen.py status field descriptions.
  // SYNC REQUIRED: if ontogen.py adds a status value, update this table
  // and ontogenStatusVariant accordingly.

  type Row = { status: OntogenStatus; expectedVariant: string };

  const table: Row[] = [
    { status: "approved",    expectedVariant: "default"     },
    { status: "llm_approved",expectedVariant: "secondary"   },
    { status: "llm_pending", expectedVariant: "outline"     },
    { status: "rejected",    expectedVariant: "destructive" },
  ];

  table.forEach(({ status, expectedVariant }) => {
    it(`"${status}" → variant "${expectedVariant}"`, () => {
      expect(ontogenStatusVariant(status)).toBe(expectedVariant);
    });
  });
});

// ---------------------------------------------------------------------------
// 2. Semantic invariants
// ---------------------------------------------------------------------------

describe("ontogenStatusVariant — semantic invariants", () => {
  it('"rejected" maps to "destructive" (human-rejected items must be visually distinct)', () => {
    // spec/feature/FRONTEND_ONTOGEN.md: rejected items shown with status badge;
    // destructive variant matches the reject action's severity.
    expect(ontogenStatusVariant("rejected")).toBe("destructive");
  });

  it('"approved" maps to "default" (human-approved is the success/affirmative variant)', () => {
    expect(ontogenStatusVariant("approved")).toBe("default");
  });

  it('"llm_approved" is non-destructive (LLM acceptance is not a failure state)', () => {
    expect(ontogenStatusVariant("llm_approved")).not.toBe("destructive");
  });

  it('"llm_pending" is non-destructive (awaiting review is not a failure state)', () => {
    expect(ontogenStatusVariant("llm_pending")).not.toBe("destructive");
  });

  it('"llm_approved" is distinct from "approved" (different variant, reflects LLM vs human gate)', () => {
    // The two approval stages must be visually distinguishable.
    expect(ontogenStatusVariant("llm_approved")).not.toBe(ontogenStatusVariant("approved"));
  });
});

// ---------------------------------------------------------------------------
// 3. Unknown/future status falls back safely
// ---------------------------------------------------------------------------

describe("ontogenStatusVariant — unknown status fallback", () => {
  it("unknown status returns a non-destructive variant (safe fallback, does not throw)", () => {
    // The switch default must not misclassify an unknown value as destructive.
    const result = ontogenStatusVariant("some_future_status");
    expect(result).not.toBe("destructive");
  });

  it("empty string does not throw and returns a valid variant", () => {
    expect(() => ontogenStatusVariant("")).not.toThrow();
    const result = ontogenStatusVariant("");
    expect(["default", "secondary", "destructive", "outline"]).toContain(result);
  });
});

// ---------------------------------------------------------------------------
// 4. ontogenStatusLabel — exhaustive table
// ---------------------------------------------------------------------------

describe("ontogenStatusLabel — exhaustive OntogenStatus table", () => {
  type Row = { status: OntogenStatus; expectedLabel: string };

  const table: Row[] = [
    { status: "approved",    expectedLabel: "approved"    },
    { status: "llm_approved",expectedLabel: "llm approved"},
    { status: "llm_pending", expectedLabel: "pending"     },
    { status: "rejected",    expectedLabel: "rejected"    },
  ];

  table.forEach(({ status, expectedLabel }) => {
    it(`"${status}" → label "${expectedLabel}"`, () => {
      expect(ontogenStatusLabel(status)).toBe(expectedLabel);
    });
  });
});

// ---------------------------------------------------------------------------
// 5. ontogenStatusLabel — semantic invariants
// ---------------------------------------------------------------------------

describe("ontogenStatusLabel — semantic invariants", () => {
  it("label for approved is non-empty string", () => {
    expect(ontogenStatusLabel("approved").length).toBeGreaterThan(0);
  });

  it("label for rejected is non-empty string", () => {
    expect(ontogenStatusLabel("rejected").length).toBeGreaterThan(0);
  });

  it('"llm_pending" label is user-readable (no underscores — spec: UI shows readable hints)', () => {
    // FRONTEND_ONTOGEN.md: pending items shown inline in review queue with readable hint
    expect(ontogenStatusLabel("llm_pending")).not.toContain("_");
  });

  it("unknown status returns the raw string as fallback (does not throw)", () => {
    const unknown = "some_future_value";
    const result = ontogenStatusLabel(unknown);
    expect(result).toBe(unknown);
  });
});
