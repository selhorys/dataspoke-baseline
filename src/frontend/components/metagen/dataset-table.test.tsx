/**
 * Tests for the metagen MetagenDatasetTable (per-dataset result rollup).
 *
 * Spec: spec/feature/FRONTEND_METAGEN.md §Result rollup — one row per dataset
 * with candidate-level counts and the boundary allowed labels; filters are a
 * dataset_urn text input + a conf_id select only (no kind / status).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import React from "react";
import { MetagenDatasetTable } from "./dataset-table";
import type { MetagenConf, MetagenDatasetListResponse } from "@/types/metagen";

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

const mockDatasets = vi.fn();
vi.mock("@/lib/api/metagen", () => ({
  useMetagenDatasets: (params: unknown) => mockDatasets(params),
}));

function makeConf(id: string, name: string): MetagenConf {
  return {
    id,
    name,
    is_enabled: true,
    schedule_tier: null,
    dataset_filter: {},
    result_limit: 3,
    overwrite_pending: true,
    dataset_affected_count: 0,
    last_run_at: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

const response: MetagenDatasetListResponse = {
  offset: 0,
  limit: 20,
  total_count: 1,
  datasets: [
    {
      dataset_urn: "urn:li:dataset:a",
      is_enabled: true,
      allowed: ["dataset.description", "column.description"],
      item_count: 4,
      approved_count: 2,
      rejected_count: 1,
      candidate_count: 7,
      last_modified_at: "2026-05-01T12:00:00Z",
    },
  ],
};

beforeEach(() => {
  mockDatasets.mockReset();
  mockDatasets.mockReturnValue({ data: response, isLoading: false });
});

describe("MetagenDatasetTable", () => {
  it("renders a dataset_urn text filter and a conf filter select, with no kind/status filters", () => {
    render(<MetagenDatasetTable confs={[makeConf("c1", "catalog policy")]} />);
    expect(screen.getByPlaceholderText(/filter by dataset urn/i)).toBeTruthy();
    expect(screen.getByLabelText(/filter by conf/i)).toBeTruthy();
    expect(screen.queryByLabelText(/filter by kind/i)).toBeNull();
    expect(screen.queryByLabelText(/filter by status/i)).toBeNull();
  });

  it("links each row's dataset_urn to the per-dataset page", () => {
    render(<MetagenDatasetTable confs={[]} />);
    const link = screen.getByRole("link", { name: "urn:li:dataset:a" });
    expect(link.getAttribute("href")).toBe(
      `/data/${encodeURIComponent("urn:li:dataset:a")}`,
    );
  });

  it("renders the candidate-level count columns", () => {
    render(<MetagenDatasetTable confs={[]} />);
    // Scope to the data row (the link's <tr>) so pagination "1" doesn't collide.
    const row = screen.getByRole("link", { name: "urn:li:dataset:a" }).closest("tr");
    expect(row).toBeTruthy();
    const cells = within(row as HTMLElement).getAllByRole("cell");
    // dataset/boundary, items, approved, rejected, candidates, last modified at
    expect(cells[1].textContent).toBe("4");
    expect(cells[2].textContent).toBe("2");
    expect(cells[3].textContent).toBe("1");
    expect(cells[4].textContent).toBe("7");
  });

  it("renders the boundary allowed labels as badges", () => {
    render(<MetagenDatasetTable confs={[]} />);
    expect(screen.getByText("dataset.description")).toBeTruthy();
    expect(screen.getByText("column.description")).toBeTruthy();
  });

  it("shows 'none' when a dataset has no boundary allowed kinds", () => {
    mockDatasets.mockReturnValue({
      data: {
        ...response,
        datasets: [
          {
            ...response.datasets[0],
            is_enabled: false,
            allowed: [],
            last_modified_at: null,
          },
        ],
      },
      isLoading: false,
    });
    render(<MetagenDatasetTable confs={[]} />);
    expect(screen.getByText("none")).toBeTruthy();
  });
});
