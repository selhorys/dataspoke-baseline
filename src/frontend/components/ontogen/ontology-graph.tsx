"use client";

/**
 * OntologyGraph — interactive force-directed view of the ontology.
 *
 * Graph nodes are ontogen nodes; links are triples (source/target = the
 * subject/object node, labelled by the triple's edge). Nodes are colored by
 * status and sized by degree; the view supports drag, zoom/pan, and
 * hover-highlight of a node's neighbors. Read-only — review actions live in the
 * Nodes/Edges/Triples tables.
 *
 * Reads GET /spoke/ontogen/result/{node,edge,triple}. Rendered client-side only
 * (imported via next/dynamic with ssr: false) because the canvas needs the DOM.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import ForceGraph2D from "react-force-graph-2d";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useOntogenEdges, useOntogenNodes, useOntogenTriples } from "@/lib/api/ontogen";
import {
  buildOntologyGraph,
  ontologyGraphColor,
  type OntologyGraphLink,
  type OntologyGraphMode,
  type OntologyGraphNode,
} from "@/lib/ontogen-graph";

const PAGE_SIZE = 100;

export function OntologyGraph() {
  const [mode, setMode] = useState<OntologyGraphMode>("all");
  const [hoverNodeId, setHoverNodeId] = useState<string | null>(null);

  const nodesQuery = useOntogenNodes({ limit: PAGE_SIZE });
  const edgesQuery = useOntogenEdges({ limit: PAGE_SIZE });
  const triplesQuery = useOntogenTriples({ limit: PAGE_SIZE });

  const isLoading = nodesQuery.isLoading || edgesQuery.isLoading || triplesQuery.isLoading;

  const graph = useMemo(
    () =>
      buildOntologyGraph(
        nodesQuery.data?.nodes ?? [],
        triplesQuery.data?.triples ?? [],
        edgesQuery.data?.edges ?? [],
        mode,
      ),
    [nodesQuery.data, triplesQuery.data, edgesQuery.data, mode],
  );

  // Neighbor sets for hover-highlight.
  const neighborIds = useMemo(() => {
    if (!hoverNodeId) return new Set<string>();
    const set = new Set<string>([hoverNodeId]);
    for (const link of graph.links) {
      const source = linkEndId(link.source);
      const target = linkEndId(link.target);
      if (source === hoverNodeId) set.add(target);
      if (target === hoverNodeId) set.add(source);
    }
    return set;
  }, [hoverNodeId, graph.links]);

  // Measure the container so the canvas fills it responsively.
  const containerRef = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ width: 0, height: 0 });

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const observer = new ResizeObserver((entries) => {
      const rect = entries[0]?.contentRect;
      if (rect) setSize({ width: rect.width, height: rect.height });
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <div className="flex items-center gap-2">
          <span className="text-sm text-muted-foreground">filter</span>
          <Select value={mode} onValueChange={(v) => setMode(v as OntologyGraphMode)}>
            <SelectTrigger className="h-8 w-40" aria-label="Graph filter">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">All</SelectItem>
              <SelectItem value="approved">Approved-only</SelectItem>
            </SelectContent>
          </Select>
        </div>
      </div>

      <div
        ref={containerRef}
        data-testid="ontology-graph-canvas"
        className="h-[560px] w-full overflow-hidden rounded-md border bg-card"
      >
        {isLoading ? (
          <Skeleton className="h-full w-full" />
        ) : graph.nodes.length === 0 ? (
          <p className="flex h-full items-center justify-center text-sm text-muted-foreground">
            No ontology nodes to display.
          </p>
        ) : (
          size.width > 0 && (
            <ForceGraph2D<OntologyGraphNode, OntologyGraphLink>
              width={size.width}
              height={size.height}
              graphData={graph}
              nodeId="id"
              nodeVal="val"
              nodeRelSize={4}
              nodeLabel={(node) => `${node.name} (${node.status})`}
              nodeColor={(node) =>
                hoverNodeId && !neighborIds.has(node.id)
                  ? "#d1d5db"
                  : ontologyGraphColor(node.status)
              }
              linkColor={(link) => {
                const dimmed =
                  hoverNodeId &&
                  linkEndId(link.source) !== hoverNodeId &&
                  linkEndId(link.target) !== hoverNodeId;
                return dimmed ? "rgba(148,163,184,0.2)" : ontologyGraphColor(link.status);
              }}
              linkLabel={(link) => link.label}
              linkDirectionalArrowLength={4}
              linkDirectionalArrowRelPos={1}
              onNodeHover={(node) => setHoverNodeId(node ? node.id : null)}
              enableNodeDrag
              enableZoomInteraction
              enablePanInteraction
            />
          )
        )}
      </div>
    </div>
  );
}

/**
 * Resolves a link endpoint to its node id. The force-graph engine mutates a
 * link's `source`/`target` from the id string into the resolved node object at
 * runtime, so both shapes are handled.
 */
function linkEndId(end: unknown): string {
  if (typeof end === "string") return end;
  if (typeof end === "object" && end !== null && "id" in end) {
    return String((end as { id: unknown }).id);
  }
  return String(end);
}
