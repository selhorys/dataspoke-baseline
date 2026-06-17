import { describe, expect, it } from "vitest";
import { buildOntologyGraph, ontologyGraphColor } from "./ontogen-graph";
import type { OntogenEdge, OntogenNode, OntogenStatus, OntogenTriple } from "@/types/ontogen";

function node(id: string, name: string, status: OntogenStatus): OntogenNode {
  return {
    id,
    name,
    description: "",
    confidence_score: 0.9,
    status,
    created_at: "",
    updated_at: "",
  };
}

function edge(id: string, label: string, status: OntogenStatus): OntogenEdge {
  return {
    id,
    label,
    semantics: null,
    confidence_score: 0.9,
    status,
    created_at: "",
    updated_at: "",
  };
}

function triple(
  id: string,
  subject_node_id: string,
  edge_id: string,
  object_node_id: string,
  status: OntogenStatus,
): OntogenTriple {
  return {
    id,
    subject_node_id,
    edge_id,
    object_node_id,
    confidence_score: 0.9,
    status,
    created_at: "",
    updated_at: "",
  };
}

const nodes = [
  node("n1", "BOOK", "approved"),
  node("n2", "ORDER_LINE", "llm_pending"),
  node("n3", "AUTHOR", "approved"),
];
const edges = [edge("e1", "references", "approved"), edge("e2", "written_by", "approved")];
const triples = [
  triple("t1", "n2", "e1", "n1", "llm_pending"),
  triple("t2", "n1", "e2", "n3", "approved"),
];

describe("buildOntologyGraph", () => {
  it("maps ontogen nodes to graph nodes with id/name/status", () => {
    const { nodes: gn } = buildOntologyGraph(nodes, triples, edges, "all");
    expect(gn).toHaveLength(3);
    expect(gn.find((n) => n.id === "n1")).toMatchObject({ name: "BOOK", status: "approved" });
  });

  it("maps triples to links labelled by their edge", () => {
    const { links } = buildOntologyGraph(nodes, triples, edges, "all");
    expect(links).toHaveLength(2);
    expect(links[0]).toMatchObject({
      source: "n2",
      target: "n1",
      label: "references",
      status: "llm_pending",
    });
  });

  it("computes node val as incident-link degree", () => {
    const { nodes: gn } = buildOntologyGraph(nodes, triples, edges, "all");
    // n1 is in both triples (object of t1, subject of t2) → degree 2.
    expect(gn.find((n) => n.id === "n1")?.val).toBe(2);
    expect(gn.find((n) => n.id === "n2")?.val).toBe(1);
    expect(gn.find((n) => n.id === "n3")?.val).toBe(1);
  });

  it("approved mode keeps only approved nodes and triples among them", () => {
    const { nodes: gn, links } = buildOntologyGraph(nodes, triples, edges, "approved");
    expect(gn.map((n) => n.id).sort()).toEqual(["n1", "n3"]);
    // t1 drops (status pending + endpoint n2 not approved); t2 stays.
    expect(links).toHaveLength(1);
    expect(links[0]).toMatchObject({ source: "n1", target: "n3", status: "approved" });
  });

  it("drops links whose endpoints are not both included", () => {
    const orphan = [triple("t3", "n1", "e1", "missing", "approved")];
    const { links } = buildOntologyGraph(nodes, orphan, edges, "all");
    expect(links).toHaveLength(0);
  });

  it("falls back to edge id when the edge is unknown", () => {
    const { links } = buildOntologyGraph(
      nodes,
      [triple("t4", "n1", "unknown-edge", "n3", "approved")],
      edges,
      "all",
    );
    expect(links[0].label).toBe("unknown-edge");
  });
});

describe("ontologyGraphColor", () => {
  it("colors approved green, rejected red, llm_* grey", () => {
    expect(ontologyGraphColor("approved")).toBe("#22c55e");
    expect(ontologyGraphColor("rejected")).toBe("#f87171");
    expect(ontologyGraphColor("llm_pending")).toBe("#9ca3af");
    expect(ontologyGraphColor("llm_approved")).toBe("#9ca3af");
  });
});
