"use client";

/**
 * CandidateCard — displays a single MetaGen candidate with Approve/Reject actions
 * and an Evidence link to its Langfuse trace (from the candidate's run_id).
 *
 * Approve: available for writer when candidate is not already approved.
 *          On finalized items, approving a sibling demotes the current approved
 *          candidate (backend supports switching — service.py §review_candidate).
 * Reject:  available when candidate.status is "llm_approved" or "approved".
 *          Rejecting an "approved" candidate removes the editable DataHub
 *          description it wrote; the confirm dialog warns about this.
 */

import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { EvidenceLink } from "@/components/ontogen/evidence-link";
import { isRejectEligible, destinationAspectLabel } from "@/lib/metagen-predicates";
import { formatDateTime } from "@/lib/format-time";
import { useDisplayTz } from "@/lib/preferences/timezone";
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
  const tz = useDisplayTz();

  const aspectLabel = destinationAspectLabel(itemKind, fieldPath);
  const rejectEligible = isRejectEligible(candidate);
  const isApproved = candidate.status === "approved";

  // Show action buttons for writer role.
  // Approve is suppressed on the already-approved candidate (no-op; approving a
  // sibling via the switch path handles demotion at the backend).
  // Reject is gated by isRejectEligible (llm_approved or approved).
  const showApprove = canWrite && !isApproved;
  const showReject = canWrite && rejectEligible;
  const showActions = showApprove || showReject;

  return (
    <div
      data-testid="metagen-candidate-card"
      data-conf-name={candidate.conf_name ?? ""}
      data-candidate-status={candidate.status}
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
        {candidate.conf_name && (
          <Badge variant="outline" className="text-xs" title="Producing conf">
            {candidate.conf_name}
          </Badge>
        )}
        {candidate.reviewed_at && (
          <span className="text-xs text-muted-foreground">
            reviewed {formatDateTime(candidate.reviewed_at, tz)}
          </span>
        )}
        <span className="inline-flex items-center gap-1 text-xs">
          <span className="text-muted-foreground">Evidence</span>
          <EvidenceLink runId={candidate.run_id} />
        </span>
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
        description={
          isApproved
            ? `This candidate will be marked rejected and the editable description it wrote to ${aspectLabel} in DataHub will be removed.`
            : "This candidate will be marked rejected and cleared on the next run."
        }
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
