/**
 * Tests for ValidationDataPanel — the validation body of the unified
 * /data/[urn] hub.
 *
 * Spec: spec/feature/FRONTEND_VALIDATION.md §Detail (moved to /data/[urn]):
 *   - An absent slot returns 404 CONFIG_NOT_FOUND → the Config empty-state: a
 *     short "No config yet." line plus a `Create` button. The Create form is NOT
 *     auto-mounted; `Create` is a plain button that enters edit state. A Reader
 *     sees the same "No config yet." line with no `Create` button.
 *   - Clicking `Create` mounts the Config form (Cancel + Save); while editing
 *     the Quality Score / Variables timeseries charts are not rendered.
 *   - An existing slot (200) is read-only with Edit/Delete + charts; Delete is a
 *     hard delete. There is no Undelete and no deleted/frozen state to surface.
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
const upsertMutate = vi.fn();
const mockUpsert = vi.fn();
vi.mock("@/lib/api/validation", () => ({
  useValidationConf: () => mockConf(),
  useUpsertValidationConf: () => mockUpsert(),
  useDeleteValidationConf: () => ({ mutate: vi.fn(), isPending: false, error: null }),
  useValidationResults: () => mockResults(),
}));

// RangePicker / charts pull in calendar + recharts internals (ResizeObserver,
// not in jsdom). The conf affordances don't depend on them, so stub.
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
// echoes the `serverError` prop the panel computes so the generic error mapping
// is asserted at the panel boundary.
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

async function renderPanel() {
  await act(async () => {
    render(<ValidationDataPanel datasetUrn={DATASET_URN} />);
  });
}

beforeEach(() => {
  mockUseMe.mockReset();
  mockConf.mockReset();
  mockResults.mockReset();
  upsertMutate.mockReset();
  mockUpsert.mockReset();
  mockResults.mockReturnValue({ data: { results: [], total_count: 0 } });
  mockUpsert.mockReturnValue({ mutate: upsertMutate, isPending: false, error: null });
});

// ── CONFIG_NOT_FOUND → Create form ──────────────────────────────────────────────

describe("ValidationDataPanel — absent slot (CONFIG_NOT_FOUND)", () => {
  it("Editor sees the empty-state (Create button + 'No config yet.'), no auto-mounted form, no charts", async () => {
    mockUseMe.mockReturnValue({ canWrite: true, isAdmin: false, isEditor: true });
    mockConf.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: makeApiError(404, "CONFIG_NOT_FOUND"),
    });
    await renderPanel();

    // Empty-state: the "No config yet." line + a Create button, and NOT the form.
    expect(screen.getByText(/no config yet/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: /^create$/i })).toBeTruthy();
    expect(screen.queryByTestId("conf-form")).toBeNull();
    expect(screen.queryByRole("button", { name: /^edit$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^delete$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^save$/i })).toBeNull();
    expect(screen.queryByTestId("score-chart")).toBeNull();
    expect(screen.queryByTestId("variables-chart")).toBeNull();
  });

  it("clicking Create mounts the Config form (Cancel + Save) and hides the empty-state and charts", async () => {
    mockUseMe.mockReturnValue({ canWrite: true, isAdmin: false, isEditor: true });
    mockConf.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: makeApiError(404, "CONFIG_NOT_FOUND"),
    });
    await renderPanel();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^create$/i }));
    });

    // Now editing: the form is mounted with the Cancel + Save cluster.
    expect(screen.getByTestId("conf-form")).toBeTruthy();
    expect(screen.getByRole("button", { name: /^cancel$/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /^save$/i })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^create$/i })).toBeNull();
    expect(screen.queryByText(/no config yet/i)).toBeNull();
    // Charts stay hidden while editing.
    expect(screen.queryByTestId("score-chart")).toBeNull();
    expect(screen.queryByTestId("variables-chart")).toBeNull();
  });

  it("Reader sees the 'No config yet.' line — no Create button, no form", async () => {
    mockUseMe.mockReturnValue({ canWrite: false, isAdmin: false, isEditor: false });
    mockConf.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: makeApiError(404, "CONFIG_NOT_FOUND"),
    });
    await renderPanel();

    expect(screen.queryByTestId("conf-form")).toBeNull();
    expect(screen.queryByRole("button", { name: /^create$/i })).toBeNull();
    expect(screen.getByText(/no config yet/i)).toBeTruthy();
  });

  it("a non-404 conf failure surfaces the error state", async () => {
    mockUseMe.mockReturnValue({ canWrite: true, isAdmin: false, isEditor: true });
    mockConf.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: makeApiError(500, "INTERNAL_ERROR"),
    });
    await renderPanel();

    expect(screen.getByText(/failed to load validation config/i)).toBeTruthy();
    expect(screen.queryByTestId("conf-form")).toBeNull();
  });

  it("a non-200 upsert error renders the generic error in the Create form", async () => {
    mockUseMe.mockReturnValue({ canWrite: true, isAdmin: false, isEditor: true });
    mockConf.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: makeApiError(404, "CONFIG_NOT_FOUND"),
    });
    mockUpsert.mockReturnValue({
      mutate: upsertMutate,
      isPending: false,
      error: makeApiError(422, "INVALID_PARAMETER"),
    });
    await renderPanel();

    // The form is not auto-mounted; enter edit state via Create, then the
    // panel-computed serverError propagates into the form.
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^create$/i }));
    });

    expect(await screen.findByText(/INVALID_PARAMETER message/)).toBeTruthy();
  });
});

// ── Config heading row ──────────────────────────────────────────────────────────
// spec: FRONTEND_VALIDATION.md §Detail — the conf block leads with a `Config`
// heading (same register as the Quality Score / Variables headings) with the
// Edit/Delete (or Cancel/Save, or Create) cluster on the same row.

describe("ValidationDataPanel — Config heading", () => {
  it("renders a 'Config' heading above the conf when a conf exists", async () => {
    mockUseMe.mockReturnValue({ canWrite: true, isAdmin: false, isEditor: true });
    mockConf.mockReturnValue({ data: makeConf(), isLoading: false, error: null });
    await renderPanel();

    // Exact match so it is not confused with "Quality Score" / "Variables" headings.
    expect(screen.getByRole("heading", { name: /^config$/i })).toBeTruthy();
  });

  it("renders the 'Config' heading in the absent-slot (Create) state too", async () => {
    mockUseMe.mockReturnValue({ canWrite: true, isAdmin: false, isEditor: true });
    mockConf.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: makeApiError(404, "CONFIG_NOT_FOUND"),
    });
    await renderPanel();

    expect(screen.getByRole("heading", { name: /^config$/i })).toBeTruthy();
    // The Create button sits on the same heading row.
    expect(screen.getByRole("button", { name: /^create$/i })).toBeTruthy();
  });
});

// ── Active conf → Edit/Delete, charts present ──────────────────────────────────

describe("ValidationDataPanel — existing conf", () => {
  it("Editor sees Edit + Delete and the read-only view — no Create form", async () => {
    mockUseMe.mockReturnValue({ canWrite: true, isAdmin: false, isEditor: true });
    mockConf.mockReturnValue({ data: makeConf(), isLoading: false, error: null });
    await renderPanel();

    expect(await screen.findByRole("button", { name: /^edit$/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /^delete$/i })).toBeTruthy();
    expect(screen.queryByTestId("conf-form")).toBeNull();
    // Timeseries charts render once a conf exists.
    expect(screen.getByTestId("score-chart")).toBeTruthy();
    expect(screen.getByTestId("variables-chart")).toBeTruthy();
  });

  it("Reader sees neither Edit nor Delete nor a Create form", async () => {
    mockUseMe.mockReturnValue({ canWrite: false, isAdmin: false, isEditor: false });
    mockConf.mockReturnValue({ data: makeConf(), isLoading: false, error: null });
    await renderPanel();

    expect(screen.queryByRole("button", { name: /^edit$/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /^delete$/i })).toBeNull();
    expect(screen.queryByTestId("conf-form")).toBeNull();
  });

  it("clicking Edit on an existing conf mounts the form and hides the charts", async () => {
    // spec: FRONTEND_VALIDATION.md §Detail — the Quality Score / Variables
    // timeseries appear only in the has-conf read-only view; while editing (create
    // OR edit) the panel shows the Config section alone.
    mockUseMe.mockReturnValue({ canWrite: true, isAdmin: false, isEditor: true });
    mockConf.mockReturnValue({ data: makeConf(), isLoading: false, error: null });
    await renderPanel();

    // Read-only view: charts present.
    expect(screen.getByTestId("score-chart")).toBeTruthy();
    expect(screen.getByTestId("variables-chart")).toBeTruthy();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /^edit$/i }));
    });

    // Editing: the form is mounted (Cancel + Save) and the charts are gone.
    expect(screen.getByTestId("conf-form")).toBeTruthy();
    expect(screen.getByRole("button", { name: /^cancel$/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /^save$/i })).toBeTruthy();
    expect(screen.queryByTestId("score-chart")).toBeNull();
    expect(screen.queryByTestId("variables-chart")).toBeNull();
  });
});
