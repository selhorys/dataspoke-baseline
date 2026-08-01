/**
 * Tests for lib/chart-grain.ts — the display-grain bucketing shared by every
 * chart surface.
 *
 * Spec traces (all quotes from spec/feature/FRONTEND_BASIC.md
 * §Shared Component Notes → ChartGrainPicker):
 *   - "It selects one of three grains — **hourly**, **daily** (default),
 *     **weekly** — governing how the rows a chart has already fetched are
 *     collapsed before plotting."
 *   - "Rows are bucketed into grain windows and each window contributes exactly
 *     **one** point: that window's **last** measurement (greatest timestamp),
 *     labelled by the truncated window start, carrying enough date component to
 *     stay unique across the selected range (hourly windows include the date,
 *     not the hour alone). Every x label is therefore distinct."
 *   - "Window boundaries are derived in the **global Settings timezone
 *     preference** (Local or UTC, default Local) … so switching Local↔UTC
 *     re-derives the buckets; weekly windows start on **Monday** and are
 *     labelled by that Monday's date."
 *   - "A row whose timestamp does not parse contributes to no window and is
 *     dropped rather than grouped under a placeholder label; when two rows in a
 *     window carry the same timestamp the later one in the fetched order wins;
 *     and because the window label occupies the `date` key, a series named
 *     `date` is never plotted."
 *
 * Ordering ("output ascending by window") is the one rule below with no verbatim
 * spec sentence; it follows from "labelled by the truncated window start" over a
 * left-to-right time axis and is pinned here as the module contract documented in
 * lib/chart-grain.ts.
 *
 * TZ discipline, in three layers:
 *   1. Every window assertion that is not *about* the timezone uses tz="utc", so
 *      the expected window is host-independent.
 *   2. The Local↔UTC divergence contract is asserted under a TZ *pinned by the
 *      test* (Asia/Seoul, UTC+9, no DST), with hard-coded expectations. A host
 *      running at UTC would make every host-getter-derived expectation coincide,
 *      so an implementation that ignored the tz argument entirely would pass —
 *      the pin is what closes that.
 *   3. The host-getter round-trip tests are kept as a second, offset-agnostic
 *      reading of the same contract.
 */

import { describe, it, expect, beforeAll, afterAll } from "vitest";
import {
  CHART_GRAINS,
  DEFAULT_CHART_GRAIN,
  grainBucket,
  isChartGrain,
  toGrainPoints,
} from "./chart-grain";

// ── Fixtures ────────────────────────────────────────────────────────────────────
// A minimal row shape: an ISO instant plus a values dict, exactly what the three
// chart call sites project (measured_at/values, data_time/variables).

interface Row {
  t: string;
  v: Record<string, number>;
}

function row(t: string, v: Record<string, number>): Row {
  return { t, v };
}

/** toGrainPoints over Row[] at the given grain/tz. */
function points(rows: Row[], grain: "hourly" | "daily" | "weekly", tz: "utc" | "local" = "utc") {
  return toGrainPoints(rows, {
    grain,
    tz,
    timeOf: (r) => r.t,
    valuesOf: (r) => r.v,
  });
}

/**
 * The host's UTC offset for a fixed instant, captured at import time — before any
 * test pins process.env.TZ. Used to prove the pin is undone afterwards.
 */
const HOST_OFFSET_MINUTES = new Date("2026-05-04T20:00:00Z").getTimezoneOffset();

// Calendar anchors used below (all verified by hand against the Gregorian
// calendar, so the tests do not restate whatever the implementation computes):
//   2026-05-04 is a MONDAY  → week 2026-05-04 … 2026-05-10 (Sunday)
//   2026-05-03 is a SUNDAY  → belongs to the week of Monday 2026-04-27
//   2026-03-01 is a SUNDAY  → belongs to the week of Monday 2026-02-23
//   2026-01-01 is a THURSDAY→ belongs to the week of Monday 2025-12-29

// ── Grain vocabulary ────────────────────────────────────────────────────────────

