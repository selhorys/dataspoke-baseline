/**
 * Maps an event status string to a Badge variant.
 *
 * Spec: spec/feature/FRONTEND_GOVERNANCE.md §Metrics detail event log.
 * EventStatus values: src/shared/models/enums.py EventStatus.
 *
 * Mapping:
 *   failure | error   → "destructive"  (signals a run that failed)
 *   warning           → "secondary"    (signals a degraded but non-fatal run)
 *   success | ok | running | info → "default"  (benign; never destructive)
 */

export type BadgeVariant = "default" | "secondary" | "destructive" | "outline";

export function eventStatusVariant(status: string): BadgeVariant {
  if (status === "failure" || status === "error") return "destructive";
  if (status === "warning") return "secondary";
  // success, ok, running, info — neutral/default
  return "default";
}
