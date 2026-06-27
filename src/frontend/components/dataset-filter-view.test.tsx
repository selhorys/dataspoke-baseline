/**
 * Tests for DatasetFilterView — read-only render of the four dataset_filter
 * dimensions; empty dimensions show an em dash.
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
});
