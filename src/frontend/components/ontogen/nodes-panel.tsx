"use client";

/**
 * NodesPanel — paginated list of ontology nodes with inline review controls.
 * GET /spoke/ontogen/result/node
 */

import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ReviewRow } from "@/components/ontogen/review-row";
import { useOntogenNodes } from "@/lib/api/ontogen";
import { ontogenStatusLabel, ontogenStatusVariant } from "@/lib/ontogen-status-variant";

const PAGE_SIZE = 20;

interface NodesPanelProps {
  canWrite: boolean;
}

export function NodesPanel({ canWrite }: NodesPanelProps) {
  const [offset, setOffset] = useState(0);
  const { data, isLoading, error } = useOntogenNodes({ offset, limit: PAGE_SIZE });

  const nodes = data?.nodes ?? [];
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
    return <p className="text-sm text-destructive">Failed to load nodes: {error.message}</p>;
  }

  if (nodes.length === 0) {
    return (
      <p className="py-8 text-center text-sm text-muted-foreground">
        No ontology nodes yet.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      <ul className="space-y-2">
        {nodes.map((node) => {
          const isPending = node.status === "llm_pending" || node.status === "llm_approved";
          return (
            <li key={node.id} className="rounded-md border p-3">
              <div className="flex items-center gap-2">
                <span className="font-semibold">{node.name}</span>
                <Badge variant={ontogenStatusVariant(node.status)}>
                  {ontogenStatusLabel(node.status)}
                </Badge>
                <span className="text-xs text-muted-foreground">
                  conf {node.confidence_score.toFixed(2)}
                </span>
              </div>
              {node.description && (
                <p className="mt-0.5 text-xs text-muted-foreground">{node.description}</p>
              )}
              {isPending && canWrite && (
                <ReviewRow id={node.id} kind="node" />
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
