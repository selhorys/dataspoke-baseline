import { describe, expect, it } from "vitest";
import { filterByApproval } from "./ontogen-filter";
import type { OntogenStatus } from "@/types/ontogen";

function item(id: string, status: OntogenStatus) {
  return { id, status };
}

const items = [
  item("a", "approved"),
  item("b", "llm_pending"),
  item("c", "llm_approved"),
  item("d", "rejected"),
  item("e", "approved"),
];

describe("filterByApproval", () => {
  it("returns all items unchanged for mode=all", () => {
    expect(filterByApproval(items, "all")).toEqual(items);
  });

  it("keeps only approved items for mode=approved", () => {
    expect(filterByApproval(items, "approved").map((i) => i.id)).toEqual(["a", "e"]);
  });

  it("keeps every non-approved status for mode=unapproved", () => {
    expect(filterByApproval(items, "unapproved").map((i) => i.id)).toEqual(["b", "c", "d"]);
  });

  it("treats llm_approved as unapproved (only human approved counts)", () => {
    expect(filterByApproval([item("x", "llm_approved")], "approved")).toEqual([]);
    expect(filterByApproval([item("x", "llm_approved")], "unapproved").map((i) => i.id)).toEqual([
      "x",
    ]);
  });

  it("handles an empty list", () => {
    expect(filterByApproval([], "approved")).toEqual([]);
  });
});
