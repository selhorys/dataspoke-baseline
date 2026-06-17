/**
 * Status-adaptive review actions for ontogen result rows.
 *
 * The backend `method/review` endpoint posts {verdict: "approve" | "reject"}
 * for any id with no status guard, so the UI offers verdicts that make sense
 * for the row's current status:
 *   - llm_pending / llm_approved → Approve + Reject
 *   - approved                   → Reject  (revoke)
 *   - rejected                   → Approve (re-approve)
 *
 * Approve remains gated by the triple dependency check at the call site.
 */

import type { OntogenStatus, ReviewVerdict } from "@/types/ontogen";

/**
 * Returns the verdicts to surface as action buttons for a row at the given
 * status. The approve action is independently gated (triple dependencies) by
 * the caller; this only decides which verdicts are offered.
 */
export function reviewActionsForStatus(status: OntogenStatus): ReviewVerdict[] {
  switch (status) {
    case "approved":
      return ["reject"];
    case "rejected":
      return ["approve"];
    case "llm_pending":
    case "llm_approved":
    default:
      return ["approve", "reject"];
  }
}
