"use client";

/**
 * EdgesPanel — uniform compact table of ontology edges (predicates) with an
 * approval filter, a created-at sort control, evidence modal, reason-confirm
 * review, and the shared Pagination control. GET /spoke/ontogen/result/edge
 */

import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Pagination, DEFAULT_PAGE_SIZE } from "@/components/pagination";
import { ReviewRow } from "@/components/ontogen/review-row";
import { EvidenceDialog } from "@/components/ontogen/evidence-dialog";
import { ApprovalFilter } from "@/components/ontogen/approval-filter";
import { SortControl, type OntogenSortMode } from "@/components/ontogen/sort-control";
import { useOntogenEdges } from "@/lib/api/ontogen";
import { ontogenStatusLabel, ontogenStatusVariant } from "@/lib/ontogen-status-variant";
import { filterByApproval, type ApprovalFilterMode } from "@/lib/ontogen-filter";
import { formatDateTime } from "@/lib/format-time";
import { useDisplayTz } from "@/lib/preferences/timezone";

interface EdgesPanelProps {
  canWrite: boolean;
}

export function EdgesPanel({ canWrite }: EdgesPanelProps) {
  const [offset, setOffset] = useState(0);
  const [limit, setLimit] = useState(DEFAULT_PAGE_SIZE);
  const [mode, setMode] = useState<ApprovalFilterMode>("all");
  const [sort, setSort] = useState<OntogenSortMode>("created_at_desc");
  const tz = useDisplayTz();
  const { data, isLoading, error } = useOntogenEdges({ offset, limit, sort });

  const total = data?.total_count ?? 0;
  const edges = filterByApproval(data?.edges ?? [], mode);

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-end gap-3">
        <SortControl
          value={sort}
          onChange={(s) => {
            setSort(s);
            setOffset(0);
          }}
        />
        <ApprovalFilter
          value={mode}
          onChange={(m) => {
            setMode(m);
            setOffset(0);
          }}
        />
      </div>

      {isLoading ? (
        <div className="space-y-2">
          {Array.from({ length: 5 }).map((_, i) => (
            <Skeleton key={i} className="h-10 w-full" />
          ))}
        </div>
      ) : error ? (
        <p className="text-sm text-destructive">Failed to load edges: {error.message}</p>
      ) : edges.length === 0 ? (
        <p className="py-8 text-center text-sm text-muted-foreground">No ontology edges.</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead className="text-xs">Title</TableHead>
              <TableHead className="text-xs">Description</TableHead>
              <TableHead className="w-28 text-xs">Status</TableHead>
              <TableHead className="w-32 text-xs">Confidence</TableHead>
              <TableHead className="w-44 text-xs">Actions</TableHead>
              <TableHead className="w-36 text-xs">Created At</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {edges.map((edge) => (
              <TableRow key={edge.id} className="text-xs">
                <TableCell className="py-2 font-medium">{edge.label}</TableCell>
                <TableCell className="max-w-[280px] truncate py-2 text-muted-foreground">
                  {edge.semantics ?? "—"}
                </TableCell>
                <TableCell className="py-2">
                  <Badge variant={ontogenStatusVariant(edge.status)}>
                    {ontogenStatusLabel(edge.status)}
                  </Badge>
                </TableCell>
                <TableCell className="py-2">
                  <div className="flex items-center gap-1">
                    <span className="text-muted-foreground">
                      {edge.confidence_score.toFixed(2)}
                    </span>
                    <EvidenceDialog kind="edge" id={edge.id} />
                  </div>
                </TableCell>
                <TableCell className="py-2">
                  {canWrite && <ReviewRow id={edge.id} kind="edge" status={edge.status} />}
                </TableCell>
                <TableCell className="whitespace-nowrap py-2 text-muted-foreground">
                  {formatDateTime(edge.created_at, tz)}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      <Pagination
        offset={offset}
        limit={limit}
        total={total}
        onOffset={setOffset}
        onLimit={setLimit}
      />
    </div>
  );
}
