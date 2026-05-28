"use client";

/**
 * CandidateCard — displays a single MetaGen candidate with Approve/Reject actions.
 *
 * Approve: available for writer when candidate is not already approved.
 *          On finalized items, approving a sibling demotes the current approved
 *          candidate (backend supports switching — service.py §review_candidate).
 * Reject:  available only when candidate.status === "llm_approved".
 */

import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { isRejectEligible, destinationAspectLabel } from "@/lib/metagen-predicates";
import { formatDateTime } from "@/lib/format-time";
import type { MetagenCandidate } from "@/types/metagen";

const STATUS_VARIANT: Record<string, "default" | "secondary" | "outline" | "destructive"> = {
  llm_approved: "secondary",
  approved: "default",
  rejected: "outline",
};

interface CandidateCardProps {
  candidate: MetagenCandidate;
  /** The item kind — used to label the destination DataHub aspect in the confirm dialog. */
  itemKind: string;
  fieldPath: string | null;
  canWrite: boolean;
  onApprove: (candidateId: string) => void;
  onReject: (candidateId: string) => void;
  isReviewing: boolean;
}

export function CandidateCard({
  candidate,
  itemKind,
  fieldPath,
  canWrite,
  onApprove,
  onReject,
  isReviewing,
}: CandidateCardProps) {
  const [approveOpen, setApproveOpen] = useState(false);
  const [rejectOpen, setRejectOpen] = useState(false);

  const aspectLabel = destinationAspectLabel(itemKind, fieldPath);
  const rejectEligible = isRejectEligible(candidate);
  const isApproved = candidate.status === "approved";

  // Show action buttons for writer role.
  // Approve is suppressed on the already-approved candidate (no-op; approving a
  // sibling via the switch path handles demotion at the backend).
  // Reject is gated by isRejectEligible (llm_approved only).
  const showApprove = canWrite && !isApproved;
  const showReject = canWrite && rejectEligible;
  const showActions = showApprove || showReject;

  return (
    <div
      className={`rounded-md border p-3 space-y-2 ${
        isApproved ? "border-primary/40 bg-primary/5" : ""
      }`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <Badge variant={STATUS_VARIANT[candidate.status] ?? "outline"} className="text-xs">
          {candidate.status}
        </Badge>
        <span className="text-xs text-muted-foreground">
          conf {candidate.confidence_score.toFixed(2)}
        </span>
        {candidate.reviewed_at && (
          <span className="text-xs text-muted-foreground">
            reviewed {formatDateTime(candidate.reviewed_at)}
          </span>
        )}
        {showActions && (
          <div className="ml-auto flex items-center gap-1.5">
            {showApprove && (
              <Button
                size="sm"
                variant="outline"
                className="h-6 px-2 text-xs"
                onClick={() => setApproveOpen(true)}
                disabled={isReviewing}
              >
                Approve
              </Button>
            )}
            {showReject && (
              <Button
                size="sm"
                variant="outline"
                className="h-6 px-2 text-xs"
                onClick={() => setRejectOpen(true)}
                disabled={isReviewing}
              >
                Reject
              </Button>
            )}
          </div>
        )}
      </div>

      <pre className="whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-foreground">
        {candidate.value}
      </pre>

      <ConfirmDialog
        open={approveOpen}
        onOpenChange={setApproveOpen}
        title="Approve candidate"
        description={`This will write to ${aspectLabel} in DataHub and lock this item. Any other approved candidate for this item will be demoted.`}
        confirmLabel="Approve"
        onConfirm={() => {
          setApproveOpen(false);
          onApprove(candidate.candidate_id);
        }}
        loading={isReviewing}
      />

      <ConfirmDialog
        open={rejectOpen}
        onOpenChange={setRejectOpen}
        title="Reject candidate"
        description="This candidate will be marked rejected and cleared on the next run."
        confirmLabel="Reject"
        onConfirm={() => {
          setRejectOpen(false);
          onReject(candidate.candidate_id);
        }}
        loading={isReviewing}
      />
    </div>
  );
}
