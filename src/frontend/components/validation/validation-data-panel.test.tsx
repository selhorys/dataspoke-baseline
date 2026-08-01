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
 *
 * Spec: spec/feature/FRONTEND_BASIC.md §shared-component-notes (RangePicker):
 *   "**A preset resolves to an open-ended window** — the lower bound only, with
 *   `to`/`until` omitted — so the read always reaches the present" and "the
 *   validation `attr/validation/result` endpoint — which names its end-bound
 *   `until` rather than `to` … — takes the upper bound in that slot instead."
 *   This panel is the ONLY surface where the end-bound param is renamed, and the
 *   spec calls out a behavior change here (future-dated `data_time` rows become
 *   visible), so the resolved window it hands the results query is asserted
 *   directly rather than only through the source scan in
 *   lib/range.import-boundary.test.ts.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act, cleanup, fireEvent } from "@testing-library/react";
import React from "react";
import { ValidationDataPanel } from "./validation-data-panel";
import { ApiError } from "@/lib/api/client";
import { RANGE_KEYS } from "@/lib/hooks/use-range-selection";
import { GRAIN_KEYS } from "@/lib/hooks/use-grain-selection";
import { useTimezoneStore } from "@/lib/preferences/timezone";
import { DEFAULT_PRESET_DAYS, presetRange } from "@/lib/range";
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
  // Args are forwarded (not swallowed) so the resolved window the panel hands
  // the results query is observable — see the "resolved result window" block.
  useValidationResults: (...args: unknown[]) => mockResults(...args),
}));

