/**
 * Tests for RangePicker — the standardized time-window control.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §shared-component-notes (RangePicker):
 *     five presets (incl. "Last 7 days"); the popover presents the preset
 *     shortcuts alongside two calendars — a start-day calendar on the left and
 *     an end-day calendar on the right, each with independent month/year
 *     navigation. Every edit in the popover, INCLUDING clicking a preset, is
 *     STAGED and takes effect only on Apply; Cancel discards. Clicking a preset
 *     stages it rather than applying immediately or closing the popover; a
 *     staged preset commits as a relative preset on Apply, while editing a
 *     calendar day commits a custom absolute range.
 *   - lib/range.ts RangeSelection: the control speaks intent — a preset emits
 *     { kind: "preset", days }; a custom edit emits { kind: "custom", from, to }
 *     (inclusive ISO-8601 UTC). Bound math is verified in lib/range.test.ts.
 *
 * Time is frozen (vi.useFakeTimers + setSystemTime) because the staged draft is
 * seeded from the resolved preset window; assertions are UTC-stable.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import React from "react";
import { RangePicker } from "./range-picker";
import type { RangeSelection } from "@/lib/range";

// Fixed "now" — matches lib/range.test.ts so seeded draft windows are deterministic.
const NOW = new Date("2024-03-15T08:30:00.000Z");

// Default selection used across tests (the 2-week preset).
const DEFAULT_SEL: RangeSelection = { kind: "preset", days: 14 };

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
});

afterEach(() => {
  vi.useRealTimers();
});

// ---------------------------------------------------------------------------
// 0. Trigger label reflects the selection intent.
//    Spec: the trigger shows the preset's label when a preset is selected, or
//    the resolved bounds (formatRange) for a custom range. (unchanged)
// ---------------------------------------------------------------------------
describe("RangePicker — trigger label", () => {
  it("shows the preset LABEL (not a date-range string) for a preset selection", () => {
    render(
      <RangePicker
        value={{ kind: "preset", days: 7 }}
        onChange={vi.fn()}
        tz="utc"
        onTzChange={vi.fn()}
        granularity="date"
      />,
    );
    const trigger = screen.getByRole("button");
    expect(trigger).toHaveTextContent("Last 7 days");
    // It must NOT render a resolved YYYY-MM-DD – YYYY-MM-DD window.
    expect(trigger).not.toHaveTextContent(/\d{4}-\d{2}-\d{2}\s+–/);
  });

  it("shows the formatRange string for a custom selection (date granularity)", () => {
    render(
      <RangePicker
        value={{
          kind: "custom",
          from: "2024-03-09T00:00:00.000Z",
          to: "2024-03-15T23:59:59.999Z",
        }}
        onChange={vi.fn()}
        tz="utc"
        onTzChange={vi.fn()}
        granularity="date"
      />,
    );
    // Matches formatRange(...) in lib/range.ts — verified independently there.
    expect(screen.getByRole("button")).toHaveTextContent(
      "2024-03-09 – 2024-03-15 UTC",
    );
  });

  it("shows the formatRange string with HH:mm for a custom datetime selection", () => {
    render(
      <RangePicker
        value={{
          kind: "custom",
          from: "2024-03-14T08:30:00.000Z",
          to: "2024-03-15T08:30:00.000Z",
        }}
        onChange={vi.fn()}
        tz="utc"
        onTzChange={vi.fn()}
        granularity="datetime"
      />,
    );
    expect(screen.getByRole("button")).toHaveTextContent(
      "2024-03-14 08:30 – 2024-03-15 08:30 UTC",
    );
  });
});

// ---------------------------------------------------------------------------
// 1. Preset click STAGES — it does not commit and does not close the popover.
//    Apply is what commits the staged preset as { kind: "preset", days }.
//    Spec: "Clicking a preset stages it rather than applying immediately or
//    closing the popover, and a staged preset still commits as a relative
//    preset on Apply."
// ---------------------------------------------------------------------------
describe("RangePicker — preset staging + Apply", () => {
  it("does NOT commit or close the popover when a preset is clicked", () => {
    const onChange = vi.fn();
    render(
      <RangePicker value={DEFAULT_SEL} onChange={onChange} tz="utc" onTzChange={vi.fn()} granularity="date" />,
    );

    // Open the popover via the trigger (its label is the active preset).
    fireEvent.click(screen.getByText(/last 2 weeks/i));
    // Click the "Last 7 days" preset — this only STAGES it.
    fireEvent.click(screen.getByText(/last 7 days/i));

    // No commit yet…
    expect(onChange).not.toHaveBeenCalled();
    // …and the popover is still open (Apply/Cancel footer present).
    expect(
      screen.getByRole("button", { name: /^apply$/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /cancel/i }),
    ).toBeInTheDocument();
  });

  it("highlights the staged preset (active variant) without committing", () => {
    const onChange = vi.fn();
    render(
      <RangePicker value={DEFAULT_SEL} onChange={onChange} tz="utc" onTzChange={vi.fn()} granularity="date" />,
    );

    fireEvent.click(screen.getByText(/last 2 weeks/i));
    const sevenDays = screen.getByRole("button", { name: /last 7 days/i });
    fireEvent.click(sevenDays);

    // The staged preset uses the "secondary" variant button (active highlight);
    // ghost presets do not. We assert the active styling token is present.
    expect(sevenDays.className).toMatch(/secondary/);
    expect(onChange).not.toHaveBeenCalled();
  });

  it("commits onChange exactly once with the staged preset on Apply (date)", () => {
    const onChange = vi.fn();
    render(
      <RangePicker value={DEFAULT_SEL} onChange={onChange} tz="utc" onTzChange={vi.fn()} granularity="date" />,
    );

    fireEvent.click(screen.getByText(/last 2 weeks/i));
    fireEvent.click(screen.getByText(/last 7 days/i));
    // Apply commits the staged preset.
    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));

    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith({ kind: "preset", days: 7 });
  });

  it("commits the staged preset on Apply in datetime granularity too", () => {
    const onChange = vi.fn();
    render(
      <RangePicker value={DEFAULT_SEL} onChange={onChange} tz="utc" onTzChange={vi.fn()} granularity="datetime" />,
    );

    fireEvent.click(screen.getByText(/last 2 weeks/i));
    fireEvent.click(screen.getByText(/last 7 days/i));
    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));

    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith({ kind: "preset", days: 7 });
  });

  it("commits a CUSTOM range when a time field is edited (time edit unsets the preset)", () => {
    // Editing a time field clears the staged preset (draftDays → null) so the
    // edited time is committed as a custom absolute window on Apply, not silently
    // dropped by the relative preset.
    const onChange = vi.fn();
    render(
      <RangePicker value={DEFAULT_SEL} onChange={onChange} tz="utc" onTzChange={vi.fn()} granularity="datetime" />,
    );

    fireEvent.click(screen.getByText(/last 2 weeks/i));
    fireEvent.click(screen.getByText(/last 7 days/i));
    const endTime = screen.getByLabelText(/end time/i) as HTMLInputElement;
    fireEvent.change(endTime, { target: { value: "06:45" } });
    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));

    expect(onChange).toHaveBeenCalledTimes(1);
    const committed = onChange.mock.calls[0][0] as RangeSelection;
    expect(committed.kind).toBe("custom");
    if (committed.kind === "custom") {
      // The edited end time (06:45 UTC) on the preset's end day is carried into
      // the to-bound.
      expect(committed.to.startsWith("2024-03-15T06:45")).toBe(true);
    }
  });
});

// ---------------------------------------------------------------------------
// 2. Popover layout — two independent calendars with month/year navigation.
//    Spec: "two calendars — a start-day calendar on the left and an end-day
//    calendar on the right, each with independent month and year navigation."
// ---------------------------------------------------------------------------
describe("RangePicker — two-calendar popover layout", () => {
  it("renders two month grids (left start + right end calendars)", () => {
    render(
      <RangePicker value={DEFAULT_SEL} onChange={vi.fn()} tz="utc" onTzChange={vi.fn()} granularity="date" />,
    );
    fireEvent.click(screen.getByText(/last 2 weeks/i));

    // react-day-picker renders each calendar's month table as role="grid".
    expect(screen.getAllByRole("grid")).toHaveLength(2);
  });

  it("renders independent month/year dropdowns per calendar (dropdown captionLayout)", () => {
    render(
      <RangePicker value={DEFAULT_SEL} onChange={vi.fn()} tz="utc" onTzChange={vi.fn()} granularity="date" />,
    );
    fireEvent.click(screen.getByText(/last 2 weeks/i));

    // captionLayout="dropdown" renders a month <select> + year <select> per
    // calendar → 2 calendars × 2 = 4 comboboxes, giving each calendar its own
    // month and year navigation.
    expect(screen.getAllByRole("combobox")).toHaveLength(4);
  });
});

// ---------------------------------------------------------------------------
// 3. Cancel discards a staged edit — onChange is never called.
// ---------------------------------------------------------------------------
describe("RangePicker — Cancel discards staged edits", () => {
  it("does NOT call onChange after a staged time edit followed by Cancel", () => {
    const onChange = vi.fn();
    render(
      <RangePicker value={DEFAULT_SEL} onChange={onChange} tz="utc" onTzChange={vi.fn()} granularity="datetime" />,
    );

    // Open the popover (seeds the draft from the resolved selection).
    fireEvent.click(screen.getByText(/last 2 weeks/i));

    // Stage a change: edit the start-time input. In datetime granularity the
    // popover renders "Start time"/"End time" inputs bound to staged state.
    const startTime = screen.getByLabelText(/start time/i) as HTMLInputElement;
    fireEvent.change(startTime, { target: { value: "03:15" } });

    // Cancel discards the staged edit.
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));

    expect(onChange).not.toHaveBeenCalled();
  });

  it("does NOT call onChange after staging a preset followed by Cancel", () => {
    const onChange = vi.fn();
    render(
      <RangePicker value={DEFAULT_SEL} onChange={onChange} tz="utc" onTzChange={vi.fn()} granularity="date" />,
    );

    fireEvent.click(screen.getByText(/last 2 weeks/i));
    fireEvent.click(screen.getByText(/last 7 days/i));
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));

    expect(onChange).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// 4. Apply commits a CUSTOM selection after a calendar DAY is clicked.
//
// Clicking a day clears the staged preset (draftDays → null) so Apply emits a
// custom absolute range. react-day-picker exposes each day as a button with an
// accessible name like "Saturday, March 9th, 2024"; the seeded month is the
// resolved preset window under the frozen clock (left=start, right=end).
// ---------------------------------------------------------------------------
describe("RangePicker — Apply commits a custom selection after a day click", () => {
  it("emits { kind: 'custom' } when a calendar day is clicked then Apply (date)", () => {
    const onChange = vi.fn();
    render(
      <RangePicker value={DEFAULT_SEL} onChange={onChange} tz="utc" onTzChange={vi.fn()} granularity="date" />,
    );

    fireEvent.click(screen.getByText(/last 2 weeks/i));

    // Click a concrete start day in the LEFT calendar. Both calendars show
    // March 2024 under the frozen clock, so "March 11th" exists in both grids;
    // scope to the first grid (left = start) to disambiguate.
    const [leftGrid] = screen.getAllByRole("grid");
    fireEvent.click(
      within(leftGrid).getByRole("button", { name: /Monday, March 11th, 2024/i }),
    );

    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));

    expect(onChange).toHaveBeenCalledTimes(1);
    const committed = onChange.mock.calls[0][0] as RangeSelection;
    expect(committed.kind).toBe("custom");
    if (committed.kind === "custom") {
      // Day click sets the start (from) bound to the clicked UTC day at 00:00.
      expect(committed.from.startsWith("2024-03-11T00:00")).toBe(true);
    }
  });

  it("commits a custom range that round-trips the edited end time under default-local tz", () => {
    // The "time edit unsets preset" assertion above runs under tz="utc" so the
    // ISO tail is deterministic. Here we exercise the DEFAULT tz="local" path
    // with an offset-AGNOSTIC check: editing the end time then Apply must emit a
    // custom range whose `to`, read back as a LOCAL wall-clock value (the same
    // interpretation composeIso used to build it), shows the edited minute. This
    // holds in any host timezone, including offset-0 CI.
    const onChange = vi.fn();
    render(
      <RangePicker value={DEFAULT_SEL} onChange={onChange} tz="local" onTzChange={vi.fn()} granularity="datetime" />,
    );

    fireEvent.click(screen.getByText(/last 2 weeks/i));
    const endTime = screen.getByLabelText(/end time/i) as HTMLInputElement;
    fireEvent.change(endTime, { target: { value: "06:45" } });
    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));

    expect(onChange).toHaveBeenCalledTimes(1);
    const committed = onChange.mock.calls[0][0] as RangeSelection;
    expect(committed.kind).toBe("custom");
    if (committed.kind === "custom") {
      const toLocal = new Date(committed.to);
      // Local wall-clock readback of the emitted bound preserves 06:45.
      expect(toLocal.getHours()).toBe(6);
      expect(toLocal.getMinutes()).toBe(45);
    }
  });

  it("custom commit carries the user-edited end time after a day click (datetime)", () => {
    const onChange = vi.fn();
    render(
      <RangePicker value={DEFAULT_SEL} onChange={onChange} tz="utc" onTzChange={vi.fn()} granularity="datetime" />,
    );

    fireEvent.click(screen.getByText(/last 2 weeks/i));

    // Click a NEW end day in the RIGHT calendar (clears the staged preset →
    // custom). We pick a day OTHER than the currently-selected end day
    // (2024-03-15), because re-clicking the selected day in rdp single mode
    // toggles it off (onSelect(undefined)) and would not clear the staged
    // preset. "March 20th" is unselected, exists in both calendars, so scope to
    // the second grid (right = end) → onSelect sets draftTo to 2024-03-20.
    const [, rightGrid] = screen.getAllByRole("grid");
    fireEvent.click(
      within(rightGrid).getByRole("button", {
        name: /Wednesday, March 20th, 2024/i,
      }),
    );

    const endTime = screen.getByLabelText(/end time/i) as HTMLInputElement;
    fireEvent.change(endTime, { target: { value: "06:45" } });

    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));

    expect(onChange).toHaveBeenCalledTimes(1);
    const committed = onChange.mock.calls[0][0] as RangeSelection;
    expect(committed.kind).toBe("custom");
    if (committed.kind === "custom") {
      // The edited end time (06:45) on the clicked end day (2024-03-20) is
      // carried into the to-bound. We pin date+time prefix, not composeIso's
      // seconds/ms tail (impl detail, covered by lib/range tests).
      expect(committed.to.startsWith("2024-03-20T06:45")).toBe(true);
    }
  });
});

// ---------------------------------------------------------------------------
// 5. Local | UTC timezone toggle in the footer.
//    Spec: "A per-picker timezone toggle — Local or UTC (default Local) —
//    governs how calendar days and times are interpreted and displayed." The
//    toggle calls onTzChange and the active zone is reflected by the active
//    (secondary) button variant.
// ---------------------------------------------------------------------------
describe("RangePicker — Local | UTC toggle", () => {
  it("renders both Local and UTC toggle buttons in the popover footer", () => {
    render(
      <RangePicker value={DEFAULT_SEL} onChange={vi.fn()} tz="local" onTzChange={vi.fn()} granularity="date" />,
    );
    fireEvent.click(screen.getByText(/last 2 weeks/i));

    expect(screen.getByRole("button", { name: /^local$/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^utc$/i })).toBeInTheDocument();
  });

  it("marks the active zone with the secondary variant (Local active by default)", () => {
    render(
      <RangePicker value={DEFAULT_SEL} onChange={vi.fn()} tz="local" onTzChange={vi.fn()} granularity="date" />,
    );
    fireEvent.click(screen.getByText(/last 2 weeks/i));

    const local = screen.getByRole("button", { name: /^local$/i });
    const utc = screen.getByRole("button", { name: /^utc$/i });
    // Active zone uses the "secondary" variant token; the inactive one does not.
    expect(local.className).toMatch(/secondary/);
    expect(utc.className).not.toMatch(/secondary/);
  });

  it("marks UTC active when tz='utc'", () => {
    render(
      <RangePicker value={DEFAULT_SEL} onChange={vi.fn()} tz="utc" onTzChange={vi.fn()} granularity="date" />,
    );
    fireEvent.click(screen.getByText(/last 2 weeks/i));

    const local = screen.getByRole("button", { name: /^local$/i });
    const utc = screen.getByRole("button", { name: /^utc$/i });
    expect(utc.className).toMatch(/secondary/);
    expect(local.className).not.toMatch(/secondary/);
  });

  it("calls onTzChange with the other mode when the inactive toggle is clicked", () => {
    const onTzChange = vi.fn();
    render(
      <RangePicker value={DEFAULT_SEL} onChange={vi.fn()} tz="local" onTzChange={onTzChange} granularity="date" />,
    );
    fireEvent.click(screen.getByText(/last 2 weeks/i));

    // Currently Local — clicking UTC switches the zone.
    fireEvent.click(screen.getByRole("button", { name: /^utc$/i }));

    expect(onTzChange).toHaveBeenCalledTimes(1);
    expect(onTzChange).toHaveBeenCalledWith("utc");
  });

  it("does NOT call onTzChange when the already-active toggle is clicked", () => {
    const onTzChange = vi.fn();
    render(
      <RangePicker value={DEFAULT_SEL} onChange={vi.fn()} tz="utc" onTzChange={onTzChange} granularity="date" />,
    );
    fireEvent.click(screen.getByText(/last 2 weeks/i));

    // Already UTC — clicking UTC is a no-op (handleTzChange early-returns).
    fireEvent.click(screen.getByRole("button", { name: /^utc$/i }));

    expect(onTzChange).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// 6. fixedWeeks — both calendars render a constant six-week grid so the popover
//    height does not jump between months.
//    Spec: two calendars (start | end). The `fixedWeeks` prop on each Calendar
//    pads every month to 6 week rows. react-day-picker exposes each week as
//    role="row" within the month grid.
// ---------------------------------------------------------------------------
describe("RangePicker — fixedWeeks six-row calendars", () => {
  it("renders two grids each with six week rows (fixed height)", () => {
    render(
      <RangePicker value={DEFAULT_SEL} onChange={vi.fn()} tz="utc" onTzChange={vi.fn()} granularity="date" />,
    );
    fireEvent.click(screen.getByText(/last 2 weeks/i));

    const grids = screen.getAllByRole("grid");
    expect(grids).toHaveLength(2);

    for (const grid of grids) {
      // rdp marks the weekday header as role="row" too, so the week rows are the
      // rows that contain day gridcells. Filter to those.
      const rows = within(grid).getAllByRole("row");
      const weekRows = rows.filter(
        (row) => within(row).queryAllByRole("gridcell").length > 0,
      );
      expect(weekRows).toHaveLength(6);
    }
  });
});
