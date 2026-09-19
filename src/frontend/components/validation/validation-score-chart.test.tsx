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
 * spec: spec/feature/FRONTEND_VALIDATION.md §Page contracts — the Quality Score
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

import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { ValidationScoreChart } from "./validation-score-chart";
import type { ValidationResultRow } from "@/types/validation";
import type { GrainPoint } from "@/lib/chart-grain";
import type { TooltipContentProps } from "recharts";

vi.mock("@/lib/preferences/timezone", () => ({ useDisplayTz: () => "utc" }));

// Captures the `content` render-function recharts' real <Tooltip> is given, so
// the tests below can invoke it directly with hand-built payload props — the
// stub can't surface a function value via a data-* attribute the way it does
// for the other plain-data props, so this module-scoped variable stands in.
// Must be prefixed "mock" — Vitest's `vi.mock` factory hoisting only allows
// referencing outer bindings whose name starts with "mock".
let mockTooltipContent: ((props: TooltipContentProps) => React.ReactNode) | null = null;
let mockScoreDot: ((props: { payload: GrainPoint; cx?: number; cy?: number }) => React.ReactNode) | null = null;
let mockActiveScoreDot: ((props: { payload: GrainPoint; cx?: number; cy?: number }) => React.ReactNode) | null = null;
let mockScoreTickFormatter: ((value: number) => string) | null = null;

vi.mock("recharts", () => {
  const Passthrough = ({ children }: { children?: React.ReactNode }) => (
    <div>{children}</div>
  );
  return {
    ResponsiveContainer: Passthrough,
    LineChart: ({
      children,
      data,
      margin,
    }: {
      children?: React.ReactNode;
      data?: { date: string }[];
      margin?: { top?: number };
    }) => (
      <div
        data-testid="line-chart"
        data-categories={JSON.stringify((data ?? []).map((d) => d.date))}
        data-points={JSON.stringify(data ?? [])}
        data-margin-top={String(margin?.top ?? "")}
      >
        {children}
      </div>
    ),
    Line: ({
      dataKey,
      type,
      stroke,
      dot,
      activeDot,
    }: {
      dataKey?: string;
      type?: string;
      stroke?: string;
      dot?: ((props: { payload: GrainPoint; cx?: number; cy?: number }) => React.ReactNode) | { r?: number } | boolean;
      activeDot?: ((props: { payload: GrainPoint; cx?: number; cy?: number }) => React.ReactNode) | { r?: number } | boolean;
    }) => {
      mockScoreDot = typeof dot === "function" ? dot : null;
      mockActiveScoreDot = typeof activeDot === "function" ? activeDot : null;
      return (
        <div
          data-testid="line"
          data-key={String(dataKey)}
          data-type={String(type)}
          data-stroke={String(stroke)}
          data-dot={JSON.stringify(typeof dot === "function" ? null : dot ?? null)}
          data-active-dot={JSON.stringify(typeof activeDot === "function" ? null : activeDot ?? null)}
        />
      );
    },
    CartesianGrid: () => null,
    XAxis: ({ dataKey, type, scale, tickFormatter, ticks }: { dataKey?: string; type?: string; scale?: string; tickFormatter?: (value: number) => string; ticks?: number[] }) => {
      mockScoreTickFormatter = tickFormatter ?? null;
      return <div data-testid="x-axis" data-key={String(dataKey)} data-type={String(type)} data-scale={String(scale)} data-tick-label={tickFormatter?.(ticks?.[0] ?? 0) ?? ""} />;
    },
    YAxis: ({ domain, padding }: { domain?: [number, number]; padding?: { top?: number; bottom?: number } }) => (
      <div data-testid="y-axis" data-domain={JSON.stringify(domain ?? null)} data-padding={JSON.stringify(padding ?? null)} />
    ),
    Tooltip: ({
      content,
    }: {
      content?: (props: TooltipContentProps) => React.ReactNode;
    }) => {
      mockTooltipContent = content ?? null;
      return null;
    },
  };
});

beforeEach(() => {
  mockTooltipContent = null;
  mockScoreDot = null;
  mockActiveScoreDot = null;
  mockScoreTickFormatter = null;
});

