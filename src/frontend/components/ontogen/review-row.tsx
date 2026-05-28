"use client";

/**
 * ReviewRow — inline approve/reject controls for a single ontogen result item.
 * Rendered for pending items (llm_pending or llm_approved) when the user
 * has write access.
 */

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { useReviewOntogenItem } from "@/lib/api/ontogen";
import { useToast } from "@/components/ui/use-toast";
import type { ReviewKind } from "@/lib/api/ontogen";

interface ReviewRowProps {
  id: string;
  kind: ReviewKind;
  disabled?: boolean;
  /** Shown next to the approve button when disabled=true */
  disabledHint?: string;
}

export function ReviewRow({ id, kind, disabled = false, disabledHint }: ReviewRowProps) {
  const [reason, setReason] = useState("");
  const reviewMutation = useReviewOntogenItem();
  const { toast } = useToast();

  const pending = reviewMutation.isPending;

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
    <div className="mt-1.5 flex flex-col gap-1.5">
      <input
        type="text"
        className="h-7 w-full rounded border border-input bg-background px-2 text-xs placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
        placeholder="reason (optional)"
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        disabled={pending}
      />
      <div className="flex items-center gap-2">
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
        <Button
          size="sm"
          variant="destructive"
          onClick={() => handleReview("reject")}
          disabled={pending}
          className="h-7 text-xs"
        >
          Reject
        </Button>
        {disabled && disabledHint && (
          <span className="text-xs text-muted-foreground">{disabledHint}</span>
        )}
      </div>
    </div>
  );
}
