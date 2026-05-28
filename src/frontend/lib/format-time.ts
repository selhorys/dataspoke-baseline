/**
 * Shared date/time formatting utilities.
 * Pure functions — no React imports; safe to use in both client and server contexts.
 */

/**
 * Formats an ISO timestamp as a human-readable relative time string.
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
 * Formats an ISO timestamp as a local date-time string (YYYY-MM-DD HH:MM).
 * Returns "—" when the input is null, undefined, or not a valid date.
 */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return "—";
  const d = new Date(t);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}