// ── Fixtures (inline, readable) ─────────────────────────────────────────────────

function row(
  dataTime: string,
  score: number,
  scoreNote: string | null = null,
): ValidationResultRow {
  return { data_time: dataTime, score, variables: { row_cnt: 1 }, score_note: scoreNote };
}

function categories(): string[] {
  return JSON.parse(
    screen.getByTestId("line-chart").getAttribute("data-categories") as string,
  ) as string[];
}

function plotted(): { date: string; timestamp: number; score?: number; score_note?: string }[] {
  return JSON.parse(
    screen.getByTestId("line-chart").getAttribute("data-points") as string,
  ) as { date: string; timestamp: number; score?: number; score_note?: string }[];
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
    // score_note's null-to-empty-string normalization is asserted deliberately,
    // once, in the tooltip describe block below — this test is about grain
    // collapse, so it only pins date/score.
    expect(plotted()).toMatchObject([{ date: "2026-05-04", score: 0.9 }]);
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
    // score_note's null-to-empty-string normalization is asserted deliberately,
    // once, in the tooltip describe block below — this test is about the
    // single-window plot, so it only pins date/score.
    expect(plotted()).toMatchObject([{ date: "2026-05-04", score: 0.82 }]);
    expect(screen.getByTestId("line")).toHaveAttribute("data-key", "score");
    expect(screen.queryByText(EMPTY_MESSAGE)).not.toBeInTheDocument();
  });

  it("colors visible dots by the 1.0 threshold and keeps an enlarged active dot", () => {
    // spec: "each point is drawn with a visible dot and an enlarged active dot,
    // so a series of a single measurement renders as one visible point".
    // The radii themselves are not spec'd — only that a dot is configured (not
    // recharts' `false`/absent) and that the active one is larger. This does
    // constrain the props to the object form `{ r }`, which is how "enlarged" is
    // made observable at all.
    render(<ValidationScoreChart results={[row("2026-05-04T09:00:00Z", 1), row("2026-05-05T09:00:00Z", 0.82)]} />);

    const line = screen.getByTestId("line");
    expect(line).toHaveAttribute("data-stroke", "#3f3f46");
    expect(line).toHaveAttribute("data-type", "linear");
    expect(screen.getByTestId("y-axis")).toHaveAttribute("data-domain", "[0,1]");
    expect(screen.getByTestId("y-axis")).toHaveAttribute("data-padding", '{"top":12,"bottom":0}');
    expect(screen.getByTestId("line-chart")).toHaveAttribute("data-margin-top", "16");
    expect(mockScoreDot).not.toBeNull();
    expect(mockActiveScoreDot).not.toBeNull();
    const passing = mockScoreDot!({ payload: plotted()[0] as GrainPoint, cx: 10, cy: 10 }) as React.ReactElement<{ fill: string; r: number }>;
    const failing = mockScoreDot!({ payload: plotted()[1] as GrainPoint, cx: 10, cy: 10 }) as React.ReactElement<{ fill: string; r: number }>;
    const activePassing = mockActiveScoreDot!({ payload: plotted()[0] as GrainPoint, cx: 10, cy: 10 }) as React.ReactElement<{ fill: string; r: number }>;
    const activeFailing = mockActiveScoreDot!({ payload: plotted()[1] as GrainPoint, cx: 10, cy: 10 }) as React.ReactElement<{ fill: string; r: number }>;
    expect(passing.props.fill).toBe("#15803d");
    expect(failing.props.fill).toBe("#f472b6");
    expect(activePassing.props).toMatchObject({ fill: "#15803d", r: 7 });
    expect(activeFailing.props).toMatchObject({ fill: "#f472b6", r: 7 });
    expect(activePassing.props.r).toBeGreaterThan(passing.props.r);
    expect(activeFailing.props.r).toBeGreaterThan(failing.props.r);
  });

  it("formats hourly and weekly real-time ticks with their grain labels", () => {
    const results = [
      row("2026-05-04T09:15:00Z", 1),
      row("2026-05-06T09:30:00Z", 0.8),
    ];
    const hourly = render(<ValidationScoreChart results={results} grain="hourly" />);
    const hourlyPoint = plotted()[0];
    expect(mockScoreTickFormatter?.(hourlyPoint.timestamp)).toBe("2026-05-04 09:00");
    hourly.unmount();

    render(<ValidationScoreChart results={results} grain="weekly" />);
    const weeklyPoint = plotted()[0];
    expect(mockScoreTickFormatter?.(weeklyPoint.timestamp)).toBe("2026-05-04");
  });

  it("uses the retained measurement timestamp as a real-time x-axis while formatting grain labels", () => {
    render(<ValidationScoreChart results={[row("2026-05-04T09:00:00Z", 0.82)]} />);
    const axis = screen.getByTestId("x-axis");
    expect(axis).toHaveAttribute("data-key", "timestamp");
    expect(axis).toHaveAttribute("data-type", "number");
    expect(axis).toHaveAttribute("data-scale", "time");
    expect(axis).toHaveAttribute("data-tick-label", "2026-05-04");
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

    // score_note's null-to-empty-string normalization is asserted deliberately,
    // once, in the tooltip describe block below — this test is about dropping
    // unparseable rows, so it only pins date/score.
    expect(plotted()).toMatchObject([{ date: "2026-05-04", score: 0.82 }]);
    expect(categories()).not.toContain("—");
    expect(screen.queryByText(EMPTY_MESSAGE)).not.toBeInTheDocument();
  });
});

