/**
 * Status filter for ontogen result tables.
 *
 * Each result tab (Nodes / Edges / Triples) carries an All / Approved /
 * Unapproved filter applied client-side over the fetched set.
 *   - Approved   = status === "approved" (human-approved)
 *   - Unapproved = every other status (llm_pending, llm_approved, rejected)
 */

import type { OntogenStatus } from "@/types/ontogen";

export type ApprovalFilterMode = "all" | "approved" | "unapproved";

/**
 * Returns the items whose status matches the given filter mode. `all` returns
 * the input unchanged; `approved` keeps only `status === "approved"`;
 * `unapproved` keeps everything else.
 */
export function filterByApproval<T extends { status: OntogenStatus }>(
  items: T[],
  mode: ApprovalFilterMode,
): T[] {
  if (mode === "all") return items;
  if (mode === "approved") return items.filter((item) => item.status === "approved");
  return items.filter((item) => item.status !== "approved");
}