describe("grain vocabulary (FRONTEND_BASIC.md §Shared Component Notes → ChartGrainPicker)", () => {
  it("offers exactly the three spec'd grains, in the spec's order", () => {
    // spec: "It selects one of three grains — hourly, daily (default), weekly".
    expect([...CHART_GRAINS]).toEqual(["hourly", "daily", "weekly"]);
  });

  it("defaults to daily", () => {
    // spec: "daily (default)".
    expect(DEFAULT_CHART_GRAIN).toBe("daily");
  });

  it("accepts only the three grains as a persisted value", () => {
    // The guard is what makes "persists across visits in browser localStorage"
    // safe against a hand-edited / stale stored value.
    for (const g of CHART_GRAINS) expect(isChartGrain(g)).toBe(true);
    expect(isChartGrain("yearly")).toBe(false);
    expect(isChartGrain("")).toBe(false);
    expect(isChartGrain("Daily")).toBe(false);
    expect(isChartGrain(null)).toBe(false);
    expect(isChartGrain(undefined)).toBe(false);
    expect(isChartGrain({ grain: "daily" })).toBe(false);
  });
});

// ── One point per window, carrying the window's LAST measurement ────────────────

describe("toGrainPoints — one point per window, the window's last measurement", () => {
  it("collapses two measurements in the same window to one point carrying the later values", () => {
    // spec: "each window contributes exactly one point: that window's last
    // measurement (greatest timestamp)".
    const out = points(
      [
        row("2026-05-04T01:00:00Z", { total: 10, doc_health: 4 }),
        row("2026-05-04T09:00:00Z", { total: 11, doc_health: 7 }),
      ],
      "daily",
    );

    expect(out).toEqual([{ date: "2026-05-04", total: 11, doc_health: 7 }]);
  });

  it("emits one point per distinct window, ascending by window", () => {
    // spec: "Every x label is therefore distinct" + labelled by the truncated
    // window start; windows plot left-to-right in time order.
    const out = points(
      [
        row("2026-05-06T12:00:00Z", { total: 3 }),
        row("2026-05-04T12:00:00Z", { total: 1 }),
        row("2026-05-05T12:00:00Z", { total: 2 }),
      ],
      "daily",
    );

    expect(out.map((p) => p.date)).toEqual(["2026-05-04", "2026-05-05", "2026-05-06"]);
    expect(out.map((p) => p.total)).toEqual([1, 2, 3]);
  });

  it("picks the window's latest measurement regardless of input ordering", () => {
    // Same six rows, three orderings. The collapsed result is a function of the
    // instants, not of the order the API happened to return them in.
    const chronological = [
      row("2026-05-04T01:00:00Z", { total: 1 }),
      row("2026-05-04T07:00:00Z", { total: 2 }),
      row("2026-05-04T23:00:00Z", { total: 3 }),
      row("2026-05-05T02:00:00Z", { total: 4 }),
      row("2026-05-05T08:00:00Z", { total: 5 }),
      row("2026-05-05T21:00:00Z", { total: 6 }),
    ];
    const expected = [
      { date: "2026-05-04", total: 3 },
      { date: "2026-05-05", total: 6 },
    ];

    const reversed = [...chronological].reverse();
    const interleaved = [
      chronological[5],
      chronological[0],
      chronological[3],
      chronological[2],
      chronological[4],
      chronological[1],
    ];

    expect(points(chronological, "daily")).toEqual(expected);
    expect(points(reversed, "daily")).toEqual(expected);
    expect(points(interleaved, "daily")).toEqual(expected);
  });

  it("resolves an exact timestamp tie to the later row in input order", () => {
    // spec: "when two rows in a window carry the same timestamp the later one in
    // the fetched order wins" — so the collapse stays deterministic for a server
    // that returns duplicates.
    const out = points(
      [
        row("2026-05-04T09:00:00Z", { total: 1 }),
        row("2026-05-04T12:00:00Z", { total: 2 }),
        row("2026-05-04T12:00:00Z", { total: 3 }),
      ],
      "daily",
    );

    expect(out).toEqual([{ date: "2026-05-04", total: 3 }]);
  });

  it("carries only the winning row's value keys (no merge across the window)", () => {
    // A point is one measurement, not a union: a key present only in an earlier
    // row of the window must not leak into the window's point.
    const out = points(
      [
        row("2026-05-04T01:00:00Z", { total: 1, legacy_only: 99 }),
        row("2026-05-04T23:00:00Z", { total: 2 }),
      ],
      "daily",
    );

    expect(out).toEqual([{ date: "2026-05-04", total: 2 }]);
    expect(out[0]).not.toHaveProperty("legacy_only");
  });
});

