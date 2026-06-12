/**
 * Tests for UnmanagedDatasetTable — empty state and URN link encoding.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_INGESTION.md §Unmanaged View:
 *     paginated table of DataHub datasets covered by no source.
 *     Each row links to /ingestion/data/[urn].
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import React from "react";
import { UnmanagedDatasetTable } from "./unmanaged-dataset-table";

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

// ---------------------------------------------------------------------------
// 1. Empty state
// ---------------------------------------------------------------------------
describe("UnmanagedDatasetTable — empty state", () => {
  it("renders the empty-state message when no URNs are provided", () => {
    render(<UnmanagedDatasetTable datasetUrns={[]} />);
    expect(
      screen.getByText(/every dataset is covered by a source/i),
    ).toBeTruthy();
  });

  it("does not render a table when the list is empty", () => {
    render(<UnmanagedDatasetTable datasetUrns={[]} />);
    expect(screen.queryByRole("table")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 2. Row rendering
// ---------------------------------------------------------------------------
describe("UnmanagedDatasetTable — row data", () => {
  it("renders the URN text for each row", () => {
    const urns = [
      "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)",
      "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.orders.daily_fulfillment_summary,DEV)",
    ];
    render(<UnmanagedDatasetTable datasetUrns={urns} />);
    urns.forEach((urn) => expect(screen.getByText(urn)).toBeTruthy());
  });

  it("renders as many rows as URNs provided", () => {
    const urns = ["urn:a", "urn:b", "urn:c"];
    render(<UnmanagedDatasetTable datasetUrns={urns} />);
    expect(screen.getAllByRole("row").length - 1).toBe(urns.length); // subtract header row
  });
});

// ---------------------------------------------------------------------------
// 3. URN link href encoding
// ---------------------------------------------------------------------------
describe("UnmanagedDatasetTable — URN link href", () => {
  it("links each URN to /ingestion/data/{encodedUrn}", () => {
    const urn =
      "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)";
    render(<UnmanagedDatasetTable datasetUrns={[urn]} />);
    const link = screen.getByRole("link");
    expect((link as HTMLAnchorElement).href).toContain(
      `/ingestion/data/${encodeURIComponent(urn)}`,
    );
  });

  it("URL-encodes colons and parentheses in the URN path segment", () => {
    const urn = "urn:li:dataset:(urn:li:dataPlatform:kafka,imazon.orders.events,DEV)";
    render(<UnmanagedDatasetTable datasetUrns={[urn]} />);
    const link = screen.getByRole("link");
    expect((link as HTMLAnchorElement).href).toContain(
      `/ingestion/data/${encodeURIComponent(urn)}`,
    );
  });
});
