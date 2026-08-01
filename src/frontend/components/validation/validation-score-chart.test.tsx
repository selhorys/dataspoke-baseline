/**
 * Tests for ValidationScoreChart — the dataset Quality Score trend chart.
 *
 * Spec traces (spec/feature/FRONTEND_BASIC.md §Shared Component Notes →
 * ChartGrainPicker — the per-dataset Validation panel's `Quality Score` heading
 * row is one of the three surfaces that bullet governs):
 *   - "Rows are bucketed into grain windows and each window contributes exactly
 *     **one** point: that window's **last** measurement (greatest timestamp),
 *     labelled by the truncated window start … Every x label is therefore
 *     distinct, and each point is drawn with a **visible dot and an enlarged
 *     active dot, so a series of a single measurement renders as one visible
 *     point** and every plotted measurement is hoverable."
 *   - "hourly windows include the date, not the hour alone"; "weekly windows
 *     start on Monday and are labelled by that Monday's date".
 *   - "A row whose timestamp does not parse contributes to no window and is
 *     dropped rather than grouped under a placeholder label."
 * spec: spec/feature/FRONTEND_VALIDATION.md §Detail — the Quality Score
 *   timeseries over `GET .../attr/validation/result` rows (`data_time`, `score`).
 *
 * recharts is stubbed (ResponsiveContainer measures the DOM; jsdom has no
 * ResizeObserver). The stub surfaces exactly the props the sentences above are
 * about: the plotted `data` (which windows exist), the XAxis `dataKey` (that the
 * bucket labels are what the axis reads), and the Line's dataKey / dot /
 * activeDot. YAxis stays stubbed to null — nothing here asserts on it.
 *
 * The display timezone is pinned to UTC so window boundaries are host-independent.
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ValidationScoreChart } from "./validation-score-chart";
import type { ValidationResultRow } from "@/types/validation";

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
        data-points={JSON.stringify(data ?? [])}
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

// ── Fixtures (inline, readable) ─────────────────────────────────────────────────

function row(dataTime: string, score: number): ValidationResultRow {
  return { data_time: dataTime, score, variables: { row_cnt: 1 } };
}

function categories(): string[] {
  return JSON.parse(
    screen.getByTestId("line-chart").getAttribute("data-categories") as string,
  ) as string[];
}

function plotted(): { date: string; score?: number }[] {
  return JSON.parse(
    screen.getByTestId("line-chart").getAttribute("data-points") as string,
  ) as { date: string; score?: number }[];
}

const EMPTY_MESSAGE = /no score data/i;

// ── One point per window, the window's last score ──────────────────────────────

describe("ValidationScoreChart — one point per grain window", () => {
  it("collapses same-window results to one point carrying the later score", () => {
    render(
      <ValidationScoreChart
        results={[row("2026-05-04T01:00:00Z", 0.4), row("2026-05-04T23:00:00Z", 0.9)]}
        grain="daily"
      />,
    );

    expect(categories()).toEqual(["2026-05-04"]);
    expect(plotted()).toEqual([{ date: "2026-05-04", score: 0.9 }]);
  });

  it("emits one point per window, ascending", () => {
    render(
      <ValidationScoreChart
        results={[
          row("2026-05-06T12:00:00Z", 0.7),
          row("2026-05-04T12:00:00Z", 0.5),
          row("2026-05-05T12:00:00Z", 0.6),
        ]}
        grain="daily"
      />,
    );

    expect(categories()).toEqual(["2026-05-04", "2026-05-05", "2026-05-06"]);
    expect(plotted().map((p) => p.score)).toEqual([0.5, 0.6, 0.7]);
  });

  it("honours the grain: hourly keeps the date, weekly folds onto the Monday", () => {
    const results = [
      row("2026-05-04T09:15:00Z", 0.1), // Monday
      row("2026-05-04T21:45:00Z", 0.2),
      row("2026-05-06T09:30:00Z", 0.3), // Wednesday, same week
    ];

    const hourly = render(<ValidationScoreChart results={results} grain="hourly" />);
    expect(categories()).toEqual([
      "2026-05-04 09:00",
      "2026-05-04 21:00",
      "2026-05-06 09:00",
    ]);
    hourly.unmount();

    const weekly = render(<ValidationScoreChart results={results} grain="weekly" />);
    expect(categories()).toEqual(["2026-05-04"]);
    weekly.unmount();

    render(<ValidationScoreChart results={results} />);
    // No grain prop → the documented daily default.
    expect(categories()).toEqual(["2026-05-04", "2026-05-06"]);
  });
});

// ── A single window still plots a visible point ────────────────────────────────

describe("ValidationScoreChart — a single grain window renders a visible point", () => {
  it("plots the series for a lone result row", () => {
    render(<ValidationScoreChart results={[row("2026-05-04T09:00:00Z", 0.82)]} />);

    expect(screen.getByTestId("line-chart")).toBeInTheDocument();
    expect(plotted()).toEqual([{ date: "2026-05-04", score: 0.82 }]);
    expect(screen.getByTestId("line")).toHaveAttribute("data-key", "score");
    expect(screen.queryByText(EMPTY_MESSAGE)).not.toBeInTheDocument();
  });

  it("configures a visible dot and a larger active dot", () => {
    // spec: "each point is drawn with a visible dot and an enlarged active dot,
    // so a series of a single measurement renders as one visible point".
    // The radii themselves are not spec'd — only that a dot is configured (not
    // recharts' `false`/absent) and that the active one is larger. This does
    // constrain the props to the object form `{ r }`, which is how "enlarged" is
    // made observable at all.
    render(<ValidationScoreChart results={[row("2026-05-04T09:00:00Z", 0.82)]} />);

    const line = screen.getByTestId("line");
    const dot = JSON.parse(line.getAttribute("data-dot") as string) as { r?: number } | null;
    const activeDot = JSON.parse(line.getAttribute("data-active-dot") as string) as {
      r?: number;
    } | null;

    expect(dot?.r).toBeGreaterThan(0);
    expect(activeDot?.r).toBeGreaterThan(dot?.r as number);
  });

  it("binds the x-axis to the bucket-label key", () => {
    // The window labels are only useful if the axis actually reads them; an axis
    // bound to any other key is the unusable-axis defect this grain work fixes.
    render(<ValidationScoreChart results={[row("2026-05-04T09:00:00Z", 0.82)]} />);
    expect(screen.getByTestId("x-axis")).toHaveAttribute("data-key", "date");
  });
});

// ── Nothing plottable ──────────────────────────────────────────────────────────

describe("ValidationScoreChart — nothing plottable", () => {
  it("shows the empty-period message when there are no results", () => {
    render(<ValidationScoreChart results={[]} />);
    expect(screen.getByText(EMPTY_MESSAGE)).toBeInTheDocument();
    expect(screen.queryByTestId("line-chart")).not.toBeInTheDocument();
  });

  it("shows the empty-period message when every timestamp is unparseable", () => {
    render(<ValidationScoreChart results={[row("not-a-date", 0.5), row("", 0.6)]} />);
    expect(screen.getByText(EMPTY_MESSAGE)).toBeInTheDocument();
    expect(screen.queryByTestId("line-chart")).not.toBeInTheDocument();
  });

  it("drops only the unparseable rows when some timestamps are valid", () => {
    render(
      <ValidationScoreChart
        results={[row("not-a-date", 0.11), row("2026-05-04T09:00:00Z", 0.82)]}
      />,
    );

    expect(plotted()).toEqual([{ date: "2026-05-04", score: 0.82 }]);
    expect(categories()).not.toContain("—");
    expect(screen.queryByText(EMPTY_MESSAGE)).not.toBeInTheDocument();
  });
});
