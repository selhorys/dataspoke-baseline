"use client";

/**
 * OntologyNavigator — shared component (FRONTEND_BASIC.md §Shared Component Notes).
 *
 * Renders a flat node list with pending triples overlaid as labelled arrows.
 * Reads GET /spoke/ontogen/result/{node,edge,triple} directly.
 * Outgoing triples per node are filtered client-side from the full triple list
 * (the API does not expose a per-node filter).
 * Approve/Reject controls are rendered only when canWrite=true.
 *
 * Usage:
 *   <OntologyNavigator canWrite={canWrite} />
 */

import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useOntogenNodes, useOntogenEdges, useOntogenTriples, useReviewOntogenItem } from "@/lib/api/ontogen";
import { ontogenStatusLabel, ontogenStatusVariant } from "@/lib/ontogen-status-variant";
import { tripleApprovalGate, buildNodesById, buildEdgesById } from "@/lib/ontogen-triple-gate";
import type { OntogenNode, OntogenStatus, OntogenTriple } from "@/types/ontogen";
import { useToast } from "@/components/ui/use-toast";

interface OntologyNavigatorProps {
  /** Whether the current user may approve or reject items. */
  canWrite: boolean;
}

/** Renders pending-item approve/reject inline controls. */
function ReviewControls({
  id,
  kind,
  disabled,
  disabledHint,
  canWrite,
}: {
  id: string;
  kind: "node" | "edge" | "triple";
  disabled?: boolean;
  disabledHint?: string;
  canWrite: boolean;
}) {
  const [reason, setReason] = useState("");
  const reviewMutation = useReviewOntogenItem();
  const { toast } = useToast();

  if (!canWrite) return null;

  const pending = reviewMutation.isPending;

  function handleReview(verdict: "approve" | "reject") {
    reviewMutation.mutate(
      { kind, id, body: { verdict, reason: reason || undefined } },
      {
        onSuccess: () => {
          setReason("");
          toast({ title: `${kind} ${verdict}d`, description: `ID: ${id}` });
        },
        onError: (err) => {
          toast({ title: "Review failed", description: err.message, variant: "destructive" });
        },
      },
    );
  }

  return (
    <div className="mt-1 flex flex-col gap-1 pl-4">
      <input
        type="text"
        className="h-7 rounded border border-input bg-background px-2 text-xs placeholder:text-muted-foreground"
        placeholder="reason (optional)"
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        disabled={pending}
      />
      <div className="flex items-center gap-2">
        <Button
          size="sm"
          variant="default"
          onClick={() => handleReview("approve")}
          disabled={pending || disabled}
          title={disabled ? disabledHint : undefined}
          className="h-7 text-xs"
        >
          Approve
        </Button>
        <Button
          size="sm"
          variant="destructive"
          onClick={() => handleReview("reject")}
          disabled={pending}
          className="h-7 text-xs"
        >
          Reject
        </Button>
        {disabled && disabledHint && (
          <span className="text-xs text-muted-foreground">{disabledHint}</span>
        )}
      </div>
    </div>
  );
}

/** Renders the triples (as labelled arrows) whose subject is the given node. */
function NodeTriples({
  node,
  triples,
  nodesById,
  edgesById,
  canWrite,
}: {
  node: OntogenNode;
  triples: OntogenTriple[];
  nodesById: Map<string, { status: OntogenStatus; name: string }>;
  edgesById: Map<string, { status: OntogenStatus; label: string }>;
  canWrite: boolean;
}) {
  const outgoing = triples.filter((t) => t.subject_node_id === node.id);
  if (outgoing.length === 0) return null;

  return (
    <ul className="mt-1 space-y-1 pl-4">
      {outgoing.map((triple) => {
        const edgeLabel = edgesById.get(triple.edge_id)?.label ?? triple.edge_id;
        const objectName = nodesById.get(triple.object_node_id)?.name ?? triple.object_node_id;
        const { canApprove, blockingHint } = tripleApprovalGate(triple, nodesById, edgesById);
        const isPending = triple.status === "llm_pending" || triple.status === "llm_approved";

        return (
          <li key={triple.id} className="rounded border bg-muted/30 px-3 py-1.5 text-sm">
            <div className="flex items-center gap-2">
              <span className="font-mono text-xs">
                {node.name}
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
            {isPending && (
              <ReviewControls
                id={triple.id}
                kind="triple"
                disabled={!canApprove}
                disabledHint={blockingHint}
                canWrite={canWrite}
              />
            )}
          </li>
        );
      })}
    </ul>
  );
}

export function OntologyNavigator({ canWrite }: OntologyNavigatorProps) {
  const nodesQuery = useOntogenNodes({ limit: 100 });
  const edgesQuery = useOntogenEdges({ limit: 100 });
  const triplesQuery = useOntogenTriples({ limit: 100 });

  const isLoading = nodesQuery.isLoading || edgesQuery.isLoading || triplesQuery.isLoading;

  const nodes = nodesQuery.data?.nodes ?? [];
  const edges = edgesQuery.data?.edges ?? [];
  const triples = triplesQuery.data?.triples ?? [];

  const nodesById = buildNodesById(nodes);
  const edgesById = buildEdgesById(edges);

  if (isLoading) {
    return (
      <div className="space-y-2">
        {Array.from({ length: 4 }).map((_, i) => (
          <Skeleton key={i} className="h-10 w-full" />
        ))}
      </div>
    );
  }

  if (nodes.length === 0) {
    return (
      <p className="py-6 text-center text-sm text-muted-foreground">
        No ontology nodes yet. Run inference to generate results.
      </p>
    );
  }

  return (
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
            {isPending && (
              <ReviewControls id={node.id} kind="node" canWrite={canWrite} />
            )}
            <NodeTriples
              node={node}
              triples={triples}
              nodesById={nodesById}
              edgesById={edgesById}
              canWrite={canWrite}
            />
          </li>
        );
      })}
    </ul>
  );
}
