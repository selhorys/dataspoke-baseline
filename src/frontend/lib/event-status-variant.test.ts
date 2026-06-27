/**
 * Tests for lib/event-status-variant.ts — eventStatusVariant mapping.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §Design system › Color tokens:
 *     "Semantic status tokens — --success (green), --warning (amber), --info
 *     (sky) ... Status badges and status-variant helpers map to these rather
 *     than overloading default / secondary, so a status reads as a status."
 *   - spec/feature/FRONTEND_GOVERNANCE.md §Metrics detail event log:
 *     events from GET .../event displayed with status badge.
 *   - src/shared/models/enums.py EventStatus:
 *     SUCCESS = "success", OK = "ok", FAILURE = "failure", ERROR = "error",
 *     RUNNING = "running", WARNING = "warning", INFO = "info"
 *
 * Semantic mapping (each status reads as a status, never overloads default):
 *   failure | error → "destructive"; warning → "warning";
 *   success | ok    → "success";     running | info → "info";
 *   unknown / empty → "secondary"    (neutral safe fallback, asserts no status).
 */

import { describe, it, expect } from "vitest";
import { eventStatusVariant } from "./event-status-variant";

// ── Status → semantic variant table driven from EventStatus enum in enums.py ──

describe("eventStatusVariant — destructive statuses (failure/error)", () => {
  // These two map to "destructive" — spec invariant: failed/errored runs must be visually distinct.

  it('maps "failure" to "destructive"', () => {
    expect(eventStatusVariant("failure")).toBe("destructive");
  });

  it('maps "error" to "destructive"', () => {
    expect(eventStatusVariant("error")).toBe("destructive");
  });
});

describe("eventStatusVariant — warning status maps to the semantic warning token", () => {
  it('maps "warning" to "warning"', () => {
    expect(eventStatusVariant("warning")).toBe("warning");
  });
});

describe("eventStatusVariant — benign statuses use semantic non-destructive tokens", () => {
  // Benign statuses: success, ok, running, info — must never render as "destructive"
  // (which would falsely signal failure to the user) and must read as a status
  // rather than the neutral default/secondary fallback.

  const benign: string[] = ["success", "ok", "running", "info"];

  benign.forEach((status) => {
    it(`"${status}" maps to a non-destructive variant`, () => {
      const variant = eventStatusVariant(status);
      expect(variant).not.toBe("destructive");
    });
  });

  it('"success" maps to "success"', () => {
    expect(eventStatusVariant("success")).toBe("success");
  });

  it('"ok" maps to "success"', () => {
    expect(eventStatusVariant("ok")).toBe("success");
  });

  it('"running" maps to "info"', () => {
    expect(eventStatusVariant("running")).toBe("info");
  });

  it('"info" maps to "info"', () => {
    expect(eventStatusVariant("info")).toBe("info");
  });
});

describe("eventStatusVariant — exhaustive table (all EventStatus values)", () => {
  // Drive from the full set of EventStatus values defined in src/shared/models/enums.py EventStatus.
  // SYNC REQUIRED: if EventStatus in src/shared/models/enums.py gains or renames members,
  // update this table and eventStatusVariant in lib/event-status-variant.ts accordingly.
  // Current members (as of enums.py): SUCCESS, OK, FAILURE, ERROR, RUNNING, WARNING, INFO.
  // Any future addition not handled here falls through to "secondary" (neutral safe fallback).

  const table: Array<[string, "secondary" | "destructive" | "success" | "warning" | "info"]> = [
    ["success", "success"],
    ["ok",      "success"],
    ["failure", "destructive"],
    ["error",   "destructive"],
    ["running", "info"],
    ["warning", "warning"],
    ["info",    "info"],
  ];

  table.forEach(([status, expected]) => {
    it(`"${status}" → "${expected}"`, () => {
      expect(eventStatusVariant(status)).toBe(expected);
    });
  });
});

describe("eventStatusVariant — unknown/empty status falls back to the neutral secondary variant", () => {
  // spec/feature/FRONTEND_BASIC.md §Design system › Color tokens: "An unrecognized
  // or empty status carries no semantic color: it falls back to the neutral
  // `secondary` variant." So the exact token here is spec-fixed, not impl-pinned —
  // and it must never read as destructive or a semantic success/warning/info badge.

  it('unknown status string maps to "secondary" (the spec-fixed neutral fallback)', () => {
    expect(eventStatusVariant("pending")).toBe("secondary");
    // Neutrality guard: a fallback must not masquerade as a failure badge.
    expect(eventStatusVariant("pending")).not.toBe("destructive");
  });

  it('empty string maps to "secondary" (the spec-fixed neutral fallback)', () => {
    expect(eventStatusVariant("")).toBe("secondary");
    expect(eventStatusVariant("")).not.toBe("destructive");
  });
});
