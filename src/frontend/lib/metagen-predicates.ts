/**
 * Pure predicate functions for MetaGen item and candidate states.
 *
 * Exported as named functions so the test agent can import and unit-test
 * them directly.
 *
 * Source of truth: src/backend/metagen/service.py §review_candidate and
 * _metagen_mappers.py §item_status.
 */

import type { MetagenCandidate, MetagenItemSummary } from "@/types/metagen";

/**
 * Returns true when the item has at least one human-approved candidate.
 * An item in this state collapses to a single approved row in the UI.
 */
export function isItemFinalized(item: Pick<MetagenItemSummary, "status">): boolean {
  return item.status === "approved";
}

/**
 * Returns true when the candidate is eligible for a Reject action.
 *
 * Reject is valid on both "llm_approved" (the normal case) and "approved"
 * candidates. Rejecting an "approved" candidate flips it to "rejected" and
 * removes the editable DataHub description it had written; a "rejected"
 * candidate is not reject-eligible.
 *
 * Candidate statuses: "llm_approved" | "approved" | "rejected"
 * Source of truth: src/api/schemas/metagen.py MetagenCandidate.status and
 * src/backend/metagen/service.py §review_candidate.
 */
export function isRejectEligible(candidate: Pick<MetagenCandidate, "status">): boolean {
  return candidate.status === "llm_approved" || candidate.status === "approved";
}

/**
 * Returns the approved candidate from a list, or null if none exists.
 */
export function findApprovedCandidate(
  candidates: MetagenCandidate[],
): MetagenCandidate | null {
  return candidates.find((c) => c.status === "approved") ?? null;
}

/**
 * Derives the human-readable label for the DataHub aspect being written
 * when a candidate is approved.
 *
 * kind "dataset.description" → "editableDatasetProperties.description"
 *   (backend: EditableDatasetPropertiesClass — service.py §_emit_to_datahub)
 * kind "column.description"  → "editableSchemaMetadata.description (column: {field_path})"
 *   (backend: EditableSchemaMetadataClass[fieldPath].description — service.py §_emit_to_datahub)
 */
export function destinationAspectLabel(
  kind: string,
  fieldPath: string | null,
): string {
  if (kind === "dataset.description") {
    return "editableDatasetProperties.description";
  }
  if (kind === "column.description") {
    const col = fieldPath ? ` (column: ${fieldPath})` : "";
    return `editableSchemaMetadata.description${col}`;
  }
  return kind;
}
