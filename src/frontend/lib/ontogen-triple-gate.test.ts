/**
 * Tests for lib/ontogen-triple-gate.ts — tripleApprovalGate, buildNodesById, buildEdgesById.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_ONTOGEN.md: "When a triple's dependencies are not yet approved
 *     the UI disables the approve button with an inline hint naming the missing dependency."
 *   - src/backend/ontogen/service.py review_triple():
 *     subj_approved = subj_row and subj_row.status == "approved"
 *     edge_approved = edge_row and edge_row.status == "approved"
 *     obj_approved  = obj_row  and obj_row.status  == "approved"
 *     if not (subj_approved and edge_approved and obj_approved):
 *         raise PreconditionFailedError("ONTOGEN_TRIPLE_DEPENDENCY_PENDING", ...)
 *   - src/api/schemas/ontogen.py OntogenStatus values:
 *     "llm_pending", "llm_approved", "approved", "rejected"
 *   - src/api/schemas/ontogen.py TripleResponse fields:
 *     subject_node_id, edge_id, object_node_id
 *
 * Gate invariant: ALL THREE of subject_node.status, edge.status, object_node.status
 * must equal "approved" (human-approved). "llm_approved" is NOT sufficient.
 * Fail-closed: missing map entry → canApprove === false.
 */

import { describe, it, expect } from "vitest";
import {
  tripleApprovalGate,
  buildNodesById,
  buildEdgesById,
} from "./ontogen-triple-gate";
import type { OntogenEdge, OntogenNode, OntogenStatus, OntogenTriple } from "@/types/ontogen";

// ---------------------------------------------------------------------------
// Minimal factory helpers — fields not relevant to gate logic are filled with
// safe defaults so tests remain readable and spec-focused.
// ---------------------------------------------------------------------------

function makeNode(id: string, status: OntogenStatus, name = id): OntogenNode {
  return {
    id,
    name,
    description: "",
    confidence_score: 0.9,
    status,
    created_at: "2025-01-01T00:00:00Z",
    updated_at: "2025-01-01T00:00:00Z",
  };
}

function makeEdge(id: string, status: OntogenStatus, label = id): OntogenEdge {
  return {
    id,
    label,
    semantics: null,
    confidence_score: 0.9,
    status,
    created_at: "2025-01-01T00:00:00Z",
    updated_at: "2025-01-01T00:00:00Z",
  };
}

function makeTriple(
  subjectNodeId: string,
  edgeId: string,
  objectNodeId: string,
): OntogenTriple {
  return {
    id: `${subjectNodeId}__${edgeId}__${objectNodeId}`,
    subject_node_id: subjectNodeId,
    edge_id: edgeId,
    object_node_id: objectNodeId,
    confidence_score: 0.8,
    status: "llm_pending",
    created_at: "2025-01-01T00:00:00Z",
    updated_at: "2025-01-01T00:00:00Z",
  };
}

// ---------------------------------------------------------------------------
// 1. Happy path — all three approved
// ---------------------------------------------------------------------------

describe("tripleApprovalGate — all three approved", () => {
  // spec/feature/FRONTEND_ONTOGEN.md: approve button enabled only when all deps are approved
  // service.py: subj+edge+obj all "approved" → no PreconditionFailedError

  it("returns canApprove=true and empty blockingHint when subject, predicate edge, and object are all approved", () => {
    const nodes = buildNodesById([
      makeNode("book", "approved", "Book"),
      makeNode("customer", "approved", "Customer"),
    ]);
    const edges = buildEdgesById([makeEdge("placed_by", "approved", "placed_by")]);
    const triple = makeTriple("book", "placed_by", "customer");

    const result = tripleApprovalGate(triple, nodes, edges);

    expect(result.canApprove).toBe(true);
    expect(result.blockingHint).toBe("");
  });
});

// ---------------------------------------------------------------------------
// 2. Each single dependency not approved — exhaustive status table
// ---------------------------------------------------------------------------

describe("tripleApprovalGate — subject node not approved", () => {
  // Backend invariant: subj_row.status == "approved" is required;
  // any other status (including llm_approved) triggers ONTOGEN_TRIPLE_DEPENDENCY_PENDING

  const nonApprovedStatuses: OntogenStatus[] = ["llm_pending", "llm_approved", "rejected"];

  nonApprovedStatuses.forEach((status) => {
    it(`subject node with status="${status}" blocks approval`, () => {
      const nodes = buildNodesById([
        makeNode("book", status, "Book"),
        makeNode("customer", "approved", "Customer"),
      ]);
      const edges = buildEdgesById([makeEdge("placed_by", "approved", "placed_by")]);
      const triple = makeTriple("book", "placed_by", "customer");

      const result = tripleApprovalGate(triple, nodes, edges);

      expect(result.canApprove).toBe(false);
      expect(result.blockingHint).not.toBe("");
      // Hint must reference the blocking subject node
      expect(result.blockingHint).toContain("Book");
    });
  });
});