// ── Quality Score tooltip ────────────────────────────────────────────────────────
// spec: spec/feature/FRONTEND_VALIDATION.md §Page contracts — "Hovering a point
//   on the Quality Score chart shows score_note in the tooltip when the
//   underlying (grain-collapsed) result carries one."
// The accessibility-attribute cases below are not spec-anchored: they pin the
// tooltip's parity with Recharts' own `DefaultTooltipContent` (announcing its
// content via `role="status"`/`aria-live` when Recharts' `accessibilityLayer`
// is enabled), a requirement established during this change's backend+frontend
// review rather than a distinct spec line.

describe("ValidationScoreChart — quality score tooltip", () => {
  it("shows the note text when the hovered point carries a score_note", () => {
    render(
      <ValidationScoreChart
        results={[row("2026-05-04T09:00:00Z", 0.4, "breached 1/5: var_03")]}
      />,
    );
    const point = plotted()[0];
    expect(mockTooltipContent).not.toBeNull();

    const { container } = render(
      mockTooltipContent!({
        active: true,
        payload: [{ payload: point }],
        label: point.date,
      } as unknown as TooltipContentProps) as React.ReactElement,
    );

    expect(within(container).getByText("breached 1/5: var_03")).toBeInTheDocument();
    expect(within(container).getByText(/Date: 2026-05-04/)).toBeInTheDocument();
    expect(within(container).getByText(/score: 0\.4000/)).toBeInTheDocument();
  });

  it("wires the tooltip's grain label to the chart's own grain prop, not a hardcoded default", () => {
    // At the default `daily` grain the label is always "Date: …", so a
    // hardcoded "Date" literal (or a call reading DEFAULT_CHART_GRAIN instead
    // of the `grain` prop) would pass every other case here undetected. Only a
    // non-default grain, exercised end-to-end (chart prop → tooltip label),
    // proves the wiring is real.
    render(
      <ValidationScoreChart
        results={[row("2026-05-04T09:00:00Z", 0.4)]}
        grain="hourly"
      />,
    );
    const point = plotted()[0];
    expect(mockTooltipContent).not.toBeNull();

    const { container } = render(
      mockTooltipContent!({
        active: true,
        payload: [{ payload: point }],
        label: point.date,
      } as unknown as TooltipContentProps) as React.ReactElement,
    );

    expect(within(container).getByText("Hour: 2026-05-04 09:00")).toBeInTheDocument();
  });

  it("renders the grain label and score with no note paragraph when score_note is null", () => {
    render(<ValidationScoreChart results={[row("2026-05-04T09:00:00Z", 0.4)]} />);
    const point = plotted()[0];
    expect(mockTooltipContent).not.toBeNull();
    // Deliberate, single assertion of the null-to-empty-string normalization
    // `valuesOf` applies (chart-grain.ts's own doc comment on display-only
    // string annotations) — the three grain/plotting tests above only pin
    // date/score and leave this detail to this test.
    expect(point.score_note).toBe("");

    const { container } = render(
      mockTooltipContent!({
        active: true,
        payload: [{ payload: point }],
        label: point.date,
      } as unknown as TooltipContentProps) as React.ReactElement,
    );

    expect(within(container).getByText(/Date: 2026-05-04/)).toBeInTheDocument();
    expect(within(container).getByText(/score: 0\.4000/)).toBeInTheDocument();
    // Exactly two lines (label + score) — no third, note paragraph.
    expect(container.querySelectorAll("p")).toHaveLength(2);
  });

  it("renders nothing when inactive or when Recharts supplies no payload entry", () => {
    render(<ValidationScoreChart results={[row("2026-05-04T09:00:00Z", 0.4)]} />);
    const point = plotted()[0];
    expect(mockTooltipContent).not.toBeNull();

    expect(
      mockTooltipContent!({
        active: false,
        payload: [{ payload: point }],
        label: point.date,
      } as unknown as TooltipContentProps),
    ).toBeNull();
    expect(
      mockTooltipContent!({
        active: true,
        payload: undefined,
        label: point.date,
      } as unknown as TooltipContentProps),
    ).toBeNull();
    // Recharts genuinely calls `content` with `active: true` and an EMPTY
    // `payload` array during hover transitions — the impl's third guard
    // clause (`payload.length === 0`) is what stops `payload[0].payload` from
    // being read off `undefined` and crashing the chart on hover.
    expect(
      mockTooltipContent!({
        active: true,
        payload: [],
        label: point.date,
      } as unknown as TooltipContentProps),
    ).toBeNull();
  });

  it("carries role=status and aria-live=assertive only when accessibilityLayer is set", () => {
    render(<ValidationScoreChart results={[row("2026-05-04T09:00:00Z", 0.4)]} />);
    const point = plotted()[0];
    expect(mockTooltipContent).not.toBeNull();

    const accessible = render(
      mockTooltipContent!({
        active: true,
        payload: [{ payload: point }],
        label: point.date,
        accessibilityLayer: true,
      } as unknown as TooltipContentProps) as React.ReactElement,
    );
    const accessibleWrapper = accessible.container.firstElementChild as HTMLElement;
    expect(accessibleWrapper).toHaveAttribute("role", "status");
    expect(accessibleWrapper).toHaveAttribute("aria-live", "assertive");

    const plain = render(
      mockTooltipContent!({
        active: true,
        payload: [{ payload: point }],
        label: point.date,
      } as unknown as TooltipContentProps) as React.ReactElement,
    );
    const plainWrapper = plain.container.firstElementChild as HTMLElement;
    expect(plainWrapper).not.toHaveAttribute("role");
    expect(plainWrapper).not.toHaveAttribute("aria-live");
  });

  it("memoizes the tooltip content function by grain — stable across an unrelated rerender, recomputed when grain changes", () => {
    // The component's own comment: a fresh inline arrow every render would
    // make React unmount/remount the tooltip subtree on every render
    // (this page polls every 15s), including mid-hover. Passing every one of
    // the tooltip tests above requires only that SOME content function be
    // supplied each render — none of them compare identity across renders, so
    // none would catch a regression to an unmemoized inline arrow.
    const results = [row("2026-05-04T09:00:00Z", 0.4)];
    const { rerender } = render(<ValidationScoreChart results={results} grain="daily" />);
    const first = mockTooltipContent;
    expect(first).not.toBeNull();

    // Same grain, same props — the memoized function must be the identical
    // reference.
    rerender(<ValidationScoreChart results={results} grain="daily" />);
    expect(mockTooltipContent).toBe(first);

    // Changed grain — the `[grain]` dependency must invalidate the memo, or
    // grainTooltipLabel(grain) inside the closure would go stale.
    rerender(<ValidationScoreChart results={results} grain="hourly" />);
    expect(mockTooltipContent).not.toBe(first);
  });
});