// ── Window boundaries ───────────────────────────────────────────────────────────

describe("grainBucket — hourly windows carry the date", () => {
  it("labels an hourly window YYYY-MM-DD HH:00", () => {
    // spec: "hourly windows include the date, not the hour alone".
    expect(grainBucket("2026-05-04T09:15:00Z", "hourly", "utc")).toBe("2026-05-04 09:00");
    expect(grainBucket("2026-05-04T00:00:00Z", "hourly", "utc")).toBe("2026-05-04 00:00");
  });

  it("does NOT collapse the same clock hour on two different days", () => {
    // The whole point of carrying the date: two 09:xx measurements a day apart
    // are two windows, so the x labels stay distinct across a multi-day range.
    const out = points(
      [
        row("2026-05-04T09:15:00Z", { total: 1 }),
        row("2026-05-05T09:45:00Z", { total: 2 }),
      ],
      "hourly",
    );

    expect(out).toEqual([
      { date: "2026-05-04 09:00", total: 1 },
      { date: "2026-05-05 09:00", total: 2 },
    ]);
  });

  it("collapses two measurements inside one clock hour to the later one", () => {
    const out = points(
      [
        row("2026-05-04T09:15:00Z", { total: 1 }),
        row("2026-05-04T09:45:00Z", { total: 2 }),
      ],
      "hourly",
    );

    expect(out).toEqual([{ date: "2026-05-04 09:00", total: 2 }]);
  });

  it("separates the last second of an hour from the first second of the next", () => {
    const out = points(
      [
        row("2026-05-04T09:59:59Z", { total: 1 }),
        row("2026-05-04T10:00:00Z", { total: 2 }),
      ],
      "hourly",
    );

    expect(out.map((p) => p.date)).toEqual(["2026-05-04 09:00", "2026-05-04 10:00"]);
  });
});

describe("grainBucket — daily windows", () => {
  it("labels a daily window YYYY-MM-DD", () => {
    expect(grainBucket("2026-05-04T09:15:00Z", "daily", "utc")).toBe("2026-05-04");
  });

  it("separates 23:59:59 from 00:00:00 of the next day", () => {
    const out = points(
      [
        row("2026-05-04T23:59:59Z", { total: 1 }),
        row("2026-05-05T00:00:00Z", { total: 2 }),
      ],
      "daily",
    );

    expect(out).toEqual([
      { date: "2026-05-04", total: 1 },
      { date: "2026-05-05", total: 2 },
    ]);
  });
});

