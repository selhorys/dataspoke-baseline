/**
 * Tests for MetagenConfView — read-only conf fields as plain text; schedule_tier
 * links to its backing Airflow DAG (metagen-<tier>).
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import React from "react";
import { MetagenConfView } from "./conf-view";
import type { MetagenConf } from "@/types/metagen";

vi.mock("@/lib/runtime-config", () => ({
  getRuntimeConfig: () => ({
    apiBaseUrl: "",
    datahubUrl: "",
    langfuseUrl: "",
    langfuseProjectId: "",
    airflowUrl: "http://airflow.example.com",
  }),
}));

function makeConf(overrides: Partial<MetagenConf> = {}): MetagenConf {
  return {
    id: "conf-1",
    name: "catalog policy",
    is_enabled: true,
    schedule_tier: "daily",
    dataset_filter: "",
    result_limit: 3,
    overwrite_pending: true,
    dataset_affected_count: 0,
    last_run_at: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-02T00:00:00Z",
    ...overrides,
  };
}

describe("MetagenConfView", () => {
  it("renders the conf fields as plain text", () => {
    render(<MetagenConfView conf={makeConf()} datasetFilter="" />);
    expect(screen.getByText("enabled")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("yes")).toBeInTheDocument();
  });

  it("links schedule_tier to its metagen DAG", () => {
    render(<MetagenConfView conf={makeConf()} datasetFilter="" />);
    expect(screen.getByRole("link", { name: /daily/ })).toHaveAttribute(
      "href",
      "http://airflow.example.com/dags/metagen-daily",
    );
  });

  it("renders an unscheduled conf's schedule_tier as plain text", () => {
    render(
      <MetagenConfView conf={makeConf({ schedule_tier: null })} datasetFilter="" />,
    );
    expect(screen.getByText("manual")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("renders a stored dataset_filter clause verbatim, line breaks kept", () => {
    // spec/feature/FRONTEND_METAGEN.md §Components — MetagenConfView renders
    // `dataset_filter` via DatasetFilterView; spec/feature/FRONTEND_BASIC.md
    // §Shared component notes — "a monospace `<pre>` block preserving the stored
    // line breaks and indentation, an em dash when the filter is empty".
    const clause = "origin = 'DEV'\nAND 'urn:li:tag:area:catalog' IN tag_urns";
    render(
      <MetagenConfView conf={makeConf({ dataset_filter: clause })} datasetFilter={clause} />,
    );

    const field = screen.getByText("dataset_filter").closest("fieldset");
    expect(field).not.toBeNull();
    const block = (field as HTMLElement).querySelector("pre");
    expect(block, "the clause must render in a <pre> block").not.toBeNull();
    // Compared raw: getByText's normalizer would collapse the newline under test.
    expect(block!.textContent).toBe(clause);
    expect(within(field as HTMLElement).queryByText("—")).toBeNull();
  });

  it("shows an em dash for the empty (all-datasets) filter", () => {
    // Backstop for the assertion above — spec/API.md §`dataset_filter` grammar:
    // "empty string = all registered datasets".
    render(<MetagenConfView conf={makeConf()} datasetFilter="" />);

    const field = screen.getByText("dataset_filter").closest("fieldset") as HTMLElement;
    expect(field.querySelector("pre")).toBeNull();
    expect(within(field).getByText("—")).toBeInTheDocument();
  });
});
