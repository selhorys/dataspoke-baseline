"use client";

/**
 * ItemKindTable — one per item kind on the /data/[urn] MetaGen panel, inside a
 * foldable panel. Rows are candidates (fetched per item via
 * `GET …/attr/metagen/item/{item_id}`). Common columns: generated value, run
 * info (conf name · confidence_score · Evidence link), status, action.
 *
 * The `column.description` instance (`groupByColumn`) adds a leading `field_path`
 * column and groups its rows by column (item): each column's candidates render
 * contiguously so the Approve / Reject action's scope — and its sibling-demotion
 * effect — is visible per column. Each row's action is keyed to that row's
 * (dataset_urn, item_id, candidate_id) via useReviewCandidate.
 *
 * Spec: spec/feature/FRONTEND_METAGEN.md §Per-dataset.
 */

import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ConfirmDialog } from "@/components/confirm-dialog";
import { EvidenceLink } from "@/components/ontogen/evidence-link";
import { useMetagenItem, useReviewCandidate } from "@/lib/api/metagen";
import {
  isRejectEligible,
  destinationAspectLabel,
} from "@/lib/metagen-predicates";
import { useToast } from "@/components/ui/use-toast";
import { ApiError } from "@/lib/api/client";
import type { MetagenCandidate, MetagenItemSummary } from "@/types/metagen";

const STATUS_VARIANT: Record<
  string,
  "default" | "secondary" | "outline" | "destructive"
> = {
  llm_approved: "secondary",
  approved: "default",
  rejected: "outline",
};

interface ItemKindTableProps {
  items: MetagenItemSummary[];
  /** True for the column.description table — adds field_path column + groups by item. */
  groupByColumn: boolean;
  canWrite: boolean;
}

