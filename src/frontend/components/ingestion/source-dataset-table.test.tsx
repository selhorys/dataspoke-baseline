/**
 * Tests for SourceDatasetTable — empty state, URN link encoding, origin badge.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_INGESTION.md §Source Detail §Datasets:
 *     source→dataset mapping table; each row links to /ingestion/data/[urn].
 *   - spec/API.md §Ingestion: origin enum: matcher | emitted | pipeline_name.
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { SourceDatasetTable } from "./source-dataset-table";
import type { IngestionSourceDatasetRow } from "@/types/ingestion";

// Mock next/link to a simple anchor so href assertions work in jsdom.
import { vi } from "vitest";
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function makeRow(overrides: Partial<IngestionSourceDatasetRow> = {}): IngestionSourceDatasetRow {
  return {
    dataset_urn:
      "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)",
    origin: "matcher",
    first_seen_at: "2024-01-01T00:00:00Z",
    last_seen_at: "2024-01-10T00:00:00Z",
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// 1. Empty state
// ---------------------------------------------------------------------------
describe("SourceDatasetTable — empty state", () => {
  it("renders the empty-state message when rows is empty", () => {
    render(<SourceDatasetTable rows={[]} />);
    expect(screen.getByText(/this source maps no datasets yet/i)).toBeTruthy();
  });

  it("does not render a table when rows is empty", () => {
    render(<SourceDatasetTable rows={[]} />);
    expect(screen.queryByRole("table")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 2. Row rendering
// ---------------------------------------------------------------------------
describe("SourceDatasetTable — row data", () => {
  it("renders the dataset URN text in each row", () => {
    const urn =
      "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)";
    render(<SourceDatasetTable rows={[makeRow({ dataset_urn: urn })]} />);
    expect(screen.getByText(urn)).toBeTruthy();
  });

  it("renders the origin badge for each origin type", () => {
    const rows: IngestionSourceDatasetRow[] = [
      makeRow({ origin: "matcher", dataset_urn: "urn:a" }),
      makeRow({ origin: "emitted", dataset_urn: "urn:b" }),
      makeRow({ origin: "pipeline_name", dataset_urn: "urn:c" }),
    ];
    render(<SourceDatasetTable rows={rows} />);
    expect(screen.getByText("matcher")).toBeTruthy();
    expect(screen.getByText("emitted")).toBeTruthy();
    expect(screen.getByText("pipeline_name")).toBeTruthy();
  });

  it("renders multiple rows when multiple datasets are provided", () => {
    const rows = [
      makeRow({ dataset_urn: "urn:a", origin: "matcher" }),
      makeRow({ dataset_urn: "urn:b", origin: "emitted" }),
    ];
    render(<SourceDatasetTable rows={rows} />);
    expect(screen.getByText("urn:a")).toBeTruthy();
    expect(screen.getByText("urn:b")).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// 3. URN link encoding — /ingestion/data/[urn] must encode the URN
// ---------------------------------------------------------------------------
describe("SourceDatasetTable — URN link href", () => {
  it("links each row's URN to /ingestion/data/{encodedUrn}", () => {
    const urn =
      "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)";
    render(<SourceDatasetTable rows={[makeRow({ dataset_urn: urn })]} />);
    const link = screen.getByRole("link");
    expect((link as HTMLAnchorElement).href).toContain(
      `/ingestion/data/${encodeURIComponent(urn)}`,
    );
  });

  it("URL-encodes colons and parentheses in DataHub URNs", () => {
    // URNs contain : ( ) which are special URL characters
    const urn = "urn:li:dataset:(urn:li:dataPlatform:kafka,imazon.orders.events,DEV)";
    render(<SourceDatasetTable rows={[makeRow({ dataset_urn: urn })]} />);
    const link = screen.getByRole("link");
    const expectedPath = `/ingestion/data/${encodeURIComponent(urn)}`;
    expect((link as HTMLAnchorElement).href).toContain(expectedPath);
  });
});
