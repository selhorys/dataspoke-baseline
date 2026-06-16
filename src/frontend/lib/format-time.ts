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

interface Fields {
  year: number;
  month: number;
  day: number;
  hours: number;
  minutes: number;
}

/** Read the wall-clock fields of a Date in the given tz. */
function readFields(d: Date, tz: TzMode): Fields {
  if (tz === "utc") {
    return {
      year: d.getUTCFullYear(),
      month: d.getUTCMonth(),
      day: d.getUTCDate(),
      hours: d.getUTCHours(),
      minutes: d.getUTCMinutes(),
    };
  }
  return {
    year: d.getFullYear(),
    month: d.getMonth(),
    day: d.getDate(),
    hours: d.getHours(),
    minutes: d.getMinutes(),
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
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const f = readFields(new Date(t), tz);
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
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const f = readFields(new Date(t), tz);
  return `${f.year}-${pad(f.month + 1)}-${pad(f.day)}`;
}
