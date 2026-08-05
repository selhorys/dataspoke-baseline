/**
 * Tests for DatasetFilterView — read-only render of the four dataset_filter
 * dimensions; empty dimensions show an em dash.
 *
 * Spec: spec/feature/FRONTEND_BASIC.md §Shared component notes → DatasetFilterView:
 * "List entries render monospaced with internal whitespace preserved, so a URN's
 * own spacing reads back as stored."
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { DatasetFilterView } from "./dataset-filter-view";

describe("DatasetFilterView", () => {
  it("renders each dimension's values", () => {
    render(
      <DatasetFilterView
        value={{
          origin: "DEV",
          tags: ["urn:li:tag:area:catalog"],
          glossary_terms: ["urn:li:glossaryTerm:Finance.Revenue"],
          dataset_urns: ["urn:li:dataset:(x)"],
        }}
      />,
    );
    expect(screen.getByText("DEV")).toBeInTheDocument();
    expect(screen.getByText("urn:li:tag:area:catalog")).toBeInTheDocument();
    expect(screen.getByText("urn:li:glossaryTerm:Finance.Revenue")).toBeInTheDocument();
    expect(screen.getByText("urn:li:dataset:(x)")).toBeInTheDocument();
  });

  it("shows an em dash for each empty dimension", () => {
    render(<DatasetFilterView value={{}} />);
    expect(screen.getAllByText("—")).toHaveLength(4);
  });

  it("renders a list entry monospaced with its whitespace preserved", () => {
    // A tag URN may carry a user-authored name with internal spacing (and a comma),
    // which must read back exactly as entered. jsdom loads no stylesheet, so the
    // rendered collapsing is not observable here — assert the exact text node plus
    // the declared whitespace/monospace handling. `whitespace-pre` and
    // `whitespace-pre-wrap` both satisfy the spec; `pre-line` (collapses spaces)
    // does not. The residual visual gap is tracked in tests/e2e/COVERAGE.md.
    render(<DatasetFilterView value={{ tags: ["urn:li:tag:two  words,catalog"] }} />);
    const entry = screen.getByText(
      (_, node) =>
        node?.tagName === "LI" && node.textContent === "urn:li:tag:two  words,catalog",
    );
    expect(entry).toBeInTheDocument();
    const preservesWhitespace = ["whitespace-pre", "whitespace-pre-wrap"].some((c) =>
      entry.classList.contains(c),
    );
    expect(preservesWhitespace).toBe(true);
    // Monospace is declared on the list that owns the entries.
    const list = entry.closest("ul");
    expect(list).not.toBeNull();
    expect(list!.classList.contains("font-mono")).toBe(true);
  });
});
