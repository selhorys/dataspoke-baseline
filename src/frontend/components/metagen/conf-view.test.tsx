/**
 * Tests for MetagenConfView — read-only conf fields as plain text; schedule_tier
 * links to its backing Airflow DAG (metagen-<tier>).
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
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
    dataset_filter: {},
    result_limit: 3,
    overwrite_pending: true,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-02T00:00:00Z",
    ...overrides,
  };
}

describe("MetagenConfView", () => {
  it("renders the conf fields as plain text", () => {
    render(<MetagenConfView conf={makeConf()} datasetFilter={{}} />);
    expect(screen.getByText("enabled")).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("yes")).toBeInTheDocument();
  });

  it("links schedule_tier to its metagen DAG", () => {
    render(<MetagenConfView conf={makeConf()} datasetFilter={{}} />);
    expect(screen.getByRole("link", { name: /daily/ })).toHaveAttribute(
      "href",
      "http://airflow.example.com/dags/metagen-daily",
    );
  });

  it("renders an unscheduled conf's schedule_tier as plain text", () => {
    render(
      <MetagenConfView conf={makeConf({ schedule_tier: null })} datasetFilter={{}} />,
    );
    expect(screen.getByText("manual")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});
