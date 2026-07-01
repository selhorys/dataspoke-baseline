/**
 * Tests for MetagenCoveredTable — the per-conf covered-datasets list.
 *
 * Spec: spec/feature/FRONTEND_METAGEN.md §Conf detail (Covered datasets table)
 * and §Components (MetagenCoveredTable).
 *   - Each row links its dataset_urn to /data/[urn].
 *   - A boundary summary (is_enabled badge + allowed summary) is rendered.
 *   - The "Show boundary-blocked" toggle drives includeDisallowed; when on, a
 *     reason column surfaces each blocked row's reason.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import React from "react";
import { MetagenCoveredTable } from "./covered-table";
import type { MetagenCoveredDatasetSummary } from "@/types/metagen";

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

const writable: MetagenCoveredDatasetSummary = {
  dataset_urn: "urn:li:dataset:writable",
  is_enabled: true,
  allowed: ["dataset.description"],
  blocked: false,
  reason: null,
};

const blocked: MetagenCoveredDatasetSummary = {
  dataset_urn: "urn:li:dataset:blocked",
  is_enabled: false,
  allowed: [],
  blocked: true,
  reason: "boundary_disabled",
};

const noop = () => {};

function renderTable(
  rows: MetagenCoveredDatasetSummary[],
  includeDisallowed: boolean,
  onIncludeDisallowedChange = vi.fn(),
) {
  return render(
    <MetagenCoveredTable
      rows={rows}
      isLoading={false}
      error={null}
      includeDisallowed={includeDisallowed}
      onIncludeDisallowedChange={onIncludeDisallowedChange}
      page={{ offset: 0, limit: 20, total: rows.length }}
      onOffset={noop}
      onLimit={noop}
    />,
  );
}

beforeEach(() => {
  if (!globalThis.ResizeObserver) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
  }
});

describe("MetagenCoveredTable", () => {
  it("links each dataset_urn to its per-dataset page", () => {
    renderTable([writable], false);
    const link = screen.getByRole("link", { name: writable.dataset_urn });
    expect(link.getAttribute("href")).toBe(
      `/data/${encodeURIComponent(writable.dataset_urn)}`,
    );
  });

  it("renders the boundary summary (enabled badge + allowed kinds)", () => {
    renderTable([writable], false);
    expect(screen.getByText("enabled")).toBeTruthy();
    expect(screen.getByText("dataset.description")).toBeTruthy();
  });

  it("hides the reason column when include_disallowed is off", () => {
    renderTable([writable], false);
    expect(screen.queryByRole("columnheader", { name: "reason" })).toBeNull();
  });

  it("shows the reason column and blocked reason when include_disallowed is on", () => {
    renderTable([writable, blocked], true);
    expect(screen.getByRole("columnheader", { name: "reason" })).toBeTruthy();
    expect(screen.getByText("boundary_disabled")).toBeTruthy();
  });

  it("toggling the checkbox calls onIncludeDisallowedChange", () => {
    const onChange = vi.fn();
    renderTable([writable], false, onChange);
    fireEvent.click(screen.getByLabelText(/show boundary-blocked/i));
    expect(onChange).toHaveBeenCalledWith(true);
  });

  it("renders an empty state when no datasets are covered", () => {
    renderTable([], false);
    expect(screen.getByText(/no covered datasets/i)).toBeTruthy();
  });
});
