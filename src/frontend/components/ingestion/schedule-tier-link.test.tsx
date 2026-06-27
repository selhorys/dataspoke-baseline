/**
 * Tests for ScheduleTierLink — schedule tier renders as a link to its backing
 * Airflow DAG for the scheduled tiers, and as plain text otherwise.
 *
 * The DAG id mirrors src/workflows/registry.py: `ingestion-active-<tier>`.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { ScheduleTierLink } from "./schedule-tier-link";

const airflowUrl = { value: "http://airflow.example.com" };

vi.mock("@/lib/runtime-config", () => ({
  getRuntimeConfig: () => ({
    apiBaseUrl: "",
    datahubUrl: "",
    langfuseUrl: "",
    langfuseProjectId: "",
    airflowUrl: airflowUrl.value,
  }),
}));

beforeEach(() => {
  airflowUrl.value = "http://airflow.example.com";
});

describe("ScheduleTierLink", () => {
  it("links a daily schedule to its Airflow DAG", () => {
    render(<ScheduleTierLink schedule="@daily" />);
    const link = screen.getByRole("link", { name: /daily/ });
    expect(link).toHaveAttribute(
      "href",
      "http://airflow.example.com/dags/ingestion-active-daily",
    );
  });

  it("links a weekly schedule to its Airflow DAG", () => {
    render(<ScheduleTierLink schedule="0 0 * * 0" />);
    expect(screen.getByRole("link", { name: /weekly/ })).toHaveAttribute(
      "href",
      "http://airflow.example.com/dags/ingestion-active-weekly",
    );
  });

  it("renders manual (no DAG) as plain text", () => {
    render(<ScheduleTierLink schedule={null} />);
    expect(screen.getByText("manual")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("renders a non-canonical cron as plain custom text", () => {
    render(<ScheduleTierLink schedule="*/5 * * * *" />);
    expect(screen.getByText("custom")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("falls back to plain text when no Airflow URL is configured", () => {
    airflowUrl.value = "";
    render(<ScheduleTierLink schedule="@daily" />);
    expect(screen.getByText("daily")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});
