"use client";

/**
 * EdgesPanel — compact table of ontology edges (predicates) with a status
 * filter and inline status-adaptive review. GET /spoke/ontogen/result/edge
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
import { useOntogenEdges } from "@/lib/api/ontogen";
import { ontogenStatusLabel, ontogenStatusVariant } from "@/lib/ontogen-status-variant";
import { filterByApproval, type ApprovalFilterMode } from "@/lib/ontogen-filter";

const PAGE_SIZE = 100;

interface EdgesPanelProps {
  canWrite: boolean;
}

export function EdgesPanel({ canWrite }: EdgesPanelProps) {
  const [offset, setOffset] = useState(0);
  const [mode, setMode] = useState<ApprovalFilterMode>("all");
  const { data, isLoading, error } = useOntogenEdges({ offset, limit: PAGE_SIZE });

  const total = data?.total_count ?? 0;
  const edges = filterByApproval(data?.edges ?? [], mode);
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
    return <p className="text-sm text-destructive">Failed to load edges: {error.message}</p>;
  }

  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <ApprovalFilter value={mode} onChange={setMode} />
      </div>

      {edges.length === 0 ? (
        <p className="py-8 text-center text-sm text-muted-foreground">No ontology edges.</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Label</TableHead>
              <TableHead>Semantics</TableHead>
              <TableHead className="w-28">Status</TableHead>
              <TableHead className="w-16">Conf</TableHead>
              <TableHead>Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {edges.map((edge) => (
              <TableRow key={edge.id} className="align-top">
                <TableCell>
                  <div className="font-semibold">{edge.label}</div>
                  <EvidenceDisclosure kind="edge" id={edge.id} />
                </TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  {edge.semantics ?? "—"}
                </TableCell>
                <TableCell>
                  <Badge variant={ontogenStatusVariant(edge.status)}>
                    {ontogenStatusLabel(edge.status)}
                  </Badge>
                </TableCell>
                <TableCell className="text-xs text-muted-foreground">
                  {edge.confidence_score.toFixed(2)}
                </TableCell>
                <TableCell>
                  {canWrite && <ReviewRow id={edge.id} kind="edge" status={edge.status} />}
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
