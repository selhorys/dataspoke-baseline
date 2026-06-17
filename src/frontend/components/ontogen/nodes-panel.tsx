"use client";

/**
 * NodesPanel — compact table of ontology nodes with a status filter and inline
 * status-adaptive review controls. GET /spoke/ontogen/result/node
 */

import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { ReviewRow } from "@/components/ontogen/review-row";
import { EvidenceDisclosure } from "@/components/ontogen/evidence-disclosure";
import { ApprovalFilter } from "@/components/ontogen/approval-filter";
import { useOntogenNodes } from "@/lib/api/ontogen";
import { ontogenStatusLabel, ontogenStatusVariant } from "@/lib/ontogen-status-variant";
import { filterByApproval, type ApprovalFilterMode } from "@/lib/ontogen-filter";

const PAGE_SIZE = 100;

interface NodesPanelProps {
  canWrite: boolean;
}

export function NodesPanel({ canWrite }: NodesPanelProps) {
  const [offset, setOffset] = useState(0);
  const [mode, setMode] = useState<ApprovalFilterMode>("all");
  const { data, isLoading, error } = useOntogenNodes({ offset, limit: PAGE_SIZE });

  const total = data?.total_count ?? 0;
  const nodes = filterByApproval(data?.nodes ?? [], mode);
  const totalPages = Math.ceil(total / PAGE_SIZE);
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  if (isLoading) {
    return (
      <div className="space-y-2">
        {Array.from({ length: 5 }).map((_, i) => (
          <Skeleton key={i} className="h-12 w-full" />
        ))}
      </div>
    );
  }

  if (error) {
    return <p className="text-sm text-destructive">Failed to load nodes: {error.message}</p>;
  }

  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <ApprovalFilter value={mode} onChange={setMode} />
      </div>

      {nodes.length === 0 ? (
        <p className="py-8 text-center text-sm text-muted-foreground">No ontology nodes.</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Name</TableHead>
              <TableHead className="w-28">Status</TableHead>
              <TableHead className="w-16">Conf</TableHead>
              <TableHead>Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {nodes.map((node) => (
              <TableRow key={node.id} className="align-top">
                <TableCell>
                  <div className="font-semibold">{node.name}</div>
                  {node.description && (
                    <p className="mt-0.5 text-xs text-muted-foreground">{node.description}</p>
                  )}
                  <EvidenceDisclosure kind="node" id={node.id} />
                </TableCell>
                <TableCell>
                  <Badge variant={ontogenStatusVariant(node.status)}>
                    {ontogenStatusLabel(node.status)}
                  </Badge>
                </TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  {node.confidence_score.toFixed(2)}
                </TableCell>
                <TableCell>
                  {canWrite && <ReviewRow id={node.id} kind="node" status={node.status} />}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      )}

      {totalPages > 1 && (
        <div className="flex items-center justify-between text-sm">
          <span className="text-muted-foreground">
            Page {currentPage} of {totalPages} ({total} total)
          </span>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
              disabled={offset === 0}
            >
              Previous
            </Button>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setOffset(offset + PAGE_SIZE)}
              disabled={offset + PAGE_SIZE >= total}
            >
              Next
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
