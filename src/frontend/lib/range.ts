/**
 * Single source of truth for time-range presets and range math used by the
 * shared <RangePicker>. Pure functions — no React imports; safe in any context.
 *
 * Bounds are expressed as ISO-8601 UTC strings (toISOString) and are inclusive
 * where present. Two resolved shapes exist and must not be confused:
 *
 *   - {@link RangeValue} — what queries USE. `to` is optional; a preset
 *     resolves with `to` absent, i.e. an open-ended window that always reaches
 *     the present so a polled panel keeps surfacing new records.
 *   - {@link ClosedRange} — what the picker EDITS. Always a concrete pair,
 *     because calendars and time fields need something to seed from.
 *
 * A timezone mode (TzMode) governs how calendar days/times are interpreted and
 * displayed; the emitted bounds are always absolute UTC instants. "utc" treats
 * a chosen day/time as a UTC wall-clock value; "local" treats it as the
 * browser's local wall-clock value, then converts to the matching UTC instant.
 *
 * CALL-SITE CONTRACT — every consumer that feeds a query key MUST keep its
 * `useMemo(() => resolveRange(sel, granularity, tz), [sel, tz])` wrapper. The
 * open upper bound removes the *dead* bound, not the need to memoize: the lower
 * bound is still computed from `new Date()`, so an unmemoized call would mint a
 * fresh `from` (and therefore a fresh query key) on every render — a cache miss
 * per render, i.e. an unbounded refetch loop. `staleTime` does not help; it is
 * per-key.
 */

export type RangeGranularity = "date" | "datetime";

/** Timezone interpretation for calendar days/times. The API contract is
 *  unchanged — bounds are always emitted as UTC ISO strings; tz only governs
 *  how days/times are interpreted and displayed. */
export type TzMode = "local" | "utc";

/** A concrete closed window — both bounds pinned. What the picker edits and
 *  what preset math produces internally; never what a query key embeds. */
export interface ClosedRange {
  /** Inclusive lower bound, ISO-8601 (UTC). */
  from: string;
  /** Inclusive upper bound, ISO-8601 (UTC). */
  to: string;
}

/**
 * A resolved window as queries consume it.
 *
 * `to` absent = open above: the read is unbounded upward and therefore always
 * reaches the newest record. Presets resolve this way; custom ranges keep the
 * closed pair the user picked. Call sites map `to` straight into the endpoint's
 * end-bound param (`to`, or `until` on `attr/validation/result`); the URL
 * builders omit falsy bounds, so an absent `to` simply drops the param.
 */
export interface RangeValue {
  /** Inclusive lower bound, ISO-8601 (UTC). */
  from: string;
  /** Inclusive upper bound, ISO-8601 (UTC). Absent = unbounded above. */
  to?: string;
}

/**
 * User intent behind a range. A preset is now-relative (its lower bound is
 * resolved against the clock and its upper bound left open, so it always
 * includes today and everything recorded since); a custom window pins concrete
 * bounds. This is what gets persisted — never the resolved
 * {@link RangeValue}, so a stored "Last 7 days" keeps tracking the present.
 */
export type RangeSelection =
  | { kind: "preset"; days: number }
  | { kind: "custom"; from: string; to: string };

export const RANGE_PRESETS = [
  { label: "Last 1 day", days: 1 },
  { label: "Last 7 days", days: 7 },
  { label: "Last 2 weeks", days: 14 },
  { label: "Last 4 weeks", days: 28 },
  { label: "Last 12 weeks", days: 84 },
] as const;

export const DEFAULT_PRESET_DAYS = 14;

// ── tz-aware Date field accessors ──────────────────────────────────────────────

interface DateFields {
  year: number;
  month: number;
  day: number;
  hours: number;
  minutes: number;
  seconds: number;
  ms: number;
}

/** Read the wall-clock fields of a Date in the given tz. */
function readFields(d: Date, tz: TzMode): DateFields {
  if (tz === "utc") {
    return {
      year: d.getUTCFullYear(),
      month: d.getUTCMonth(),
      day: d.getUTCDate(),
      hours: d.getUTCHours(),
      minutes: d.getUTCMinutes(),
      seconds: d.getUTCSeconds(),
      ms: d.getUTCMilliseconds(),
    };
  }
  return {
    year: d.getFullYear(),
    month: d.getMonth(),
    day: d.getDate(),
    hours: d.getHours(),
    minutes: d.getMinutes(),
    seconds: d.getSeconds(),
    ms: d.getMilliseconds(),
  };
}

/** Build a UTC instant from wall-clock fields interpreted in the given tz. */
function makeInstant(f: DateFields, tz: TzMode): Date {
  if (tz === "utc") {
    return new Date(
      Date.UTC(f.year, f.month, f.day, f.hours, f.minutes, f.seconds, f.ms),
    );
  }
  return new Date(f.year, f.month, f.day, f.hours, f.minutes, f.seconds, f.ms);
}

/**
 * Build the closed window covering the last `days` ending at now, interpreted
 * in `tz`. Internal — the concrete pair a preset *would* cover right now.
 *
 * Date mode bounds whole days (in `tz`): `from` = 00:00:00.000 of
 * (today - (days-1)), `to` = 23:59:59.999 of today — so "Last 1 day" is today
 * only, "Last 7 days" is today plus the prior six.
 *
 * Datetime mode bounds the exact instant: `from` = (now - days), `to` = now
 * (tz-independent, since both are absolute instants).
 */
