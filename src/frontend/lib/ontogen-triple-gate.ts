/**
 * Triple approval gating logic.
 *
 * The backend returns 422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING when a triple's
 * subject node, predicate edge, or object node is not yet `status='approved'`
 * (human-approved). The UI mirrors this check client-side to disable the
 * approve button before the request would fail.
 *
 * Gate rule: ALL THREE of (subject_node.status, edge.status, object_node.status)
 * must equal "approved" before the triple can itself be approved.
 *
 * Note: "llm_approved" is NOT sufficient — only human "approved" qualifies.
 */

import type { OntogenEdge, OntogenNode, OntogenStatus, OntogenTriple } from "@/types/ontogen";

export interface TripleGateResult {
  /** Whether the triple can be approved right now. */
  canApprove: boolean;
  /** Human-readable hint listing the blocking dependencies (empty when canApprove=true). */
  blockingHint: string;
}

/**
 * Returns whether a triple's dependencies are all `status='approved'` and
 * provides a blocking hint for the UI when they are not.
 *
 * @param triple  - The triple to check.
 * @param nodesById  - Map of node id → OntogenNode (all known nodes).
 * @param edgesById  - Map of edge id → OntogenEdge (all known edges).
 */
export function tripleApprovalGate(
  triple: OntogenTriple,
  nodesById: Map<string, { status: OntogenStatus; name: string }>,
  edgesById: Map<string, { status: OntogenStatus; label: string }>,
): TripleGateResult {
  const blocking: string[] = [];

  const subjectNode = nodesById.get(triple.subject_node_id);
  if (!subjectNode || subjectNode.status !== "approved") {
    const label = subjectNode ? subjectNode.name : triple.subject_node_id;
    blocking.push(`node "${label}" (${subjectNode?.status ?? "unknown"})`);
  }

  const edge = edgesById.get(triple.edge_id);
  if (!edge || edge.status !== "approved") {
    const label = edge ? edge.label : triple.edge_id;
    blocking.push(`edge "${label}" (${edge?.status ?? "unknown"})`);
  }

  const objectNode = nodesById.get(triple.object_node_id);
  if (!objectNode || objectNode.status !== "approved") {
    const label = objectNode ? objectNode.name : triple.object_node_id;
    blocking.push(`node "${label}" (${objectNode?.status ?? "unknown"})`);
  }

  if (blocking.length === 0) {
    return { canApprove: true, blockingHint: "" };
  }

  return {
    canApprove: false,
    blockingHint: `Blocked: approve ${blocking.join(", ")} first`,
  };
}

/**
 * Builds a Map<id, { status, name }> from a list of OntogenNode for use with
 * tripleApprovalGate.
 */
export function buildNodesById(
  nodes: OntogenNode[],
): Map<string, { status: OntogenStatus; name: string }> {
  return new Map(nodes.map((n) => [n.id, { status: n.status, name: n.name }]));
}

/**
 * Builds a Map<id, { status, label }> from a list of OntogenEdge for use with
 * tripleApprovalGate.
 */
export function buildEdgesById(
  edges: OntogenEdge[],
): Map<string, { status: OntogenStatus; label: string }> {
  return new Map(edges.map((e) => [e.id, { status: e.status, label: e.label }]));
}
