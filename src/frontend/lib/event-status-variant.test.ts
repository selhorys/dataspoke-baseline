/**
 * Tests for lib/event-status-variant.ts — eventStatusVariant mapping.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_GOVERNANCE.md §Metrics detail event log:
 *     events from GET .../event displayed with status badge.
 *   - src/shared/models/enums.py EventStatus:
 *     SUCCESS = "success", OK = "ok", FAILURE = "failure", ERROR = "error",
 *     RUNNING = "running", WARNING = "warning", INFO = "info"
 *   - Invariant: failure/error → destructive; warning → secondary;
 *     all benign statuses (success, ok, running, info) → NOT destructive.
 */

import { describe, it, expect } from "vitest";
import { eventStatusVariant } from "./event-status-variant";

// ── Status → variant table driven from EventStatus enum in enums.py ───────────

describe("eventStatusVariant — destructive statuses (failure/error)", () => {
  // These two map to "destructive" — spec invariant: failed/errored runs must be visually distinct.

  it('maps "failure" to "destructive"', () => {
    expect(eventStatusVariant("failure")).toBe("destructive");
  });

  it('maps "error" to "destructive"', () => {
    expect(eventStatusVariant("error")).toBe("destructive");
  });
});

describe("eventStatusVariant — warning status", () => {
  it('maps "warning" to "secondary"', () => {
    expect(eventStatusVariant("warning")).toBe("secondary");
  });
});

describe("eventStatusVariant — benign statuses are NOT destructive (spec invariant)", () => {
  // Benign statuses: success, ok, running, info — must never render as "destructive"
  // (which would falsely signal failure to the user).

  const benign: string[] = ["success", "ok", "running", "info"];

  benign.forEach((status) => {
    it(`"${status}" maps to a non-destructive variant`, () => {
      const variant = eventStatusVariant(status);
      expect(variant).not.toBe("destructive");
    });
  });

  it('"success" maps to "default"', () => {
    expect(eventStatusVariant("success")).toBe("default");
  });

  it('"ok" maps to "default"', () => {
    expect(eventStatusVariant("ok")).toBe("default");
  });

  it('"running" maps to "default"', () => {
    expect(eventStatusVariant("running")).toBe("default");
  });

  it('"info" maps to "default"', () => {
    expect(eventStatusVariant("info")).toBe("default");
  });
});

describe("eventStatusVariant — exhaustive table (all EventStatus values)", () => {
  // Drive from the full set of EventStatus values defined in src/shared/models/enums.py EventStatus.
  // SYNC REQUIRED: if EventStatus in src/shared/models/enums.py gains or renames members,
  // update this table and eventStatusVariant in lib/event-status-variant.ts accordingly.
  // Current members (as of enums.py): SUCCESS, OK, FAILURE, ERROR, RUNNING, WARNING, INFO.
  // Any future addition not handled here will fall through to "default" (safe fallback).

  const table: Array<[string, "default" | "secondary" | "destructive" | "outline"]> = [
    ["success", "default"],
    ["ok",      "default"],
    ["failure", "destructive"],
    ["error",   "destructive"],
    ["running", "default"],
    ["warning", "secondary"],
    ["info",    "default"],
  ];

  table.forEach(([status, expected]) => {
    it(`"${status}" → "${expected}"`, () => {
      expect(eventStatusVariant(status)).toBe(expected);
    });
  });
});

describe("eventStatusVariant — unknown/future status falls back to default", () => {
  it("unknown status string maps to default (safe fallback)", () => {
    expect(eventStatusVariant("pending")).toBe("default");
  });

  it("empty string maps to default", () => {
    expect(eventStatusVariant("")).toBe("default");
  });
});
