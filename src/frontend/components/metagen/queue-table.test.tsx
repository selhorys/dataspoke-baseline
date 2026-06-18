/**
 * Tests for the metagen QueueTable conf_id filter.
 *
 * Spec: spec/feature/FRONTEND_METAGEN.md §Result queue — the queue is filterable
 * by dataset_urn, kind, status, and conf_id. Each row links to the owning
 * dataset page where review happens.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { QueueTable } from "./queue-table";
import type { MetagenConf, MetagenItemListResponse } from "@/types/metagen";

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

const mockQueue = vi.fn();
vi.mock("@/lib/api/metagen", () => ({
  useMetagenQueue: (params: unknown) => mockQueue(params),
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
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

const queueResponse: MetagenItemListResponse = {
  offset: 0,
  limit: 20,
  total_count: 1,
  items: [
    {
      dataset_urn: "urn:li:dataset:a",
      item_id: "dataset.description",
      kind: "dataset.description",
      field_path: null,
      status: "llm_approved",
      candidate_count: 2,
      composite_id: "urn:li:dataset:a::dataset.description",
    },
  ],
};

beforeEach(() => {
  mockQueue.mockReset();
  mockQueue.mockReturnValue({ data: queueResponse, isLoading: false });
});

describe("QueueTable", () => {
  it("renders a conf filter select populated from the confs prop", () => {
    render(<QueueTable confs={[makeConf("c1", "catalog policy")]} />);
    expect(screen.getByLabelText(/filter by conf/i)).toBeTruthy();
  });

  it("links each row's dataset_urn to the per-dataset page", () => {
    render(<QueueTable confs={[]} />);
    const link = screen.getByRole("link", { name: "urn:li:dataset:a" });
    expect(link.getAttribute("href")).toBe(
      `/metagen/data/${encodeURIComponent("urn:li:dataset:a")}`,
    );
  });

  it("renders the candidate_count column", () => {
    render(<QueueTable confs={[]} />);
    expect(screen.getByText("2")).toBeTruthy();
  });
});
