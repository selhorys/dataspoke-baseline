/**
 * Tests for the cross-dataset Validation list page — /validation.
 *
 * Spec: spec/feature/FRONTEND_VALIDATION.md §List — two checkboxes (covered,
 *   default checked; uncovered, default unchecked) map to the server-side
 *   `coverage` query param on GET /spoke/validation:
 *     covered only      → coverage="covered"
 *     covered+uncovered → coverage="both"
 *     uncovered only    → coverage="uncovered"
 *     neither           → query disabled, empty table
 * Spec: spec/API.md §Validation — uncovered rows carry null description /
 *   variable_count / latest_data_time / latest_score, rendered as an em-dash.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import React from "react";
import ValidationListPage from "./page";
import type { ValidationListItem } from "@/types/validation";

// ── Mocks ──────────────────────────────────────────────────────────────────────
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

// Capture the (params, opts) the page passes so we can assert coverage mapping
// and the enabled gate.
const mockUseValidationList = vi.fn();
vi.mock("@/lib/api/validation", () => ({
  useValidationList: (
    params: { coverage?: string; offset?: number; limit?: number },
    opts: { enabled?: boolean },
  ) => mockUseValidationList(params, opts),
}));

function lastCall(): [
  { coverage?: string; offset?: number; limit?: number },
  { enabled?: boolean },
] {
  return mockUseValidationList.mock.calls.at(-1) as [
    { coverage?: string; offset?: number; limit?: number },
    { enabled?: boolean },
  ];
}

const COVERED_ROW: ValidationListItem = {
  dataset_urn:
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.orders.daily_fulfillment_summary,DEV)",
  description: "Daily order fulfillment quality",
  variable_count: 3,
  latest_data_time: "2026-06-20T00:00:00Z",
  latest_score: 0.91,
  updated_at: "2026-06-20T00:00:00Z",
};

const UNCOVERED_ROW: ValidationListItem = {
  dataset_urn:
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)",
  description: null,
  variable_count: null,
  latest_data_time: null,
  latest_score: null,
  updated_at: null,
};

function setListData(rows: ValidationListItem[]): void {
  mockUseValidationList.mockReturnValue({
    data: { offset: 0, limit: 20, total_count: rows.length, validations: rows },
    isLoading: false,
    error: null,
  });
}

async function renderPage(): Promise<void> {
  await act(async () => {
    render(<ValidationListPage />);
  });
}

beforeEach(() => {
  mockUseValidationList.mockReset();
  setListData([COVERED_ROW]);
});

// ── Coverage checkbox → coverage param mapping ──────────────────────────────────

describe("ValidationListPage — coverage filter mapping", () => {
  it("defaults to covered checked / uncovered unchecked → coverage='covered', enabled", async () => {
    await renderPage();
    const covered = screen.getByLabelText("covered") as HTMLInputElement;
    const uncovered = screen.getByLabelText("uncovered") as HTMLInputElement;
    expect(covered.getAttribute("aria-checked")).toBe("true");
    expect(uncovered.getAttribute("aria-checked")).toBe("false");

    const [params, opts] = lastCall();
    expect(params.coverage).toBe("covered");
    expect(opts.enabled).toBe(true);
  });

  it("checking uncovered as well → coverage='both'", async () => {
    await renderPage();
    await act(async () => {
      fireEvent.click(screen.getByLabelText("uncovered"));
    });
    const [params, opts] = lastCall();
    expect(params.coverage).toBe("both");
    expect(opts.enabled).toBe(true);
  });

  it("unchecking covered (uncovered only) → coverage='uncovered'", async () => {
    await renderPage();
    await act(async () => {
      fireEvent.click(screen.getByLabelText("uncovered"));
    });
    await act(async () => {
      fireEvent.click(screen.getByLabelText("covered"));
    });
    const [params, opts] = lastCall();
    expect(params.coverage).toBe("uncovered");
    expect(opts.enabled).toBe(true);
  });

  it("neither checked → query disabled, no coverage param, no rows", async () => {
    await renderPage();
    await act(async () => {
      fireEvent.click(screen.getByLabelText("covered"));
    });
    // Primary invariant: with no coverage selected the query is disabled and no
    // coverage param is sent.
    const [params, opts] = lastCall();
    expect(opts.enabled).toBe(false);
    expect(params.coverage).toBeUndefined();
    // Behavioral consequence: no dataset rows are rendered (no URN links). The
    // exact empty-state wording is incidental UX copy, not asserted.
    expect(
      screen.queryByRole("link", { name: COVERED_ROW.dataset_urn }),
    ).toBeNull();
  });
});

// ── Null-cell em-dash for uncovered rows ────────────────────────────────────────

describe("ValidationListPage — uncovered row rendering", () => {
  it("renders an em-dash for null description / variables / data_time / score", async () => {
    setListData([UNCOVERED_ROW]);
    await renderPage();

    // The URN link is present; the four null fields render as em-dashes.
    expect(
      screen.getByRole("link", { name: UNCOVERED_ROW.dataset_urn }),
    ).toBeTruthy();
    const emDashes = screen.getAllByText("—");
    // description, variable_count, latest_data_time, latest_score → 4 cells.
    expect(emDashes.length).toBeGreaterThanOrEqual(4);
    // No score Badge value is rendered for a null score.
    expect(screen.queryByText(/^0\.\d+$/)).toBeNull();
  });

  it("renders concrete values (no em-dash) for a covered row", async () => {
    setListData([COVERED_ROW]);
    await renderPage();
    expect(screen.getByText("Daily order fulfillment quality")).toBeTruthy();
    expect(screen.getByText("3")).toBeTruthy(); // variable_count
  });
});
