/**
 * Tests for the Governance Metrics list page — /governance/metrics.
 *
 * Focus: the "Last Run" column (F2) — formatted last_run_at when present, em-dash
 * when null. last_run_at is the list-row-only field derived from the latest
 * METRIC.RUN_COMPLETE event.
 *
 * Spec: spec/feature/FRONTEND_GOVERNANCE.md §Metrics — list adds a Last Run column.
 * Spec: spec/API.md §Metric — GET /spoke/governance/metric — each row carries
 *   last_run_at (null when the metric has never completed a run).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act, within } from "@testing-library/react";
import React from "react";
import GovernanceMetricsPage from "./page";
import { ApiError } from "@/lib/api/client";
import { useAuthStore } from "@/lib/auth/store";
import type { MetricDefinitionListItem } from "@/types/governance";

// jsdom lacks ResizeObserver (used by Radix Select).
if (typeof global.ResizeObserver === "undefined") {
  global.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

const mockUseGovernanceMetrics = vi.fn();
vi.mock("@/lib/api/governance", () => ({
  useGovernanceMetrics: (params: unknown) => mockUseGovernanceMetrics(params),
}));

vi.mock("@/lib/auth/use-me", () => ({ useMe: () => ({ canWrite: false }) }));
vi.mock("@/lib/preferences/timezone", () => ({ useDisplayTz: () => "utc" }));

function metric(overrides: Partial<MetricDefinitionListItem> = {}): MetricDefinitionListItem {
  return {
    id: "ingestion-freshness",
    mode: "active",
    is_enabled: true,
    metric_type: "ingestion-freshness",
    title: "Ingestion freshness",
    description: "freshness",
    metrics: [
    { name: "total", color: "#64748B", idx: 1 },
    { name: "ingested_in_time", color: "#22C55E", idx: 2 },
  ],
    metric_conf: { time_window_sec: 172800 },
    schedule_tier: "daily",
    dataset_filter: "",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-02-01T00:00:00Z",
    last_run_at: "2026-03-15T10:30:00Z",
    ...overrides,
  };
}

function setData(metrics: MetricDefinitionListItem[]): void {
  mockUseGovernanceMetrics.mockReturnValue({
    data: { offset: 0, limit: 20, total_count: metrics.length, metrics },
    isLoading: false,
    error: null,
  });
}

async function renderPage(): Promise<void> {
  await act(async () => {
    render(<GovernanceMetricsPage />);
  });
}

beforeEach(() => {
  mockUseGovernanceMetrics.mockReset();
});

describe("GovernanceMetricsPage — Last Run column", () => {
  it("renders a 'Last Run' column header", async () => {
    setData([metric()]);
    await renderPage();
    expect(screen.getByRole("columnheader", { name: "Last Run" })).toBeTruthy();
  });

  it("renders the formatted last_run_at when the metric has completed a run", async () => {
    setData([metric({ id: "has-run", last_run_at: "2026-03-15T10:30:00Z" })]);
    await renderPage();
    // formatDateTime(..., "utc") → "YYYY-MM-DD HH:MM".
    expect(screen.getByText("2026-03-15 10:30")).toBeTruthy();
  });

  it("renders an em-dash in the Last Run cell when the metric has never completed a run", async () => {
    setData([metric({ id: "never-run", schedule_tier: "daily", last_run_at: null })]);
    await renderPage();
    // Scope to the Last Run cell of the (single) data row rather than counting
    // em-dashes page-wide: Last Run is the last column, so the never-run row's last
    // cell must render the dash and no formatted timestamp.
    const dataRow = screen
      .getAllByRole("row")
      .find((r) => within(r).queryByText("Ingestion freshness"));
    expect(dataRow).toBeTruthy();
    const cells = within(dataRow as HTMLElement).getAllByRole("cell");
    const lastRunCell = cells[cells.length - 1];
    expect(lastRunCell.textContent).toBe("—");
  });
});

// ---------------------------------------------------------------------------
// Failed read — the inline error render point
// ---------------------------------------------------------------------------

/**
 * Spec: spec/feature/FRONTEND_BASIC.md §Query Error Policy — "A page or panel
 * that surfaces a failed read inline renders it through QueryErrorState", and
 * §Peripherals (/admin/peripherals) — on a deployment whose DataHub is unwired
 * "the affected pages render the muted QueryErrorState onboarding state pointing
 * back here".
 *
 * This is a call-site test, not a second copy of components/query-error-state.test.tsx.
 * `QueryErrorStateProps.error` is typed `unknown`, so a page that hands over
 * `error.message`, `String(error)`, or another query's error still typechecks and
 * still renders — just always on the destructive branch, which reinstates on that
 * page exactly the alarm state this policy removed. One representative page pins
 * that the error object reaches the component intact.
 */
describe("GovernanceMetricsPage — a failed read renders through QueryErrorState", () => {
  const PERIPHERAL_NOT_CONFIGURED_ERROR = new ApiError(
    {
      error_code: "PERIPHERAL_NOT_CONFIGURED",
      message: "DataHub is not configured",
      trace_id: "aaaaaaaa-0000-0000-0000-000000000000",
      resp_time: "2026-07-01T00:00:00Z",
      detail: { peripheral: "datahub" },
    },
    503,
  );

  function setError(error: unknown): void {
    mockUseGovernanceMetrics.mockReturnValue({ data: undefined, isLoading: false, error });
  }

  beforeEach(() => {
    useAuthStore.setState({
      me: {
        id: "u1",
        email: "admin@example.com",
        name: "Admin",
        role: "Admin",
        has_password: true,
        has_google: false,
        created_at: "2026-01-01T00:00:00Z",
        updated_at: "2026-01-01T00:00:00Z",
      },
    });
  });

  it("hands the error object through, so a 503 PERIPHERAL_NOT_CONFIGURED reaches the onboarding branch", async () => {
    setError(PERIPHERAL_NOT_CONFIGURED_ERROR);
    await renderPage();

    // Names the peripheral and points an admin at the page that fixes it.
    expect(document.body.textContent).toContain("DataHub");
    expect(screen.getByRole("link", { name: /peripherals/i })).toHaveAttribute(
      "href",
      "/admin/peripherals",
    );
  });

  it("does not render the destructive copy for that error", async () => {
    setError(PERIPHERAL_NOT_CONFIGURED_ERROR);
    await renderPage();

    expect(screen.queryByText(/Failed to load metrics/)).not.toBeInTheDocument();
  });

  it("still renders the ordinary error state for every other failure", async () => {
    // Backstop: the page has not simply been rewired to always show onboarding.
    setError(new Error("Database connection failed"));
    await renderPage();

    expect(screen.getByText("Failed to load metrics: Database connection failed")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /peripherals/i })).not.toBeInTheDocument();
  });
});
