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
import { render, screen, cleanup } from "@testing-library/react";
import { MetricCard } from "./metric-card";
import { formatDate } from "@/lib/format-time";
import type { MetricDefinition, MetricResult } from "@/types/governance";
import type { RangeValue } from "@/lib/range";

// ── Mocks ──────────────────────────────────────────────────────────────────────

// Deterministic timezone so measured_at formatting does not depend on the host.
vi.mock("@/lib/preferences/timezone", () => ({ useDisplayTz: () => "utc" }));

const mockLatest = vi.fn();
const mockRanged = vi.fn();
// Args are forwarded (not swallowed) so the params the card hands each read are
// observable — see the "grain adds no request parameter" block.
vi.mock("@/lib/api/governance", () => ({
  useLatestMetricResult: (...args: unknown[]) => mockLatest(...args),
  useMetricResults: (...args: unknown[]) => mockRanged(...args),
}));

// recharts pulls in ResponsiveContainer/ResizeObserver (absent in jsdom). Stub
// the LineChart to a marker so the chart's mount is observable without measuring.
vi.mock("recharts", () => {
  const Passthrough = ({ children }: { children?: React.ReactNode }) => (
    <div>{children}</div>
  );
  return {
    ResponsiveContainer: Passthrough,
    // The plotted x categories are echoed so the "grain adds no request
    // parameter" block can prove the grain prop really reached the chart.
    LineChart: ({
      children,
      data,
    }: {
      children?: React.ReactNode;
      data?: { date: string }[];
    }) => (
      <div
        data-testid="line-chart"
        data-categories={JSON.stringify((data ?? []).map((d) => d.date))}
      >
        {children}
      </div>
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

// ── Grain is display-only ───────────────────────────────────────────────────────
// spec: spec/feature/FRONTEND_BASIC.md §Shared Component Notes → ChartGrainPicker:
//   "the grain is a **client-side display concern and adds no request
//   parameter**: it never alters the `from` / `to` / `until` / `limit` a call
//   site sends".
// spec: spec/feature/FRONTEND_GOVERNANCE.md §Dashboard: "range drives every
//   card's trend `from`/`to` (plus a limit); grain drives no request parameter".
//
// The FULL argument list of both reads is compared, not merely the absence of a
// `grain` key: the leak the spec sentence names is a CHANGED existing param
// (e.g. a larger `limit` at hourly), which no type check can see.

describe("MetricCard — grain adds no request parameter", () => {
  function categories(): string[] {
    return JSON.parse(
      screen.getByTestId("line-chart").getAttribute("data-categories") as string,
    ) as string[];
  }

  it("issues identical latest + trend reads at hourly and at weekly", () => {
    render(<MetricCard metric={METRIC} range={RANGE} grain="hourly" />);
    const hourlyLatest = mockLatest.mock.calls.at(-1);
    const hourlyRanged = mockRanged.mock.calls.at(-1);
    // Backstop: the grain really did reach the chart, so the equality below is
    // "grain changed, params didn't" — not "the prop was ignored".
    // RANGED spans two calendar weeks (2026-05-19 → week of 05-18,
    // 2026-05-26 → week of 05-25), so hourly and weekly label differently.
    expect(categories()).toEqual(["2026-05-19 00:00", "2026-05-26 00:00"]);

    cleanup();
    render(<MetricCard metric={METRIC} range={RANGE} grain="weekly" />);
    expect(categories()).toEqual(["2026-05-18", "2026-05-25"]);

    expect(mockRanged.mock.calls.at(-1)).toEqual(hourlyRanged);
    expect(mockLatest.mock.calls.at(-1)).toEqual(hourlyLatest);
  });

  it("issues the same reads with no grain prop at all as with one", () => {
    render(<MetricCard metric={METRIC} range={RANGE} />);
    const defaultRanged = mockRanged.mock.calls.at(-1);

    cleanup();
    render(<MetricCard metric={METRIC} range={RANGE} grain="hourly" />);

    expect(mockRanged.mock.calls.at(-1)).toEqual(defaultRanged);
  });
});
