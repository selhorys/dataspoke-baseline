/**
 * Tests for lib/range.ts — preset math, selection resolution, and formatting
 * across both granularities.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  DEFAULT_PRESET_DAYS,
  presetRange,
  resolveRange,
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

// Note: the exact end-of-day millisecond (T23:59:59.999Z) and start-of-day
// (T00:00:00.000Z) bounds below pin the impl's deliberate whole-UTC-day,
// inclusive contract — not a spec line (the spec only requires inclusive
// {from,to} displayed as YYYY-MM-DD). They are TZ-stable under the frozen clock
// and guard against off-by-one regressions in the day math.
describe("presetRange — date granularity", () => {
  it("Last 1 day is today only (whole UTC day)", () => {
    const r = presetRange(1, "date", "utc");
    expect(r.from).toBe("2024-03-15T00:00:00.000Z");
    expect(r.to).toBe("2024-03-15T23:59:59.999Z");
  });

  it("Last 7 days spans today plus the prior six", () => {
    const r = presetRange(7, "date", "utc");
    expect(r.from).toBe("2024-03-09T00:00:00.000Z");
    expect(r.to).toBe("2024-03-15T23:59:59.999Z");
  });

  it("Last 14 days from is 13 days before today at start of UTC day", () => {
    const r = presetRange(14, "date", "utc");
    expect(r.from).toBe("2024-03-02T00:00:00.000Z");
    expect(r.to).toBe("2024-03-15T23:59:59.999Z");
  });
});

describe("presetRange — datetime granularity", () => {
  it("bounds the exact instant: from = now - days, to = now", () => {
    const r = presetRange(1, "datetime", "utc");
    expect(r.to).toBe("2024-03-15T08:30:00.000Z");
    expect(r.from).toBe("2024-03-14T08:30:00.000Z");
  });

  it("subtracts the full day count for the lower bound", () => {
    const r = presetRange(7, "datetime", "utc");
    expect(r.from).toBe("2024-03-08T08:30:00.000Z");
    expect(r.to).toBe("2024-03-15T08:30:00.000Z");
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
    expect(resolveRange({ kind: "preset", days: 7 }, "date", "utc")).toEqual({
      from: "2024-03-09T00:00:00.000Z",
      to: "2024-03-15T23:59:59.999Z",
    });
  });

  it("renews a preset to include today after the clock advances a day", () => {
    // Core invariant: a stored preset re-resolves to a window ending on the
    // CURRENT day, so it always includes today.
    const before = resolveRange({ kind: "preset", days: 7 }, "date", "utc");
    expect(before.to).toBe("2024-03-15T23:59:59.999Z");

    // Advance the clock to the next UTC day.
    vi.setSystemTime(new Date("2024-03-16T08:30:00.000Z"));

    const after = resolveRange({ kind: "preset", days: 7 }, "date", "utc");
    // Window now ends on the new day and starts six days earlier.
    expect(after.to).toBe("2024-03-16T23:59:59.999Z");
    expect(after.from).toBe("2024-03-10T00:00:00.000Z");
  });

  it("renews a datetime preset to the new instant after the clock advances", () => {
    const before = resolveRange({ kind: "preset", days: 1 }, "datetime", "utc");
    expect(before.to).toBe("2024-03-15T08:30:00.000Z");

    vi.setSystemTime(new Date("2024-03-16T09:00:00.000Z"));

    const after = resolveRange({ kind: "preset", days: 1 }, "datetime", "utc");
    expect(after.to).toBe("2024-03-16T09:00:00.000Z");
    expect(after.from).toBe("2024-03-15T09:00:00.000Z");
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
});

describe("formatRange", () => {
  it("formats date granularity as YYYY-MM-DD – YYYY-MM-DD UTC", () => {
    const r = presetRange(7, "date", "utc");
    expect(formatRange(r, "date", "utc")).toBe("2024-03-09 – 2024-03-15 UTC");
  });

  it("formats datetime granularity with HH:mm and a UTC tag", () => {
    const r = presetRange(1, "datetime", "utc");
    expect(formatRange(r, "datetime", "utc")).toBe(
      "2024-03-14 08:30 – 2024-03-15 08:30 UTC",
    );
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
// Spec trace: spec/feature/FRONTEND_BASIC.md — the global Settings timezone
//   preference (Local/UTC) governs how calendar days and times are interpreted
//   and displayed; the emitted/queried bounds remain canonical inclusive UTC ISO
//   regardless. The `tz` argument here is that global preference threaded through
//   (there is no per-picker timezone control).
// ---------------------------------------------------------------------------
describe("presetRange — date granularity, tz=local (offset-agnostic)", () => {
  it("from is LOCAL midnight and to is LOCAL end-of-day", () => {
    const r = presetRange(7, "date", "local");
    const from = new Date(r.from);
    const to = new Date(r.to);

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
    const r = presetRange(7, "date", "local");
    const from = new Date(r.from);
    const to = new Date(r.to);

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
    const r = presetRange(1, "date", "local");
    const from = new Date(r.from);
    const to = new Date(r.to);
    expect(from.getDate()).toBe(now.getDate());
    expect(to.getDate()).toBe(now.getDate());
    expect(from.getHours()).toBe(0);
    expect(to.getHours()).toBe(23);
  });
});

describe("presetRange — datetime granularity is tz-independent", () => {
  it("from=now-days, to=now identically for local and utc (absolute instants)", () => {
    // Datetime bounds are absolute instants computed purely from the clock, so
    // the emitted ISO strings are identical regardless of tz interpretation.
    const local = presetRange(7, "datetime", "local");
    const utc = presetRange(7, "datetime", "utc");
    expect(local).toEqual(utc);
    // And they pin the frozen clock exactly (tz-stable: pure instant math).
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
