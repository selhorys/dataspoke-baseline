/**
 * Tests for the metagen conf list page.
 *
 * Spec: spec/feature/FRONTEND_METAGEN.md §Conf list.
 *   - One row per conf with name, is_enabled, schedule_tier, dataset_filter
 *     summary, result_limit.
 *   - Editor sees a "Create conf" button and per-row Run; Reader sees neither.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import MetagenConfListPage from "./page";
import type { MetagenConf } from "@/types/metagen";

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

const mockUseMe = vi.fn();
vi.mock("@/lib/auth/use-me", () => ({ useMe: () => mockUseMe() }));

const mockConfList = vi.fn();
vi.mock("@/lib/api/metagen", () => ({
  useMetagenConfList: () => mockConfList(),
  useRunMetagenConf: () => ({ mutate: vi.fn(), isPending: false, error: null }),
}));

vi.mock("@/components/ui/use-toast", () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

function makeConf(overrides: Partial<MetagenConf> = {}): MetagenConf {
  return {
    id: "conf-1",
    name: "catalog policy",
    is_enabled: true,
    schedule_tier: "daily",
    dataset_filter: { tags: ["pii"] },
    result_limit: 5,
    overwrite_pending: true,
    dataset_affected_count: 0,
    last_run_at: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-02T00:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  mockUseMe.mockReset();
  mockConfList.mockReset();
  mockConfList.mockReturnValue({
    data: { confs: [makeConf()], total_count: 1 },
    isLoading: false,
    error: null,
  });
});

describe("metagen conf list page", () => {
  it("renders a row per conf with name and key fields", () => {
    mockUseMe.mockReturnValue({ canWrite: false });
    render(<MetagenConfListPage />);

    expect(screen.getByRole("link", { name: "catalog policy" })).toBeTruthy();
    expect(screen.getByText("enabled")).toBeTruthy();
    expect(screen.getByText("daily")).toBeTruthy();
    expect(screen.getByText("1 tag")).toBeTruthy();
  });

  it("Editor sees Create conf and a per-row Run action", () => {
    mockUseMe.mockReturnValue({ canWrite: true });
    render(<MetagenConfListPage />);

    const createLink = screen.getByRole("link", { name: /create conf/i });
    expect(createLink.getAttribute("href")).toBe("/metagen/conf/new");
    expect(screen.getByRole("button", { name: /run conf catalog policy/i })).toBeTruthy();
  });

  it("Reader sees neither Create conf nor Run", () => {
    mockUseMe.mockReturnValue({ canWrite: false });
    render(<MetagenConfListPage />);

    expect(screen.queryByRole("link", { name: /create conf/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /run conf/i })).toBeNull();
  });
});
