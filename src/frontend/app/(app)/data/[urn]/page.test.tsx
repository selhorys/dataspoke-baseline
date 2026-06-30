/**
 * Tests for the unified per-dataset hub page — /data/[urn].
 *
 * Spec: spec/feature/FRONTEND_BASIC.md §Per-dataset page:
 *   - three summary Cards (Ingestion / Validation / MetaGen) reading the
 *     reverse-lookup, validation conf+latest result, and metagen boundary+items.
 *     The Ingestion summary card carries the owning-source link plus the
 *     last-run status badge and time (the ingestion reverse-lookup folds into the
 *     card — there is NO separate Ingestion foldable panel).
 *   - three foldable CollapsiblePanels (Validation, MetaGen, Events), each
 *     wrapping the corresponding per-feature body / the unified timeline.
 *
 * The panel BODIES (ValidationDataPanel, MetagenDataPanel, EventsPanel) are
 * covered by their own component tests; here they are stubbed so the page test
 * asserts COMPOSITION — the three cards and three titled panels — without
 * re-exercising the bodies.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act, within } from "@testing-library/react";
import React from "react";
import DatasetHubPage from "./page";

// ── Mocks ──────────────────────────────────────────────────────────────────────
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

// Summary-card hooks.
const mockReverseLookup = vi.fn();
const mockValidationConf = vi.fn();
const mockValidationResults = vi.fn();
const mockMetagenBoundary = vi.fn();
const mockMetagenItems = vi.fn();

vi.mock("@/lib/api/ingestion", () => ({
  useIngestionReverseLookup: () => mockReverseLookup(),
}));
vi.mock("@/lib/api/validation", () => ({
  useValidationConf: () => mockValidationConf(),
  useValidationResults: () => mockValidationResults(),
}));
vi.mock("@/lib/api/metagen", () => ({
  useMetagenBoundary: () => mockMetagenBoundary(),
  useMetagenItems: () => mockMetagenItems(),
}));

// Panel bodies — stubbed (covered by their own tests). Each renders a testid so
// the page test can assert it is mounted inside its CollapsiblePanel. There is
// no Ingestion panel body: the ingestion reverse-lookup is folded into the
// Ingestion summary card, not a CollapsiblePanel.
// The validation body is covered by its own component test; here it is stubbed
// so the page test asserts COMPOSITION, not the panel internals.
vi.mock("@/components/validation/validation-data-panel", () => ({
  ValidationDataPanel: () =>
    React.createElement("div", { "data-testid": "validation-body" }, "validation body"),
}));
vi.mock("@/components/metagen/metagen-data-panel", () => ({
  MetagenDataPanel: () =>
    React.createElement("div", { "data-testid": "metagen-body" }, "metagen body"),
}));
vi.mock("@/components/events-panel", () => ({
  EventsPanel: () =>
    React.createElement("div", { "data-testid": "events-body" }, "events body"),
}));

const DATASET_URN =
  "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)";

async function renderPage() {
  // The page reads `use(params)`; pass the URN already-decoded (server-render
  // shape) so the page does not double-decode.
  const params = Promise.resolve({ urn: DATASET_URN });
  await act(async () => {
    render(
      <React.Suspense fallback={<div data-testid="suspense-fallback" />}>
        <DatasetHubPage params={params} />
      </React.Suspense>,
    );
  });
}

beforeEach(() => {
  mockReverseLookup.mockReset();
  mockValidationConf.mockReset();
  mockValidationResults.mockReset();
  mockMetagenBoundary.mockReset();
  mockMetagenItems.mockReset();

  // Default: everything loaded with data so cards render their populated state.
  mockReverseLookup.mockReturnValue({
    data: {
      source_id: "src-1",
      name: "orders-source",
      mode: "DATAHUB_MANAGED",
      latest_run: { status: "success", occurred_at: "2026-06-20T12:00:00Z" },
    },
    isLoading: false,
  });
  mockValidationConf.mockReturnValue({
    data: {
      dataset_urn: DATASET_URN,
      description: "row count",
      variables: [{ name: "row_cnt", description: "row count" }],
    },
    isLoading: false,
    error: null,
  });
  mockValidationResults.mockReturnValue({
    data: { results: [{ score: 0.9 }], total_count: 1 },
  });
  mockMetagenBoundary.mockReturnValue({
    data: { dataset_urn: DATASET_URN, is_enabled: true },
    isLoading: false,
  });
  mockMetagenItems.mockReturnValue({
    data: { items: [{ id: "i1" }, { id: "i2" }], total_count: 2 },
  });
});

describe("DatasetHubPage — /data/[urn]", () => {
  it("renders the dataset URN in the header", async () => {
    await renderPage();
    expect(screen.getByRole("heading", { name: DATASET_URN })).toBeTruthy();
  });

  it("renders the three summary cards (Ingestion / Validation / MetaGen)", async () => {
    await renderPage();
    // Card titles. Ingestion appears only as the summary-card title now (there is
    // no Ingestion panel); Validation/MetaGen appear as card title + panel header.
    expect(screen.getAllByText("Ingestion").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Validation").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("MetaGen").length).toBeGreaterThanOrEqual(1);

    // Card-specific populated content proves the summary hooks drive the cards.
    expect(screen.getByText("orders-source")).toBeTruthy(); // ingestion source name
    expect(screen.getByText(/last run success/i)).toBeTruthy(); // latest_run status
    expect(screen.getByText(/1 variable/i)).toBeTruthy(); // validation conf var count
    expect(screen.getByText(/2 candidate items/i)).toBeTruthy(); // metagen item count
  });

  it("Ingestion summary card links the source name to its detail and shows the last-run time", async () => {
    // The reverse-lookup folds into the Ingestion card: the source name is a Link
    // to /ingestion/sources/[id] and the latest_run time renders beside the badge.
    // spec: FRONTEND_BASIC.md §Per-dataset page — Ingestion summary card carries
    // last-run time + owning-source link.
    await renderPage();

    const sourceLink = screen.getByRole("link", { name: "orders-source" });
    expect((sourceLink as HTMLAnchorElement).getAttribute("href")).toBe(
      "/ingestion/sources/src-1",
    );
    // latest_run.occurred_at = 2026-06-20T12:00:00Z → formatDateTime renders the
    // date; tz offset can shift only to the adjacent day, never out of month.
    expect(screen.getByText(/2026-06-2[01]/)).toBeTruthy();
  });

  it("renders three foldable panels (Validation / MetaGen / Events) and NO Ingestion panel", async () => {
    await renderPage();
    // Three CollapsiblePanel headers (toggle buttons) — Validation, MetaGen, Events.
    expect(screen.getByRole("button", { name: /validation/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /metagen/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /events/i })).toBeTruthy();

    // The Ingestion reverse-lookup is a summary card, not a panel — there is no
    // CollapsiblePanel toggle button named "Ingestion".
    expect(screen.queryByRole("button", { name: /^ingestion$/i })).toBeNull();

    // Each remaining panel hosts its stubbed body (open by default).
    expect(screen.getByTestId("validation-body")).toBeTruthy();
    expect(screen.getByTestId("metagen-body")).toBeTruthy();
    expect(screen.getByTestId("events-body")).toBeTruthy();
    // No ingestion panel body is mounted.
    expect(screen.queryByTestId("ingestion-body")).toBeNull();
  });

  it("Ingestion card shows the Unmanaged state when the dataset has no source", async () => {
    mockReverseLookup.mockReturnValue({
      data: { source_id: null },
      isLoading: false,
    });
    await renderPage();
    expect(screen.getByText(/unmanaged/i)).toBeTruthy();
  });

  it("Validation card shows the latest result data_time beside the score", async () => {
    // spec: FRONTEND_BASIC.md §Per-dataset page — the Validation summary card
    // shows the latest result's data_time (resultsData.results[0].data_time)
    // formatted with the tz helper, alongside the latest score.
    mockValidationResults.mockReturnValue({
      data: {
        results: [{ score: 0.9, data_time: "2026-05-10T08:00:00Z" }],
        total_count: 1,
      },
    });
    await renderPage();
    // Scope to the score row inside the Validation card: the data_time renders as a
    // sibling of the "Latest score" badge in the same flex row, proving it is shown
    // specifically alongside the score (not merely somewhere on the page).
    const scoreEl = screen.getByText(/latest score/i);
    const scoreRow = scoreEl.closest("div.flex");
    expect(scoreRow).not.toBeNull();
    // formatDateTime renders YYYY-MM-DD; local tz can shift the day by ±1, never
    // out of the month (mirrors the ingestion-card date assertion above).
    expect(
      within(scoreRow as HTMLElement).getByText(/2026-05-(09|10|11)/),
    ).toBeTruthy();
  });

  it("Validation card shows No config / No score when the conf 404s and no results", async () => {
    // 404 conf + no results → "No config" + "No score yet".
    const { ApiError } = await import("@/lib/api/client");
    mockValidationConf.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: new ApiError(
        {
          error_code: "CONFIG_NOT_FOUND",
          message: "not found",
          trace_id: "t",
          resp_time: "2026-06-19T00:00:00Z",
        },
        404,
      ),
    });
    mockValidationResults.mockReturnValue({
      data: { results: [], total_count: 0 },
    });
    await renderPage();
    expect(screen.getByText(/no config/i)).toBeTruthy();
    expect(screen.getByText(/no score yet/i)).toBeTruthy();
  });
});
