"use client";

/**
 * ReviewRow — status-adaptive approve/reject controls for a single ontogen
 * result item. The offered verdicts depend on the row's current status
 * (see reviewActionsForStatus): pending offers Approve + Reject, an approved
 * row offers Reject (revoke), a rejected row offers Approve (re-approve).
 * Rendered for every row when the user has write access.
 */

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { useReviewOntogenItem } from "@/lib/api/ontogen";
import { useToast } from "@/components/ui/use-toast";
import { reviewActionsForStatus } from "@/lib/ontogen-review-actions";
import type { ReviewKind } from "@/lib/api/ontogen";
import type { OntogenStatus } from "@/types/ontogen";

interface ReviewRowProps {
  id: string;
  kind: ReviewKind;
  status: OntogenStatus;
  /** Disables the Approve action (e.g. unmet triple dependencies). */
  disabled?: boolean;
  /** Shown next to the approve button when disabled=true */
  disabledHint?: string;
}

export function ReviewRow({ id, kind, status, disabled = false, disabledHint }: ReviewRowProps) {
  const [reason, setReason] = useState("");
  const reviewMutation = useReviewOntogenItem();
  const { toast } = useToast();

  const pending = reviewMutation.isPending;
  const verdicts = reviewActionsForStatus(status);

  function handleReview(verdict: "approve" | "reject") {
    reviewMutation.mutate(
      { kind, id, body: { verdict, reason: reason || undefined } },
      {
        onSuccess: () => {
          setReason("");
          toast({ title: `${kind} ${verdict}d` });
        },
        onError: (err) => {
          toast({ title: "Review failed", description: err.message, variant: "destructive" });
        },
      },
    );
  }

  return (
    <div className="flex flex-col gap-1.5">
      <input
        type="text"
        className="h-7 w-full rounded border border-input bg-background px-2 text-xs placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
        placeholder="reason (optional)"
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        disabled={pending}
      />
      <div className="flex items-center gap-2">
        {verdicts.includes("approve") && (
          <Button
            size="sm"
            variant="default"
            onClick={() => handleReview("approve")}
            disabled={pending || disabled}
            title={disabled && disabledHint ? disabledHint : undefined}
            className="h-7 text-xs"
          >
            Approve
          </Button>
        )}
        {verdicts.includes("reject") && (
          <Button
            size="sm"
            variant="destructive"
            onClick={() => handleReview("reject")}
            disabled={pending}
            className="h-7 text-xs"
          >
            Reject
          </Button>
        )}
        {disabled && disabledHint && verdicts.includes("approve") && (
          <span className="text-xs text-muted-foreground">{disabledHint}</span>
        )}
      </div>
    </div>
  );
}
