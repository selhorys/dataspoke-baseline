/**
 * Tests for the metagen uncovered page.
 *
 * Spec: spec/feature/FRONTEND_METAGEN.md §Uncovered.
 *   - Read-only table of datasets reached by no conf, each carrying a reason.
 *   - The include_disallowed toggle drives the hook's includeDisallowed arg:
 *     off (default) → only no_conf_match; on → also boundary_blocked.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import React from "react";
import MetagenUncoveredPage from "./page";

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

const mockUncovered = vi.fn();
vi.mock("@/lib/api/metagen", () => ({
  useMetagenUncovered: (includeDisallowed: boolean) => mockUncovered(includeDisallowed),
}));

beforeEach(() => {
  mockUncovered.mockReset();
  mockUncovered.mockImplementation((includeDisallowed: boolean) => ({
    data: {
      total_count: includeDisallowed ? 2 : 1,
      datasets: includeDisallowed
        ? [
            { dataset_urn: "urn:li:dataset:a", reason: "no_conf_match" },
            { dataset_urn: "urn:li:dataset:b", reason: "boundary_blocked" },
          ]
        : [{ dataset_urn: "urn:li:dataset:a", reason: "no_conf_match" }],
    },
    isLoading: false,
    error: null,
  }));
});

describe("metagen uncovered page", () => {
  it("defaults to include_disallowed off and shows no_conf_match rows", () => {
    render(<MetagenUncoveredPage />);

    expect(mockUncovered).toHaveBeenCalledWith(false);
    // The reason badge in the table — scoped to a table cell to avoid matching
    // the literal in the page's descriptive copy.
    expect(screen.getByRole("cell", { name: "no_conf_match" })).toBeTruthy();
    expect(screen.queryByRole("cell", { name: "boundary_blocked" })).toBeNull();
  });

  it("toggling include_disallowed switches the hook arg and reveals boundary_blocked rows", () => {
    render(<MetagenUncoveredPage />);

    const toggle = screen.getByLabelText(/include_disallowed/i);
    fireEvent.click(toggle);

    expect(mockUncovered).toHaveBeenCalledWith(true);
    expect(screen.getByRole("cell", { name: "boundary_blocked" })).toBeTruthy();
  });

  it("renders each dataset_urn as a link to its per-dataset page", () => {
    render(<MetagenUncoveredPage />);

    const link = screen.getByRole("link", { name: "urn:li:dataset:a" });
    expect(link.getAttribute("href")).toBe(
      `/metagen/data/${encodeURIComponent("urn:li:dataset:a")}`,
    );
  });
});
