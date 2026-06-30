/**
 * Tests for the Dataset catalog page — /governance/datasets.
 *
 * Spec: spec/feature/FRONTEND_GOVERNANCE.md §Datasets — a cross-feature list of
 *   every registered dataset, consuming GET /spoke/common/data. Columns:
 *     dataset_urn  → /data/[urn]
 *     datahub      → external DataHub deep-link (gated on runtime datahubUrl)
 *     ingestion    → one label per covering source; label text = platform, linked to
 *                    /ingestion/sources/[id], with a mode badge; em-dash when none
 *     validation   → "Covered" / "Uncovered" badge from validation.covered
 *     metagen      → matching conf names → /metagen/conf/[id], or em-dash
 *   Shared offset/limit Pagination over `total_count`.
 * Spec: spec/API.md §Data Resource — GET /spoke/common/data row shape
 *   ({dataset_urn, ingestion: [{source_id,name,mode,platform}], validation: {covered},
 *    metagen: [{conf_id,name}]}).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import React from "react";
import GovernanceDatasetsPage from "./page";
import type { DatasetListItem } from "@/types/dataset";

// ── Mocks ──────────────────────────────────────────────────────────────────────
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

const mockUseDatasetList = vi.fn();
vi.mock("@/lib/api/datasets", () => ({
  useDatasetList: (params: { offset?: number; limit?: number }) =>
    mockUseDatasetList(params),
}));

// DatahubDatasetLink reads getRuntimeConfig().datahubUrl; control it per describe.
const mockGetRuntimeConfig = vi.fn();
vi.mock("@/lib/runtime-config", () => ({
  getRuntimeConfig: () => mockGetRuntimeConfig(),
}));

const COVERED_ROW: DatasetListItem = {
  dataset_urn:
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)",
  ingestion: [
    { source_id: "src-9", name: "catalog-source", mode: "DATAHUB_MANAGED", platform: "postgres" },
  ],
  validation: { covered: true },
  metagen: [{ conf_id: "conf-1", name: "Catalog descriptions" }],
};

const UNMANAGED_ROW: DatasetListItem = {
  dataset_urn:
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)",
  ingestion: [],
  validation: { covered: false },
  metagen: [],
};

function setData(rows: DatasetListItem[], total = rows.length): void {
  mockUseDatasetList.mockReturnValue({
    data: { offset: 0, limit: 20, total_count: total, datasets: rows },
    isLoading: false,
    error: null,
  });
}

async function renderPage(): Promise<void> {
  await act(async () => {
    render(<GovernanceDatasetsPage />);
  });
}

beforeEach(() => {
  mockUseDatasetList.mockReset();
  mockGetRuntimeConfig.mockReset();
  mockGetRuntimeConfig.mockReturnValue({ datahubUrl: "http://datahub.example.com" });
  setData([COVERED_ROW, UNMANAGED_ROW]);
});

// ── Columns + links ─────────────────────────────────────────────────────────────

describe("GovernanceDatasetsPage — columns and links", () => {
  it("renders the five column headers including validation", async () => {
    await renderPage();
    expect(screen.getByRole("columnheader", { name: "dataset_urn" })).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "datahub" })).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "ingestion" })).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "validation" })).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "metagen" })).toBeTruthy();
  });

  it("links the dataset_urn to its per-dataset hub /data/[urn]", async () => {
    await renderPage();
    const urnLink = screen.getByRole("link", { name: COVERED_ROW.dataset_urn });
    expect(urnLink.getAttribute("href")).toBe(
      `/data/${encodeURIComponent(COVERED_ROW.dataset_urn)}`,
    );
  });

  it("renders the ingestion source as a platform-labelled link to /ingestion/sources/[id] with a mode badge", async () => {
    await renderPage();
    // The ingestion column label text is the source platform ("postgres"), NOT the
    // source name — spec: FRONTEND_GOVERNANCE §Datasets — ingestion type label.
    const sourceLink = screen.getByRole("link", { name: "postgres" });
    expect(sourceLink.getAttribute("href")).toBe("/ingestion/sources/src-9");
    // The source name is not used as the label text.
    expect(screen.queryByRole("link", { name: "catalog-source" })).toBeNull();
    // modeLabel(DATAHUB_MANAGED) === "DataHub-managed" rendered as the adjacent badge.
    expect(screen.getByText("DataHub-managed")).toBeTruthy();
  });

  it("renders Covered/Uncovered validation badges from validation.covered", async () => {
    await renderPage();
    // COVERED_ROW.validation.covered === true → "Covered"; UNMANAGED_ROW false →
    // "Uncovered". spec: FRONTEND_GOVERNANCE §Datasets — validation coverage column.
    expect(screen.getByText("Covered")).toBeTruthy();
    expect(screen.getByText("Uncovered")).toBeTruthy();
  });

  it("links each matching metagen conf to /metagen/conf/[id]", async () => {
    await renderPage();
    const confLink = screen.getByRole("link", { name: "Catalog descriptions" });
    expect(confLink.getAttribute("href")).toBe("/metagen/conf/conf-1");
  });

  it("renders a DataHub deep-link per row when datahubUrl is configured", async () => {
    await renderPage();
    const datahubLinks = screen.getAllByRole("link", { name: /datahub/i });
    // One per row (2 rows).
    expect(datahubLinks.length).toBe(2);
    expect(datahubLinks[0].getAttribute("href")).toBe(
      `http://datahub.example.com/dataset/${encodeURIComponent(COVERED_ROW.dataset_urn)}`,
    );
  });
});

// ── Em-dash for missing coverage ────────────────────────────────────────────────

describe("GovernanceDatasetsPage — null/empty coverage", () => {
  it("renders an em-dash for empty ingestion and empty metagen, Uncovered for validation", async () => {
    setData([UNMANAGED_ROW]);
    await renderPage();
    // ingestion [] + metagen [] → two em-dash cells for this row.
    const emDashes = screen.getAllByText("—");
    expect(emDashes.length).toBeGreaterThanOrEqual(2);
    // validation.covered false → an "Uncovered" badge (not an em-dash).
    expect(screen.getByText("Uncovered")).toBeTruthy();
    // No source/platform links for an unmanaged dataset.
    expect(screen.queryByRole("link", { name: "postgres" })).toBeNull();
  });

  it("falls back to an em-dash in the datahub column when datahubUrl is unset", async () => {
    mockGetRuntimeConfig.mockReturnValue({ datahubUrl: "" });
    setData([COVERED_ROW]);
    await renderPage();
    expect(screen.queryByRole("link", { name: /datahub/i })).toBeNull();
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(1);
  });

  it("shows an empty-state row when there are no registered datasets", async () => {
    setData([], 0);
    await renderPage();
    expect(screen.getByText(/no registered datasets found/i)).toBeTruthy();
  });
});

// ── Pagination wiring ───────────────────────────────────────────────────────────

describe("GovernanceDatasetsPage — pagination", () => {
  it("shows the total_count in the page-range label and advances offset on Next", async () => {
    // total_count = 42 (> one page of 20) so a second page exists.
    setData([COVERED_ROW, UNMANAGED_ROW], 42);
    await renderPage();

    expect(screen.getByText(/of 42/)).toBeTruthy();

    const next = screen.getByRole("button", { name: /next/i });
    expect((next as HTMLButtonElement).disabled).toBe(false);
    await act(async () => {
      fireEvent.click(next);
    });
    // The page re-fetches with the advanced offset (= limit = 20).
    const lastParams = mockUseDatasetList.mock.calls.at(-1)?.[0] as {
      offset?: number;
    };
    expect(lastParams.offset).toBe(20);
  });
});
