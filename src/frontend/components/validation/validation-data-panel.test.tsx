/**
 * Tests for ValidationDataPanel — the validation body of the unified
 * /data/[urn] hub — covering the deleted-state freeze/restore affordances and
 * the page-level "Show deleted" toggle that gates them.
 *
 * Spec: spec/feature/FRONTEND_VALIDATION.md §Detail (moved to /data/[urn]) +
 *       spec/feature/VALIDATION.md §Rule Configuration (freeze + restore):
 *   - A soft-deleted slot returns 404 VALIDATION_CONF_REMOVED. The frozen-rule
 *     view (Undelete + frozen message + preserved charts) is gated by the
 *     page-level `showDeleted` prop:
 *       - showDeleted=false (default) → the slot reads as a never-created slot:
 *         Create empty-state, NO Undelete, and NO leaked deleted-rule history
 *         (no score / variable timeseries charts).
 *       - showDeleted=true → frozen state: Undelete button, frozen message, and
 *         the preserved timeseries charts. Clicking Undelete calls the restore
 *         mutation; the result history is preserved on the backend.
 *   - While off+removed, submitting Create issues a PUT the backend rejects with
 *     409 VALIDATION_CONF_REMOVED; the panel surfaces a targeted inline hint
 *     pointing at the "Show deleted" toggle.
 *   - A never-created slot returns 404 CONFIG_NOT_FOUND — the Create form shows
 *     (the toggle has no effect).
 *   - An active slot (200) is read-only with Edit/Delete (the toggle has no
 *     effect).
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
const mockResults = vi.fn();
const restoreMutate = vi.fn();
const upsertMutate = vi.fn();
const mockRestore = vi.fn();
const mockUpsert = vi.fn();
vi.mock("@/lib/api/validation", () => ({
  useValidationConf: () => mockConf(),
  useUpsertValidationConf: () => mockUpsert(),
  useDeleteValidationConf: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useRestoreValidationConf: () => mockRestore(),
  useValidationResults: () => mockResults(),
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
// assert presence of the form without exercising its field internals. The stub
// echoes the `serverError` prop the panel computes (the panel maps the backend
// 409 VALIDATION_CONF_REMOVED to a targeted hint and passes it here), so the
// 409→hint mapping is asserted at the panel boundary.
vi.mock("@/components/validation/validation-conf-form", () => ({
  ValidationConfForm: ({ serverError }: { serverError?: string }) =>
    React.createElement(
      "form",
      { "data-testid": "conf-form" },
      "conf form",
      serverError
        ? React.createElement("p", { "data-testid": "conf-form-error" }, serverError)
        : null,
    ),
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

async function renderPanel(showDeleted = true) {
  await act(async () => {
    render(<ValidationDataPanel datasetUrn={DATASET_URN} showDeleted={showDeleted} />);
  });
}

beforeEach(() => {
  mockUseMe.mockReset();
  mockConf.mockReset();
  mockResults.mockReset();
  restoreMutate.mockReset();
  upsertMutate.mockReset();
  mockRestore.mockReset();
  mockUpsert.mockReset();
  mockResults.mockReturnValue({ data: { results: [], total_count: 0 } });
  mockRestore.mockReturnValue({ mutate: restoreMutate, isPending: false, error: null });
  mockUpsert.mockReturnValue({ mutate: upsertMutate, isPending: false, error: null });
});

// ── VALIDATION_CONF_REMOVED + Show deleted ON → frozen Undelete state ────────────

describe("ValidationDataPanel — soft-deleted, Show deleted ON", () => {
  it("Editor sees Undelete + frozen message + preserved charts — no Create form, no Edit/Delete", async () => {
    mockUseMe.mockReturnValue({ canWrite: true, isAdmin: false, isEditor: true });
    mockConf.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: makeApiError(404, "VALIDATION_CONF_REMOVED"),
    });
    // Preserved result history of the frozen rule — surfaced only when the toggle
    // is on (the timeseries charts render off this history).
    mockResults.mockReturnValue({
      data: { results: [{ score: 0.9 }], total_count: 1 },
    });
    await renderPanel(true);

    // Frozen-rule view: Undelete only.
    expect(await screen.findByRole("button", { name: /undelete/i })).toBeTruthy();
    expect(screen.queryByTestId("conf-form")).toBeNull();
    expect(screen.queryByRole("button", { name: /^create$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^edit$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^delete$/i })).toBeNull();

    // Frozen message + preserved timeseries history are visible while toggle on.
    expect(screen.getByText(/this validation config is deleted/i)).toBeTruthy();
    expect(screen.getByTestId("score-chart")).toBeTruthy();
    expect(screen.getByTestId("variables-chart")).toBeTruthy();
  });

  it("clicking Undelete fires the restore mutation", async () => {
    mockUseMe.mockReturnValue({ canWrite: true, isAdmin: false, isEditor: true });
    mockConf.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: makeApiError(404, "VALIDATION_CONF_REMOVED"),
    });
    await renderPanel(true);

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
    await renderPanel(true);

    expect(screen.queryByRole("button", { name: /undelete/i })).toBeNull();
    expect(screen.queryByTestId("conf-form")).toBeNull();
    expect(screen.queryByRole("button", { name: /^create$/i })).toBeNull();
  });
});

// ── VALIDATION_CONF_REMOVED + Show deleted OFF (default) → reads as never-created ─

describe("ValidationDataPanel — soft-deleted, Show deleted OFF (default)", () => {
  it("Editor sees the Create empty-state — NO Undelete, NO frozen message, NO charts", async () => {
    mockUseMe.mockReturnValue({ canWrite: true, isAdmin: false, isEditor: true });
    mockConf.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: makeApiError(404, "VALIDATION_CONF_REMOVED"),
    });
    // A removed slot with results in the history: while OFF the history must NOT
    // leak — the panel renders identically to a never-created slot.
    mockResults.mockReturnValue({
      data: { results: [{ score: 0.9 }], total_count: 1 },
    });
    await renderPanel(false);

    // Identical to a never-created slot: Create form + Create button.
    expect(await screen.findByTestId("conf-form")).toBeTruthy();
    expect(screen.getByRole("button", { name: /^create$/i })).toBeTruthy();

    // No frozen-rule affordances or leaked deleted-rule history.
    expect(screen.queryByRole("button", { name: /undelete/i })).toBeNull();
    expect(screen.queryByText(/this validation config is deleted/i)).toBeNull();
    expect(screen.queryByTestId("score-chart")).toBeNull();
    expect(screen.queryByTestId("variables-chart")).toBeNull();
  });

  it("Reader sees the never-created empty state — no Create form, no Undelete, no charts", async () => {
    mockUseMe.mockReturnValue({ canWrite: false, isAdmin: false, isEditor: false });
    mockConf.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: makeApiError(404, "VALIDATION_CONF_REMOVED"),
    });
    mockResults.mockReturnValue({
      data: { results: [{ score: 0.9 }], total_count: 1 },
    });
    await renderPanel(false);

    expect(screen.queryByRole("button", { name: /undelete/i })).toBeNull();
    expect(screen.queryByTestId("conf-form")).toBeNull();
    expect(screen.queryByTestId("score-chart")).toBeNull();
    expect(screen.queryByTestId("variables-chart")).toBeNull();
  });

  it("submitting Create → backend 409 VALIDATION_CONF_REMOVED surfaces the 'Show deleted' hint", async () => {
    mockUseMe.mockReturnValue({ canWrite: true, isAdmin: false, isEditor: true });
    mockConf.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: makeApiError(404, "VALIDATION_CONF_REMOVED"),
    });
    // The Create submit issues a PUT that the backend rejects: a PUT does not
    // resurrect a frozen rule. Mock the upsert mutation as carrying that error.
    mockUpsert.mockReturnValue({
      mutate: upsertMutate,
      isPending: false,
      error: makeApiError(409, "VALIDATION_CONF_REMOVED"),
    });
    await renderPanel(false);

    // The targeted inline hint points the user at the "Show deleted" toggle
    // (copy matched loosely — wording may evolve, the mapping is the invariant).
    expect(await screen.findByText(/enable .*show deleted/i)).toBeTruthy();
    // It must NOT fall back to the generic "error_code: message" rendering.
    expect(screen.queryByText(/VALIDATION_CONF_REMOVED message/)).toBeNull();
  });

  it("submitting Create → non-409 upsert error renders the generic error, NOT the hint", async () => {
    mockUseMe.mockReturnValue({ canWrite: true, isAdmin: false, isEditor: true });
    mockConf.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: makeApiError(404, "VALIDATION_CONF_REMOVED"),
    });
    // A generic, non-409 upsert failure (e.g. validation 422) must NOT be
    // mislabeled as the "Show deleted" hint — that mapping is scoped strictly to
    // 409 VALIDATION_CONF_REMOVED.
    mockUpsert.mockReturnValue({
      mutate: upsertMutate,
      isPending: false,
      error: makeApiError(422, "INVALID_PARAMETER"),
    });
    await renderPanel(false);

    // The Create form is the empty-state surface for an OFF+removed slot; it
    // echoes the generic "error_code: message" fallback the panel passes down.
    expect(await screen.findByText(/INVALID_PARAMETER message/)).toBeTruthy();
    // The targeted hint must be absent (same loose matcher as the positive test).
    expect(screen.queryByText(/enable .*show deleted/i)).toBeNull();
  });

  it("submitting Create → a different 409 error_code renders the generic error, NOT the hint", async () => {
    mockUseMe.mockReturnValue({ canWrite: true, isAdmin: false, isEditor: true });
    mockConf.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: makeApiError(404, "VALIDATION_CONF_REMOVED"),
    });
    // Shares the 409 status with the hint trigger but carries a different
    // error_code — pins that the error_code guard is load-bearing, not just the
    // status (the hint is scoped to 409 *VALIDATION_CONF_REMOVED* specifically).
    mockUpsert.mockReturnValue({
      mutate: upsertMutate,
      isPending: false,
      error: makeApiError(409, "RESOURCE_CONFLICT"),
    });
    await renderPanel(false);

    expect(await screen.findByText(/RESOURCE_CONFLICT message/)).toBeTruthy();
    expect(screen.queryByText(/enable .*show deleted/i)).toBeNull();
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

  it("Editor at showDeleted=false sees the same Create empty-state — the toggle has no effect", async () => {
    mockUseMe.mockReturnValue({ canWrite: true, isAdmin: false, isEditor: true });
    mockConf.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: makeApiError(404, "CONFIG_NOT_FOUND"),
    });
    // The toggle gates only the soft-deleted slot; a never-created slot renders
    // identically with showDeleted off.
    await renderPanel(false);

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

  it("Editor at showDeleted=false still sees Edit + Delete and no Undelete — the toggle has no effect", async () => {
    mockUseMe.mockReturnValue({ canWrite: true, isAdmin: false, isEditor: true });
    mockConf.mockReturnValue({ data: makeConf(), isLoading: false, error: null });
    // An active (200) slot is unaffected by the toggle, which gates only the
    // soft-deleted slot.
    await renderPanel(false);

    expect(await screen.findByRole("button", { name: /^edit$/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /^delete$/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /undelete/i })).toBeNull();
  });
});
