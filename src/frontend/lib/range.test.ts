/**
 * Tests for lib/range.ts — preset math, selection resolution, and formatting
 * across both granularities.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §shared-component-notes (RangePicker):
 *     "**A preset resolves to an open-ended window** — the lower bound only,
 *     with `to`/`until` omitted — so the read always reaches the present" and
 *     "**A custom range resolves to the closed inclusive pair** the user picked
 *     and keeps both bounds."
 *   - same section: "A preset with no matching label falls back to that
 *     resolved-bounds form for the granularity with its open upper bound
 *     rendered as the literal `now` — `YYYY-MM-DD – now <tz>` (date) /
 *     `YYYY-MM-DD HH:mm – now <tz>` (datetime)."
 *   - same section: "Presets are **relative**: a preset stores intent rather
 *     than pinned bounds — a lower bound resolved against the present and an
 *     upper bound left open — so 'Last 7 days' always includes today and
 *     everything recorded since; custom ranges are **absolute**."
 *
 * The two resolvers are held apart deliberately and each block says which one it
 * exercises: `resolveRange`/`presetRange` are the QUERY path (open above for a
 * preset) and `resolveRangeForEdit` is the EDITOR path (a concrete closed pair
 * for the picker's calendars). The whole-day / instant upper-bound math belongs
 * to the editor path, so the day-math assertions live in the editor blocks.
 *
 * SPEC vs IMPL CONTRACT — the spec fixes the *shape* of a preset window (a lower
 * bound resolved against the present, an upper bound left open, "always includes
 * today and everything recorded since"). It does not fix the exact instants. So
 * every assertion on a concrete millisecond below — the `T00:00:00.000Z` lower
 * bound, the `T23:59:59.999Z` editor upper bound, the `day - (days - 1)`
 * step-back, and the `days * 86_400_000 - 1` span — is an IMPL contract inferred
 * from the preset label, kept as an off-by-one guard. Those are labelled inline
 * where they appear; only the quoted sentences above are spec text.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  DEFAULT_PRESET_DAYS,
  presetRange,
  resolveRange,
  resolveRangeForEdit,
  defaultSelection,
  selectionLabel,
  isRangeSelection,
  formatRange,
} from "./range";

// Fixed "now" so UTC-day math is deterministic: 2024-03-15T08:30:00.000Z.
const NOW = new Date("2024-03-15T08:30:00.000Z");

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.useRealTimers();
});

// IMPL CONTRACT (not spec text): the exact start-of-day (T00:00:00.000Z) lower
// bounds below pin the impl's deliberate whole-UTC-day choice. The spec fixes
// only that the lower bound is "resolved against the present" and rendered as
// `YYYY-MM-DD` (date granularity). These assertions are TZ-stable under the
// frozen clock and guard against off-by-one regressions in the day math.
//
// QUERY path. spec/feature/FRONTEND_BASIC.md §shared-component-notes: "A preset
// resolves to an open-ended window — the lower bound only, with `to`/`until`
// omitted". The `to === undefined` assertions are not vacuous: the custom
// pass-through blocks below (resolveRange / resolveRangeForEdit on a custom
// selection) prove the same functions DO return a `to` when the selection
// carries one, so a resolver that dropped every upper bound would fail there.
describe("presetRange — date granularity (query path: open above)", () => {
  it("Last 1 day starts at today's UTC midnight and is open above", () => {
    const r = presetRange(1, "date", "utc");
    expect(r.from).toBe("2024-03-15T00:00:00.000Z");
    expect(r.to).toBeUndefined();
  });

  it("Last 7 days starts six days back and is open above", () => {
    const r = presetRange(7, "date", "utc");
    expect(r.from).toBe("2024-03-09T00:00:00.000Z");
    expect(r.to).toBeUndefined();
  });

  it("Last 14 days from is 13 days before today at start of UTC day", () => {
    const r = presetRange(14, "date", "utc");
    expect(r.from).toBe("2024-03-02T00:00:00.000Z");
    expect(r.to).toBeUndefined();
  });

  // Calendar-day arithmetic has no exception at a month or year boundary.
  // IMPL CONTRACT (inferred from the preset label, not spec text): the impl
  // steps back with `day - (days - 1)`, i.e. a 7-day preset starts six days
  // before today. That expression goes NON-POSITIVE here (2 - 6 = -4) and relies
  // on Date.UTC normalizing the field. Every other case in this file keeps it
  // ≥ 1, so a regression that clamped it (or hand-rolled the borrow) would pass
  // them all.
  it("steps back across a month boundary into a leap February", () => {
    vi.setSystemTime(new Date("2024-03-02T08:30:00.000Z"));
    const r = presetRange(7, "date", "utc");
    // 2024-03-02 minus six days = 2024-02-25 — correct only if Feb 29 exists.
    expect(r.from).toBe("2024-02-25T00:00:00.000Z");
    expect(r.to).toBeUndefined();
  });

  it("steps back across a year boundary", () => {
    vi.setSystemTime(new Date("2024-01-03T08:30:00.000Z"));
    const r = presetRange(7, "date", "utc");
    expect(r.from).toBe("2023-12-28T00:00:00.000Z");
    expect(r.to).toBeUndefined();
  });
});

describe("presetRange — datetime granularity (query path: open above)", () => {
  it("bounds the lower instant at now - days and leaves the upper open", () => {
    const r = presetRange(1, "datetime", "utc");
    expect(r.from).toBe("2024-03-14T08:30:00.000Z");
    expect(r.to).toBeUndefined();
  });

  it("subtracts the full day count for the lower bound", () => {
    const r = presetRange(7, "datetime", "utc");
    expect(r.from).toBe("2024-03-08T08:30:00.000Z");
    expect(r.to).toBeUndefined();
  });
});

// EDITOR path. The picker's calendars and time fields need a concrete bound on
// both ends, so resolveRangeForEdit pins the window a preset covers *right now*.
// The whole-UTC-day, inclusive end-of-day upper bound (T23:59:59.999Z) is
// exercised here — through the one entry point that is allowed to produce it.
//
// spec/feature/FRONTEND_BASIC.md §shared-component-notes: the popover "presents
// the preset shortcuts alongside two calendars" seeded from the active
// selection; the same section keeps that pinned upper bound off the query path —
// "**A preset resolves to an open-ended window** — the lower bound only, with
// `to`/`until` omitted".
//
// IMPL CONTRACT (not spec text), the counterpart of the query-path note above
// for the other bound family: the exact end-of-day millisecond
// (T23:59:59.999Z) is the impl's inclusive whole-day choice. The spec's only
// statement about the covered span is that a preset "always includes today and
// everything recorded since" (FRONTEND_BASIC.md §shared-component-notes); the
// day count implied by the "Last 7 days" label is what the millisecond
// assertions below encode, as an off-by-one guard.
describe("resolveRangeForEdit — date granularity closes the preset window", () => {
  it("Last 1 day is today only (whole UTC day)", () => {
    const r = resolveRangeForEdit({ kind: "preset", days: 1 }, "date", "utc");
    expect(r.from).toBe("2024-03-15T00:00:00.000Z");
    expect(r.to).toBe("2024-03-15T23:59:59.999Z");
  });

  it("Last 7 days closes at today's end-of-day, six whole days after its start", () => {
    const r = resolveRangeForEdit({ kind: "preset", days: 7 }, "date", "utc");
    expect(r.from).toBe("2024-03-09T00:00:00.000Z");
    expect(r.to).toBe("2024-03-15T23:59:59.999Z");
  });

  it("Last 14 days from is 13 days before today at start of UTC day", () => {
    const r = resolveRangeForEdit({ kind: "preset", days: 14 }, "date", "utc");
    expect(r.from).toBe("2024-03-02T00:00:00.000Z");
    expect(r.to).toBe("2024-03-15T23:59:59.999Z");
  });

  // Boundary twin of the query-path case: the editor's whole-day math must
  // normalize a non-positive day field the same way.
  it("steps back across a month boundary into a leap February", () => {
    vi.setSystemTime(new Date("2024-03-02T08:30:00.000Z"));
    const r = resolveRangeForEdit({ kind: "preset", days: 7 }, "date", "utc");
    expect(r.from).toBe("2024-02-25T00:00:00.000Z");
    expect(r.to).toBe("2024-03-02T23:59:59.999Z");
  });

  it("steps back across a year boundary", () => {
    vi.setSystemTime(new Date("2024-01-03T08:30:00.000Z"));
    const r = resolveRangeForEdit({ kind: "preset", days: 7 }, "date", "utc");
    expect(r.from).toBe("2023-12-28T00:00:00.000Z");
    expect(r.to).toBe("2024-01-03T23:59:59.999Z");
  });
});

// ---------------------------------------------------------------------------
// The invariants the two-resolver split rests on, swept over a wide preset
// range so no single frozen date can carry them.
//
// spec/feature/FRONTEND_BASIC.md §shared-component-notes: a preset resolves to
// "the lower bound only" on the query path, and the picker's popover seeds its
// calendars from the same selection — so the two resolvers must agree on the
// lower bound. The spec's statement about the covered span is that a preset
// "always includes today and everything recorded since"; the exact `days`-long
// closed window the editor produces from that label is IMPL CONTRACT, asserted
// below as an off-by-one guard.
// ---------------------------------------------------------------------------
describe("presetRange / resolveRangeForEdit — swept invariants", () => {
  const DAY_LENGTHS = [1, 2, 7, 14, 28, 84, 365, 400];

  it("both resolvers derive the SAME lower bound, for every length/granularity/tz", () => {
    for (const days of DAY_LENGTHS) {
      for (const granularity of ["date", "datetime"] as const) {
        for (const tz of ["utc", "local"] as const) {
          expect(presetRange(days, granularity, tz).from).toBe(
            resolveRangeForEdit({ kind: "preset", days }, granularity, tz).from,
          );
        }
      }
    }
  });

  it("the editor window spans exactly `days` whole UTC days in date mode", () => {
    // IMPL CONTRACT (not spec text): `days * 86_400_000 - 1` encodes the
    // inclusive whole-day window the impl derives from the "Last N days" label.
    // Asserted in utc only: a local-tz span crosses DST transitions, where a
    // calendar day is not 24h. 400 days reaches back past 2023-02 from the
    // frozen 2024-03-15 clock, crossing both a year boundary and a leap day.
    for (const days of DAY_LENGTHS) {
      const r = resolveRangeForEdit({ kind: "preset", days }, "date", "utc");
      const span = new Date(r.to).getTime() - new Date(r.from).getTime();
      expect(span).toBe(days * 86_400_000 - 1);
      expect(new Date(r.from).getTime()).toBeLessThan(new Date(r.to).getTime());
    }
  });

  it("the editor window is ordered from ≤ to in datetime mode too", () => {
    for (const days of DAY_LENGTHS) {
      const r = resolveRangeForEdit({ kind: "preset", days }, "datetime", "utc");
      expect(new Date(r.from).getTime()).toBeLessThan(
        new Date(r.to).getTime(),
      );
    }
  });
});

describe("resolveRangeForEdit — datetime granularity closes the preset window", () => {
  it("bounds the exact instant: from = now - days, to = now", () => {
    const r = resolveRangeForEdit({ kind: "preset", days: 1 }, "datetime", "utc");
    expect(r.to).toBe("2024-03-15T08:30:00.000Z");
    expect(r.from).toBe("2024-03-14T08:30:00.000Z");
  });

  it("subtracts the full day count for the lower bound", () => {
    const r = resolveRangeForEdit({ kind: "preset", days: 7 }, "datetime", "utc");
    expect(r.from).toBe("2024-03-08T08:30:00.000Z");
    expect(r.to).toBe("2024-03-15T08:30:00.000Z");
  });
});

describe("resolveRangeForEdit — custom selection", () => {
  it("returns the user's bounds verbatim (nothing is re-derived from the clock)", () => {
    const sel = {
      kind: "custom" as const,
      from: "2024-03-01T00:00:00.000Z",
      to: "2024-03-05T23:59:59.999Z",
    };
    expect(resolveRangeForEdit(sel, "date", "utc")).toEqual({
      from: sel.from,
      to: sel.to,
    });
    expect(resolveRangeForEdit(sel, "datetime", "utc")).toEqual({
      from: sel.from,
      to: sel.to,
    });
  });
});

describe("defaultSelection", () => {
  it("is the DEFAULT_PRESET_DAYS preset", () => {
    expect(defaultSelection()).toEqual({ kind: "preset", days: DEFAULT_PRESET_DAYS });
  });
});

describe("resolveRange", () => {
  it("resolves a preset against now (equals presetRange — relative renewal)", () => {
    // A preset tracks "now", not a frozen value: resolving it equals computing
    // presetRange against the current clock.
    expect(resolveRange({ kind: "preset", days: 7 }, "date", "utc")).toEqual(
      presetRange(7, "date", "utc"),
    );
    // spec/feature/FRONTEND_BASIC.md §shared-component-notes: "A preset resolves
    // to an open-ended window — the lower bound only, with `to`/`until` omitted".
    expect(resolveRange({ kind: "preset", days: 7 }, "date", "utc")).toEqual({
      from: "2024-03-09T00:00:00.000Z",
    });
    expect(
      resolveRange({ kind: "preset", days: 7 }, "date", "utc").to,
    ).toBeUndefined();
  });

  it("renews a preset's lower bound after the clock advances a day", () => {
    // Core invariant: a stored preset re-resolves against the CURRENT day, so a
    // fresh visit slides the lower bound forward. The upper bound is open in
    // both resolutions — the window reaches the present without being re-pinned.
    const before = resolveRange({ kind: "preset", days: 7 }, "date", "utc");
    expect(before.from).toBe("2024-03-09T00:00:00.000Z");
    expect(before.to).toBeUndefined();

    // Advance the clock to the next UTC day.
    vi.setSystemTime(new Date("2024-03-16T08:30:00.000Z"));

    const after = resolveRange({ kind: "preset", days: 7 }, "date", "utc");
    // Window now starts six days before the NEW day, and is still open above.
    expect(after.from).toBe("2024-03-10T00:00:00.000Z");
    expect(after.to).toBeUndefined();
  });

  it("renews a datetime preset's lower bound to the new instant", () => {
    const before = resolveRange({ kind: "preset", days: 1 }, "datetime", "utc");
    expect(before.from).toBe("2024-03-14T08:30:00.000Z");
    expect(before.to).toBeUndefined();

    vi.setSystemTime(new Date("2024-03-16T09:00:00.000Z"));

    const after = resolveRange({ kind: "preset", days: 1 }, "datetime", "utc");
    expect(after.from).toBe("2024-03-15T09:00:00.000Z");
    expect(after.to).toBeUndefined();
  });

  it("passes custom bounds through unchanged", () => {
    const sel = {
      kind: "custom" as const,
      from: "2024-03-01T00:00:00.000Z",
      to: "2024-03-05T23:59:59.999Z",
    };
    expect(resolveRange(sel, "date", "utc")).toEqual({ from: sel.from, to: sel.to });
  });

  it("keeps a custom selection absolute when the clock advances", () => {
    // Custom windows pin concrete bounds — clock-independent (the opposite of
    // the preset renewal invariant above).
    const sel = {
      kind: "custom" as const,
      from: "2024-03-01T00:00:00.000Z",
      to: "2024-03-05T23:59:59.999Z",
    };
    const before = resolveRange(sel, "date", "utc");

    vi.setSystemTime(new Date("2024-06-20T00:00:00.000Z"));

    const after = resolveRange(sel, "date", "utc");
    expect(after).toEqual(before);
    expect(after).toEqual({ from: sel.from, to: sel.to });
  });
});

describe("isRangeSelection", () => {
  it("accepts valid preset and custom shapes", () => {
    expect(isRangeSelection({ kind: "preset", days: 7 })).toBe(true);
    expect(
      isRangeSelection({ kind: "custom", from: "a", to: "b" }),
    ).toBe(true);
  });

  it("rejects null and non-object primitives", () => {
    expect(isRangeSelection(null)).toBe(false);
    expect(isRangeSelection(undefined)).toBe(false);
    expect(isRangeSelection(42)).toBe(false);
    expect(isRangeSelection("preset")).toBe(false);
    expect(isRangeSelection(true)).toBe(false);
  });

  it("rejects a preset with missing or non-number days", () => {
    expect(isRangeSelection({ kind: "preset" })).toBe(false);
    expect(isRangeSelection({ kind: "preset", days: "7" })).toBe(false);
    expect(isRangeSelection({ kind: "preset", days: null })).toBe(false);
  });

  it("rejects a custom selection with missing or non-string bounds", () => {
    expect(isRangeSelection({ kind: "custom", from: 1, to: 2 })).toBe(false);
    expect(isRangeSelection({ kind: "custom", from: "a" })).toBe(false);
    expect(isRangeSelection({ kind: "custom", to: "b" })).toBe(false);
    expect(isRangeSelection({ kind: "custom" })).toBe(false);
  });

  it("rejects an unknown or missing kind", () => {
    expect(isRangeSelection({ kind: "other" })).toBe(false);
    expect(isRangeSelection({ days: 7 })).toBe(false);
    expect(isRangeSelection({})).toBe(false);
  });
});

describe("selectionLabel", () => {
  it("uses the preset label for a known preset", () => {
    expect(selectionLabel({ kind: "preset", days: 7 }, "date", "utc")).toBe(
      "Last 7 days",
    );
  });

  it("formats a custom selection (with tz tag)", () => {
    expect(
      selectionLabel(
        {
          kind: "custom",
          from: "2024-03-09T00:00:00.000Z",
          to: "2024-03-15T23:59:59.999Z",
        },
        "date",
        "utc",
      ),
    ).toBe("2024-03-09 – 2024-03-15 UTC");
  });

  // spec/feature/FRONTEND_BASIC.md §shared-component-notes: "A preset with no
  // matching label falls back to that resolved-bounds form for the granularity
  // with its open upper bound rendered as the literal `now` — `YYYY-MM-DD – now
  // <tz>` (date) / `YYYY-MM-DD HH:mm – now <tz>` (datetime)."
  //
  // `days: 3` is deliberately absent from RANGE_PRESETS; the "known preset"
  // case above (days: 7 → "Last 7 days") is the backstop proving the fallback
  // branch is genuinely the one under test here.
  it("falls back to the open resolved window for an unmatched preset (date)", () => {
    expect(selectionLabel({ kind: "preset", days: 3 }, "date", "utc")).toBe(
      "2024-03-13 – now UTC",
    );
  });

  it("falls back to the open resolved window for an unmatched preset (datetime)", () => {
    expect(selectionLabel({ kind: "preset", days: 3 }, "datetime", "utc")).toBe(
      "2024-03-12 08:30 – now UTC",
    );
  });
});

describe("formatRange — closed window (custom range)", () => {
  it("formats date granularity as YYYY-MM-DD – YYYY-MM-DD UTC", () => {
    const closed = {
      from: "2024-03-09T00:00:00.000Z",
      to: "2024-03-15T23:59:59.999Z",
    };
    expect(formatRange(closed, "date", "utc")).toBe("2024-03-09 – 2024-03-15 UTC");
  });

  it("formats datetime granularity with HH:mm and a UTC tag", () => {
    const closed = {
      from: "2024-03-14T08:30:00.000Z",
      to: "2024-03-15T08:30:00.000Z",
    };
    expect(formatRange(closed, "datetime", "utc")).toBe(
      "2024-03-14 08:30 – 2024-03-15 08:30 UTC",
    );
  });
});

describe("formatRange — open window (preset)", () => {
  // Guards the silent-NaN failure mode: formatting an absent bound as a date
  // yields "NaN-NaN-NaN" rather than throwing, so the assertions pin the
  // literal "now" AND explicitly reject NaN leaking into the label.
  it("renders the absent upper bound as the literal 'now' (date)", () => {
    const open = presetRange(7, "date", "utc");
    const label = formatRange(open, "date", "utc");
    expect(label).toBe("2024-03-09 – now UTC");
    expect(label).not.toContain("NaN");
  });

  it("renders the absent upper bound as the literal 'now' (datetime)", () => {
    const open = presetRange(1, "datetime", "utc");
    const label = formatRange(open, "datetime", "utc");
    expect(label).toBe("2024-03-14 08:30 – now UTC");
    expect(label).not.toContain("NaN");
  });

  it("keeps the tz tag in its trailing position for the local zone too", () => {
    const label = formatRange({ from: "2024-03-09T00:00:00.000Z" }, "date", "local");
    expect(label.endsWith(" – now (local)")).toBe(true);
    expect(label).not.toContain("NaN");
  });
});

// ---------------------------------------------------------------------------
// tz="local" coverage.
//
// FLAKINESS CAVEAT: tz="local" math depends on the HOST machine timezone, which
// vi.setSystemTime does NOT fix (it freezes the clock, not the zone). Hardcoding
// a UTC instant for a local wall-clock value would assume a specific offset and
// break on any CI box not in that zone. So every assertion below is
// offset-agnostic: we assert tz-stable INVARIANTS — round-trip / property
// equalities and the local wall-clock getters of the produced bounds — never an
// absolute ISO string. These hold in any host timezone, including offset-0 (UTC)
// CI environments where local and utc happen to coincide.
//
// Spec trace: spec/feature/FRONTEND_BASIC.md §shared-component-notes — "The
//   picker has **no per-panel timezone control**: like all timestamps in the UI,
//   the calendar days and times it shows are interpreted and displayed in the
//   **global Settings timezone preference** (Local or UTC, default Local). Every
//   bound it emits is a canonical inclusive UTC ISO instant regardless." The `tz`
//   argument here is that global preference threaded through.
// ---------------------------------------------------------------------------
describe("presetRange — date granularity, tz=local (offset-agnostic)", () => {
  it("from is LOCAL midnight, and the window stays open above", () => {
    const r = presetRange(7, "date", "local");
    const from = new Date(r.from);

    // Read the produced bound back with LOCAL getters — independent of host
    // offset. from = 00:00:00.000 local.
    expect(from.getHours()).toBe(0);
    expect(from.getMinutes()).toBe(0);
    expect(from.getSeconds()).toBe(0);
    expect(from.getMilliseconds()).toBe(0);

    expect(r.to).toBeUndefined();
  });

  it("from is (days-1) local days earlier than today", () => {
    const now = new Date(); // frozen at NOW
    const r = presetRange(7, "date", "local");
    const from = new Date(r.from);

    // The window starts exactly 6 distinct local calendar days back: the
    // local-midnight `from` plus six days reaches the local-midnight of today.
    // Compare against a locally-constructed reference rather than a hardcoded
    // instant.
    const expectedFrom = new Date(
      now.getFullYear(),
      now.getMonth(),
      now.getDate() - 6,
      0,
      0,
      0,
      0,
    );
    expect(from.getTime()).toBe(expectedFrom.getTime());
  });

  it("Last 1 day local starts at today's local midnight", () => {
    const now = new Date();
    const r = presetRange(1, "date", "local");
    const from = new Date(r.from);
    expect(from.getDate()).toBe(now.getDate());
    expect(from.getHours()).toBe(0);
    expect(r.to).toBeUndefined();
  });
});

// The whole-local-day upper bound math moved here with the closed window: same
// offset-agnostic invariants, now asserted on the editor entry point.
describe("resolveRangeForEdit — date granularity, tz=local (offset-agnostic)", () => {
  it("from is LOCAL midnight and to is LOCAL end-of-day", () => {
    const edit = resolveRangeForEdit({ kind: "preset", days: 7 }, "date", "local");
    const from = new Date(edit.from);
    const to = new Date(edit.to);

    // Read the produced bounds back with LOCAL getters — independent of host
    // offset. from = 00:00:00.000 local; to = 23:59:59.999 local.
    expect(from.getHours()).toBe(0);
    expect(from.getMinutes()).toBe(0);
    expect(from.getSeconds()).toBe(0);
    expect(from.getMilliseconds()).toBe(0);

    expect(to.getHours()).toBe(23);
    expect(to.getMinutes()).toBe(59);
    expect(to.getSeconds()).toBe(59);
    expect(to.getMilliseconds()).toBe(999);
  });

  it("to is today's LOCAL date and from is (days-1) local days earlier", () => {
    const now = new Date(); // frozen at NOW
    const edit = resolveRangeForEdit({ kind: "preset", days: 7 }, "date", "local");
    const from = new Date(edit.from);
    const to = new Date(edit.to);

    // `to` lands on today's local calendar date.
    expect(to.getFullYear()).toBe(now.getFullYear());
    expect(to.getMonth()).toBe(now.getMonth());
    expect(to.getDate()).toBe(now.getDate());

    // The window spans exactly 7 distinct local calendar days: the local-midnight
    // `from` plus six days reaches the local-midnight of today. Compare against a
    // locally-constructed reference rather than a hardcoded instant.
    const expectedFrom = new Date(
      now.getFullYear(),
      now.getMonth(),
      now.getDate() - 6,
      0,
      0,
      0,
      0,
    );
    expect(from.getTime()).toBe(expectedFrom.getTime());
  });

  it("Last 1 day local is today only (local midnight → local end-of-day)", () => {
    const now = new Date();
    const edit = resolveRangeForEdit({ kind: "preset", days: 1 }, "date", "local");
    const from = new Date(edit.from);
    const to = new Date(edit.to);
    expect(from.getDate()).toBe(now.getDate());
    expect(to.getDate()).toBe(now.getDate());
    expect(from.getHours()).toBe(0);
    expect(to.getHours()).toBe(23);
  });
});

describe("presetRange — datetime granularity is tz-independent", () => {
  it("from=now-days identically for local and utc (absolute instants)", () => {
    // Datetime bounds are absolute instants computed purely from the clock, so
    // the emitted ISO strings are identical regardless of tz interpretation.
    const local = presetRange(7, "datetime", "local");
    const utc = presetRange(7, "datetime", "utc");
    expect(local).toEqual(utc);
    // And it pins the frozen clock exactly (tz-stable: pure instant math).
    expect(local.from).toBe("2024-03-08T08:30:00.000Z");
    expect(local.to).toBeUndefined();
  });

  it("the editor's closed pair is tz-independent too (to = now)", () => {
    const local = resolveRangeForEdit({ kind: "preset", days: 7 }, "datetime", "local");
    const utc = resolveRangeForEdit({ kind: "preset", days: 7 }, "datetime", "utc");
    expect(local).toEqual(utc);
    expect(local.to).toBe("2024-03-15T08:30:00.000Z");
    expect(local.from).toBe("2024-03-08T08:30:00.000Z");
  });
});

describe("resolveRange — tz=local passes custom bounds through unchanged", () => {
  it("custom selection is returned verbatim for both tz values", () => {
    const sel = {
      kind: "custom" as const,
      from: "2024-03-01T04:00:00.000Z",
      to: "2024-03-05T19:30:00.999Z",
    };
    // Custom bounds are absolute UTC instants — tz governs display only, never
    // the stored/resolved bounds. Local and utc resolution must coincide.
    expect(resolveRange(sel, "date", "local")).toEqual({
      from: sel.from,
      to: sel.to,
    });
    expect(resolveRange(sel, "datetime", "local")).toEqual(
      resolveRange(sel, "datetime", "utc"),
    );
  });
});

describe("formatRange / selectionLabel — tz=local tag + local wall-clock", () => {
  it("formatRange (date) ends with the local tag and shows the local date", () => {
    const sel = {
      kind: "custom" as const,
      from: "2024-03-09T00:00:00.000Z",
      to: "2024-03-15T23:59:59.999Z",
    };
    const label = formatRange({ from: sel.from, to: sel.to }, "date", "local");

    // Tag is the local marker, not " UTC".
    expect(label.endsWith(" (local)")).toBe(true);
    expect(label).not.toMatch(/UTC$/);

    // The rendered date portions equal the LOCAL getters of each bound (no
    // hardcoded offset). Build the expected "YYYY-MM-DD" from local getters.
    const pad = (n: number) => String(n).padStart(2, "0");
    const fmtLocalDate = (iso: string) => {
      const d = new Date(iso);
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    };
    expect(label).toBe(
      `${fmtLocalDate(sel.from)} – ${fmtLocalDate(sel.to)} (local)`,
    );
  });

  it("formatRange (datetime) shows local wall-clock HH:mm with the local tag", () => {
    const sel = {
      kind: "custom" as const,
      from: "2024-03-14T08:30:00.000Z",
      to: "2024-03-15T17:45:00.999Z",
    };
    const label = formatRange(
      { from: sel.from, to: sel.to },
      "datetime",
      "local",
    );
    expect(label.endsWith(" (local)")).toBe(true);

    const pad = (n: number) => String(n).padStart(2, "0");
    const fmtLocal = (iso: string) => {
      const d = new Date(iso);
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(
        d.getDate(),
      )} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    };
    expect(label).toBe(`${fmtLocal(sel.from)} – ${fmtLocal(sel.to)} (local)`);
  });

  it("selectionLabel keeps the preset label regardless of tz (no tag for known presets)", () => {
    // A known preset shows its static label in either zone — the tag only
    // appears on formatted (custom / unknown-preset) windows.
    expect(selectionLabel({ kind: "preset", days: 7 }, "date", "local")).toBe(
      "Last 7 days",
    );
  });

  it("selectionLabel (custom) carries the local tag and local wall-clock date", () => {
    const sel = {
      kind: "custom" as const,
      from: "2024-03-09T00:00:00.000Z",
      to: "2024-03-15T23:59:59.999Z",
    };
    const label = selectionLabel(sel, "date", "local");
    expect(label.endsWith(" (local)")).toBe(true);
    const pad = (n: number) => String(n).padStart(2, "0");
    const fmtLocalDate = (iso: string) => {
      const d = new Date(iso);
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    };
    expect(label).toBe(
      `${fmtLocalDate(sel.from)} – ${fmtLocalDate(sel.to)} (local)`,
    );
  });
});
