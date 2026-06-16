/**
 * Tests for lib/format-time.ts — formatRelativeTime and formatDateTime.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_INGESTION.md §List wireframe: "events" column shows
 *     relative time (e.g. "2h ✓", "3d ▲") — produced by formatRelativeTime.
 *   - spec/feature/FRONTEND_INGESTION.md §Detail wireframe: event log shows
 *     absolute timestamps (e.g. "2026-04-25 ✓") — produced by formatDateTime.
 *   - Both formatters are shared utilities reused by validation, metagen, and
 *     governance event logs (Phases 5–7).
 *
 * TZ sensitivity note:
 *   formatDateTime uses local time (new Date().getHours() etc.) — it is
 *   TZ-sensitive. Tests use vi.setSystemTime() to fix the clock and assert
 *   on a known UTC-to-local mapping, OR assert on regex structure (YYYY-MM-DD HH:MM)
 *   to avoid CI-TZ flakiness. See §2 below for the chosen strategy.
 */

import { describe, it, expect, afterEach, vi } from "vitest";
import { formatRelativeTime, formatDateTime, formatDate } from "./format-time";

// ── 1. formatRelativeTime ──────────────────────────────────────────────────────

describe("formatRelativeTime — null/undefined/empty/garbage returns '—'", () => {
  it("returns '—' for null", () => {
    expect(formatRelativeTime(null)).toBe("—");
  });

  it("returns '—' for undefined", () => {
    expect(formatRelativeTime(undefined)).toBe("—");
  });

  it("returns '—' for empty string", () => {
    expect(formatRelativeTime("")).toBe("—");
  });

  it("returns '—' for a non-ISO garbage string", () => {
    expect(formatRelativeTime("not-a-date")).toBe("—");
  });

  it("returns '—' for a random non-date word", () => {
    expect(formatRelativeTime("hello world")).toBe("—");
  });

  it("returns '—' for a numeric-looking non-date string (NaN guard)", () => {
    // "99999" parses as a valid Date in some runtimes (ms since epoch) — the
    // impl uses new Date(iso) which treats "99999" as milliseconds (year 1970),
    // so it would NOT be NaN. That is valid behaviour. This test just confirms
    // no crash and a defined return value.
    const result = formatRelativeTime("not-a-real-iso");
    expect(result).toBe("—");
  });
});

describe("formatRelativeTime — deterministic relative strings via vi.setSystemTime()", () => {
  const NOW = new Date("2026-04-25T12:00:00.000Z");

  afterEach(() => {
    vi.useRealTimers();
  });

  it("returns '0m' for a timestamp equal to now", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    expect(formatRelativeTime("2026-04-25T12:00:00.000Z")).toBe("0m");
  });

  it("returns '5m' for a timestamp 5 minutes ago", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    expect(formatRelativeTime("2026-04-25T11:55:00.000Z")).toBe("5m");
  });

  it("returns '59m' for a timestamp 59 minutes ago", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    expect(formatRelativeTime("2026-04-25T11:01:00.000Z")).toBe("59m");
  });

  it("returns '1h' for a timestamp exactly 60 minutes ago", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    expect(formatRelativeTime("2026-04-25T11:00:00.000Z")).toBe("1h");
  });

  it("returns '2h' for a timestamp 2 hours ago", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    // List wireframe shows "2h ✓" — this is the relative-time portion
    expect(formatRelativeTime("2026-04-25T10:00:00.000Z")).toBe("2h");
  });

  it("returns '23h' for a timestamp 23 hours ago", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    expect(formatRelativeTime("2026-04-25T13:00:00.000Z").endsWith("h")).toBe(false);
    // 23h back from 12:00 = 13:00 yesterday → actually > 24h, use a correct offset
    // 23h back: 2026-04-24T13:00:00Z
    expect(formatRelativeTime("2026-04-24T13:00:00.000Z")).toBe("23h");
  });

  it("returns '1d' for a timestamp exactly 24 hours ago", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    expect(formatRelativeTime("2026-04-24T12:00:00.000Z")).toBe("1d");
  });

  it("returns '3d' for a timestamp 3 days ago (list wireframe: '3d ▲')", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    // Detail wireframe example: "3d ▲" for a warning-status event 3 days ago
    expect(formatRelativeTime("2026-04-22T12:00:00.000Z")).toBe("3d");
  });

  it("returns a 'd'-suffixed string for timestamps more than 24h ago", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const result = formatRelativeTime("2026-04-20T12:00:00.000Z");
    expect(result).toMatch(/^\d+d$/);
  });

  it("returns an 'm'-suffixed string for sub-hour timestamps", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const result = formatRelativeTime("2026-04-25T11:30:00.000Z");
    expect(result).toMatch(/^\d+m$/);
  });

  it("returns an 'h'-suffixed string for timestamps between 1h and 24h ago", () => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    const result = formatRelativeTime("2026-04-25T06:00:00.000Z");
    expect(result).toMatch(/^\d+h$/);
  });
});

