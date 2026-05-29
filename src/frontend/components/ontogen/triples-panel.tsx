"use client";

/**
 * TriplesPanel — paginated list of ontology triples with gated inline review.
 * GET /spoke/ontogen/result/triple
 *
 * Triple approve is gated: disabled with a hint when the subject node, predicate
 * edge, or object node is not yet status='approved'. This mirrors the backend's
 * 422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING guard.
 */

import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ReviewRow } from "@/components/ontogen/review-row";
import { useOntogenEdges, useOntogenNodes, useOntogenTriples } from "@/lib/api/ontogen";
import { ontogenStatusLabel, ontogenStatusVariant } from "@/lib/ontogen-status-variant";
import { tripleApprovalGate, buildNodesById, buildEdgesById } from "@/lib/ontogen-triple-gate";

const PAGE_SIZE = 20;

interface TriplesPanelProps {
  canWrite: boolean;
}

export function TriplesPanel({ canWrite }: TriplesPanelProps) {
  const [offset, setOffset] = useState(0);

  // Fetch triples (paginated) plus ALL nodes and edges for gating lookup.
  const triplesQuery = useOntogenTriples({ offset, limit: PAGE_SIZE });
  const nodesQuery = useOntogenNodes({ limit: 100 });
  const edgesQuery = useOntogenEdges({ limit: 100 });

  const triples = triplesQuery.data?.triples ?? [];
  const total = triplesQuery.data?.total_count ?? 0;
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

  if (triples.length === 0) {
    return (
      <p className="py-8 text-center text-sm text-muted-foreground">
        No ontology triples yet.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      <ul className="space-y-2">
        {triples.map((triple) => {
          const subjectName = nodesById.get(triple.subject_node_id)?.name ?? triple.subject_node_id;
          const edgeLabel = edgesById.get(triple.edge_id)?.label ?? triple.edge_id;
          const objectName = nodesById.get(triple.object_node_id)?.name ?? triple.object_node_id;

          const isPending = triple.status === "llm_pending" || triple.status === "llm_approved";
          const { canApprove, blockingHint } = tripleApprovalGate(triple, nodesById, edgesById);

          return (
            <li key={triple.id} className="rounded-md border p-3">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="font-mono text-sm">
                  {subjectName}
                  <span className="mx-1 text-muted-foreground">--{edgeLabel}--&gt;</span>
                  {objectName}
                </span>
                <Badge variant={ontogenStatusVariant(triple.status)}>
                  {ontogenStatusLabel(triple.status)}
                </Badge>
                <span className="text-xs text-muted-foreground">
                  conf {triple.confidence_score.toFixed(2)}
                </span>
              </div>
              {isPending && canWrite && (
                <ReviewRow
                  id={triple.id}
                  kind="triple"
                  disabled={!canApprove}
                  disabledHint={blockingHint || undefined}
                />
              )}
            </li>
          );
        })}
      </ul>

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
