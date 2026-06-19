/**
 * Tests for ValidationDataPanel — the validation body of the unified
 * /data/[urn] hub — covering the deleted-state freeze/restore affordances.
 *
 * Spec: spec/feature/FRONTEND_VALIDATION.md §Detail (moved to /data/[urn]) +
 *       spec/feature/VALIDATION.md §Rule Configuration (freeze + restore):
 *   - A soft-deleted slot returns 404 VALIDATION_CONF_REMOVED — the panel shows
 *     an "Undelete" button only (no Create form, no Edit/Delete). Clicking it
 *     calls the restore mutation; the result history is preserved on the backend.
 *   - A never-created slot returns 404 CONFIG_NOT_FOUND — the Create form shows.
 *   - Reader (canWrite=false) sees no write affordances in either state.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act, fireEvent } from "@testing-library/react";
import React from "react";
import { ValidationDataPanel } from "./validation-data-panel";
import { ApiError } from "@/lib/api/client";
import type { ValidationConfResponse } from "@/types/validation";

// ── Mocks ──────────────────────────────────────────────────────────────────────
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}));

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

const mockUseMe = vi.fn();
vi.mock("@/lib/auth/use-me", () => ({ useMe: () => mockUseMe() }));

const mockConf = vi.fn();
const restoreMutate = vi.fn();
const upsertMutate = vi.fn();
const mockRestore = vi.fn();
const mockUpsert = vi.fn();
vi.mock("@/lib/api/validation", () => ({
  useValidationConf: () => mockConf(),
  useUpsertValidationConf: () => mockUpsert(),
  useDeleteValidationConf: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useRestoreValidationConf: () => mockRestore(),
  useValidationResults: () => ({ data: { results: [], total_count: 0 } }),
}));

// RangePicker / charts pull in calendar + recharts internals (ResizeObserver,
// not in jsdom). The deleted-state affordances don't depend on them, so stub.
vi.mock("@/components/range-picker", () => ({
  RangePicker: () => React.createElement("div", { "data-testid": "range-picker" }),
}));
vi.mock("@/components/validation/validation-score-chart", () => ({
  ValidationScoreChart: () => React.createElement("div", { "data-testid": "score-chart" }),
}));
vi.mock("@/components/validation/validation-variables-chart", () => ({
  ValidationVariablesChart: () =>
    React.createElement("div", { "data-testid": "variables-chart" }),
}));

// The conf form renders a Create/Save submit only in its host; stub it so we can
// assert presence of the form without exercising its field internals.
vi.mock("@/components/validation/validation-conf-form", () => ({
  ValidationConfForm: () =>
    React.createElement("form", { "data-testid": "conf-form" }, "conf form"),
}));

const DATASET_URN =
  "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.orders.daily_fulfillment_summary,DEV)";

function makeApiError(status: number, errorCode: string): ApiError {
  return new ApiError(
    {
      error_code: errorCode,
      message: `${errorCode} message`,
      trace_id: "t-1",
      resp_time: "2026-05-02T00:00:00Z",
    },
    status,
  );
}

function makeConf(): ValidationConfResponse {
  return {
    dataset_urn: DATASET_URN,
    description: "Daily row count check",
    variables: [{ name: "row_cnt", description: "Daily row count" }],
    created_at: "2026-05-01T00:00:00Z",
    updated_at: "2026-05-02T00:00:00Z",
  };
}

async function renderPanel() {
  await act(async () => {
    render(<ValidationDataPanel datasetUrn={DATASET_URN} />);
  });
}

beforeEach(() => {
  mockUseMe.mockReset();
  mockConf.mockReset();
  restoreMutate.mockReset();
  upsertMutate.mockReset();
  mockRestore.mockReset();
  mockUpsert.mockReset();
  mockRestore.mockReturnValue({ mutate: restoreMutate, isPending: false, error: null });
  mockUpsert.mockReturnValue({ mutate: upsertMutate, isPending: false, error: null });
});

// ── VALIDATION_CONF_REMOVED → Undelete-only ─────────────────────────────────────

describe("ValidationDataPanel — soft-deleted (VALIDATION_CONF_REMOVED)", () => {
  it("Editor sees only an Undelete button — no Create form, no Edit/Delete", async () => {
    mockUseMe.mockReturnValue({ canWrite: true, isAdmin: false, isEditor: true });
    mockConf.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: makeApiError(404, "VALIDATION_CONF_REMOVED"),
    });
    await renderPanel();

    expect(await screen.findByRole("button", { name: /undelete/i })).toBeTruthy();
    expect(screen.queryByTestId("conf-form")).toBeNull();
    expect(screen.queryByRole("button", { name: /^create$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^edit$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^delete$/i })).toBeNull();
  });

  it("clicking Undelete fires the restore mutation", async () => {
    mockUseMe.mockReturnValue({ canWrite: true, isAdmin: false, isEditor: true });
    mockConf.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: makeApiError(404, "VALIDATION_CONF_REMOVED"),
    });
    await renderPanel();

    const button = await screen.findByRole("button", { name: /undelete/i });
    fireEvent.click(button);
    expect(restoreMutate).toHaveBeenCalledTimes(1);
    expect(upsertMutate).not.toHaveBeenCalled();
  });

  it("Reader sees no Undelete and no Create form while frozen", async () => {
    mockUseMe.mockReturnValue({ canWrite: false, isAdmin: false, isEditor: false });
    mockConf.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: makeApiError(404, "VALIDATION_CONF_REMOVED"),
    });
    await renderPanel();

    expect(screen.queryByRole("button", { name: /undelete/i })).toBeNull();
    expect(screen.queryByTestId("conf-form")).toBeNull();
    expect(screen.queryByRole("button", { name: /^create$/i })).toBeNull();
  });
});

// ── CONFIG_NOT_FOUND → Create form ──────────────────────────────────────────────

describe("ValidationDataPanel — never created (CONFIG_NOT_FOUND)", () => {
  it("Editor sees the Create form and Create button — no Undelete", async () => {
    mockUseMe.mockReturnValue({ canWrite: true, isAdmin: false, isEditor: true });
    mockConf.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: makeApiError(404, "CONFIG_NOT_FOUND"),
    });
    await renderPanel();

    expect(await screen.findByTestId("conf-form")).toBeTruthy();
    expect(screen.getByRole("button", { name: /^create$/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /undelete/i })).toBeNull();
  });

  it("Reader sees neither the Create form nor an Undelete button", async () => {
    mockUseMe.mockReturnValue({ canWrite: false, isAdmin: false, isEditor: false });
    mockConf.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: makeApiError(404, "CONFIG_NOT_FOUND"),
    });
    await renderPanel();

    expect(screen.queryByTestId("conf-form")).toBeNull();
    expect(screen.queryByRole("button", { name: /^create$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /undelete/i })).toBeNull();
  });
});

// ── Active conf → no Undelete, Edit/Delete present ──────────────────────────────

describe("ValidationDataPanel — active conf", () => {
  it("Editor sees Edit + Delete and no Undelete; an active rule is not restorable", async () => {
    mockUseMe.mockReturnValue({ canWrite: true, isAdmin: false, isEditor: true });
    mockConf.mockReturnValue({ data: makeConf(), isLoading: false, error: null });
    await renderPanel();

    expect(await screen.findByRole("button", { name: /^edit$/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /^delete$/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /undelete/i })).toBeNull();
    expect(screen.queryByTestId("conf-form")).toBeNull();
  });
});