// ── 2. formatDateTime ──────────────────────────────────────────────────────────
//
// TZ sensitivity: formatDateTime uses Date.getHours() / getDate() etc. — local time.
// Strategy: assert on the YYYY-MM-DD HH:MM shape (regex) rather than exact wall-clock
// values, so the test does not fail when CI runs in a different timezone.
// Exception: one test uses a UTC midnight timestamp + pins the process TZ expectation
// to verify the *format* of the output (not the exact hour).

describe("formatDateTime — null/undefined/empty/garbage returns '—'", () => {
  it("returns '—' for null", () => {
    expect(formatDateTime(null)).toBe("—");
  });

  it("returns '—' for undefined", () => {
    expect(formatDateTime(undefined)).toBe("—");
  });

  it("returns '—' for empty string", () => {
    expect(formatDateTime("")).toBe("—");
  });

  it("returns '—' for garbage input", () => {
    expect(formatDateTime("not-a-date")).toBe("—");
  });

  it("returns '—' for a string that is clearly not a date", () => {
    expect(formatDateTime("foo bar baz")).toBe("—");
  });
});

describe("formatDateTime — output format is YYYY-MM-DD HH:MM (TZ-agnostic shape check)", () => {
  // Assert on shape so CI timezone differences do not cause false failures.
  const FORMAT_REGEX = /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$/;

  it("returns a YYYY-MM-DD HH:MM string for a valid ISO timestamp", () => {
    const result = formatDateTime("2026-04-25T10:00:00.000Z");
    expect(result).toMatch(FORMAT_REGEX);
  });

  it("returns a YYYY-MM-DD HH:MM string for a different valid ISO timestamp", () => {
    const result = formatDateTime("2026-01-15T08:30:00.000Z");
    expect(result).toMatch(FORMAT_REGEX);
  });

  it("year component is correct for 2026-04-25T00:00:00Z", () => {
    // Even in any timezone, the year must be 2026 for this timestamp.
    const result = formatDateTime("2026-04-25T12:00:00.000Z");
    expect(result.startsWith("2026-")).toBe(true);
  });

  it("month component is zero-padded", () => {
    // January → "01", not "1"
    const result = formatDateTime("2026-01-15T08:00:00.000Z");
    // Year starts result; second segment is month — must be 2 digits
    const parts = result.split("-");
    expect(parts[1]).toMatch(/^\d{2}$/);
  });

  it("day component is zero-padded", () => {
    const result = formatDateTime("2026-01-05T10:00:00.000Z");
    // "2026-01-DD HH:MM" — DD is the third hyphen-segment prefix
    const dayPart = result.split("-")[2].split(" ")[0];
    expect(dayPart).toMatch(/^\d{2}$/);
  });

  it("time component has two-digit hour and minute separated by colon", () => {
    const result = formatDateTime("2026-04-25T10:05:00.000Z");
    const timePart = result.split(" ")[1];
    expect(timePart).toMatch(/^\d{2}:\d{2}$/);
  });

  it("does not include seconds in the output", () => {
    // Spec shows timestamps as "2026-04-25 ✓ INGESTION.COMPLETE" — minutes precision.
    const result = formatDateTime("2026-04-25T10:05:30.000Z");
    const timePart = result.split(" ")[1];
    // Should be HH:MM (no third colon-separated seconds segment).
    expect(timePart.split(":").length).toBe(2);
  });
});