describe("grainBucket — weekly windows start on Monday", () => {
  it("labels a week by that week's Monday", () => {
    // spec: "weekly windows start on Monday and are labelled by that Monday's date".
    // 2026-05-04 is a Monday; 2026-05-07 (Thursday) is inside the same week.
    expect(grainBucket("2026-05-04T00:00:00Z", "weekly", "utc")).toBe("2026-05-04");
    expect(grainBucket("2026-05-07T13:00:00Z", "weekly", "utc")).toBe("2026-05-04");
  });

  it("puts a Sunday and the FOLLOWING Monday in different weeks", () => {
    // The Monday start is what makes this a boundary: an ISO week ends Sunday.
    // 2026-05-03 is a Sunday (week of Monday 2026-04-27); 2026-05-04 is a Monday.
    expect(grainBucket("2026-05-03T12:00:00Z", "weekly", "utc")).toBe("2026-04-27");
    expect(grainBucket("2026-05-04T00:00:00Z", "weekly", "utc")).toBe("2026-05-04");

    const out = points(
      [
        row("2026-05-03T12:00:00Z", { total: 1 }),
        row("2026-05-04T00:00:00Z", { total: 2 }),
      ],
      "weekly",
    );
    expect(out).toEqual([
      { date: "2026-04-27", total: 1 },
      { date: "2026-05-04", total: 2 },
    ]);
  });

  it("collapses Monday through Sunday of one week into that Monday's point", () => {
    const out = points(
      [
        row("2026-05-04T08:00:00Z", { total: 1 }), // Monday
        row("2026-05-06T08:00:00Z", { total: 2 }), // Wednesday
        row("2026-05-08T08:00:00Z", { total: 3 }), // Friday
        row("2026-05-10T23:59:00Z", { total: 4 }), // Sunday, last of the week
      ],
      "weekly",
    );

    expect(out).toEqual([{ date: "2026-05-04", total: 4 }]);
  });

  it("labels a week spanning a month boundary with the previous month's Monday", () => {
    // 2026-03-01 is a Sunday — the last day of the week of Monday 2026-02-23.
    expect(grainBucket("2026-03-01T10:00:00Z", "weekly", "utc")).toBe("2026-02-23");
    // Its Monday-side sibling in the same week is in February.
    expect(grainBucket("2026-02-23T00:00:00Z", "weekly", "utc")).toBe("2026-02-23");

    const out = points(
      [
        row("2026-02-25T10:00:00Z", { total: 1 }), // Wednesday
        row("2026-03-01T10:00:00Z", { total: 2 }), // Sunday of the SAME week
      ],
      "weekly",
    );
    expect(out).toEqual([{ date: "2026-02-23", total: 2 }]);
  });

  it("labels a week spanning a year boundary with the previous year's Monday", () => {
    // 2026-01-01 is a Thursday — the week of Monday 2025-12-29.
    expect(grainBucket("2026-01-01T05:00:00Z", "weekly", "utc")).toBe("2025-12-29");
    expect(grainBucket("2025-12-31T22:00:00Z", "weekly", "utc")).toBe("2025-12-29");

    const out = points(
      [
        row("2025-12-31T22:00:00Z", { total: 1 }),
        row("2026-01-01T05:00:00Z", { total: 2 }), // same week, later instant
        row("2026-01-05T05:00:00Z", { total: 3 }), // next Monday → next week
      ],
      "weekly",
    );
    expect(out).toEqual([
      { date: "2025-12-29", total: 2 },
      { date: "2026-01-05", total: 3 },
    ]);
  });
});

// ── Display timezone drives the boundaries (TZ pinned) ──────────────────────────

