/**
 * Tests for OntogenConfView — read-only conf fields as plain text; schedule_tier
 * links to its backing Airflow DAG (ontogen-<tier>); default_run_prompt renders
 * preformatted (em dash when empty).
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { OntogenConfView } from "./conf-view";
import type { OntogenConf } from "@/types/ontogen";

vi.mock("@/lib/runtime-config", () => ({
  getRuntimeConfig: () => ({
    apiBaseUrl: "",
    datahubUrl: "",
    langfuseUrl: "",
    langfuseProjectId: "",
    airflowUrl: "http://airflow.example.com",
  }),
}));

function makeConf(overrides: Partial<OntogenConf> = {}): OntogenConf {
  return {
    is_enabled: true,
    schedule_tier: "weekly",
    dataset_filter: {},
    default_run_prompt: "infer the ontology",
    updated_at: "2026-01-02T00:00:00Z",
    ...overrides,
  };
}

describe("OntogenConfView", () => {
  it("links schedule_tier to its ontogen DAG and shows the run prompt", () => {
    render(<OntogenConfView conf={makeConf()} datasetFilter={{}} />);
    expect(screen.getByRole("link", { name: /weekly/ })).toHaveAttribute(
      "href",
      "http://airflow.example.com/dags/ontogen-weekly",
    );
    expect(screen.getByText("infer the ontology")).toBeInTheDocument();
    expect(screen.getByText("enabled")).toBeInTheDocument();
  });

  it("renders an em dash for an empty default_run_prompt and plain manual tier", () => {
    render(
      <OntogenConfView
        conf={makeConf({ schedule_tier: null, default_run_prompt: null })}
        datasetFilter={{}}
      />,
    );
    expect(screen.getByText("manual")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    // dataset_filter (4 dims) + default_run_prompt all empty → 5 em dashes.
    expect(screen.getAllByText("—")).toHaveLength(5);
  });
});
