"use client";

/**
 * NodesPanel — uniform compact table of ontology nodes with an approval filter,
 * a created-at sort control, evidence modal, reason-confirm review, and the
 * shared Pagination control. GET /spoke/ontogen/result/node
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
import { useOntogenNodes } from "@/lib/api/ontogen";
import { ontogenStatusLabel, ontogenStatusVariant } from "@/lib/ontogen-status-variant";
import { filterByApproval, type ApprovalFilterMode } from "@/lib/ontogen-filter";
import { formatDateTime } from "@/lib/format-time";
import { useDisplayTz } from "@/lib/preferences/timezone";

interface NodesPanelProps {
  canWrite: boolean;
}

export function NodesPanel({ canWrite }: NodesPanelProps) {
  const [offset, setOffset] = useState(0);
  const [limit, setLimit] = useState(DEFAULT_PAGE_SIZE);
  const [mode, setMode] = useState<ApprovalFilterMode>("all");
  const [sort, setSort] = useState<OntogenSortMode>("created_at_desc");
  const tz = useDisplayTz();
  const { data, isLoading, error } = useOntogenNodes({ offset, limit, sort });

  const total = data?.total_count ?? 0;
  const nodes = filterByApproval(data?.nodes ?? [], mode);

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
        <p className="text-sm text-destructive">Failed to load nodes: {error.message}</p>
      ) : nodes.length === 0 ? (
        <p className="py-8 text-center text-sm text-muted-foreground">No ontology nodes.</p>
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
            {nodes.map((node) => (
              <TableRow key={node.id} className="text-xs">
                <TableCell className="py-2 font-medium">{node.name}</TableCell>
                <TableCell className="max-w-[280px] truncate py-2 text-muted-foreground">
                  {node.description || "—"}
                </TableCell>
                <TableCell className="py-2">
                  <Badge variant={ontogenStatusVariant(node.status)}>
                    {ontogenStatusLabel(node.status)}
                  </Badge>
                </TableCell>
                <TableCell className="py-2">
                  <div className="flex items-center gap-1">
                    <span className="text-muted-foreground">
                      {node.confidence_score.toFixed(2)}
                    </span>
                    <EvidenceDialog kind="node" id={node.id} />
                  </div>
                </TableCell>
                <TableCell className="py-2">
                  {canWrite && <ReviewRow id={node.id} kind="node" status={node.status} />}
                </TableCell>
                <TableCell className="whitespace-nowrap py-2 text-muted-foreground">
                  {formatDateTime(node.created_at, tz)}
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