describe("display timezone — hard expectations under a pinned host TZ", () => {
  // spec: "Window boundaries are derived in the global Settings timezone
  // preference (Local or UTC, default Local) … so switching Local↔UTC re-derives
  // the buckets; weekly windows start on Monday".
  //
  // The host TZ is pinned for this block so "local" has a KNOWN offset (UTC+9,
  // and Asia/Seoul observes no DST, so the offset is constant year-round). Every
  // expectation below is hard-coded: an implementation that read UTC fields
  // regardless of the tz argument fails here on any host, which is exactly what
  // the host-getter round-trips further down cannot detect on a UTC host.
  const saved = process.env.TZ;

  beforeAll(() => {
    process.env.TZ = "Asia/Seoul";
  });

  afterAll(() => {
    // `process.env.TZ = undefined` would store the STRING "undefined" and leave
    // the process on a bogus zone for every later test in this file, so an unset
    // TZ must be restored by deleting the key.
    if (saved === undefined) delete process.env.TZ;
    else process.env.TZ = saved;
    // Assert the restore actually landed: the host's own offset is back.
    expect(process.env.TZ).toBe(saved);
    expect(new Date("2026-05-04T20:00:00Z").getTimezoneOffset()).toBe(
      HOST_OFFSET_MINUTES,
    );
  });

  it("precondition: the pinned TZ is in effect (UTC+9)", () => {
    // Backstop for the pin itself — if a runtime TZ change were ignored, every
    // assertion in this block would silently degrade to a UTC reading on a UTC
    // host. 2026-05-04T20:00Z is 2026-05-05 05:00 in Seoul.
    const d = new Date("2026-05-04T20:00:00Z");
    expect(d.getHours()).toBe(5);
    expect(d.getDate()).toBe(5);
    expect(d.getTimezoneOffset()).toBe(-540);
  });

  it("buckets an instant into different daily windows under local vs utc", () => {
    // Same instant, two display timezones, two calendar days.
    expect(grainBucket("2026-05-04T20:00:00Z", "daily", "local")).toBe("2026-05-05");
    expect(grainBucket("2026-05-04T20:00:00Z", "daily", "utc")).toBe("2026-05-04");
  });

  it("buckets an instant into different hourly windows under local vs utc", () => {
    expect(grainBucket("2026-05-04T20:00:00Z", "hourly", "local")).toBe("2026-05-05 05:00");
    expect(grainBucket("2026-05-04T20:00:00Z", "hourly", "utc")).toBe("2026-05-04 20:00");
  });

  it("buckets an instant into different weekly windows under local vs utc", () => {
    // 2026-05-03T20:00Z is Sunday 20:00 UTC — but Monday 05:00 in Seoul, so the
    // Monday-start week boundary lands on the other side depending on the tz.
    expect(grainBucket("2026-05-03T20:00:00Z", "weekly", "local")).toBe("2026-05-04");
    expect(grainBucket("2026-05-03T20:00:00Z", "weekly", "utc")).toBe("2026-04-27");
  });

  it("re-derives a whole series when the display tz flips", () => {
    const rows = [
      row("2026-05-04T20:00:00Z", { total: 1 }), // Seoul 05-05 05:00 / UTC 05-04 20:00
      row("2026-05-05T02:00:00Z", { total: 2 }), // Seoul 05-05 11:00 / UTC 05-05 02:00
    ];

    // Local (UTC+9): both instants are the same Seoul calendar day → one window.
    expect(points(rows, "daily", "local")).toEqual([{ date: "2026-05-05", total: 2 }]);
    // UTC: they straddle midnight → two windows.
    expect(points(rows, "daily", "utc")).toEqual([
      { date: "2026-05-04", total: 1 },
      { date: "2026-05-05", total: 2 },
    ]);
  });

  it("honours the tz argument at every grain (no collapse onto one reading)", () => {
    // Sunday 20:00 UTC = Monday 05:00 in Seoul: hour, calendar day AND week all
    // differ between the two readings of this one instant.
    const iso = "2026-05-03T20:00:00Z";
    for (const grain of CHART_GRAINS) {
      expect(grainBucket(iso, grain, "local")).not.toBe(grainBucket(iso, grain, "utc"));
    }
  });
});

