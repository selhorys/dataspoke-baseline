import { describe, expect, it } from "vitest";
import { reviewActionsForStatus } from "./ontogen-review-actions";

describe("reviewActionsForStatus", () => {
  it("offers Approve and Reject for llm_pending", () => {
    expect(reviewActionsForStatus("llm_pending")).toEqual(["approve", "reject"]);
  });

  it("offers Approve and Reject for llm_approved", () => {
    expect(reviewActionsForStatus("llm_approved")).toEqual(["approve", "reject"]);
  });

  it("offers only Reject (revoke) for an approved row", () => {
    expect(reviewActionsForStatus("approved")).toEqual(["reject"]);
  });

  it("offers only Approve (re-approve) for a rejected row", () => {
    expect(reviewActionsForStatus("rejected")).toEqual(["approve"]);
  });
});
