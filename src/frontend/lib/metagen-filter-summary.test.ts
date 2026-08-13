import { describe, it, expect } from "vitest";
import { FILTER_SUMMARY_MAX_CHARS, summarizeDatasetFilter } from "./metagen-filter-summary";

describe("summarizeDatasetFilter", () => {
  it("returns 'all datasets' for an empty clause", () => {
    expect(summarizeDatasetFilter("")).toBe("all datasets");
    expect(summarizeDatasetFilter("   \n  ")).toBe("all datasets");
  });

  it("returns 'all datasets' for null/undefined", () => {
    expect(summarizeDatasetFilter(null)).toBe("all datasets");
    expect(summarizeDatasetFilter(undefined)).toBe("all datasets");
  });

  it("renders a short clause verbatim", () => {
    expect(summarizeDatasetFilter("origin = 'PROD'")).toBe("origin = 'PROD'");
  });

  it("collapses line breaks and indentation to single spaces", () => {
    expect(
      summarizeDatasetFilter("origin = 'PROD'\n    AND 'urn:li:tag:pii' IN tag_urns"),
    ).toBe("origin = 'PROD' AND 'urn:li:tag:pii' IN tag_urns");
  });

  it("truncates a long clause with an ellipsis", () => {
    const clause = `origin = '${"P".repeat(200)}'`;
    const summary = summarizeDatasetFilter(clause);
    expect(summary.endsWith("…")).toBe(true);
    expect(summary.length).toBeLessThanOrEqual(FILTER_SUMMARY_MAX_CHARS + 1);
  });
});