export function ItemKindTable({
  items,
  groupByColumn,
  canWrite,
}: ItemKindTableProps) {
  const colCount = groupByColumn ? 5 : 4;

  return (
    <div className="overflow-x-auto rounded-md border">
      <table className="w-full text-sm">
        <thead className="bg-muted/50">
          <tr>
            {groupByColumn && (
              <th className="px-3 py-2 text-left text-xs font-medium text-muted-foreground">
                field
              </th>
            )}
            <th className="px-3 py-2 text-left text-xs font-medium text-muted-foreground">
              generated value
            </th>
            <th className="px-3 py-2 text-left text-xs font-medium text-muted-foreground">
              run info
            </th>
            <th className="px-3 py-2 text-left text-xs font-medium text-muted-foreground">
              status
            </th>
            <th className="px-3 py-2 text-right text-xs font-medium text-muted-foreground">
              action
            </th>
          </tr>
        </thead>
        <tbody className="divide-y">
          {items.map((item) => (
            <ItemRows
              key={item.composite_id}
              item={item}
              groupByColumn={groupByColumn}
              canWrite={canWrite}
              colCount={colCount}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

interface ItemRowsProps {
  item: MetagenItemSummary;
  groupByColumn: boolean;
  canWrite: boolean;
  colCount: number;
}

/**
 * Renders one item's candidate rows as a contiguous group. For the
 * column.description table the leading field_path cell spans the group's rows
 * (rowSpan) so the column grouping — and the per-column approve scope — is
 * visible. The review action is keyed per (datasetUrn, itemId, candidateId).
 */
function ItemRows({ item, groupByColumn, canWrite, colCount }: ItemRowsProps) {
  const { toast } = useToast();
  const { data: detail, isLoading } = useMetagenItem(
    item.dataset_urn,
    item.item_id,
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

  if (isLoading && !detail) {
    return (
      <tr>
        {groupByColumn && (
          <td className="px-3 py-2 align-top font-mono text-xs">
            {item.field_path ?? "—"}
          </td>
        )}
        <td className="px-3 py-2" colSpan={colCount - (groupByColumn ? 1 : 0)}>
          <Skeleton className="h-8 w-full" />
        </td>
      </tr>
    );
  }

  const candidates = detail?.candidates ?? [];

  if (candidates.length === 0) {
    return (
      <tr>
        {groupByColumn && (
          <td className="px-3 py-2 align-top font-mono text-xs">
            {item.field_path ?? "—"}
          </td>
        )}
        <td
          className="px-3 py-2 text-xs text-muted-foreground"
          colSpan={colCount - (groupByColumn ? 1 : 0)}
        >
          No candidates yet.
        </td>
      </tr>
    );
  }

  return (
    <>
      {candidates.map((candidate, idx) => (
        <CandidateRow
          key={candidate.candidate_id}
          candidate={candidate}
          itemKind={item.kind}
          fieldPath={item.field_path}
          canWrite={canWrite}
          isReviewing={reviewMutation.isPending}
          onApprove={handleApprove}
          onReject={handleReject}
          // Leading field_path cell: only the first row of the column group
          // renders it (rowSpan over the whole group).
          fieldCell={
            groupByColumn
              ? idx === 0
                ? {
                    fieldPath: item.field_path,
                    rowSpan: candidates.length,
                  }
                : null
              : undefined
          }
        />
      ))}
    </>
  );
}

interface CandidateRowProps {
  candidate: MetagenCandidate;
  itemKind: string;
  fieldPath: string | null;
  canWrite: boolean;
  isReviewing: boolean;
  onApprove: (candidateId: string) => void;
  onReject: (candidateId: string) => void;
  /**
   * undefined → no field column (dataset.description table).
   * null → field column exists but this row is covered by an earlier rowSpan.
   * object → render the leading field cell with the given rowSpan.
   */
  fieldCell?:
    | undefined
    | null
    | { fieldPath: string | null; rowSpan: number };
}

function CandidateRow({
  candidate,
  itemKind,
  fieldPath,
  canWrite,
  isReviewing,
  onApprove,
  onReject,
  fieldCell,
}: CandidateRowProps) {
  const [approveOpen, setApproveOpen] = useState(false);
  const [rejectOpen, setRejectOpen] = useState(false);

  const aspectLabel = destinationAspectLabel(itemKind, fieldPath);
  const isApproved = candidate.status === "approved";
  const showApprove = canWrite && !isApproved;
  const showReject = canWrite && isRejectEligible(candidate);

  return (
    <tr
      data-testid="metagen-candidate-row"
      data-conf-name={candidate.conf_name ?? ""}
      data-candidate-status={candidate.status}
      className="align-top hover:bg-muted/30"
    >
      {fieldCell !== undefined &&
        fieldCell !== null && (
          <td
            className="border-r px-3 py-2 align-top font-mono text-xs"
            rowSpan={fieldCell.rowSpan}
          >
            {fieldCell.fieldPath ?? "—"}
          </td>
        )}
      <td className="px-3 py-2">
        <pre className="max-w-md whitespace-pre-wrap break-words font-mono text-xs leading-relaxed text-foreground">
          {candidate.value}
        </pre>
      </td>
      <td className="px-3 py-2 text-xs">
        <div className="flex flex-wrap items-center gap-1.5">
          {candidate.conf_name ? (
            <Badge variant="outline" className="text-xs" title="Producing conf">
              {candidate.conf_name}
            </Badge>
          ) : (
            <Badge
              variant="outline"
              className="text-xs italic text-muted-foreground"
              title="Producing conf was deleted; this result is parentless"
            >
              no conf
            </Badge>
          )}
          <span className="text-muted-foreground">
            {candidate.confidence_score.toFixed(2)}
          </span>
          <span className="inline-flex items-center gap-1">
            <span className="text-muted-foreground">Evidence</span>
            <EvidenceLink runId={candidate.run_id} />
          </span>
        </div>
      </td>
      <td className="px-3 py-2">
        <Badge
          variant={STATUS_VARIANT[candidate.status] ?? "outline"}
          className="text-xs"
        >
          {candidate.status}
        </Badge>
      </td>
      <td className="px-3 py-2 text-right">
        {(showApprove || showReject) && (
          <div className="inline-flex items-center gap-1.5">
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
      </td>

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
    </tr>
  );
}
