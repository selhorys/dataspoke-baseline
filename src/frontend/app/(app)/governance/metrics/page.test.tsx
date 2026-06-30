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
    metrics: ["total", "ingested_in_time"],
    metric_conf: { time_window_sec: 172800 },
    schedule_tier: "daily",
    dataset_filter: {},
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
