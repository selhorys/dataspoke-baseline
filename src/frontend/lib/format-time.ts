/**
 * Shared date/time formatting utilities.
 * Pure functions — no React imports; safe to use in both client and server contexts.
 *
 * The display timezone (TzMode) governs the wall-clock fields rendered: "utc"
 * uses the UTC getters, anything else uses the browser's local getters. The
 * default is "local". Callers pass the active tz (from useDisplayTz) so every
 * timestamp tracks the global preference.
 */

import type { TzMode } from "@/lib/range";

function pad(n: number): string {
  return String(n).padStart(2, "0");
}

/** Wall-clock fields of an instant, read in a display timezone. */
export interface TzParts {
  year: number;
  /** 0-based, as with Date#getMonth. */
  month: number;
  day: number;
  hours: number;
  minutes: number;
  /** 0 = Sunday … 6 = Saturday, as with Date#getDay. */
  weekday: number;
}

/**
 * Read the wall-clock fields of an ISO instant in the given tz.
 * Returns null when the input is null, undefined, or not a valid date — the
 * single place the UTC/local getter split lives; every other tz-aware read in
 * this module (and lib/chart-grain.ts) goes through it.
 */
export function tzParts(
  iso: string | null | undefined,
  tz: TzMode = "local",
): TzParts | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  if (tz === "utc") {
    return {
      year: d.getUTCFullYear(),
      month: d.getUTCMonth(),
      day: d.getUTCDate(),
      hours: d.getUTCHours(),
      minutes: d.getUTCMinutes(),
      weekday: d.getUTCDay(),
    };
  }
  return {
    year: d.getFullYear(),
    month: d.getMonth(),
    day: d.getDate(),
    hours: d.getHours(),
    minutes: d.getMinutes(),
    weekday: d.getDay(),
  };
}

/**
 * Formats an ISO timestamp as a human-readable relative time string.
 * Timezone-independent (operates on absolute instants).
 * Returns "—" when the input is null, undefined, or not a valid date.
 */
export function formatRelativeTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const diff = Date.now() - t;
  const mins = Math.floor(diff / 60_000);
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

/**
 * Formats an ISO timestamp as a date-time string (YYYY-MM-DD HH:MM) in `tz`.
 * Returns "—" when the input is null, undefined, or not a valid date.
 */
export function formatDateTime(
  iso: string | null | undefined,
  tz: TzMode = "local",
): string {
  const f = tzParts(iso, tz);
  if (!f) return "—";
  return `${f.year}-${pad(f.month + 1)}-${pad(f.day)} ${pad(f.hours)}:${pad(f.minutes)}`;
}

/**
 * Formats an ISO timestamp as a date string (YYYY-MM-DD) in `tz`.
 * Returns "—" when the input is null, undefined, or not a valid date.
 */
export function formatDate(
  iso: string | null | undefined,
  tz: TzMode = "local",
): string {
  const f = tzParts(iso, tz);
  if (!f) return "—";
  return `${f.year}-${pad(f.month + 1)}-${pad(f.day)}`;
}
