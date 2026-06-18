"use client";

/**
 * ReviewRow — status-adaptive approve/reject controls for a single ontogen
 * result item. The offered verdicts depend on the row's current status
 * (see reviewActionsForStatus): pending offers Approve + Reject, an approved
 * row offers Reject (revoke), a rejected row offers Approve (re-approve).
 *
 * Each verdict opens a confirm Dialog with an optional reason textarea; the
 * `{ verdict, reason }` body is submitted on Confirm. Rendered for every row
 * when the user has write access. The Approve action may be gated (unmet triple
 * dependencies): the disabled button carries the gate hint as `title` hover text.
 */

import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { useReviewOntogenItem } from "@/lib/api/ontogen";
import { useToast } from "@/components/ui/use-toast";
import { reviewActionsForStatus } from "@/lib/ontogen-review-actions";
import type { ReviewKind } from "@/lib/api/ontogen";
import type { OntogenStatus, ReviewVerdict } from "@/types/ontogen";

interface ReviewRowProps {
  id: string;
  kind: ReviewKind;
  status: OntogenStatus;
  /** Disables the Approve action (e.g. unmet triple dependencies). */
  disabled?: boolean;
  /** Hover text on the disabled Approve button explaining the gate. */
  disabledHint?: string;
}

export function ReviewRow({ id, kind, status, disabled = false, disabledHint }: ReviewRowProps) {
  const [activeVerdict, setActiveVerdict] = useState<ReviewVerdict | null>(null);
  const [reason, setReason] = useState("");
  const reviewMutation = useReviewOntogenItem();
  const { toast } = useToast();

  const pending = reviewMutation.isPending;
  const verdicts = reviewActionsForStatus(status);

  function openConfirm(verdict: ReviewVerdict) {
    setReason("");
    setActiveVerdict(verdict);
  }

  function closeConfirm() {
    if (pending) return;
    setActiveVerdict(null);
  }

  function handleConfirm() {
    if (!activeVerdict) return;
    const verdict = activeVerdict;
    reviewMutation.mutate(
      { kind, id, body: { verdict, reason: reason || undefined } },
      {
        onSuccess: () => {
          setReason("");
          setActiveVerdict(null);
          toast({ title: `${kind} ${verdict}d` });
        },
        onError: (err) => {
          toast({ title: "Review failed", description: err.message, variant: "destructive" });
        },
      },
    );
  }

  return (
    <div className="flex items-center gap-2">
      {verdicts.includes("approve") && (
        <Button
          size="sm"
          variant="default"
          onClick={() => openConfirm("approve")}
          disabled={pending || disabled}
          title={disabled ? disabledHint : undefined}
          className="h-7 text-xs"
        >
          Approve
        </Button>
      )}
      {verdicts.includes("reject") && (
        <Button
          size="sm"
          variant="destructive"
          onClick={() => openConfirm("reject")}
          disabled={pending}
          className="h-7 text-xs"
        >
          Reject
        </Button>
      )}

      <Dialog open={activeVerdict !== null} onOpenChange={(o) => (o ? undefined : closeConfirm())}>
        <DialogContent className="sm:max-w-[425px]">
          <DialogHeader>
            <DialogTitle>
              {activeVerdict === "approve" ? "Approve" : "Reject"} {kind}
            </DialogTitle>
          </DialogHeader>
          <textarea
            className="min-h-20 w-full rounded border border-input bg-background px-2 py-1.5 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
            placeholder="reason (optional)"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            disabled={pending}
          />
          <DialogFooter>
            <Button variant="outline" onClick={closeConfirm} disabled={pending}>
              Cancel
            </Button>
            <Button
              variant={activeVerdict === "reject" ? "destructive" : "default"}
              onClick={handleConfirm}
              disabled={pending}
            >
              {pending ? "Processing..." : "Confirm"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
