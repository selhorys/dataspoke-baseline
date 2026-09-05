/**
 * Tests for ValidationVariablesChart — small-multiples rendering.
 *
 * Spec: spec/feature/FRONTEND_VALIDATION.md §Page contracts:
 *   "small multiples — one auto-scaled, full-width line chart per declared
 *    variable stacked in a single column (one chart per row), each captioned
 *    with the variable's name and description".
 *
 * recharts is mocked to a lightweight stub so the grid structure and per-chart
 * captions can be asserted without ResponsiveContainer DOM measurement. The stub
 * also surfaces each chart's plotted x categories, which is what the shared-axis
 * assertions below read.
 *
 * Grain traces (spec/feature/FRONTEND_BASIC.md §Shared Component Notes →
 * ChartGrainPicker): "Rows are bucketed into grain windows and each window
 * contributes exactly one point: that window's last measurement … Every x label
 * is therefore distinct." The Quality Score row's grain is shared with these
 * small multiples, so all stacked charts must plot the same window set.
 *
 * The display timezone is pinned to UTC so window boundaries are host-independent.
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ValidationVariablesChart } from "./validation-variables-chart";
import type { ValidationResultRow, ValidationVariable } from "@/types/validation";

vi.mock("@/lib/preferences/timezone", () => ({ useDisplayTz: () => "utc" }));

// Mock recharts: each LineChart renders a marker element so we can count charts,
// carrying its plotted x categories so the shared-axis invariant is observable.
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
      dot,
      activeDot,
    }: {
      dataKey?: string;
      dot?: { r?: number } | boolean;
      activeDot?: { r?: number } | boolean;
    }) => (
      <div
        data-testid="line"
        data-key={String(dataKey)}
        data-dot={JSON.stringify(dot ?? null)}
        data-active-dot={JSON.stringify(activeDot ?? null)}
      />
    ),
    CartesianGrid: () => null,
    XAxis: ({ dataKey }: { dataKey?: string }) => (
      <div data-testid="x-axis" data-key={String(dataKey)} />
    ),
    YAxis: () => null,
    Tooltip: () => null,
  };
});

function row(dataTime: string, variables: Record<string, number>): ValidationResultRow {
  return { data_time: dataTime, score: 1, variables, score_note: null };
}

describe("ValidationVariablesChart — small multiples (FRONTEND_VALIDATION.md §Page contracts)", () => {
  const variables: ValidationVariable[] = [
    { name: "row_cnt", description: "Daily row count" },
    { name: "qty_total", description: "Total quantity" },
    { name: "anomaly_flag", description: "" },
  ];

  const results: ValidationResultRow[] = [
    row("2026-05-01T00:00:00Z", { row_cnt: 100, qty_total: 5000, anomaly_flag: 0 }),
    row("2026-05-02T00:00:00Z", { row_cnt: 110, qty_total: 5200, anomaly_flag: 1 }),
  ];

  it("renders one chart per declared variable", () => {
    render(<ValidationVariablesChart results={results} variables={variables} />);
    expect(screen.getAllByTestId("line-chart")).toHaveLength(3);
  });

  it("captions each chart with the variable name", () => {
    render(<ValidationVariablesChart results={results} variables={variables} />);
    expect(screen.getByText("row_cnt")).toBeInTheDocument();
    expect(screen.getByText("qty_total")).toBeInTheDocument();
    expect(screen.getByText("anomaly_flag")).toBeInTheDocument();
  });

  it("renders non-empty variable descriptions as captions", () => {
    render(<ValidationVariablesChart results={results} variables={variables} />);
    expect(screen.getByText("Daily row count")).toBeInTheDocument();
    expect(screen.getByText("Total quantity")).toBeInTheDocument();
  });

  it("shows the empty-period message when there are no results", () => {
    render(<ValidationVariablesChart results={[]} variables={variables} />);
    expect(screen.getByText("No variable data for this period.")).toBeInTheDocument();
    expect(screen.queryByTestId("line-chart")).not.toBeInTheDocument();
  });

  it("falls back to observed result keys when no variables are declared", () => {
    render(<ValidationVariablesChart results={results} variables={[]} />);
    // Three distinct keys observed across the result rows.
    expect(screen.getAllByTestId("line-chart")).toHaveLength(3);
  });
});

// ── Ragged variable coverage ────────────────────────────────────────────────────
// spec: FRONTEND_VALIDATION.md §Page contracts — "one auto-scaled, full-width
//   line chart per declared variable stacked in a single column (one chart per
//   row), each captioned with the variable's name and description".
// spec: FRONTEND_BASIC.md §Shared Component Notes → ChartGrainPicker — the grain
//   collapses fetched rows to one point per window; the small multiples share the
//   Quality Score row's grain, so they must share one window set.

describe("ValidationVariablesChart — ragged variable coverage", () => {
  // `row_cnt` is measured in every window; `qty_total` only in the first one.
  const raggedResults: ValidationResultRow[] = [
    row("2026-05-01T10:00:00Z", { row_cnt: 100, qty_total: 5000 }),
    row("2026-05-02T10:00:00Z", { row_cnt: 110 }),
    row("2026-05-03T10:00:00Z", { row_cnt: 120 }),
  ];

  const raggedVariables: ValidationVariable[] = [
    { name: "row_cnt", description: "Daily row count" },
    { name: "qty_total", description: "Total quantity" },
  ];

  function categoriesPerChart(): string[][] {
    return screen
      .getAllByTestId("line-chart")
      .map(
        (el) => JSON.parse(el.getAttribute("data-categories") as string) as string[],
      );
  }

  it("plots every small multiple over the identical x category set", () => {
    render(
      <ValidationVariablesChart results={raggedResults} variables={raggedVariables} />,
    );

    const perChart = categoriesPerChart();
    expect(perChart).toHaveLength(2);
    // One category per grain window, ascending — the same set in every chart, so
    // a given window sits at the same horizontal position in each.
    expect(perChart[0]).toEqual(["2026-05-01", "2026-05-02", "2026-05-03"]);
    expect(perChart[1]).toEqual(perChart[0]);
  });

  it("keeps a sparsely-measured variable's chart on the full window set", () => {
    render(
      <ValidationVariablesChart results={raggedResults} variables={raggedVariables} />,
    );

    // qty_total was measured in only one of the three windows, yet its chart is
    // not narrowed to that window — otherwise the stacked charts would not line up.
    expect(categoriesPerChart()[1]).toHaveLength(3);
    expect(screen.getByText("qty_total")).toBeInTheDocument();
  });

  // NOT spec-derived: FRONTEND_VALIDATION.md §Page contracts says "one
  // auto-scaled, full-width line chart per declared variable" and describes no
  // exception, so
  // substituting a placeholder for a variable with no measurements is an
  // implementation contract this test pins, not a spec requirement. What the
  // spec does bind — and what is asserted alongside — is that the declared
  // variable still gets its captioned slot ("each captioned with the variable's
  // name and description").
  it("shows a per-chart no-data placeholder for a variable present in zero results", () => {
    render(
      <ValidationVariablesChart
        results={raggedResults}
        variables={[
          { name: "row_cnt", description: "Daily row count" },
          { name: "never_measured", description: "Declared but never emitted" },
        ]}
      />,
    );

    // The declared variable still gets its captioned slot …
    expect(screen.getByText("never_measured")).toBeInTheDocument();
    expect(screen.getByText("Declared but never emitted")).toBeInTheDocument();
    // … with a no-data placeholder instead of an empty plot, and no chart of its own.
    expect(screen.getByText(/^no data$/i)).toBeInTheDocument();
    expect(screen.getAllByTestId("line-chart")).toHaveLength(1);
    // The period-level empty state is NOT what renders here — other variables have data.
    expect(screen.queryByText("No variable data for this period.")).not.toBeInTheDocument();
  });
});

// ── Grain collapse ──────────────────────────────────────────────────────────────
// spec: FRONTEND_BASIC.md §Shared Component Notes → ChartGrainPicker — "each
//   window contributes exactly one point: that window's last measurement".

describe("ValidationVariablesChart — grain collapse", () => {
  const sameDay: ValidationResultRow[] = [
    row("2026-05-04T01:00:00Z", { row_cnt: 100 }),
    row("2026-05-04T23:00:00Z", { row_cnt: 140 }),
  ];
  const variables: ValidationVariable[] = [{ name: "row_cnt", description: "" }];

  it("renders a series for a single window of data", () => {
    render(
      <ValidationVariablesChart results={sameDay} variables={variables} grain="daily" />,
    );

    const chart = screen.getByTestId("line-chart");
    expect(JSON.parse(chart.getAttribute("data-categories") as string)).toEqual([
      "2026-05-04",
    ]);
    expect(screen.queryByText("No variable data for this period.")).not.toBeInTheDocument();
  });

  it("draws a visible dot and a larger active dot on every small multiple", () => {
    // spec: FRONTEND_BASIC.md §Shared Component Notes → ChartGrainPicker — "each
    // point is drawn with a visible dot and an enlarged active dot, so a series
    // of a single measurement renders as one visible point and every plotted
    // measurement is hoverable". The radii are not spec'd — only that a dot is
    // configured (not recharts' `false`/absent) and the active one is larger,
    // which does constrain the props to the object form `{ r }`.
    render(
      <ValidationVariablesChart results={sameDay} variables={variables} grain="daily" />,
    );

    const lines = screen.getAllByTestId("line");
    expect(lines.length).toBeGreaterThan(0);
    for (const line of lines) {
      const dot = JSON.parse(line.getAttribute("data-dot") as string) as {
        r?: number;
      } | null;
      const activeDot = JSON.parse(line.getAttribute("data-active-dot") as string) as {
        r?: number;
      } | null;
      expect(dot?.r).toBeGreaterThan(0);
      expect(activeDot?.r).toBeGreaterThan(dot?.r as number);
    }
  });

  it("binds each small multiple's x-axis to the bucket-label key", () => {
    // The window labels are only useful if the axis actually reads them; an axis
    // bound to any other key is the unusable-axis defect this grain work fixes.
    render(
      <ValidationVariablesChart results={sameDay} variables={variables} grain="daily" />,
    );

    const axes = screen.getAllByTestId("x-axis");
    expect(axes.length).toBeGreaterThan(0);
    for (const axis of axes) expect(axis).toHaveAttribute("data-key", "date");
  });

  it("shows the empty-period message when every timestamp is unparseable", () => {
    render(
      <ValidationVariablesChart
        results={[row("not-a-date", { row_cnt: 100 }), row("", { row_cnt: 110 })]}
        variables={variables}
      />,
    );

    expect(screen.getByText("No variable data for this period.")).toBeInTheDocument();
    expect(screen.queryByTestId("line-chart")).not.toBeInTheDocument();
  });
});