function presetWindow(
  days: number,
  granularity: RangeGranularity,
  tz: TzMode,
): ClosedRange {
  const now = new Date();
  if (granularity === "date") {
    const nf = readFields(now, tz);
    const to = makeInstant(
      { year: nf.year, month: nf.month, day: nf.day, hours: 23, minutes: 59, seconds: 59, ms: 999 },
      tz,
    );
    // Step back (days - 1) whole days from today's date in tz.
    const fromBase = makeInstant(
      { year: nf.year, month: nf.month, day: nf.day - (days - 1), hours: 0, minutes: 0, seconds: 0, ms: 0 },
      tz,
    );
    return { from: fromBase.toISOString(), to: to.toISOString() };
  }
  const from = new Date(now.getTime() - days * 24 * 60 * 60 * 1000);
  return { from: from.toISOString(), to: now.toISOString() };
}

/**
 * Resolve a preset into the window queries should use: its lower bound only.
 *
 * The upper bound is deliberately omitted so the read stays open above and a
 * polled panel keeps picking up records written after page load. The lower
 * bound is derived from `new Date()`, so this MUST stay behind the call site's
 * `useMemo` — see the call-site contract in the module header.
 *
 * For the concrete pair the picker seeds its calendars from, use
 * {@link resolveRangeForEdit}.
 */
export function presetRange(
  days: number,
  granularity: RangeGranularity,
  tz: TzMode,
): RangeValue {
  return { from: presetWindow(days, granularity, tz).from };
}

/**
 * Resolve a {@link RangeSelection} into the bounds a query should send.
 * Presets resolve open above (lower bound recomputed against now, so today and
 * everything since is always included); custom selections pin absolute UTC
 * instants and pass through as a closed pair (tz only governs how they are
 * displayed/seeded, not stored).
 *
 * Keep every call behind `useMemo(..., [selection, tz])` — see the module
 * header.
 */
export function resolveRange(
  sel: RangeSelection,
  granularity: RangeGranularity,
  tz: TzMode,
): RangeValue {
  if (sel.kind === "preset") {
    return presetRange(sel.days, granularity, tz);
  }
  return { from: sel.from, to: sel.to };
}

/**
 * Resolve a {@link RangeSelection} into a concrete closed pair, for seeding an
 * *editor* (the RangePicker's calendars and time fields) that needs a bound on
 * both ends. A preset resolves to the window it covers at the moment of the
 * call.
 *
 * MUST NOT be used to build a query key or query params: it reinstates the
 * frozen upper bound that {@link resolveRange} exists to avoid. Use
 * {@link resolveRange} for anything that hits the API.
 */
export function resolveRangeForEdit(
  sel: RangeSelection,
  granularity: RangeGranularity,
  tz: TzMode,
): ClosedRange {
  if (sel.kind === "preset") {
    return presetWindow(sel.days, granularity, tz);
  }
  return { from: sel.from, to: sel.to };
}

/** Default selection: the {@link DEFAULT_PRESET_DAYS}-day preset. */
export function defaultSelection(): RangeSelection {
  return { kind: "preset", days: DEFAULT_PRESET_DAYS };
}

/** Shape guard for safe parsing of persisted (localStorage) selections. */
export function isRangeSelection(x: unknown): x is RangeSelection {
  if (typeof x !== "object" || x === null) return false;
  const v = x as Record<string, unknown>;
  if (v.kind === "preset") return typeof v.days === "number";
  if (v.kind === "custom") return typeof v.from === "string" && typeof v.to === "string";
  return false;
}

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

/** Short tz tag appended to formatted labels so the zone is visible. */
function tzTag(tz: TzMode): string {
  return tz === "utc" ? " UTC" : " (local)";
}

/** Format a single ISO bound as "YYYY-MM-DD" in the given tz. */
function formatDateBound(iso: string, tz: TzMode): string {
  const f = readFields(new Date(iso), tz);
  return `${f.year}-${pad(f.month + 1)}-${pad(f.day)}`;
}

/** Format a single ISO bound as "YYYY-MM-DD HH:mm" in the given tz. */
function formatDateTimeBound(iso: string, tz: TzMode): string {
  const f = readFields(new Date(iso), tz);
  return `${formatDateBound(iso, tz)} ${pad(f.hours)}:${pad(f.minutes)}`;
}

/**
 * Human-readable label for the trigger button, rendered in `tz` with a short
 * zone tag. "YYYY-MM-DD – YYYY-MM-DD <tz>" (date) or "… HH:mm – … HH:mm <tz>"
 * (datetime). An open upper bound renders as the literal "now" — formatting an
 * absent bound as a date would silently print "NaN-NaN-NaN".
 */
export function formatRange(
  value: RangeValue,
  granularity: RangeGranularity,
  tz: TzMode,
): string {
  const fmt = granularity === "date" ? formatDateBound : formatDateTimeBound;
  const upper = value.to === undefined ? "now" : fmt(value.to, tz);
  return `${fmt(value.from, tz)} – ${upper}${tzTag(tz)}`;
}

/**
 * Human-readable label for the trigger button, driven by intent.
 * Preset → its {@link RANGE_PRESETS} label (falling back to the formatted
 * resolved window — "YYYY-MM-DD – now <tz>" — if no label matches); custom →
 * the formatted bounds.
 */
export function selectionLabel(
  sel: RangeSelection,
  granularity: RangeGranularity,
  tz: TzMode,
): string {
  if (sel.kind === "preset") {
    const preset = RANGE_PRESETS.find((p) => p.days === sel.days);
    if (preset) return preset.label;
    return formatRange(presetRange(sel.days, granularity, tz), granularity, tz);
  }
  return formatRange({ from: sel.from, to: sel.to }, granularity, tz);
}
