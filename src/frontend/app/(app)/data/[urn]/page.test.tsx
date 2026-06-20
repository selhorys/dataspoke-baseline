/**
 * Tests for the unified per-dataset hub page — /data/[urn].
 *
 * Spec: spec/feature/FRONTEND_BASIC.md §Per-dataset page:
 *   - three summary Cards (Ingestion / Validation / MetaGen) reading the
 *     reverse-lookup, validation conf+latest result, and metagen boundary+items,
 *   - four foldable CollapsiblePanels (Ingestion, Validation, MetaGen, Events),
 *     each wrapping the corresponding per-feature body / the unified timeline.
 *
 * The panel BODIES (IngestionDataPanel, ValidationDataPanel, MetagenDataPanel,
 * EventsPanel) are covered by their own component tests; here they are stubbed so
 * the page test asserts COMPOSITION — the three cards and four titled panels —
 * without re-exercising the bodies.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act, fireEvent } from "@testing-library/react";
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
// the page test can assert it is mounted inside its CollapsiblePanel.
vi.mock("@/components/ingestion/ingestion-data-panel", () => ({
  IngestionDataPanel: () =>
    React.createElement("div", { "data-testid": "ingestion-body" }, "ingestion body"),
}));
// The validation body is covered by its own component test; here it is stubbed,
// but the stub reflects the `showDeleted` prop the page threads through so the
// page test can assert the page-level toggle drives the panel. The panel's real
// behavior (removed + OFF → Create empty-state; removed + ON → Undelete) is
// asserted in validation-data-panel.test.tsx; here the stub renders the
// affordance the panel WOULD show for the toggle state, so the page test proves
// the wiring, not the panel internals.
vi.mock("@/components/validation/validation-data-panel", () => ({
  ValidationDataPanel: ({ showDeleted }: { showDeleted: boolean }) =>
    React.createElement(
      "div",
      { "data-testid": "validation-body" },
      showDeleted ? "Undelete" : "Create",
    ),
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
      latest_run: { status: "success" },
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
    // Card titles. There is one of each at card-title level; the panels reuse the
    // same labels, so assert each label appears at least once.
    expect(screen.getAllByText("Ingestion").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("Validation").length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText("MetaGen").length).toBeGreaterThanOrEqual(1);

    // Card-specific populated content proves the summary hooks drive the cards.
    expect(screen.getByText("orders-source")).toBeTruthy(); // ingestion source name
    expect(screen.getByText(/last run success/i)).toBeTruthy(); // latest_run status
    expect(screen.getByText(/1 variable/i)).toBeTruthy(); // validation conf var count
    expect(screen.getByText(/2 candidate items/i)).toBeTruthy(); // metagen item count
  });

  it("renders the four foldable panels with the panel bodies mounted", async () => {
    await renderPage();
    // Four CollapsiblePanel headers (toggle buttons) — one per feature + Events.
    expect(screen.getByRole("button", { name: /ingestion/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /validation/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /metagen/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /events/i })).toBeTruthy();

    // Each panel hosts its stubbed body (open by default).
    expect(screen.getByTestId("ingestion-body")).toBeTruthy();
    expect(screen.getByTestId("validation-body")).toBeTruthy();
    expect(screen.getByTestId("metagen-body")).toBeTruthy();
    expect(screen.getByTestId("events-body")).toBeTruthy();
  });

  it("Ingestion card shows the Unmanaged state when the dataset has no source", async () => {
    mockReverseLookup.mockReturnValue({
      data: { source_id: null },
      isLoading: false,
    });
    await renderPage();
    expect(screen.getByText(/unmanaged/i)).toBeTruthy();
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

// ── Page-level "Show deleted" toggle ─────────────────────────────────────────────
// Spec: spec/feature/FRONTEND_BASIC.md §Per-dataset page (ShowDeletedToggle) +
//       spec/feature/FRONTEND_VALIDATION.md §Detail — the page header carries a
//       page-level "Show deleted" checkbox (default OFF) that gates the deleted
//       validation slot's frozen view. While OFF a removed slot reads as
//       never-created (no leaked config/score); flipping it ON reveals the frozen
//       Undelete state.

describe("DatasetHubPage — Show deleted toggle", () => {
  // Put the page in the removed-slot state so the toggle has an observable effect.
  async function renderRemoved() {
    const { ApiError } = await import("@/lib/api/client");
    const removedError = new ApiError(
      {
        error_code: "VALIDATION_CONF_REMOVED",
        message: "removed",
        trace_id: "t",
        resp_time: "2026-06-19T00:00:00Z",
      },
      404,
    );
    mockValidationConf.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: removedError,
    });
    // A removed slot still has preserved results on the backend; the page must
    // NOT leak that history while the toggle is off.
    mockValidationResults.mockReturnValue({
      data: { results: [{ score: 0.95 }], total_count: 1 },
    });
    await renderPage();
  }

  it("renders the page-level 'Show deleted' checkbox, default OFF", async () => {
    await renderPage();
    const checkbox = screen.getByRole("checkbox", { name: /show deleted/i });
    expect(checkbox).toBeTruthy();
    // Radix surfaces unchecked as aria-checked="false".
    expect(checkbox.getAttribute("aria-checked")).toBe("false");
  });

  it("toggling 'Show deleted' flips the Validation panel between Create (OFF) and Undelete (ON)", async () => {
    await renderRemoved();

    // Default OFF → the removed slot reads as never-created: Create empty-state.
    const validationBody = screen.getByTestId("validation-body");
    expect(validationBody.textContent).toContain("Create");
    expect(validationBody.textContent).not.toContain("Undelete");

    // Flip the page-level toggle ON.
    const checkbox = screen.getByRole("checkbox", { name: /show deleted/i });
    await act(async () => {
      fireEvent.click(checkbox);
    });

    // ON → the frozen Undelete state appears.
    expect(screen.getByTestId("validation-body").textContent).toContain("Undelete");
    expect(screen.getByTestId("validation-body").textContent).not.toContain("Create");
  });

  it("Validation summary card shows No config / No score yet while OFF + removed (no leaked score)", async () => {
    await renderRemoved();

    // While OFF the removed slot must present as a never-created slot — neither
    // the (preserved) config nor the (preserved) latest score may leak.
    expect(screen.getByText(/no config/i)).toBeTruthy();
    expect(screen.getByText(/no score yet/i)).toBeTruthy();
    expect(screen.queryByText(/latest score/i)).toBeNull();
  });
});
