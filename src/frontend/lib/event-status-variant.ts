/**
 * Maps an event status string to a Badge variant.
 *
 * Spec: spec/feature/FRONTEND_GOVERNANCE.md §Metrics detail event log.
 * EventStatus values: src/shared/models/enums.py EventStatus.
 *
 * Mapping (semantic status tokens — a status reads as a status):
 *   failure | error   → "destructive"  (signals a run that failed)
 *   warning           → "warning"      (signals a degraded but non-fatal run)
 *   success | ok      → "success"      (a run that completed cleanly)
 *   running | info    → "info"         (known in-flight / informational states)
 *   unknown / empty   → "secondary"    (neutral — no status claim asserted)
 */

export type BadgeVariant =
  | "default"
  | "secondary"
  | "destructive"
  | "success"
  | "warning"
  | "info"
  | "outline";

export function eventStatusVariant(status: string): BadgeVariant {
  if (status === "failure" || status === "error") return "destructive";
  if (status === "warning") return "warning";
  if (status === "success" || status === "ok") return "success";
  if (status === "running" || status === "info") return "info";
  // Unknown / empty / unrecognized — read neutral, assert no status.
  return "secondary";
}
