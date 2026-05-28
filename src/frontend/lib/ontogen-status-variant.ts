/**
 * Maps an OntogenStatus string to a Badge variant and display label.
 *
 * Status values from src/api/schemas/ontogen.py:
 *   llm_pending  — LLM-created, awaiting review
 *   llm_approved — LLM-reviewer accepted + high confidence
 *   approved     — human-approved
 *   rejected     — human-rejected
 */

import type { BadgeVariant } from "@/lib/event-status-variant";

export function ontogenStatusVariant(status: string): BadgeVariant {
  switch (status) {
    case "approved":
      return "default";
    case "llm_approved":
      return "secondary";
    case "rejected":
      return "destructive";
    case "llm_pending":
    default:
      return "outline";
  }
}

export function ontogenStatusLabel(status: string): string {
  switch (status) {
    case "approved":
      return "approved";
    case "llm_approved":
      return "llm approved";
    case "rejected":
      return "rejected";
    case "llm_pending":
      return "pending";
    default:
      return status;
  }
}
