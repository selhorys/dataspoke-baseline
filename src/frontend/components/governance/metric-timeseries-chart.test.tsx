/**
 * Tests for MetricTimeseriesChart — the metric result trend chart.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_GOVERNANCE.md §Dashboard (combined metric card):
 *     "that metric's trend chart — **one line per entry of the metric's
 *     `metrics[]` series descriptors, drawn in `idx` order and stroked with each
 *     descriptor's `color`**, one visible point per grain window".
 *   - spec/feature/FRONTEND_GOVERNANCE.md §Metric detail (Result panel):
 *     "[Recharts line chart — one line per series, in `idx` order, stroked with
 *     each `color`]".
 *   - spec/API.md §Metric — Definition body, `metrics`: "The dashboard chart
 *     draws one line per descriptor, in `idx` order, stroked with `color`".
 *   - spec/feature/FRONTEND_BASIC.md §Shared Component Notes → ChartGrainPicker:
 *     "Rows are bucketed into grain windows and each window contributes exactly
 *     one point: that window's last measurement … each point is drawn with a
 *     **visible dot and an enlarged active dot, so a series of a single
 *     measurement renders as one visible point** and every plotted measurement
 *     is hoverable."
 *   - same bullet: "hourly windows include the date, not the hour alone" /
 *     "weekly windows start on Monday" — grain is honoured as a prop here.
 *
 * recharts is stubbed (ResponsiveContainer measures the DOM; jsdom has no
 * ResizeObserver). The stub follows components/validation/validation-variables-chart.test.tsx
 * and additionally surfaces the props the spec sentences above are about —
 * `dataKey` (which series exist), `stroke` (the color each is drawn in), the
 * plotted `data` (which windows exist), the dot / activeDot config, and the
 * XAxis `dataKey` (that the axis actually reads the bucket labels). Series are
 * emitted as sibling markers in render order, so document order *is* draw order.
 * YAxis stays stubbed to null; nothing here asserts on it.
 *
 * The display timezone is pinned to UTC so window boundaries are host-independent.
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MetricTimeseriesChart } from "./metric-timeseries-chart";
import { colorForKey } from "@/lib/chart-colors";
import type { MetricResult } from "@/types/governance";

vi.mock("@/lib/preferences/timezone", () => ({ useDisplayTz: () => "utc" }));

vi.mock("recharts", () => {
  const Passthrough = ({ children }: { children?: React.ReactNode }) => (
    <div>{children}</div>
  );
  return {
    ResponsiveContainer: Passthrough,
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
    Line: ({
      dataKey,
      stroke,
      dot,
      activeDot,
    }: {
      dataKey?: string;
      stroke?: string;
      dot?: { r?: number } | boolean;
      activeDot?: { r?: number } | boolean;
    }) => (
      <div
        data-testid="line"
        data-key={String(dataKey)}
        data-stroke={String(stroke)}
        data-dot={JSON.stringify(dot ?? null)}
        data-active-dot={JSON.stringify(activeDot ?? null)}
      />
    ),
    CartesianGrid: () => null,
    Legend: () => null,
    XAxis: ({ dataKey }: { dataKey?: string }) => (
      <div data-testid="x-axis" data-key={String(dataKey)} />
    ),
    YAxis: () => null,
    Tooltip: () => null,
  };
});

// ── Fixtures (inline, readable) ─────────────────────────────────────────────────

function result(measuredAt: string, values: Record<string, number>): MetricResult {
  return {
    id: `r-${measuredAt}`,
    metric_id: "doc-health-dev",
    values,
    measured_at: measuredAt,
  };
}

function seriesKeys(): string[] {
  return screen
    .getAllByTestId("line")
    .map((el) => el.getAttribute("data-key") as string)
    .sort();
}

/** Series keys in draw order (document order), unsorted — for `idx` ordering. */
function drawOrder(): string[] {
  return screen
    .getAllByTestId("line")
    .map((el) => el.getAttribute("data-key") as string);
}

/** The stroke each series is drawn with, keyed by series name. */
function strokes(): Record<string, string> {
  return Object.fromEntries(
    screen
      .getAllByTestId("line")
      .map((el) => [el.getAttribute("data-key") as string, el.getAttribute("data-stroke") as string]),
  );
}

function categories(): string[] {
  return JSON.parse(
    screen.getByTestId("line-chart").getAttribute("data-categories") as string,
  ) as string[];
}

const EMPTY_MESSAGE = /no measurement data/i;

// ── Series: one line per values key ────────────────────────────────────────────

