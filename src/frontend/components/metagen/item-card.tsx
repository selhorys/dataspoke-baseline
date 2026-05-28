"use client";

/**
 * ItemCard — renders a MetaGen item with its candidates.
 *
 * Finalized items (status === "approved") collapse to show the approved candidate
 * at the top with a toggle to expand sibling candidates as read-only history.
 */

import { useState } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { CandidateCard } from "@/components/metagen/candidate-card";
import { useMetagenItem, useReviewCandidate } from "@/lib/api/metagen";
import { isItemFinalized, findApprovedCandidate } from "@/lib/metagen-predicates";
import { useToast } from "@/components/ui/use-toast";
import { ApiError } from "@/lib/api/client";
import type { MetagenItemSummary } from "@/types/metagen";

const ITEM_STATUS_VARIANT: Record<
  string,
  "default" | "secondary" | "outline" | "destructive"
> = {
  pending: "outline",
  llm_approved: "secondary",
  approved: "default",
};

interface ItemCardProps {
  item: MetagenItemSummary;
  canWrite: boolean;
}

export function ItemCard({ item, canWrite }: ItemCardProps) {
  const [expanded, setExpanded] = useState(false);
  const [siblingExpanded, setSiblingExpanded] = useState(false);
  const { toast } = useToast();

  const finalized = isItemFinalized(item);

  // Fetch detail only when expanded or finalized (finalized collapsed rows need
  // the approved candidate text + date for the summary row; non-finalized
  // collapsed items have no content to show until expanded).
  const { data: detail, isLoading: detailLoading } = useMetagenItem(
    item.dataset_urn,
    item.item_id,
    { enabled: expanded || finalized },
  );

  const reviewMutation = useReviewCandidate();

  function handleApprove(candidateId: string) {
    reviewMutation.mutate(
      {
        datasetUrn: item.dataset_urn,
        itemId: item.item_id,
        candidateId,
        body: { verdict: "approve" },
      },
      {
        onSuccess: () => {
          toast({ title: "Candidate approved", description: "Written to DataHub." });
        },
        onError: (err) => {
          const msg =
            err instanceof ApiError
              ? `${err.error_code}: ${err.message}`
              : err.message;
          toast({ title: "Approve failed", description: msg, variant: "destructive" });
        },
      },
    );
  }

  function handleReject(candidateId: string) {
    reviewMutation.mutate(
      {
        datasetUrn: item.dataset_urn,
        itemId: item.item_id,
        candidateId,
        body: { verdict: "reject" },
      },
      {
        onSuccess: () => {
          toast({ title: "Candidate rejected" });
        },
        onError: (err) => {
          const msg =
            err instanceof ApiError
              ? `${err.error_code}: ${err.message}`
              : err.message;
          toast({ title: "Reject failed", description: msg, variant: "destructive" });
        },
      },
    );
  }

  const approvedCandidate = detail ? findApprovedCandidate(detail.candidates) : null;
  const siblingCandidates = detail
    ? detail.candidates.filter((c) => c.status !== "approved")
    : [];

  const label = item.field_path
    ? `${item.kind} (${item.field_path})`
    : item.kind;

  return (
    <div className="rounded-lg border p-4 space-y-3">
      {/* Header */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-sm font-medium">{label}</span>
        <Badge
          variant={ITEM_STATUS_VARIANT[item.status] ?? "outline"}
          className="text-xs"
        >
          {item.status}
        </Badge>
        <span className="text-xs text-muted-foreground">
          {item.candidate_count} candidate{item.candidate_count !== 1 ? "s" : ""}
        </span>
        <Button
          variant="ghost"
          size="sm"
          className="ml-auto h-6 px-2 text-xs"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? (
            <>
              <ChevronUp className="mr-1 h-3.5 w-3.5" />
              Collapse
            </>
          ) : (
            <>
              <ChevronDown className="mr-1 h-3.5 w-3.5" />
              {finalized ? "View" : "Review"}
            </>
          )}
        </Button>
      </div>

      {/* Finalized summary row */}
      {finalized && approvedCandidate && !expanded && (
        <div className="rounded-md border border-primary/30 bg-primary/5 px-3 py-2 text-sm text-muted-foreground">
          Approved on{" "}
          {approvedCandidate.reviewed_at
            ? new Date(approvedCandidate.reviewed_at).toLocaleDateString()
            : "—"}{" "}
          — expand to view details
        </div>
      )}

      {/* Expanded candidates */}
      {expanded && (
        <div className="space-y-3">
          {detailLoading && !detail && (
            <>
              <Skeleton className="h-20 w-full" />
              <Skeleton className="h-20 w-full" />
            </>
          )}

          {detail && detail.candidates.length === 0 && (
            <p className="text-sm text-muted-foreground">No candidates yet.</p>
          )}

          {/* Approved candidate first */}
          {detail && approvedCandidate && (
            <CandidateCard
              candidate={approvedCandidate}
              itemKind={item.kind}
              fieldPath={item.field_path}
              canWrite={canWrite}
              onApprove={handleApprove}
              onReject={handleReject}
              isReviewing={reviewMutation.isPending}
            />
          )}

          {/* Sibling candidates (non-approved) */}
          {detail && finalized && siblingCandidates.length > 0 && (
            <div className="space-y-1">
              <Button
                variant="ghost"
                size="sm"
                className="h-6 px-1 text-xs text-muted-foreground"
                onClick={() => setSiblingExpanded((v) => !v)}
              >
                {siblingExpanded ? "Hide" : "Show"} {siblingCandidates.length} sibling
                candidate{siblingCandidates.length !== 1 ? "s" : ""}
              </Button>
              {siblingExpanded && (
                <div className="space-y-2 pl-2">
                  {siblingCandidates.map((c) => (
                    <CandidateCard
                      key={c.candidate_id}
                      candidate={c}
                      itemKind={item.kind}
                      fieldPath={item.field_path}
                      canWrite={canWrite}
                      onApprove={handleApprove}
                      onReject={handleReject}
                      isReviewing={reviewMutation.isPending}
                    />
                  ))}
                </div>
              )}
            </div>
          )}

          {/* Non-finalized: show all candidates */}
          {detail && !finalized &&
            detail.candidates.map((c) => (
              <CandidateCard
                key={c.candidate_id}
                candidate={c}
                itemKind={item.kind}
                fieldPath={item.field_path}
                canWrite={canWrite}
                onApprove={handleApprove}
                onReject={handleReject}
                isReviewing={reviewMutation.isPending}
              />
            ))}
        </div>
      )}
    </div>
  );
}