// RangePicker / charts pull in calendar + recharts internals (ResizeObserver,
// not in jsdom). The conf affordances don't depend on them, so stub.
vi.mock("@/components/range-picker", () => ({
  RangePicker: () => React.createElement("div", { "data-testid": "range-picker" }),
}));
// The chart stubs echo the `grain` prop they were handed, so the "grain is
// display-only" block below can prove the two renders really differed in grain
// while the read params stayed byte-identical.
vi.mock("@/components/validation/validation-score-chart", () => ({
  ValidationScoreChart: ({ grain }: { grain?: string }) =>
    React.createElement("div", {
      "data-testid": "score-chart",
      "data-grain": String(grain),
    }),
}));
vi.mock("@/components/validation/validation-variables-chart", () => ({
  ValidationVariablesChart: ({ grain }: { grain?: string }) =>
    React.createElement("div", {
      "data-testid": "variables-chart",
      "data-grain": String(grain),
    }),
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

/** Params the panel last handed to useValidationResults. */
function lastResultsParams(): {
  from?: string;
  until?: string;
  limit?: number;
} {
  return mockResults.mock.calls.at(-1)?.[1];
}

beforeEach(() => {
  // The range selection is persisted in localStorage; clear it so each test
  // starts from the DEFAULT_PRESET_DAYS preset unless it seeds one explicitly.
  localStorage.clear();
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
    // spec: FRONTEND_BASIC.md §Shared Component Notes → ChartGrainPicker — the
    // picker sits in "the per-dataset page's Validation panel `Quality Score`
    // heading row (beside that row's RangePicker)". ONE picker governs both chart
    // sections, so the Variables section must not carry a second one. The real
    // ChartGrainPicker renders here (only the RangePicker and the charts are
    // stubbed), so this counts actual pickers.
    expect(
      screen.getAllByRole("combobox", { name: "Chart grain" }),
    ).toHaveLength(1);
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

// ---------------------------------------------------------------------------
// Resolved result window — the `until` slot.
//
// spec/feature/FRONTEND_BASIC.md §shared-component-notes (RangePicker):
//   "**A preset resolves to an open-ended window** — the lower bound only, with
//   `to`/`until` omitted — so the read always reaches the present" … "the
//   validation `attr/validation/result` endpoint — which names its end-bound
//   `until` rather than `to` … — takes the upper bound in that slot instead."
//
// spec/feature/FRONTEND_VALIDATION.md §Page contracts: "In `date` granularity
// the RangePicker drives `?from=&until=&limit=` — this endpoint names its
// end-bound param `until` rather than `to`".
//
// spec/feature/FRONTEND_BASIC.md §shared-component-notes, on why the open upper
// bound matters *here* specifically: "validation results carry a
// caller-supplied `data_time`, so an open upper bound surfaces future-dated
// rows; a row dated ahead of the present is an anomaly worth surfacing, not
// hiding."
//
// lib/range is deliberately NOT mocked here: the point is that the panel feeds
// the REAL resolver's output into the `until` slot. Determinism comes from a
// frozen clock plus the real display-timezone store read back in the test, so
// no host-offset assumption is baked in.
// ---------------------------------------------------------------------------
describe("ValidationDataPanel — resolved result window", () => {
  // 2024-03-15T08:30Z, matching lib/range.test.ts.
  const NOW = new Date("2024-03-15T08:30:00.000Z");

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    mockUseMe.mockReturnValue({ canWrite: true, isAdmin: false, isEditor: true });
    mockConf.mockReturnValue({ data: makeConf(), isLoading: false, error: null });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("sends an OPEN window for a preset selection (from + limit, no `until`)", async () => {
    await renderPanel();

    const params = lastResultsParams();
    expect(mockResults.mock.calls.at(-1)?.[0]).toBe(DATASET_URN);
    // The lower bound is the real resolver's, in the app's active display tz.
    const tz = useTimezoneStore.getState().tz;
    expect(params.from).toBe(presetRange(DEFAULT_PRESET_DAYS, "date", tz).from);
    // The behavior change this issue is about: the upper bound is not sent, so
    // the read reaches the present — including rows whose `data_time` is ahead
    // of the clock, which a pinned end-of-today `until` used to hide.
    expect(params.until).toBeUndefined();
    expect(params.limit).toBe(1000);
  });

  it("sends the CLOSED pair for a custom selection (upper bound kept in `until`)", async () => {
    // Backstop for the absence assertion above: the panel is not "always drop
    // the upper bound", it forwards whatever the resolver produced. A stored
    // custom selection keeps both bounds, so a regression that unconditionally
    // stripped `until` — or one that never populated it — fails here.
    localStorage.setItem(
      RANGE_KEYS.validationResults,
      JSON.stringify({
        kind: "custom",
        from: "2024-02-01T00:00:00.000Z",
        to: "2024-02-10T23:59:59.999Z",
      }),
    );

    await renderPanel();

    const params = lastResultsParams();
    expect(params.from).toBe("2024-02-01T00:00:00.000Z");
    expect(params.until).toBe("2024-02-10T23:59:59.999Z");
  });

  it("holds the lower bound stable across renders as the clock advances", () => {
    // The upper bound stays absent so the polled read keeps reaching new rows;
    // the lower bound must NOT be re-derived per render, because it participates
    // in the query key. spec/feature/FRONTEND_BASIC.md §shared-component-notes:
    // "a preset's *lower* bound is a function of the selection and the display
    // timezone alone — never of the render or the poll tick … re-resolving it
    // per render would mint a new key every render and spin an unbounded
    // refetch loop."
    //
    // Granularity here is `date`, so the clock is advanced by a full UTC day —
    // a smaller step would not change what a fresh resolution yields and the
    // test would pass vacuously.
    const { rerender } = render(<ValidationDataPanel datasetUrn={DATASET_URN} />);
    const first = lastResultsParams();
    expect(first.until).toBeUndefined();

    const tz = useTimezoneStore.getState().tz;
    vi.setSystemTime(new Date(NOW.getTime() + 24 * 60 * 60 * 1000));

    // Backstop: prove the advanced clock really does change what a fresh
    // resolution would yield, so an unchanged `from` below means "the memo
    // held", not "the clock never moved".
    expect(presetRange(DEFAULT_PRESET_DAYS, "date", tz).from).not.toBe(
      first.from,
    );

    rerender(<ValidationDataPanel datasetUrn={DATASET_URN} />);
    const second = lastResultsParams();
    expect(second.from).toBe(first.from);
    expect(second.until).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// Display grain is display-only — it must not reach the read.
//
// spec/feature/FRONTEND_BASIC.md §shared-component-notes (ChartGrainPicker):
//   "the grain is a **client-side display concern and adds no request
//   parameter**: it never alters the `from` / `to` / `until` / `limit` a call
//   site sends".
//
// The whole params object is compared, not just an absent `grain` key: the leak
// shape the sentence names is a CHANGED existing param (e.g. a larger `limit` at
// hourly), which no type check can see. The clock is frozen because a preset's
// lower bound is resolved against it, so two separate mounts milliseconds apart
// would otherwise differ for reasons unrelated to grain.
// ---------------------------------------------------------------------------
describe("ValidationDataPanel — grain adds no request parameter", () => {
  const NOW = new Date("2024-03-15T08:30:00.000Z");

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(NOW);
    mockUseMe.mockReturnValue({ canWrite: true, isAdmin: false, isEditor: true });
    mockConf.mockReturnValue({ data: makeConf(), isLoading: false, error: null });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("hands the results query identical params at hourly and at weekly", async () => {
    localStorage.setItem(GRAIN_KEYS.validationResults, "hourly");
    await renderPanel();
    const hourlyCall = mockResults.mock.calls.at(-1);
    // Backstop: the stored grain really did reach the charts, so the equality
    // below is "grain changed, params didn't" and not "grain never applied".
    expect(screen.getByTestId("score-chart")).toHaveAttribute("data-grain", "hourly");

    cleanup();
    localStorage.setItem(GRAIN_KEYS.validationResults, "weekly");
    await renderPanel();
    expect(screen.getByTestId("score-chart")).toHaveAttribute("data-grain", "weekly");

    // Same URN, same from/until/limit — the entire argument list is unchanged.
    expect(mockResults.mock.calls.at(-1)).toEqual(hourlyCall);
  });

  it("applies the one panel grain to BOTH chart sections", async () => {
    // spec: the picker sits in the Quality Score heading row and governs the
    // panel's charts; the Variables small multiples share it "so both stay in
    // lockstep".
    localStorage.setItem(GRAIN_KEYS.validationResults, "weekly");
    await renderPanel();

    expect(screen.getByTestId("score-chart")).toHaveAttribute("data-grain", "weekly");
    expect(screen.getByTestId("variables-chart")).toHaveAttribute(
      "data-grain",
      "weekly",
    );
  });
});