describe("window boundaries derive from the display timezone", () => {
  // spec: "Window boundaries are derived in the global Settings timezone
  // preference (Local or UTC …) … so switching Local↔UTC re-derives the buckets."
  //
  // The suite runs in the host's TZ, so the expectation is derived from the same
  // Date's own getters rather than hard-coded, and the divergence claim branches
  // on the host offset (both branches assert — neither can pass vacuously).

  function ymdFromLocal(d: Date): string {
    const p = (n: number) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  }

  function ymdFromUtc(d: Date): string {
    const p = (n: number) => String(n).padStart(2, "0");
    return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())}`;
  }

  // Two instants at opposite edges of a local calendar day, so on any host with a
  // non-zero UTC offset at least one lands on a different UTC calendar day than
  // local. Built INSIDE each test (not at collection time) so they always reflect
  // the host zone, never a zone some other block has pinned.
  const earlyLocalOf = () => new Date(2026, 4, 5, 0, 30, 0); // local 2026-05-05 00:30
  const lateLocalOf = () => new Date(2026, 4, 5, 23, 30, 0); // local 2026-05-05 23:30

  it("buckets daily against the display tz's own calendar day", () => {
    for (const d of [earlyLocalOf(), lateLocalOf()]) {
      const iso = d.toISOString();
      expect(grainBucket(iso, "daily", "local")).toBe(ymdFromLocal(d));
      expect(grainBucket(iso, "daily", "utc")).toBe(ymdFromUtc(d));
    }
  });

  it("buckets hourly against the display tz's own wall-clock hour", () => {
    const lateLocal = lateLocalOf();
    const iso = lateLocal.toISOString();
    const p = (n: number) => String(n).padStart(2, "0");
    expect(grainBucket(iso, "hourly", "local")).toBe(
      `${ymdFromLocal(lateLocal)} ${p(lateLocal.getHours())}:00`,
    );
    expect(grainBucket(iso, "hourly", "utc")).toBe(
      `${ymdFromUtc(lateLocal)} ${p(lateLocal.getUTCHours())}:00`,
    );
  });

  it("re-derives the buckets when the display tz flips (offset host) and agrees at UTC+0", () => {
    const earlyLocal = earlyLocalOf();
    const lateLocal = lateLocalOf();
    const offsetMinutes = earlyLocal.getTimezoneOffset(); // 0 only for a UTC host
    const isos = [earlyLocal.toISOString(), lateLocal.toISOString()];
    const diverged = isos.filter(
      (iso) => grainBucket(iso, "daily", "local") !== grainBucket(iso, "daily", "utc"),
    );

    if (offsetMinutes !== 0) {
      // At least one edge-of-day instant falls on a different calendar day.
      expect(diverged.length).toBeGreaterThan(0);
    } else {
      // Backstop for a UTC host: the two modes must then agree everywhere.
      expect(diverged).toEqual([]);
      expect(grainBucket(isos[0], "daily", "local")).toBe("2026-05-05");
    }
  });

  it("re-buckets a whole series when the display tz flips (offset host)", () => {
    const earlyLocal = earlyLocalOf();
    const lateLocal = lateLocalOf();
    const rows = [
      row(earlyLocal.toISOString(), { total: 1 }),
      row(lateLocal.toISOString(), { total: 2 }),
    ];
    const local = points(rows, "daily", "local");
    const utc = points(rows, "daily", "utc");

    if (earlyLocal.getTimezoneOffset() !== 0) {
      // Same instants, different windows → the plotted labels differ.
      expect(local.map((p) => p.date)).not.toEqual(utc.map((p) => p.date));
    } else {
      expect(local).toEqual(utc);
    }
    // Backstop independent of the branch: local always sees exactly one window
    // (both instants are the same local calendar day, by construction).
    expect(local).toEqual([{ date: "2026-05-05", total: 2 }]);
  });
});

// ── Unparseable timestamps are skipped, never bucketed ──────────────────────────

describe("rows with an unparseable timestamp", () => {
  // spec: "A row whose timestamp does not parse contributes to no window and is
  // dropped rather than grouped under a placeholder label".
  it("has no window at all (grainBucket returns null for every grain)", () => {
    for (const grain of CHART_GRAINS) {
      expect(grainBucket("not-a-date", grain, "utc")).toBeNull();
      expect(grainBucket("", grain, "utc")).toBeNull();
    }
  });

  it("skips them entirely and leaves the surviving rows correctly bucketed", () => {
    // spec: "dropped rather than grouped under a placeholder label" — a single
    // bogus label would swallow every bad row into one fabricated point.
    const out = points(
      [
        row("not-a-date", { total: 99 }),
        row("2026-05-04T09:00:00Z", { total: 1 }),
        row("", { total: 98 }),
        row("2026-05-05T09:00:00Z", { total: 2 }),
      ],
      "daily",
    );

    expect(out).toEqual([
      { date: "2026-05-04", total: 1 },
      { date: "2026-05-05", total: 2 },
    ]);
    expect(out.map((p) => p.date)).not.toContain("—");
    expect(out.some((p) => p.total === 99 || p.total === 98)).toBe(false);
  });

  it("returns [] when every row is unparseable", () => {
    expect(points([row("not-a-date", { total: 1 }), row("", { total: 2 })], "daily")).toEqual([]);
  });

  it("returns [] for empty input", () => {
    expect(points([], "daily")).toEqual([]);
    expect(points([], "hourly")).toEqual([]);
    expect(points([], "weekly")).toEqual([]);
  });
});

// ── Label ordering is chronological ordering ────────────────────────────────────

describe("bucket labels sort chronologically as plain strings", () => {
  // spec: "labelled by the truncated window start … Every x label is therefore
  // distinct" — combined with "one point per grain window … sorted ascending by
  // window", the categorical x-axis is ordered by the label string itself. This
  // sweep is what makes the month/year underflow in the weekly Monday step-back
  // (2026-03-01 → 2026-02-23, 2026-01-01 → 2025-12-29) a general property rather
  // than three hand-picked dates.
  //
  // tz is "utc" throughout for HOST-INDEPENDENCE: the distribution guards below
  // (how many distinct days/weeks the sweep touches) are exact only for a fixed
  // reading, and a local reading would make them depend on the runner's zone.
  // Ordering itself does survive a local reading — a DST fall-back re-emits the
  // SAME label for the repeated hour, which still satisfies `<=`; what DST costs
  // at local is label distinctness, not label order.

  /** Deterministic PRNG (LCG) — no dependency, identical sequence on every run. */
  function lcg(seed: number): () => number {
    let s = seed >>> 0;
    return () => {
      s = (Math.imul(s, 1664525) + 1013904223) >>> 0;
      return s / 0x1_0000_0000;
    };
  }

  // ~500 instants spread over three years spanning two year boundaries and every
  // month boundary, sorted ascending.
  const START = Date.UTC(2024, 10, 1); // 2024-11-01
  const SPAN_MS = 3 * 365 * 24 * 60 * 60 * 1000;
  const rand = lcg(20260504);
  const instants = Array.from({ length: 500 }, () =>
    new Date(START + Math.floor(rand() * SPAN_MS)).toISOString(),
  ).sort();

  it.each(CHART_GRAINS.map((g) => [g] as const))(
    "%s labels are non-decreasing as the instant advances",
    (grain) => {
      let compared = 0;
      for (let i = 1; i < instants.length; i++) {
        const prev = grainBucket(instants[i - 1], grain, "utc");
        const curr = grainBucket(instants[i], grain, "utc");
        expect(prev).not.toBeNull();
        expect(curr).not.toBeNull();
        expect(
          (prev as string) <= (curr as string),
          `${grain}: ${instants[i - 1]} → "${prev}" must not sort after ${instants[i]} → "${curr}"`,
        ).toBe(true);
        compared++;
      }
      // Backstop: the loop actually ran over the generated series.
      expect(compared).toBe(499);
    },
  );

  it("coarsening a grain never increases the number of points", () => {
    // spec: "each window contributes exactly one point" — a weekly window is a
    // union of daily windows, which are unions of hourly ones, so the point count
    // can only shrink as the grain coarsens.
    const rows = instants.map((iso, i) => row(iso, { total: i }));
    const hourly = points(rows, "hourly").length;
    const daily = points(rows, "daily").length;
    const weekly = points(rows, "weekly").length;

    expect(weekly).toBeLessThanOrEqual(daily);
    expect(daily).toBeLessThanOrEqual(hourly);
    // Backstop: the sweep is coarse enough that the chain is strict, so an
    // implementation that ignored the grain could not satisfy it.
    expect(weekly).toBeLessThan(hourly);
  });

  it("re-collapsing an already-collapsed series is a no-op", () => {
    // Idempotence is the same "exactly one point per window" rule applied twice.
    // Only daily/weekly are round-tripped: their label IS a parseable date, while
    // an hourly label ("YYYY-MM-DD HH:00") is not an ISO instant, so feeding it
    // back is undefined rather than expected-to-hold.
    const rows = instants.map((iso, i) => row(iso, { total: i }));
    for (const grain of ["daily", "weekly"] as const) {
      const once = points(rows, grain);
      const twice = toGrainPoints(once, {
        grain,
        tz: "utc",
        timeOf: (p) => p.date,
        valuesOf: (p) =>
          Object.fromEntries(
            Object.entries(p).filter(([k]) => k !== "date"),
          ) as Record<string, number>,
      });
      expect(twice).toEqual(once);
    }
  });

  it("the sweep really crosses year, month and week boundaries", () => {
    // Guard against a degenerate generator silently reducing the sweep above to
    // a handful of same-day instants.
    const weeks = new Set(instants.map((iso) => grainBucket(iso, "weekly", "utc")));
    const days = new Set(instants.map((iso) => grainBucket(iso, "daily", "utc")));
    const years = new Set(instants.map((iso) => iso.slice(0, 4)));
    expect(years.size).toBeGreaterThanOrEqual(3);
    expect(weeks.size).toBeGreaterThan(100);
    expect(days.size).toBeGreaterThan(300);
  });
});