describe("tripleApprovalGate — object node not approved", () => {
  const nonApprovedStatuses: OntogenStatus[] = ["llm_pending", "llm_approved", "rejected"];

  nonApprovedStatuses.forEach((status) => {
    it(`object node with status="${status}" blocks approval`, () => {
      const nodes = buildNodesById([
        makeNode("book", "approved", "Book"),
        makeNode("customer", status, "Customer"),
      ]);
      const edges = buildEdgesById([makeEdge("placed_by", "approved", "placed_by")]);
      const triple = makeTriple("book", "placed_by", "customer");

      const result = tripleApprovalGate(triple, nodes, edges);

      expect(result.canApprove).toBe(false);
      expect(result.blockingHint).not.toBe("");
      expect(result.blockingHint).toContain("Customer");
    });
  });
});

describe("tripleApprovalGate — predicate edge not approved", () => {
  const nonApprovedStatuses: OntogenStatus[] = ["llm_pending", "llm_approved", "rejected"];

  nonApprovedStatuses.forEach((status) => {
    it(`edge with status="${status}" blocks approval`, () => {
      const nodes = buildNodesById([
        makeNode("book", "approved", "Book"),
        makeNode("customer", "approved", "Customer"),
      ]);
      const edges = buildEdgesById([makeEdge("placed_by", status, "placed_by")]);
      const triple = makeTriple("book", "placed_by", "customer");

      const result = tripleApprovalGate(triple, nodes, edges);

      expect(result.canApprove).toBe(false);
      expect(result.blockingHint).not.toBe("");
      expect(result.blockingHint).toContain("placed_by");
    });
  });
});

// ---------------------------------------------------------------------------
// 3. The subtle invariant: llm_approved is NOT sufficient
// ---------------------------------------------------------------------------

