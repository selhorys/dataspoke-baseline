"use client";

/**
 * EdgesPanel — paginated list of ontology edges (predicates) with inline review.
 * GET /spoke/ontogen/result/edge
 */

import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ReviewRow } from "@/components/ontogen/review-row";
import { useOntogenEdges } from "@/lib/api/ontogen";
import { ontogenStatusLabel, ontogenStatusVariant } from "@/lib/ontogen-status-variant";

const PAGE_SIZE = 20;

interface EdgesPanelProps {
  canWrite: boolean;
}

export function EdgesPanel({ canWrite }: EdgesPanelProps) {
  const [offset, setOffset] = useState(0);
  const { data, isLoading, error } = useOntogenEdges({ offset, limit: PAGE_SIZE });

  const edges = data?.edges ?? [];
  const total = data?.total_count ?? 0;
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

  if (edges.length === 0) {
    return (
      <p className="py-8 text-center text-sm text-muted-foreground">
        No ontology edges yet.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      <ul className="space-y-2">
        {edges.map((edge) => {
          const isPending = edge.status === "llm_pending" || edge.status === "llm_approved";
          return (
            <li key={edge.id} className="rounded-md border p-3">
              <div className="flex items-center gap-2">
                <span className="font-semibold">{edge.label}</span>
                <Badge variant={ontogenStatusVariant(edge.status)}>
                  {ontogenStatusLabel(edge.status)}
                </Badge>
                <span className="text-xs text-muted-foreground">
                  conf {edge.confidence_score.toFixed(2)}
                </span>
              </div>
              {edge.semantics && (
                <p className="mt-0.5 text-xs text-muted-foreground">{edge.semantics}</p>
              )}
              {isPending && canWrite && (
                <ReviewRow id={edge.id} kind="edge" />
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
