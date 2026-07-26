/**
 * Tests for the metagen uncovered page.
 *
 * Spec: spec/feature/FRONTEND_METAGEN.md §Uncovered.
 *   - Read-only table of datasets reached by no conf, each carrying a reason.
 *   - The include_disallowed toggle drives the hook's includeDisallowed arg:
 *     off by default, true once toggled. Which rows that arg widens the set to is
 *     the server's contract (see tests/e2e/ground/metagen/uncovered.spec.ts); here
 *     the page is held only to passing the right arg and rendering what it receives.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import React from "react";
import MetagenUncoveredPage from "./page";

// Shared link components read peripheral-links; this suite mounts no
// QueryClientProvider, so it substitutes the all-unconfigured stub — every
// DataHub / Langfuse link resolves to the "render no link" state.
// See lib/api/peripheral-links.mock.ts.
vi.mock("@/lib/api/peripheral-links", async () =>
  (await import("@/lib/api/peripheral-links.mock")).unconfiguredPeripheralLinksModule(),
);


vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

const mockUncovered = vi.fn();
vi.mock("@/lib/api/metagen", () => ({
  useMetagenUncovered: (includeDisallowed: boolean) => mockUncovered(includeDisallowed),
}));

// The mock returns the SAME payload for both arg values on purpose. Narrowing the
// off-payload here would only re-test the fixture's own branch: the widening is the
// server's job (include_disallowed is a query param), and the page's whole contract
// is (a) which arg it passes and (b) rendering whatever comes back. The real
// off ⊂ on widening is proven end-to-end in
// tests/e2e/ground/metagen/uncovered.spec.ts against a seeded boundary_blocked row.
const ROWS = [
  { dataset_urn: "urn:li:dataset:a", reason: "no_conf_match" },
  { dataset_urn: "urn:li:dataset:b", reason: "boundary_blocked" },
];

beforeEach(() => {
  mockUncovered.mockReset();
  mockUncovered.mockImplementation(() => ({
    data: { total_count: ROWS.length, datasets: ROWS },
    isLoading: false,
    error: null,
  }));
});

describe("metagen uncovered page", () => {
  it("defaults to include_disallowed off", () => {
    render(<MetagenUncoveredPage />);

    expect(mockUncovered).toHaveBeenCalledWith(false);
    expect(mockUncovered).not.toHaveBeenCalledWith(true);
  });

  it("renders every row the hook returns, each with its reason", () => {
    render(<MetagenUncoveredPage />);

    // Reason badges scoped to table cells so the literals in the page's descriptive
    // copy cannot satisfy the assertion. The page applies no reason filtering of its
    // own — both classifications render whenever the server returns them.
    expect(screen.getByRole("cell", { name: "no_conf_match" })).toBeTruthy();
    expect(screen.getByRole("cell", { name: "boundary_blocked" })).toBeTruthy();
  });

  it("toggling include_disallowed switches the hook arg to true", () => {
    render(<MetagenUncoveredPage />);

    const toggle = screen.getByLabelText(/show boundary-blocked datasets/i);
    fireEvent.click(toggle);

    expect(mockUncovered).toHaveBeenCalledWith(true);
  });

  it("renders each dataset_urn as a link to its per-dataset page", () => {
    render(<MetagenUncoveredPage />);

    const link = screen.getByRole("link", { name: "urn:li:dataset:a" });
    expect(link.getAttribute("href")).toBe(
      `/data/${encodeURIComponent("urn:li:dataset:a")}`,
    );
  });
});