describe("tripleApprovalGate — llm_approved is NOT sufficient (critical backend invariant)", () => {
  // service.py: subj_approved = subj_row and subj_row.status == "approved"  (strict equality)
  // "llm_approved" is the LLM reviewer's acceptance, NOT human approval.
  // The frontend must mirror this: a triple whose deps are all "llm_approved" must be blocked.

  it("triple with all three deps at llm_approved is blocked (not human-approved)", () => {
    const nodes = buildNodesById([
      makeNode("order_line", "llm_approved", "Order Line"),
      makeNode("book", "llm_approved", "Book"),
    ]);
    const edges = buildEdgesById([makeEdge("references", "llm_approved", "references")]);
    const triple = makeTriple("order_line", "references", "book");

    const result = tripleApprovalGate(triple, nodes, edges);

    expect(result.canApprove).toBe(false);
    expect(result.blockingHint).not.toBe("");
  });

  it("triple with one dep approved and two at llm_approved is blocked", () => {
    const nodes = buildNodesById([
      makeNode("order_line", "approved", "Order Line"),
      makeNode("book", "llm_approved", "Book"),
    ]);
    const edges = buildEdgesById([makeEdge("references", "llm_approved", "references")]);
    const triple = makeTriple("order_line", "references", "book");

    const result = tripleApprovalGate(triple, nodes, edges);

    expect(result.canApprove).toBe(false);
  });

  it("triple transitions to canApprove=true only when all move from llm_approved to approved", () => {
    const triple = makeTriple("order_line", "references", "book");

    // Before human review — all llm_approved
    const beforeNodes = buildNodesById([
      makeNode("order_line", "llm_approved", "Order Line"),
      makeNode("book", "llm_approved", "Book"),
    ]);
    const beforeEdges = buildEdgesById([makeEdge("references", "llm_approved", "references")]);
    expect(tripleApprovalGate(triple, beforeNodes, beforeEdges).canApprove).toBe(false);

    // After human review of all deps
    const afterNodes = buildNodesById([
      makeNode("order_line", "approved", "Order Line"),
      makeNode("book", "approved", "Book"),
    ]);
    const afterEdges = buildEdgesById([makeEdge("references", "approved", "references")]);
    expect(tripleApprovalGate(triple, afterNodes, afterEdges).canApprove).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 4. Fail-closed on missing map entries
// ---------------------------------------------------------------------------

describe("tripleApprovalGate — fail-closed when deps are missing from maps", () => {
  // Spec invariant: a triple referencing an unknown node/edge → canApprove=false,
  // does NOT throw, does NOT fail open.

  it("missing subject node → canApprove=false (does not throw)", () => {
    const nodes = buildNodesById([
      // "book" absent
      makeNode("customer", "approved", "Customer"),
    ]);
    const edges = buildEdgesById([makeEdge("placed_by", "approved", "placed_by")]);
    const triple = makeTriple("book", "placed_by", "customer");

    const result = tripleApprovalGate(triple, nodes, edges);

    expect(result.canApprove).toBe(false);
    expect(result.blockingHint).not.toBe("");
    // Hint falls back to the raw ID when the node isn't in the map
    expect(result.blockingHint).toContain("book");
  });

  it("missing object node → canApprove=false (does not throw)", () => {
    const nodes = buildNodesById([
      makeNode("book", "approved", "Book"),
      // "customer" absent
    ]);
    const edges = buildEdgesById([makeEdge("placed_by", "approved", "placed_by")]);
    const triple = makeTriple("book", "placed_by", "customer");

    const result = tripleApprovalGate(triple, nodes, edges);

    expect(result.canApprove).toBe(false);
    expect(result.blockingHint).toContain("customer");
  });

  it("missing predicate edge → canApprove=false (does not throw)", () => {
    const nodes = buildNodesById([
      makeNode("book", "approved", "Book"),
      makeNode("customer", "approved", "Customer"),
    ]);
    const edges = buildEdgesById([
      // "placed_by" absent
    ]);
    const triple = makeTriple("book", "placed_by", "customer");

    const result = tripleApprovalGate(triple, nodes, edges);

    expect(result.canApprove).toBe(false);
    expect(result.blockingHint).toContain("placed_by");
  });

  it("all three deps missing → canApprove=false, blockingHint mentions all three ids", () => {
    const nodes = buildNodesById([]);
    const edges = buildEdgesById([]);
    const triple = makeTriple("book", "placed_by", "customer");

    const result = tripleApprovalGate(triple, nodes, edges);

    expect(result.canApprove).toBe(false);
    expect(result.blockingHint).toContain("book");
    expect(result.blockingHint).toContain("placed_by");
    expect(result.blockingHint).toContain("customer");
  });
});

// ---------------------------------------------------------------------------
// 5. blockingHint names the blocking dependency
// ---------------------------------------------------------------------------

describe("tripleApprovalGate — blockingHint content", () => {
  // spec/feature/FRONTEND_ONTOGEN.md: "UI disables the approve button with an inline hint
  // naming the missing dependency"

  it("hint includes the node name (not just id) when node is in the map", () => {
    const nodes = buildNodesById([
      makeNode("order_line", "llm_pending", "Order Line"),
      makeNode("book", "approved", "Book"),
    ]);
    const edges = buildEdgesById([makeEdge("references", "approved", "references")]);
    const triple = makeTriple("order_line", "references", "book");

    const result = tripleApprovalGate(triple, nodes, edges);

    expect(result.canApprove).toBe(false);
    expect(result.blockingHint).toContain("Order Line");
  });

  it("hint is empty when canApprove is true", () => {
    const nodes = buildNodesById([
      makeNode("book", "approved", "Book"),
      makeNode("customer", "approved", "Customer"),
    ]);
    const edges = buildEdgesById([makeEdge("placed_by", "approved", "placed_by")]);
    const triple = makeTriple("book", "placed_by", "customer");

    const result = tripleApprovalGate(triple, nodes, edges);

    expect(result.canApprove).toBe(true);
    expect(result.blockingHint).toBe("");
  });

  it("hint mentions multiple blocking items when more than one dep is not approved", () => {
    const nodes = buildNodesById([
      makeNode("order_line", "llm_pending", "Order Line"),
      makeNode("book", "llm_approved", "Book"),
    ]);
    const edges = buildEdgesById([makeEdge("references", "approved", "references")]);
    const triple = makeTriple("order_line", "references", "book");

    const result = tripleApprovalGate(triple, nodes, edges);

    expect(result.canApprove).toBe(false);
    // Both blocking items must appear in the hint
    expect(result.blockingHint).toContain("Order Line");
    expect(result.blockingHint).toContain("Book");
  });
});

// ---------------------------------------------------------------------------
// 6. buildNodesById
// ---------------------------------------------------------------------------

describe("buildNodesById", () => {
  // Map keyed by node.id with value shape { status, name }

  it("returns an empty Map for an empty array", () => {
    const result = buildNodesById([]);
    expect(result.size).toBe(0);
  });

  it("maps each node id to its status and name", () => {
    const nodes = [
      makeNode("book", "approved", "Book"),
      makeNode("order_line", "llm_pending", "Order Line"),
    ];
    const result = buildNodesById(nodes);

    expect(result.size).toBe(2);
    expect(result.get("book")).toEqual({ status: "approved", name: "Book" });
    expect(result.get("order_line")).toEqual({ status: "llm_pending", name: "Order Line" });
  });

  it("returns a Map (not a plain object)", () => {
    const result = buildNodesById([makeNode("x", "approved", "X")]);
    expect(result).toBeInstanceOf(Map);
  });

  it("last-write-wins on duplicate ids (sane deduplication)", () => {
    const nodes = [
      makeNode("book", "llm_pending", "Book Draft"),
      makeNode("book", "approved", "Book Final"),
    ];
    const result = buildNodesById(nodes);

    // Should have exactly one entry for "book"
    expect(result.size).toBe(1);
    // Last entry wins (Array.map → Map constructor: last pair for duplicate key wins)
    expect(result.get("book")?.status).toBe("approved");
    expect(result.get("book")?.name).toBe("Book Final");
  });
});

// ---------------------------------------------------------------------------
// 7. buildEdgesById
// ---------------------------------------------------------------------------

describe("buildEdgesById", () => {
  it("returns an empty Map for an empty array", () => {
    const result = buildEdgesById([]);
    expect(result.size).toBe(0);
  });

  it("maps each edge id to its status and label", () => {
    const edges = [
      makeEdge("placed_by", "approved", "placed by"),
      makeEdge("references", "llm_pending", "references"),
    ];
    const result = buildEdgesById(edges);

    expect(result.size).toBe(2);
    expect(result.get("placed_by")).toEqual({ status: "approved", label: "placed by" });
    expect(result.get("references")).toEqual({ status: "llm_pending", label: "references" });
  });

  it("returns a Map (not a plain object)", () => {
    const result = buildEdgesById([makeEdge("e", "approved", "E")]);
    expect(result).toBeInstanceOf(Map);
  });

  it("last-write-wins on duplicate ids", () => {
    const edges = [
      makeEdge("references", "llm_pending", "ref v1"),
      makeEdge("references", "approved", "ref v2"),
    ];
    const result = buildEdgesById(edges);

    expect(result.size).toBe(1);
    expect(result.get("references")?.status).toBe("approved");
  });
});

// ---------------------------------------------------------------------------
// 8. Cross-position exhaustive status table
// ---------------------------------------------------------------------------

describe("tripleApprovalGate — full OntogenStatus cross-position table", () => {
  // This table drives all four status values in each of the three dependency
  // positions (subject node / predicate edge / object node).
  // Only the (approved, approved, approved) triple returns canApprove=true.

  type Row = {
    subjectStatus: OntogenStatus;
    edgeStatus: OntogenStatus;
    objectStatus: OntogenStatus;
    expectedCanApprove: boolean;
  };

  const table: Row[] = [
    // All three approved — only green case
    { subjectStatus: "approved",    edgeStatus: "approved",    objectStatus: "approved",    expectedCanApprove: true  },
    // Each position individually non-approved (others approved)
    { subjectStatus: "llm_pending", edgeStatus: "approved",    objectStatus: "approved",    expectedCanApprove: false },
    { subjectStatus: "llm_approved",edgeStatus: "approved",    objectStatus: "approved",    expectedCanApprove: false },
    { subjectStatus: "rejected",    edgeStatus: "approved",    objectStatus: "approved",    expectedCanApprove: false },
    { subjectStatus: "approved",    edgeStatus: "llm_pending", objectStatus: "approved",    expectedCanApprove: false },
    { subjectStatus: "approved",    edgeStatus: "llm_approved",objectStatus: "approved",    expectedCanApprove: false },
    { subjectStatus: "approved",    edgeStatus: "rejected",    objectStatus: "approved",    expectedCanApprove: false },
    { subjectStatus: "approved",    edgeStatus: "approved",    objectStatus: "llm_pending", expectedCanApprove: false },
    { subjectStatus: "approved",    edgeStatus: "approved",    objectStatus: "llm_approved",expectedCanApprove: false },
    { subjectStatus: "approved",    edgeStatus: "approved",    objectStatus: "rejected",    expectedCanApprove: false },
    // All three at the same non-approved status
    { subjectStatus: "llm_pending", edgeStatus: "llm_pending", objectStatus: "llm_pending", expectedCanApprove: false },
    { subjectStatus: "llm_approved",edgeStatus: "llm_approved",objectStatus: "llm_approved",expectedCanApprove: false },
    { subjectStatus: "rejected",    edgeStatus: "rejected",    objectStatus: "rejected",    expectedCanApprove: false },
  ];

  table.forEach(({ subjectStatus, edgeStatus, objectStatus, expectedCanApprove }) => {
    it(`(subject=${subjectStatus}, edge=${edgeStatus}, object=${objectStatus}) → canApprove=${expectedCanApprove}`, () => {
      const nodes = buildNodesById([
        makeNode("subj", subjectStatus, "Subject"),
        makeNode("obj", objectStatus, "Object"),
      ]);
      const edges = buildEdgesById([makeEdge("pred", edgeStatus, "predicate")]);
      const triple = makeTriple("subj", "pred", "obj");

      const result = tripleApprovalGate(triple, nodes, edges);
      expect(result.canApprove).toBe(expectedCanApprove);
    });
  });
});
