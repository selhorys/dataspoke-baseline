/**
 * Assembles force-directed graph data from ontogen result sets.
 *
 * Graph nodes are ontogen nodes; links are triples (source = subject node,
 * target = object node, labelled by the triple's edge). Node `val` is its
 * degree (count of incident links) so the renderer can size by connectivity.
 *
 * Filter modes:
 *   - "all"      → every node and triple
 *   - "approved" → only human-approved nodes and the triples among them
 *
 * Pure and renderer-agnostic so it can be unit-tested without a canvas.
 */

import type { OntogenEdge, OntogenNode, OntogenStatus, OntogenTriple } from "@/types/ontogen";

export type OntologyGraphMode = "all" | "approved";

export interface OntologyGraphNode {
  id: string;
  name: string;
  status: OntogenStatus;
  /** Degree (incident link count) — drives node size in the renderer. */
  val: number;
}

export interface OntologyGraphLink {
  source: string;
  target: string;
  label: string;
  status: OntogenStatus;
}

export interface OntologyGraphData {
  nodes: OntologyGraphNode[];
  links: OntologyGraphLink[];
}

/**
 * Maps an ontogen status to a graph color: approved → green, rejected →
 * muted red, llm_* → grey.
 */
export function ontologyGraphColor(status: OntogenStatus): string {
  switch (status) {
    case "approved":
      return "#22c55e"; // green-500
    case "rejected":
      return "#f87171"; // red-400 (muted)
    case "llm_pending":
    case "llm_approved":
    default:
      return "#9ca3af"; // gray-400
  }
}

/**
 * Builds the graph data structure from the ontogen result sets.
 *
 * @param nodes   - All ontogen nodes.
 * @param triples - All ontogen triples (each becomes a link).
 * @param edges   - All ontogen edges (used to label links by the triple's edge).
 * @param mode    - "all" or "approved" (approved-only subgraph).
 */
export function buildOntologyGraph(
  nodes: OntogenNode[],
  triples: OntogenTriple[],
  edges: OntogenEdge[],
  mode: OntologyGraphMode,
): OntologyGraphData {
  const edgeLabelById = new Map(edges.map((e) => [e.id, e.label]));

  const includedNodes =
    mode === "approved" ? nodes.filter((n) => n.status === "approved") : nodes;
  const includedNodeIds = new Set(includedNodes.map((n) => n.id));

  const includedTriples = triples.filter((t) => {
    if (!includedNodeIds.has(t.subject_node_id) || !includedNodeIds.has(t.object_node_id)) {
      return false;
    }
    if (mode === "approved") {
      return t.status === "approved";
    }
    return true;
  });

  const degree = new Map<string, number>();
  for (const triple of includedTriples) {
    degree.set(triple.subject_node_id, (degree.get(triple.subject_node_id) ?? 0) + 1);
    degree.set(triple.object_node_id, (degree.get(triple.object_node_id) ?? 0) + 1);
  }

  const graphNodes: OntologyGraphNode[] = includedNodes.map((node) => ({
    id: node.id,
    name: node.name,
    status: node.status,
    val: degree.get(node.id) ?? 0,
  }));

  const graphLinks: OntologyGraphLink[] = includedTriples.map((triple) => ({
    source: triple.subject_node_id,
    target: triple.object_node_id,
    label: edgeLabelById.get(triple.edge_id) ?? triple.edge_id,
    status: triple.status,
  }));

  return { nodes: graphNodes, links: graphLinks };
}
