/**
 * Tests for the shared <ChartGrainPicker> control.
 *
 * Spec traces (spec/feature/FRONTEND_BASIC.md §Shared Component Notes →
 * ChartGrainPicker):
 *   - "It selects one of three grains — **hourly**, **daily** (default),
 *     **weekly** — governing how the rows a chart has already fetched are
 *     collapsed before plotting."
 *   - "the grain is a client-side display concern and adds no request
 *     parameter" — the control's only output is the onChange callback; it issues
 *     no fetch of its own ("The picker has no API of its own").
 *
 * jsdom limitation: Radix's Select opens through pointer-capture APIs jsdom does
 * not implement, so the options are unreachable with a real click here. Following
 * the established repo pattern (components/pagination.test.tsx,
 * components/ontogen/nodes-panel.test.tsx), `@/components/ui/select` is replaced
 * with a native-ish stub so every option is statically in the DOM and selecting
 * one fires onValueChange.
 *
 * WHAT THE STUB DOES AND DOES NOT PROVE: the option set, their values and labels,
 * the forwarded `value`, and the `onChange` payload are real signal — they come
 * from the component. The ARIA roles queried below (`option`, `combobox`) are
 * hardcoded BY THE STUB, so no assertion here can fail if the real Radix
 * primitives stop exposing them; only the `aria-label` string is the component's
 * own. The real ARIA contract — and the open→click gesture — is proven in a
 * browser by tests/e2e/ground/governance/metric-grain.spec.ts, which selects on
 * exactly `getByRole("combobox", { name: "Chart grain" })` and
 * `getByRole("option", …)`.
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import React from "react";

vi.mock("@/components/ui/select", () => ({
  Select: ({
    value,
    onValueChange,
    children,
  }: {
    value?: string;
    onValueChange?: (v: string) => void;
    children?: React.ReactNode;
  }) =>
    React.createElement(
      "div",
      { "data-testid": "select-root", "data-value": value },
      React.Children.map(children, (child) =>
        React.isValidElement(child)
          ? React.cloneElement(
              child as React.ReactElement<{ onValueChange?: (v: string) => void }>,
              { onValueChange },
            )
          : child,
      ),
    ),
  SelectTrigger: ({
    children,
    ...rest
  }: {
    children?: React.ReactNode;
    "aria-label"?: string;
  }) =>
    React.createElement(
      "button",
      {
        type: "button",
        role: "combobox",
        "aria-label": rest["aria-label"],
        "data-testid": "select-trigger",
      },
      children,
    ),
  SelectValue: ({ children }: { children?: React.ReactNode }) =>
    React.createElement("span", null, children),
  SelectContent: ({
    children,
    onValueChange,
  }: {
    children?: React.ReactNode;
    onValueChange?: (v: string) => void;
  }) =>
    React.createElement(
      "div",
      { "data-testid": "select-content" },
      React.Children.map(children, (child) =>
        React.isValidElement(child)
          ? React.cloneElement(
              child as React.ReactElement<{ onValueChange?: (v: string) => void }>,
              { onValueChange },
            )
          : child,
      ),
    ),
  SelectItem: ({
    value,
    children,
    onValueChange,
  }: {
    value: string;
    children?: React.ReactNode;
    onValueChange?: (v: string) => void;
  }) =>
    React.createElement(
      "button",
      {
        type: "button",
        role: "option",
        "aria-selected": false,
        "data-testid": `grain-${value}`,
        onClick: () => onValueChange?.(value),
      },
      children,
    ),
}));

import { ChartGrainPicker } from "./chart-grain-picker";

describe("ChartGrainPicker — grain options (FRONTEND_BASIC.md §Shared Component Notes)", () => {
  it("offers exactly hourly / daily / weekly", () => {
    render(<ChartGrainPicker value="daily" onChange={() => {}} />);

    expect(screen.getByTestId("grain-hourly")).toBeInTheDocument();
    expect(screen.getByTestId("grain-daily")).toBeInTheDocument();
    expect(screen.getByTestId("grain-weekly")).toBeInTheDocument();
    expect(screen.getAllByRole("option")).toHaveLength(3);
  });

  it("labels the options in human form", () => {
    render(<ChartGrainPicker value="daily" onChange={() => {}} />);

    expect(screen.getByTestId("grain-hourly")).toHaveTextContent("Hourly");
    expect(screen.getByTestId("grain-daily")).toHaveTextContent("Daily");
    expect(screen.getByTestId("grain-weekly")).toHaveTextContent("Weekly");
  });

  it("exposes the control by an accessible name", () => {
    render(<ChartGrainPicker value="daily" onChange={() => {}} />);
    expect(screen.getByRole("combobox", { name: "Chart grain" })).toBeInTheDocument();
  });
});

describe("ChartGrainPicker — selection", () => {
  it("reflects the controlled value", () => {
    const { rerender } = render(<ChartGrainPicker value="daily" onChange={() => {}} />);
    expect(screen.getByTestId("select-root")).toHaveAttribute("data-value", "daily");

    rerender(<ChartGrainPicker value="weekly" onChange={() => {}} />);
    expect(screen.getByTestId("select-root")).toHaveAttribute("data-value", "weekly");
  });

  it.each([
    ["hourly", "grain-hourly"],
    ["daily", "grain-daily"],
    ["weekly", "grain-weekly"],
  ])("calls onChange with %s when that option is chosen", (grain, testId) => {
    const onChange = vi.fn();
    render(<ChartGrainPicker value="daily" onChange={onChange} />);

    fireEvent.click(screen.getByTestId(testId));

    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith(grain);
  });
});

// The spec's "adds no request parameter … The picker has no API of its own" is
// NOT asserted here: this component is presentational and imports nothing
// network-capable, so any such assertion would be unfalsifiable. The claim is
// about the panel OWNERS' reads, and it is exercised where it can fail, once per
// surface the spec names:
//   - per-dataset Validation panel → components/validation/validation-data-panel.test.tsx
//     ("grain adds no request parameter")
//   - governance dashboard card    → components/governance/metric-card.test.tsx
//     ("grain adds no request parameter")
//   - governance metric detail     → tests/e2e/ground/governance/metric-grain.spec.ts,
//     which watches the real `attr/result` requests across two grain switches.
