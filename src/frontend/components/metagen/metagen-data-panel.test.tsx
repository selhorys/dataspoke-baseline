/**
 * Tests for MetagenDataPanel — the MetaGen body of the unified /data/[urn] hub,
 * focused on the boundary section's mode-driven header-right action cluster
 * (mirroring Validation).
 *
 * Spec: spec/feature/FRONTEND_METAGEN.md §Per-dataset (moved to /data/[urn]) —
 * the boundary action controls live in a header-right cluster, mode-driven:
 *   - no boundary (null)        → "No config yet." empty-state + a "Create"
 *     button only; the BoundaryForm is NOT auto-mounted.
 *   - clicking "Create" enters edit state → BoundaryForm + "Save" + "Cancel";
 *     the Generated Items section is hidden while editing.
 *   - boundary exists, read     → "Edit" + "Delete"; two outlined group boxes
 *     titled `is_enabled` and `allowed`, laid out horizontally.
 *   - boundary exists, edit     → "Cancel" + "Save"; editable form body.
 *   - "Save" is type=submit bound to the form via form= so it submits the
 *     BoundaryForm and drives the upsert; on success it returns to read mode.
 *   - write controls are gated on canWrite (Editor/Admin); a reader sees none.
 */
import { describe, it, expect, vi, beforeEach, beforeAll } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import React from "react";
import { MetagenDataPanel } from "./metagen-data-panel";
import type { MetagenBoundary, MetagenBoundaryPutBody } from "@/types/metagen";

// Radix Checkbox (inside the real BoundaryForm) depends on ResizeObserver,
// which jsdom does not provide.
beforeAll(() => {
  if (!globalThis.ResizeObserver) {
    globalThis.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
  }
});

// ── Mocks ──────────────────────────────────────────────────────────────────────
const mockUseMe = vi.fn();
vi.mock("@/lib/auth/use-me", () => ({ useMe: () => mockUseMe() }));

const mockToast = vi.fn();
vi.mock("@/components/ui/use-toast", () => ({ useToast: () => ({ toast: mockToast }) }));

const mockBoundary = vi.fn();
const mockItems = vi.fn();
const mockUpsert = vi.fn();
const mockDelete = vi.fn();
const upsertMutate = vi.fn();
const deleteMutate = vi.fn();
vi.mock("@/lib/api/metagen", () => ({
  useMetagenBoundary: () => mockBoundary(),
  useMetagenItems: () => mockItems(),
  useUpsertMetagenBoundary: () => mockUpsert(),
  useDeleteMetagenBoundary: () => mockDelete(),
}));

// ItemKindTable / ConfirmDialog are out of scope for the boundary cluster; stub them.
vi.mock("@/components/metagen/item-kind-table", () => ({
  ItemKindTable: () => React.createElement("div", { "data-testid": "item-kind-table" }),
}));
vi.mock("@/components/confirm-dialog", () => ({
  ConfirmDialog: () => React.createElement("div", { "data-testid": "confirm-dialog" }),
}));

const DATASET_URN =
  "urn:li:dataset:(urn:li:dataPlatform:hive,example_db.catalog.t,PROD)";

function makeBoundary(overrides: Partial<MetagenBoundary> = {}): MetagenBoundary {
  return {
    dataset_urn: DATASET_URN,
    is_enabled: true,
    allowed: ["dataset.description"],
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  mockUseMe.mockReset();
  mockBoundary.mockReset();
  mockItems.mockReset();
  mockUpsert.mockReset();
  mockDelete.mockReset();
  upsertMutate.mockReset();
  deleteMutate.mockReset();
  mockUseMe.mockReturnValue({ canWrite: true, isAdmin: false, isEditor: true });
  mockItems.mockReturnValue({ data: { items: [] }, isLoading: false });
  mockUpsert.mockReturnValue({ mutate: upsertMutate, isPending: false });
  mockDelete.mockReturnValue({ mutate: deleteMutate, isPending: false });
});

