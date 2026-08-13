/**
 * Tests for DatasetFilterView — read-only render of the `dataset_filter` SQL
 * clause; an empty filter shows an em dash.
 *
 * Spec: spec/feature/FRONTEND_BASIC.md §Shared component notes → DatasetFilterView:
 * "a monospace <pre> block preserving the stored line breaks and indentation, an
 * em dash when the filter is empty".
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { DatasetFilterView } from "./dataset-filter-view";

const CLAUSE = "origin = 'PROD'\n    AND 'urn:li:tag:area:catalog' IN tag_urns";

describe("DatasetFilterView", () => {
  it("renders the clause in a monospace pre block, line breaks preserved", () => {
    const { container } = render(<DatasetFilterView value={CLAUSE} />);
    const pre = container.querySelector("pre");
    expect(pre).not.toBeNull();
    expect(pre!.textContent).toBe(CLAUSE);
    expect(pre!.classList.contains("font-mono")).toBe(true);
    // jsdom loads no stylesheet, so assert the declared whitespace handling.
    const preservesWhitespace = ["whitespace-pre", "whitespace-pre-wrap"].some((c) =>
      pre!.classList.contains(c),
    );
    expect(preservesWhitespace).toBe(true);
  });

  it("shows an em dash for an empty filter", () => {
    const { container } = render(<DatasetFilterView value="" />);
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(container.querySelector("pre")).toBeNull();
  });

  it("treats a whitespace-only filter as empty", () => {
    render(<DatasetFilterView value={"   \n  "} />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });
});
