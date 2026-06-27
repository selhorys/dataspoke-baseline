/**
 * Tests for MetricCard — the combined dashboard card for a single enabled metric.
 *
 * Spec: spec/feature/FRONTEND_GOVERNANCE.md §Dashboard:
 *   "Each card stacks, top to bottom: the metric `title`, a `metric_type`
 *    outline badge, the latest `values` dict (each key on its own line as
 *    `key: value`) with its measured-at date, and that metric's per-metric
 *    trend chart (one line per that metric's `values` key)."
 *
 * The two governance hooks (latest stat + ranged trend) are mocked, and recharts
 * is stubbed to a lightweight marker so the card structure can be asserted
 * without ResponsiveContainer DOM measurement (no ResizeObserver in jsdom).
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { MetricCard } from "./metric-card";
import { formatDate } from "@/lib/format-time";
import type { MetricDefinition, MetricResult } from "@/types/governance";
import type { RangeValue } from "@/lib/range";

// ── Mocks ──────────────────────────────────────────────────────────────────────

// Deterministic timezone so measured_at formatting does not depend on the host.
vi.mock("@/lib/preferences/timezone", () => ({ useDisplayTz: () => "utc" }));

const mockLatest = vi.fn();
const mockRanged = vi.fn();
vi.mock("@/lib/api/governance", () => ({
  useLatestMetricResult: () => mockLatest(),
  useMetricResults: () => mockRanged(),
}));

// recharts pulls in ResponsiveContainer/ResizeObserver (absent in jsdom). Stub
// the LineChart to a marker so the chart's mount is observable without measuring.
vi.mock("recharts", () => {
  const Passthrough = ({ children }: { children?: React.ReactNode }) => (
    <div>{children}</div>
  );
  return {
    ResponsiveContainer: Passthrough,
    LineChart: ({ children }: { children?: React.ReactNode }) => (
      <div data-testid="line-chart">{children}</div>
    ),
    Line: () => <div data-testid="line" />,
    CartesianGrid: () => null,
    Legend: () => null,
    XAxis: () => null,
    YAxis: () => null,
    Tooltip: () => null,
  };
});

// ── Fixtures (inline, readable) ─────────────────────────────────────────────────

const METRIC: MetricDefinition = {
  id: "doc-health-dev",
  mode: "active",
  is_enabled: true,
  metric_type: "doc-health",
  title: "Doc Health (DEV)",
  description: "Daily documentation-completeness check across DEV datasets",
  metrics: ["total", "doc_health"],
  metric_conf: {},
  schedule_tier: "daily",
  dataset_filter: { origin: "DEV" },
  created_at: "2026-05-26T00:00:00Z",
  updated_at: "2026-05-26T00:00:00Z",
};

const RANGE: RangeValue = {
  from: "2026-05-12T00:00:00Z",
  to: "2026-05-26T00:00:00Z",
};

function result(measuredAt: string, values: Record<string, number>): MetricResult {
  return {
    id: `r-${measuredAt}`,
    metric_id: METRIC.id,
    values,
    measured_at: measuredAt,
  };
}

const LATEST = result("2026-05-26T00:00:00Z", { total: 8, doc_health: 5 });
const RANGED: MetricResult[] = [
  result("2026-05-19T00:00:00Z", { total: 7, doc_health: 4 }),
  result("2026-05-26T00:00:00Z", { total: 8, doc_health: 5 }),
];

beforeEach(() => {
  mockLatest.mockReset();
  mockRanged.mockReset();
  mockLatest.mockReturnValue({ data: { results: [LATEST] }, isLoading: false });
  mockRanged.mockReturnValue({ data: { results: RANGED } });
});

// ── Tests ───────────────────────────────────────────────────────────────────────

describe("MetricCard — combined dashboard card (FRONTEND_GOVERNANCE.md §Dashboard)", () => {
  it("renders the metric title", () => {
    render(<MetricCard metric={METRIC} range={RANGE} />);
    expect(screen.getByText("Doc Health (DEV)")).toBeInTheDocument();
  });

  it("renders the metric_type outline badge", () => {
    render(<MetricCard metric={METRIC} range={RANGE} />);
    expect(screen.getByText("doc-health")).toBeInTheDocument();
  });

  it("renders each latest values key and its value", () => {
    render(<MetricCard metric={METRIC} range={RANGE} />);
    // key + value of every entry in the latest result's `values` dict.
    expect(screen.getByText("total")).toBeInTheDocument();
    expect(screen.getByText("8")).toBeInTheDocument();
    expect(screen.getByText("doc_health")).toBeInTheDocument();
    expect(screen.getByText("5")).toBeInTheDocument();
  });

  it("renders the latest result's measured-at date alongside its values", () => {
    render(<MetricCard metric={METRIC} range={RANGE} />);
    // spec: FRONTEND_GOVERNANCE.md §Dashboard — each card renders the latest
    // `values` dict *with its measured-at date*. Derive the expected string from
    // the same formatter the component uses (formatDate, UTC tz mocked above),
    // not from a hard-coded format the spec does not pin.
    expect(
      screen.getByText(formatDate(LATEST.measured_at, "utc")),
    ).toBeInTheDocument();
  });

  it("mounts the per-metric trend chart when ranged results are present", () => {
    render(<MetricCard metric={METRIC} range={RANGE} />);
    // The trend chart mounts (LineChart marker) and the empty-period message
    // is absent given non-empty ranged results.
    expect(screen.getByTestId("line-chart")).toBeInTheDocument();
    expect(
      screen.queryByText("No measurement data for this period."),
    ).not.toBeInTheDocument();
  });

  it("shows the chart empty-period message when there are no ranged results", () => {
    mockRanged.mockReturnValue({ data: { results: [] } });
    render(<MetricCard metric={METRIC} range={RANGE} />);
    expect(
      screen.getByText("No measurement data for this period."),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("line-chart")).not.toBeInTheDocument();
  });

  it("falls back to a no-results message when no latest stat exists", () => {
    mockLatest.mockReturnValue({ data: { results: [] }, isLoading: false });
    render(<MetricCard metric={METRIC} range={RANGE} />);
    expect(screen.getByText("No results yet.")).toBeInTheDocument();
    // Structural guard: with no latest stat, the values rows must be absent —
    // "total" would render only from the latest result's `values` dict.
    expect(screen.queryByText("total")).not.toBeInTheDocument();
  });
});
