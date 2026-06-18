import { describe, it, expect } from "vitest";
import { summarizeDatasetFilter } from "./metagen-filter-summary";

describe("summarizeDatasetFilter", () => {
  it("returns 'all datasets' for an empty filter", () => {
    expect(summarizeDatasetFilter({})).toBe("all datasets");
  });

  it("returns 'all datasets' for null/undefined", () => {
    expect(summarizeDatasetFilter(null)).toBe("all datasets");
    expect(summarizeDatasetFilter(undefined)).toBe("all datasets");
  });

  it("summarizes origin", () => {
    expect(summarizeDatasetFilter({ origin: "PROD" })).toBe("origin=PROD");
  });

  it("ignores blank/whitespace origin", () => {
    expect(summarizeDatasetFilter({ origin: "   " })).toBe("all datasets");
  });

  it("pluralizes each list dimension", () => {
    expect(summarizeDatasetFilter({ tags: ["pii"] })).toBe("1 tag");
    expect(summarizeDatasetFilter({ tags: ["pii", "gdpr"] })).toBe("2 tags");
    expect(summarizeDatasetFilter({ glossary_terms: ["t"] })).toBe("1 term");
    expect(summarizeDatasetFilter({ dataset_urns: ["a", "b"] })).toBe("2 URNs");
  });

  it("joins multiple dimensions in order", () => {
    expect(
      summarizeDatasetFilter({
        origin: "PROD",
        tags: ["pii"],
        glossary_terms: ["x", "y"],
        dataset_urns: ["a"],
      }),
    ).toBe("origin=PROD, 1 tag, 2 terms, 1 URN");
  });
});
