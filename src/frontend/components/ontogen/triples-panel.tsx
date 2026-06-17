"use client";

/**
 * TriplesPanel — compact table of ontology triples with a status filter and
 * gated inline review. GET /spoke/ontogen/result/triple
 *
 * Triple approve is gated: disabled with a hint when the subject node, predicate
 * edge, or object node is not yet status='approved'. This mirrors the backend's
 * 422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING guard.
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
import { useOntogenEdges, useOntogenNodes, useOntogenTriples } from "@/lib/api/ontogen";
import { ontogenStatusLabel, ontogenStatusVariant } from "@/lib/ontogen-status-variant";
import { tripleApprovalGate, buildNodesById, buildEdgesById } from "@/lib/ontogen-triple-gate";
import { filterByApproval, type ApprovalFilterMode } from "@/lib/ontogen-filter";

const PAGE_SIZE = 100;

interface TriplesPanelProps {
  canWrite: boolean;
}

export function TriplesPanel({ canWrite }: TriplesPanelProps) {
  const [offset, setOffset] = useState(0);
  const [mode, setMode] = useState<ApprovalFilterMode>("all");

  // Fetch triples (paginated) plus ALL nodes and edges for gating lookup.
  const triplesQuery = useOntogenTriples({ offset, limit: PAGE_SIZE });
  const nodesQuery = useOntogenNodes({ limit: PAGE_SIZE });
  const edgesQuery = useOntogenEdges({ limit: PAGE_SIZE });

  const total = triplesQuery.data?.total_count ?? 0;
  const triples = filterByApproval(triplesQuery.data?.triples ?? [], mode);
  const totalPages = Math.ceil(total / PAGE_SIZE);
  const currentPage = Math.floor(offset / PAGE_SIZE) + 1;

  const nodesById = buildNodesById(nodesQuery.data?.nodes ?? []);
  const edgesById = buildEdgesById(edgesQuery.data?.edges ?? []);

  const isLoading = triplesQuery.isLoading || nodesQuery.isLoading || edgesQuery.isLoading;

  if (isLoading) {
    return (
      <div className="space-y-2">
        {Array.from({ length: 5 }).map((_, i) => (
          <Skeleton key={i} className="h-12 w-full" />
        ))}
      </div>
    );
  }

  if (triplesQuery.error) {
    return (
      <p className="text-sm text-destructive">
        Failed to load triples: {triplesQuery.error.message}
      </p>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <ApprovalFilter value={mode} onChange={setMode} />
      </div>

      {triples.length === 0 ? (
        <p className="py-8 text-center text-sm text-muted-foreground">No ontology triples.</p>
      ) : (
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Triple</TableHead>
              <TableHead className="w-28">Status</TableHead>
              <TableHead className="w-16">Conf</TableHead>
              <TableHead>Actions</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {triples.map((triple) => {
              const subjectName =
                nodesById.get(triple.subject_node_id)?.name ?? triple.subject_node_id;
              const edgeLabel = edgesById.get(triple.edge_id)?.label ?? triple.edge_id;
              const objectName =
                nodesById.get(triple.object_node_id)?.name ?? triple.object_node_id;
              const { canApprove, blockingHint } = tripleApprovalGate(triple, nodesById, edgesById);

              return (
                <TableRow key={triple.id} className="align-top">
                  <TableCell>
                    <span className="font-mono text-sm">
                      {subjectName}
                      <span className="mx-1 text-muted-foreground">--{edgeLabel}--&gt;</span>
                      {objectName}
                    </span>
                    <EvidenceDisclosure kind="triple" id={triple.id} />
                  </TableCell>
                  <TableCell>
                    <Badge variant={ontogenStatusVariant(triple.status)}>
                      {ontogenStatusLabel(triple.status)}
                    </Badge>
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">
                    {triple.confidence_score.toFixed(2)}
                  </TableCell>
                  <TableCell>
                    {canWrite && (
                      <ReviewRow
                        id={triple.id}
                        kind="triple"
                        status={triple.status}
                        disabled={!canApprove}
                        disabledHint={blockingHint || undefined}
                      />
                    )}
                  </TableCell>
                </TableRow>
              );
            })}
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
