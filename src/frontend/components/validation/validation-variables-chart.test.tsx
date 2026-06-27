/**
 * Tests for ValidationVariablesChart — small-multiples rendering.
 *
 * Spec: spec/feature/FRONTEND_VALIDATION.md §Page contracts:
 *   "small multiples — one auto-scaled line chart per declared variable in a
 *    single full-width column (one chart per row), each captioned with the
 *    variable's name and description".
 *
 * recharts is mocked to a lightweight stub so the grid structure and per-chart
 * captions can be asserted without ResponsiveContainer DOM measurement.
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ValidationVariablesChart } from "./validation-variables-chart";
import type { ValidationResultRow, ValidationVariable } from "@/types/validation";

// Mock recharts: each LineChart renders a marker element so we can count charts.
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
    XAxis: () => null,
    YAxis: () => null,
    Tooltip: () => null,
  };
});

function row(dataTime: string, variables: Record<string, number>): ValidationResultRow {
  return { data_time: dataTime, score: 1, variables };
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