describe("MetricTimeseriesChart — one line per values key (FRONTEND_GOVERNANCE.md §Dashboard)", () => {
  it("renders one series per key observed across the results", () => {
    render(
      <MetricTimeseriesChart
        results={[
          result("2026-05-04T00:00:00Z", { total: 7, doc_health: 4 }),
          result("2026-05-05T00:00:00Z", { total: 8, doc_health: 5 }),
        ]}
      />,
    );

    expect(seriesKeys()).toEqual(["doc_health", "total"]);
  });

  it("restricts the series to an explicit valueKeys list when given", () => {
    render(
      <MetricTimeseriesChart
        results={[result("2026-05-04T00:00:00Z", { total: 7, doc_health: 4 })]}
        valueKeys={["doc_health"]}
      />,
    );

    expect(seriesKeys()).toEqual(["doc_health"]);
  });

  it("never plots a values key literally named `date`", () => {
    // `date` is the x category key of a grain point, so a same-named value key
    // is shadowed by the bucket label; plotting it would feed a string into the
    // numeric Y domain.
    render(
      <MetricTimeseriesChart
        results={[
          result("2026-05-04T00:00:00Z", { date: 3, total: 7 }),
          result("2026-05-05T00:00:00Z", { date: 4, total: 8 }),
        ]}
      />,
    );

    expect(seriesKeys()).toEqual(["total"]);
    // The x categories remain the bucket labels, not the shadowed numbers.
    expect(categories()).toEqual(["2026-05-04", "2026-05-05"]);
  });
});

// ── Series descriptors: order by idx, stroke by color ──────────────────────────

describe("MetricTimeseriesChart — series descriptors decide order and color", () => {
  // spec/API.md §Metric — `metrics`: "The dashboard chart draws one line per
  // descriptor, in `idx` order, stroked with `color`."
  const RESULTS = [
    result("2026-05-04T00:00:00Z", { total: 7, doc_health: 4 }),
    result("2026-05-05T00:00:00Z", { total: 8, doc_health: 5 }),
  ];

  it("draws the descriptors in `idx` order, not in the order they arrive", () => {
    render(
      <MetricTimeseriesChart
        results={RESULTS}
        series={[
          { name: "total", color: "#64748B", idx: 2 },
          { name: "doc_health", color: "#A855F7", idx: 1 },
        ]}
      />,
    );

    // Declared total-then-doc_health, ordered doc_health-then-total by `idx`.
    expect(drawOrder()).toEqual(["doc_health", "total"]);
  });

  it("strokes each line with its descriptor's color", () => {
    render(
      <MetricTimeseriesChart
        results={RESULTS}
        series={[
          { name: "total", color: "#64748B", idx: 1 },
          { name: "doc_health", color: "#A855F7", idx: 2 },
        ]}
      />,
    );

    expect(strokes()).toEqual({ total: "#64748B", doc_health: "#A855F7" });
  });

  it("draws one line per descriptor — a values key with no descriptor is not plotted", () => {
    // "one line per entry of the metric's `metrics[]` series descriptors": the
    // descriptor list, not the result rows, is what decides the series set.
    render(
      <MetricTimeseriesChart
        results={RESULTS}
        series={[{ name: "doc_health", color: "#A855F7", idx: 1 }]}
      />,
    );

    expect(drawOrder()).toEqual(["doc_health"]);
  });

  it("still draws a descriptor whose key is absent from the fetched results", () => {
    // A metric that has just gained a series has descriptors ahead of its data;
    // the line is declared (empty) rather than silently dropped.
    render(
      <MetricTimeseriesChart
        results={[result("2026-05-04T00:00:00Z", { total: 7 })]}
        series={[
          { name: "total", color: "#64748B", idx: 1 },
          { name: "doc_health", color: "#A855F7", idx: 2 },
        ]}
      />,
    );

    expect(drawOrder()).toEqual(["total", "doc_health"]);
  });

  it("falls back to the palette color when no descriptors are supplied", () => {
    // Backstop for the color assertion above: without descriptors the chart is
    // still stroked (from the shared palette), so "stroke === descriptor color"
    // is a real behaviour rather than the only stroke the component can produce.
    render(<MetricTimeseriesChart results={RESULTS} />);

    const palette = strokes();
    expect(palette.total).toBe(colorForKey("total", ["doc_health", "total"]));
    expect(palette.doc_health).toBe(colorForKey("doc_health", ["doc_health", "total"]));
    expect(palette.total).not.toBe("#64748B");
  });

  it("does not reorder the caller's descriptor array in place", () => {
    // The card and the detail page both hand `conf.metrics` straight from the
    // query cache; sorting it in place would rewrite cached data. (Component
    // contract, not a spec sentence — recorded here so the intent is explicit.)
    const series = [
      { name: "total", color: "#64748B", idx: 2 },
      { name: "doc_health", color: "#A855F7", idx: 1 },
    ];
    render(<MetricTimeseriesChart results={RESULTS} series={series} />);

    expect(series.map((s) => s.name)).toEqual(["total", "doc_health"]);
  });
});

// ── A single window still plots ────────────────────────────────────────────────