// ── Section titles ──────────────────────────────────────────────────────────────
describe("MetagenDataPanel — section titles", () => {
  it("renders the 'Boundary Config' and 'Generated Items' section headings", () => {
    // spec: FRONTEND_BASIC.md §Per-dataset page / FRONTEND_METAGEN §Per-dataset —
    // attr/metagen/boundary → "Boundary Config"; attr/metagen/item → "Generated Items".
    mockBoundary.mockReturnValue({ data: makeBoundary(), isLoading: false });
    render(<MetagenDataPanel datasetUrn={DATASET_URN} />);

    expect(screen.getByRole("heading", { name: "Boundary Config" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Generated Items" })).toBeTruthy();
  });
});

// ── No boundary (null) → empty-state + Create only; form not auto-mounted ────────
describe("MetagenDataPanel — no boundary", () => {
  it("shows the 'No config yet.' empty-state + Create only (no Edit/Cancel/Delete/Save), form not mounted", () => {
    mockBoundary.mockReturnValue({ data: null, isLoading: false });
    const { container } = render(<MetagenDataPanel datasetUrn={DATASET_URN} />);

    expect(screen.getByText(/no config yet/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: /^create$/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^edit$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^cancel$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^delete$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^save$/i })).toBeNull();
    // The BoundaryForm is NOT auto-mounted — its is_enabled checkbox is absent.
    expect(container.querySelector("#boundary-is-enabled")).toBeNull();
  });

  it("clicking Create mounts the BoundaryForm (Save + Cancel) and hides Generated Items", () => {
    mockBoundary.mockReturnValue({ data: null, isLoading: false });
    const { container } = render(<MetagenDataPanel datasetUrn={DATASET_URN} />);

    fireEvent.click(screen.getByRole("button", { name: /^create$/i }));

    // Now editing: the form body (is_enabled checkbox) + Save/Cancel cluster appear.
    expect(container.querySelector("#boundary-is-enabled")).toBeTruthy();
    expect(screen.getByRole("button", { name: /^save$/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /^cancel$/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^create$/i })).toBeNull();
    expect(screen.queryByText(/no config yet/i)).toBeNull();
    // Generated Items is hidden while editing the boundary.
    expect(screen.queryByRole("heading", { name: "Generated Items" })).toBeNull();
  });
});

// ── Boundary exists, read mode → Edit + Delete; two group boxes ──────────────────
describe("MetagenDataPanel — boundary exists (read mode)", () => {
  it("shows Edit + Delete (no Save/Cancel) and the two outlined group boxes", () => {
    mockBoundary.mockReturnValue({ data: makeBoundary(), isLoading: false });
    const { container } = render(<MetagenDataPanel datasetUrn={DATASET_URN} />);

    expect(screen.getByRole("button", { name: /^edit$/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /^delete$/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^save$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^cancel$/i })).toBeNull();
    // Read-only: no editable form field (the is_enabled checkbox only exists in edit/create).
    expect(container.querySelector("#boundary-is-enabled")).toBeNull();
    // Two outlined group boxes titled `is_enabled` and `allowed` (legends).
    const legends = Array.from(container.querySelectorAll("legend")).map((l) =>
      l.textContent,
    );
    expect(legends).toContain("is_enabled");
    expect(legends).toContain("allowed");
    // The `allowed` box surfaces the configured aspect as a badge.
    expect(screen.getByText("dataset.description")).toBeTruthy();
  });
});

// ── Edit mode → Cancel + Save; editable body; Generated Items hidden ─────────────
describe("MetagenDataPanel — boundary exists, edit mode", () => {
  it("clicking Edit swaps the cluster to Cancel + Save, shows the form, hides Generated Items", () => {
    mockBoundary.mockReturnValue({ data: makeBoundary(), isLoading: false });
    render(<MetagenDataPanel datasetUrn={DATASET_URN} />);

    fireEvent.click(screen.getByRole("button", { name: /^edit$/i }));

    expect(screen.getByRole("button", { name: /^cancel$/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /^save$/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^edit$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^delete$/i })).toBeNull();
    // Editable form body: the allowed checkbox is now present and seeded.
    const allowedCheckbox = screen.getByLabelText("dataset.description") as HTMLInputElement;
    expect(allowedCheckbox).toBeTruthy();
    // Generated Items is hidden while editing the boundary.
    expect(screen.queryByRole("heading", { name: "Generated Items" })).toBeNull();
  });

  it("clicking Save submits the form → upsert mutation with the boundary body", async () => {
    mockBoundary.mockReturnValue({ data: makeBoundary(), isLoading: false });
    // On success, the panel calls the mutation's onSuccess (returning to read mode).
    upsertMutate.mockImplementation((_body, opts) => opts?.onSuccess?.());
    render(<MetagenDataPanel datasetUrn={DATASET_URN} />);

    fireEvent.click(screen.getByRole("button", { name: /^edit$/i }));
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(upsertMutate).toHaveBeenCalledTimes(1));
    const body = upsertMutate.mock.calls[0][0] as MetagenBoundaryPutBody;
    expect(body).toMatchObject({
      is_enabled: true,
      allowed: ["dataset.description"],
    });

    // On success the panel exits edit mode → read-only cluster (Edit/Delete) returns.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^edit$/i })).toBeTruthy(),
    );
    expect(screen.queryByRole("button", { name: /^save$/i })).toBeNull();
  });

  it("a failed Save fires a destructive error toast and stays in edit mode", async () => {
    // spec: FRONTEND_METAGEN.md §Per-dataset — a boundary PUT failure surfaces an
    // error and leaves the user in the editable form to retry.
    mockBoundary.mockReturnValue({ data: makeBoundary(), isLoading: false });
    // The mutation invokes onError with an Error-shaped rejection.
    upsertMutate.mockImplementation((_body, opts) =>
      opts?.onError?.(new Error("boundary write rejected")),
    );
    render(<MetagenDataPanel datasetUrn={DATASET_URN} />);

    fireEvent.click(screen.getByRole("button", { name: /^edit$/i }));
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(upsertMutate).toHaveBeenCalledTimes(1));

    // Destructive error toast surfacing the server message. The title copy is
    // not spec-mandated, so assert only the variant + propagated message.
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({
        description: "boundary write rejected",
        variant: "destructive",
      }),
    );

    // The panel stays in edit mode (Cancel + Save remain; no read-mode Edit).
    expect(screen.getByRole("button", { name: /^save$/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /^cancel$/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^edit$/i })).toBeNull();
  });
});

// ── Reader (canWrite=false) → no write affordances ──────────────────────────────
describe("MetagenDataPanel — reader", () => {
  it("renders none of Edit/Save/Delete/Cancel", () => {
    mockUseMe.mockReturnValue({ canWrite: false, isAdmin: false, isEditor: false });
    mockBoundary.mockReturnValue({ data: makeBoundary(), isLoading: false });
    render(<MetagenDataPanel datasetUrn={DATASET_URN} />);

    expect(screen.queryByRole("button", { name: /^edit$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^save$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^delete$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^cancel$/i })).toBeNull();
  });
});
