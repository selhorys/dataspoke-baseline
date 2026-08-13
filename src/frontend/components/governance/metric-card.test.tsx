/**
 * Tests for MetricCard — the combined dashboard card for a single enabled metric.
 *
 * Spec: spec/feature/FRONTEND_GOVERNANCE.md §Dashboard (Combined metric card):
 *   "The card header carries the metric `title` as an emphasized heading on the
 *    left and, top-right at a smaller size, a `metric_type` outline badge beside
 *    a `Details` button linking to `/governance/metrics/{id}`. Below the heading
 *    sits `description` in small muted text. The body then stacks the latest
 *    `values` dict as a compact stat row … with its measured-at date, and that
 *    metric's trend chart — one line per entry of the metric's `metrics[]` series
 *    descriptors, drawn in `idx` order and stroked with each descriptor's
 *    `color` … `description` and `metrics` both come from the list read, so the
 *    card needs no extra fetch."
 *
 * The two governance hooks (latest stat + ranged trend) are mocked, and recharts
 * is stubbed to a lightweight marker so the card structure can be asserted
 * without ResponsiveContainer DOM measurement (no ResizeObserver in jsdom). The
 * mocked governance module exports **only** those two hooks, so a card that
 * reached for a third read (e.g. a per-metric conf fetch for `description` /
 * `metrics`) would fail on an undefined import — that is the backstop for the
 * "needs no extra fetch" clause.
 */

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, cleanup, within } from "@testing-library/react";
import React from "react";
import { MetricCard } from "./metric-card";
import { formatDate } from "@/lib/format-time";
import type { MetricDefinition, MetricResult } from "@/types/governance";
import type { RangeValue } from "@/lib/range";

// ── Mocks ──────────────────────────────────────────────────────────────────────

// Deterministic timezone so measured_at formatting does not depend on the host.
vi.mock("@/lib/preferences/timezone", () => ({ useDisplayTz: () => "utc" }));

// next/link → plain anchor so the Details link's href is assertable in jsdom.
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

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
    // `dataKey` / `stroke` are echoed so the card can be shown to hand its
    // metric's series descriptors (order + color) down to the chart.
    Line: ({ dataKey, stroke }: { dataKey?: string; stroke?: string }) => (
      <div data-testid="line" data-key={String(dataKey)} data-stroke={String(stroke)} />
    ),
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
  metrics: [
    { name: "total", color: "#64748B", idx: 1 },
    { name: "doc_health", color: "#A855F7", idx: 2 },
  ],
  metric_conf: {},
  schedule_tier: "daily",
  dataset_filter: "origin = 'DEV'",
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

// ── Header: title left, type badge + Details right, description beneath ────────
// spec: FRONTEND_GOVERNANCE.md §Dashboard (Combined metric card) — "The card
// header carries the metric `title` as an emphasized heading on the left and,
// top-right at a smaller size, a `metric_type` outline badge beside a `Details`
// button linking to `/governance/metrics/{id}`. Below the heading sits
// `description` in small muted text."

describe("MetricCard — header layout (FRONTEND_GOVERNANCE.md §Dashboard)", () => {
  /** True when `a` comes before `b` in document order. */
  function precedes(a: Element, b: Element): boolean {
    return Boolean(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
  }

  /** The row holding the title — the header line the badge/Details sit on. */
  function titleRow(): HTMLElement {
    const row = screen.getByText(METRIC.title).parentElement;
    expect(row, "the card title must sit in a header row").not.toBeNull();
    return row as HTMLElement;
  }

  it("puts the metric_type badge and the Details link on the title's row, after the title", () => {
    // "the metric `title` … on the left and, top-right …, a `metric_type` outline
    // badge beside a `Details` button". jsdom loads no stylesheet, so the
    // observable form of "left / top-right" is: same row, badge and button
    // following the title.
    render(<MetricCard metric={METRIC} range={RANGE} />);
    const row = within(titleRow());
    const title = screen.getByText(METRIC.title);
    const badge = row.getByText(METRIC.metric_type);
    const details = row.getByRole("link", { name: "Details" });

    expect(precedes(title, badge)).toBe(true);
    expect(precedes(badge, details)).toBe(true);
  });

  it("renders the description below the title row and above the stat row", () => {
    render(<MetricCard metric={METRIC} range={RANGE} />);
    const description = screen.getByText(METRIC.description);
    const statLabel = screen.getByText("doc_health");

    expect(within(titleRow()).queryByText(METRIC.description)).toBeNull();
    expect(precedes(screen.getByText(METRIC.title), description)).toBe(true);
    expect(precedes(description, statLabel)).toBe(true);
  });

  it("renders the description in small muted text", () => {
    render(<MetricCard metric={METRIC} range={RANGE} />);
    const description = screen.getByText(METRIC.description);
    expect(description.className).toContain("text-xs");
    expect(description.className).toContain("text-muted-foreground");
  });

  it("links Details to the metric's detail route, id percent-encoded", () => {
    render(<MetricCard metric={METRIC} range={RANGE} />);
    expect(screen.getByRole("link", { name: "Details" })).toHaveAttribute(
      "href",
      `/governance/metrics/${encodeURIComponent(METRIC.id)}`,
    );
  });

  it("keeps the badge at a smaller type scale than the title", () => {
    // "top-right at a smaller size". Only the relation is spec'd, not the exact
    // tokens, so this asserts the title is the larger declared scale.
    render(<MetricCard metric={METRIC} range={RANGE} />);
    expect(screen.getByText(METRIC.title).className).toContain("text-lg");
    expect(screen.getByText(METRIC.metric_type).className).toMatch(/text-\[10px\]|text-xs/);
  });

  it("renders the stat row and the chart after the header content", () => {
    // Backstop for the ordering assertions: they are only meaningful if the card
    // really does render the body below the header.
    render(<MetricCard metric={METRIC} range={RANGE} />);
    expect(precedes(screen.getByText(METRIC.description), screen.getByTestId("line-chart"))).toBe(
      true,
    );
  });
});

// ── The card's chart is drawn from the metric's series descriptors ─────────────
// spec: FRONTEND_GOVERNANCE.md §Dashboard — "that metric's trend chart — one line
// per entry of the metric's `metrics[]` series descriptors, drawn in `idx` order
// and stroked with each descriptor's `color`".

describe("MetricCard — hands its series descriptors to the trend chart", () => {
  function lines(): Array<{ key: string; stroke: string }> {
    return screen.getAllByTestId("line").map((el) => ({
      key: el.getAttribute("data-key") as string,
      stroke: el.getAttribute("data-stroke") as string,
    }));
  }

  it("draws one line per descriptor, in idx order, stroked with its color", () => {
    // Descriptors declared out of `idx` order so the ordering is observable.
    const metric: MetricDefinition = {
      ...METRIC,
      metrics: [
        { name: "total", color: "#64748B", idx: 2 },
        { name: "doc_health", color: "#A855F7", idx: 1 },
      ],
    };
    render(<MetricCard metric={metric} range={RANGE} />);

    expect(lines()).toEqual([
      { key: "doc_health", stroke: "#A855F7" },
      { key: "total", stroke: "#64748B" },
    ]);
  });

  it("plots only the declared descriptors, not every key in the results", () => {
    const metric: MetricDefinition = {
      ...METRIC,
      metrics: [{ name: "doc_health", color: "#A855F7", idx: 1 }],
    };
    render(<MetricCard metric={metric} range={RANGE} />);

    // RANGED carries both `total` and `doc_health`; only the descriptor is drawn.
    expect(lines()).toEqual([{ key: "doc_health", stroke: "#A855F7" }]);
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