describe("MetricTimeseriesChart — a single grain window renders a visible point", () => {
  it("plots one point (with its series) when every result falls in one window", () => {
    // spec (FRONTEND_BASIC §ChartGrainPicker): "each point is drawn with a
    // visible dot and an enlarged active dot, so a series of a single
    // measurement renders as one visible point".
    render(
      <MetricTimeseriesChart
        results={[
          result("2026-05-04T01:00:00Z", { total: 7, doc_health: 4 }),
          result("2026-05-04T23:00:00Z", { total: 8, doc_health: 5 }),
        ]}
        grain="daily"
      />,
    );

    expect(screen.getByTestId("line-chart")).toBeInTheDocument();
    expect(categories()).toEqual(["2026-05-04"]);
    expect(seriesKeys()).toEqual(["doc_health", "total"]);
    expect(screen.queryByText(EMPTY_MESSAGE)).not.toBeInTheDocument();
  });

  it("plots a single result row", () => {
    render(
      <MetricTimeseriesChart results={[result("2026-05-04T09:00:00Z", { total: 7 })]} />,
    );

    expect(categories()).toEqual(["2026-05-04"]);
    expect(seriesKeys()).toEqual(["total"]);
  });

  it("configures a visible dot and a larger active dot on every series", () => {
    render(
      <MetricTimeseriesChart results={[result("2026-05-04T09:00:00Z", { total: 7 })]} />,
    );

    for (const line of screen.getAllByTestId("line")) {
      const dot = JSON.parse(line.getAttribute("data-dot") as string) as {
        r?: number;
      } | null;
      const activeDot = JSON.parse(line.getAttribute("data-active-dot") as string) as {
        r?: number;
      } | null;
      // A dot is configured (not the recharts `false` / absent default), and the
      // active dot is the enlarged one. The radii themselves are not spec'd; the
      // "enlarged" comparison does constrain the props to the object form `{ r }`,
      // which is the only way the relation is observable at all.
      expect(dot?.r).toBeGreaterThan(0);
      expect(activeDot?.r).toBeGreaterThan(dot?.r as number);
    }
  });

  it("binds the x-axis to the bucket-label key", () => {
    // The window labels are only useful if the axis actually reads them; an axis
    // bound to any other key is the unusable-axis defect this grain work fixes.
    render(
      <MetricTimeseriesChart results={[result("2026-05-04T09:00:00Z", { total: 7 })]} />,
    );
    expect(screen.getByTestId("x-axis")).toHaveAttribute("data-key", "date");
  });
});

// ── Grain governs the windows ──────────────────────────────────────────────────

describe("MetricTimeseriesChart — grain governs the plotted windows", () => {
  const results = [
    result("2026-05-04T09:15:00Z", { total: 1 }), // Monday
    result("2026-05-04T21:45:00Z", { total: 2 }),
    result("2026-05-06T09:30:00Z", { total: 3 }), // Wednesday, same week
  ];

  it("hourly keeps each distinct clock hour, labelled with its date", () => {
    render(<MetricTimeseriesChart results={results} grain="hourly" />);
    expect(categories()).toEqual([
      "2026-05-04 09:00",
      "2026-05-04 21:00",
      "2026-05-06 09:00",
    ]);
  });

  it("daily collapses each calendar day to its last measurement", () => {
    render(<MetricTimeseriesChart results={results} grain="daily" />);
    expect(categories()).toEqual(["2026-05-04", "2026-05-06"]);
  });

  it("weekly collapses the week onto its Monday", () => {
    render(<MetricTimeseriesChart results={results} grain="weekly" />);
    expect(categories()).toEqual(["2026-05-04"]);
  });

  it("defaults to daily when no grain is supplied", () => {
    render(<MetricTimeseriesChart results={results} />);
    expect(categories()).toEqual(["2026-05-04", "2026-05-06"]);
  });
});

// ── Empty states ───────────────────────────────────────────────────────────────

describe("MetricTimeseriesChart — nothing plottable", () => {
  it("shows the empty-period message when there are no results", () => {
    render(<MetricTimeseriesChart results={[]} />);
    expect(screen.getByText(EMPTY_MESSAGE)).toBeInTheDocument();
    expect(screen.queryByTestId("line-chart")).not.toBeInTheDocument();
  });

  it("shows the empty-period message when every timestamp is unparseable", () => {
    // Unparseable rows have no window, so they are skipped — leaving nothing to
    // plot. The user must see the empty state, not a chart with bare axes.
    render(
      <MetricTimeseriesChart
        results={[
          result("not-a-date", { total: 7 }),
          { id: "r-blank", metric_id: "m", values: { total: 8 }, measured_at: "" },
        ]}
      />,
    );

    expect(screen.getByText(EMPTY_MESSAGE)).toBeInTheDocument();
    expect(screen.queryByTestId("line-chart")).not.toBeInTheDocument();
  });

  it("still plots the parseable rows when only some timestamps are unparseable", () => {
    render(
      <MetricTimeseriesChart
        results={[
          result("not-a-date", { total: 99 }),
          result("2026-05-04T09:00:00Z", { total: 7 }),
        ]}
      />,
    );

    expect(categories()).toEqual(["2026-05-04"]);
    expect(screen.queryByText(EMPTY_MESSAGE)).not.toBeInTheDocument();
  });
});
