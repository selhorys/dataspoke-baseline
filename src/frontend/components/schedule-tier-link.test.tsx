/**
 * Tests for ScheduleTierLink + scheduleDagId — a schedule tier renders as a link
 * to its backing Airflow DAG when a dagId is supplied, and as plain text
 * otherwise.
 *
 * DAG ids mirror src/workflows/registry.py: `ingestion-active-<tier>`,
 * `metagen-<tier>`, `ontogen-<tier>`.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { ScheduleTierLink, scheduleDagId } from "./schedule-tier-link";

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

describe("scheduleDagId", () => {
  it("builds the DAG id for linkable tiers across feature prefixes", () => {
    expect(scheduleDagId("ingestion-active", "daily")).toBe("ingestion-active-daily");
    expect(scheduleDagId("metagen", "hourly")).toBe("metagen-hourly");
    expect(scheduleDagId("ontogen", "weekly")).toBe("ontogen-weekly");
  });

  it("returns null for unscheduled tiers", () => {
    expect(scheduleDagId("metagen", "manual")).toBeNull();
    expect(scheduleDagId("ingestion-active", "custom")).toBeNull();
    expect(scheduleDagId("ontogen", null)).toBeNull();
    expect(scheduleDagId("metagen", undefined)).toBeNull();
  });
});

describe("ScheduleTierLink", () => {
  it("links a daily tier to its Airflow DAG", () => {
    render(<ScheduleTierLink tier="daily" dagId="ingestion-active-daily" />);
    const link = screen.getByRole("link", { name: /daily/ });
    expect(link).toHaveAttribute(
      "href",
      "http://airflow.example.com/dags/ingestion-active-daily",
    );
  });

  it("links each feature's DAG prefix", () => {
    const { rerender } = render(
      <ScheduleTierLink tier="weekly" dagId="metagen-weekly" />,
    );
    expect(screen.getByRole("link", { name: /weekly/ })).toHaveAttribute(
      "href",
      "http://airflow.example.com/dags/metagen-weekly",
    );

    rerender(<ScheduleTierLink tier="hourly" dagId="ontogen-hourly" />);
    expect(screen.getByRole("link", { name: /hourly/ })).toHaveAttribute(
      "href",
      "http://airflow.example.com/dags/ontogen-hourly",
    );
  });

  it("renders a null dagId (manual) as plain text", () => {
    render(<ScheduleTierLink tier="manual" dagId={null} />);
    expect(screen.getByText("manual")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("renders an unscheduled custom tier as plain text", () => {
    render(<ScheduleTierLink tier="custom" dagId={null} />);
    expect(screen.getByText("custom")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });

  it("falls back to plain text when no Airflow URL is configured", () => {
    airflowUrl.value = "";
    render(<ScheduleTierLink tier="daily" dagId="ingestion-active-daily" />);
    expect(screen.getByText("daily")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});