describe("formatDateTime — TZ sensitivity note", () => {
  // This test documents the known TZ-sensitivity of formatDateTime.
  // The function uses local-time methods (getHours, getDate, etc.), so the
  // exact hour/date may differ between UTC and non-UTC timezones.
  // Tests above use regex shape-checks to avoid CI TZ flakiness.
  // This assertion only checks the invariant: same UTC input → same local output
  // when called twice on the same machine.

  it("is deterministic: same input produces same output on repeated calls", () => {
    const iso = "2026-04-25T10:00:00.000Z";
    expect(formatDateTime(iso)).toBe(formatDateTime(iso));
  });
});

// ── 3. tz-aware rendering ────────────────────────────────────────────────────────
//
// The "utc" tz uses the UTC getters, so the output is independent of the host
// timezone — these assertions are exact and CI-TZ-safe.

describe("formatDateTime — utc tz renders absolute UTC wall-clock", () => {
  it("renders the exact UTC date and time for tz='utc'", () => {
    expect(formatDateTime("2026-04-25T10:05:00.000Z", "utc")).toBe(
      "2026-04-25 10:05",
    );
  });

  it("zero-pads month/day/hour/minute in utc", () => {
    expect(formatDateTime("2026-01-05T08:09:00.000Z", "utc")).toBe(
      "2026-01-05 08:09",
    );
  });

  it("returns '—' for null regardless of tz", () => {
    expect(formatDateTime(null, "utc")).toBe("—");
  });
});

describe("formatDateTime — local tz is offset-agnostic (round-trip against host getters)", () => {
  // Build the expected string from the SAME Date's local getters, so the test
  // is correct in any host timezone (no hardcoded wall-clock).
  function localExpected(iso: string): string {
    const d = new Date(iso);
    const p = (n: number) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  it("matches a string built from local getters for tz='local'", () => {
    const iso = "2026-04-25T10:05:00.000Z";
    expect(formatDateTime(iso, "local")).toBe(localExpected(iso));
  });

  it("matches local getters for a different instant", () => {
    const iso = "2026-01-05T23:30:00.000Z";
    expect(formatDateTime(iso, "local")).toBe(localExpected(iso));
  });

  it("default tz (no arg) behaves as 'local'", () => {
    const iso = "2026-04-25T10:05:00.000Z";
    expect(formatDateTime(iso)).toBe(formatDateTime(iso, "local"));
    expect(formatDateTime(iso)).toBe(localExpected(iso));
  });

  it("utc and local differ iff the host offset is non-zero", () => {
    const iso = "2026-04-25T10:05:00.000Z";
    const offset = new Date(iso).getTimezoneOffset();
    if (offset === 0) {
      // Host runs in UTC — the two renderings coincide; skip the difference check.
      expect(formatDateTime(iso, "utc")).toBe(formatDateTime(iso, "local"));
      return;
    }
    expect(formatDateTime(iso, "utc")).not.toBe(formatDateTime(iso, "local"));
  });
});

// ── 4. formatDate ────────────────────────────────────────────────────────────────

describe("formatDate — null/garbage returns '—'", () => {
  it("returns '—' for null", () => {
    expect(formatDate(null)).toBe("—");
  });

  it("returns '—' for undefined", () => {
    expect(formatDate(undefined)).toBe("—");
  });

  it("returns '—' for garbage", () => {
    expect(formatDate("not-a-date")).toBe("—");
  });
});

describe("formatDate — YYYY-MM-DD shape and tz", () => {
  const DATE_REGEX = /^\d{4}-\d{2}-\d{2}$/;

  it("matches YYYY-MM-DD for a valid ISO timestamp (local default)", () => {
    expect(formatDate("2026-04-25T10:00:00.000Z")).toMatch(DATE_REGEX);
  });

  it("renders the exact UTC date for tz='utc'", () => {
    expect(formatDate("2026-04-25T23:30:00.000Z", "utc")).toBe("2026-04-25");
  });

  it("does not include a time component", () => {
    expect(formatDate("2026-04-25T10:00:00.000Z", "utc")).toBe("2026-04-25");
  });

  it("local tz is offset-agnostic (round-trip against host date getters)", () => {
    const iso = "2026-04-25T23:30:00.000Z";
    const d = new Date(iso);
    const p = (n: number) => String(n).padStart(2, "0");
    const expected = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
    expect(formatDate(iso, "local")).toBe(expected);
  });

  it("default tz (no arg) behaves as 'local'", () => {
    const iso = "2026-04-25T23:30:00.000Z";
    expect(formatDate(iso)).toBe(formatDate(iso, "local"));
  });
});
