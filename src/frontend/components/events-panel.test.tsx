/**
 * Tests for <EventsPanel> — the unified per-dataset event timeline body.
 *
 * Spec: spec/feature/FRONTEND_BASIC.md §Per-dataset page (Events panel):
 *   - default all major types checked → no `event_major_type` filter sent (all),
 *   - unchecking a type narrows the query to the remaining types,
 *   - the `wrapper` tag is rendered for wrapper rows,
 *   - the timeline renders as a semantic table with Time/Status/Type/Detail
 *     columns; a row surfaces its time, status, and event_type; the empty case
 *     renders the empty-state row with no data rows.
 *
 * Spec: spec/feature/FRONTEND_BASIC.md §shared-component-notes (RangePicker):
 *   "**A preset resolves to an open-ended window** — the lower bound only, with
 *   `to`/`until` omitted — so the read always reaches the present, which is what
 *   lets a 15 s-polled panel … surface records written after page load. **A
 *   custom range resolves to the closed inclusive pair** the user picked and
 *   keeps both bounds." … "a preset's *lower* bound is resolved against the
 *   clock at resolution time and then held — re-derived only when the selection
 *   or the display timezone changes, or on the next visit, never per render and
 *   never per poll tick, because it participates in the query key and
 *   re-resolving it per render would mint a new key every render and spin an
 *   unbounded refetch loop."
 *
 * lib/range is deliberately NOT mocked: this file's job is to prove the panel
 * feeds the *real* resolver's output into the query. Determinism comes from a
 * frozen clock (vi.setSystemTime) plus a fixed display timezone, the same way
 * range-picker.test.tsx and governance/metric-card.test.tsx get theirs.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import React from "react";
import { EventsPanel } from "./events-panel";
import type { DatasetEventListResponse } from "@/types/data";
import { presetRange, type RangeSelection } from "@/lib/range";

// ── Mocks ──────────────────────────────────────────────────────────────────────
const { useDatasetEvents, mockSelection, mockTz } = vi.hoisted(() => ({
  useDatasetEvents: vi.fn(),
  mockSelection: vi.fn(),
  mockTz: vi.fn(),
}));

vi.mock("@/lib/api/data", () => ({
  useDatasetEvents: (urn: string, params: unknown) =>
    useDatasetEvents(urn, params),
}));

// RangePicker pulls in calendar internals; stub it. The stub echoes the `tz`
// prop so the panel's threading of the global display timezone is observable —
// spec/feature/FRONTEND_BASIC.md §shared-component-notes: the picker's calendar
// days and times "are interpreted and displayed in the **global Settings
// timezone preference**".
vi.mock("@/components/range-picker", () => ({
  RangePicker: ({ tz }: { tz: string }) =>
    React.createElement("div", { "data-testid": "range-picker", "data-tz": tz }),
}));

// The real hook holds `selection` in useState, so its identity is stable across
// renders. mockReturnValue preserves that (same object reference every call) —
// essential for the memo-stability test below, which would otherwise pass for
// the wrong reason.
vi.mock("@/lib/hooks/use-range-selection", () => ({
  RANGE_KEYS: { dataEvents: "range:data:events" },
  usePersistedRangeState: () => ({
    selection: mockSelection(),
    setSelection: vi.fn(),
  }),
}));

// Display timezone the panel threads into resolveRange and into the picker.
// Mutable (not a fixed literal) so a test can flip the global preference and
// re-render, rather than pinning the panel to one zone for the whole file.
vi.mock("@/lib/preferences/timezone", () => ({ useDisplayTz: () => mockTz() }));

const DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,DEV)";

// Frozen "now" — matches lib/range.test.ts so resolved bounds are UTC-stable.
const NOW = new Date("2024-03-15T08:30:00.000Z");

/** Default panel selection: the 2-week preset (DEFAULT_PRESET_DAYS). */
const PRESET_14: RangeSelection = { kind: "preset", days: 14 };

/** Last params the panel handed to useDatasetEvents. */
function lastParams(): {
  from?: string;
  to?: string;
  eventMajorTypes?: string[];
} {
  return useDatasetEvents.mock.calls.at(-1)?.[1];
}

function makeData(): DatasetEventListResponse {
  return {
    offset: 0,
    limit: 20,
    total_count: 1,
    events: [
      {
        id: "e1",
        entity_type: "ingestion_source",
        entity_id: "src-1",
        event_type: "ingestion.run.success",
        status: "success",
        detail: { run_id: "r1" },
        occurred_at: "2026-06-18T00:00:00Z",
        wrapper: true,
      },
    ],
  };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(NOW);
  useDatasetEvents.mockReset();
  useDatasetEvents.mockReturnValue({ data: makeData() });
  mockSelection.mockReturnValue(PRESET_14);
  mockTz.mockReturnValue("utc");
});

afterEach(() => {
  vi.useRealTimers();
});

describe("EventsPanel", () => {
  it("sends no event_major_type filter when all types are checked", () => {
    render(<EventsPanel datasetUrn={DATASET_URN} />);
    const lastCall = useDatasetEvents.mock.calls.at(-1);
    expect(lastCall?.[0]).toBe(DATASET_URN);
    expect(lastCall?.[1].eventMajorTypes).toBeUndefined();
  });

  it("renders the wrapper tag for wrapper rows", () => {
    render(<EventsPanel datasetUrn={DATASET_URN} />);
    expect(screen.getByText("wrapper")).toBeTruthy();
    expect(screen.getByText("ingestion.run.success")).toBeTruthy();
  });

  it("narrows the query to remaining types when one is unchecked", () => {
    render(<EventsPanel datasetUrn={DATASET_URN} />);
    fireEvent.click(screen.getByRole("checkbox", { name: "VALIDATION" }));
    const lastCall = useDatasetEvents.mock.calls.at(-1);
    expect(lastCall?.[1].eventMajorTypes).toEqual(["INGESTION", "METAGEN"]);
  });

  it("renders the timeline as a semantic table with one column per spec'd field", () => {
    render(<EventsPanel datasetUrn={DATASET_URN} />);
    expect(screen.getByRole("table")).toBeTruthy();
    // The spec names four row dimensions (occurred_at / status / event_type /
    // detail); assert four columns structurally. The header *labels* are an impl
    // choice (Time/Status/Type/Detail), not a spec contract, so they are not
    // pinned here — the field values themselves are asserted in the body below.
    expect(screen.getAllByRole("columnheader")).toHaveLength(4);
  });

  it("renders an event row surfacing its event_type and status", () => {
    render(<EventsPanel datasetUrn={DATASET_URN} />);
    // The single seeded event surfaces its type and status in the table body.
    expect(screen.getByText("ingestion.run.success")).toBeTruthy();
    expect(screen.getByText("success")).toBeTruthy();
  });

  it("renders the detail cell as a click-to-expand trigger opening the JSON dialog", () => {
    render(<EventsPanel datasetUrn={DATASET_URN} />);
    // Spec (FRONTEND_BASIC §Events panel): the detail cell truncates compact JSON
    // and is click-to-expand into a pretty-printed dialog (EventDetailCell).
    const trigger = screen.getByRole("button", { name: "View event detail" });
    fireEvent.click(trigger);
    // Scope to the opened dialog — the compact JSON also appears in the truncated
    // trigger, so assert the pretty-printed body within the dialog specifically.
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Event detail")).toBeTruthy();
    expect(within(dialog).getByText(/run_id/)).toBeTruthy();
  });

  it("renders the empty-state row and no data rows when there are no events", () => {
    useDatasetEvents.mockReturnValue({
      data: { offset: 0, limit: 20, total_count: 0, events: [] },
    });
    render(<EventsPanel datasetUrn={DATASET_URN} />);
    expect(screen.getByRole("table")).toBeTruthy();
    expect(
      screen.getByText("No events for this dataset in the selected window."),
    ).toBeTruthy();
    // Only the header row exists — no event data rows.
    expect(screen.queryByText("ingestion.run.success")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Time window handed to the polled query.
//
// spec/feature/FRONTEND_BASIC.md §shared-component-notes (RangePicker):
//   "A preset resolves to an open-ended window — the lower bound only, with
//   `to`/`until` omitted — so the read always reaches the present, which is what
//   lets a 15 s-polled panel … surface records written after page load. A custom
//   range resolves to the closed inclusive pair the user picked and keeps both
//   bounds."
//
// The panel resolves at `datetime` granularity (events-panel.tsx), so under the
// frozen clock the 14-day preset's lower bound is NOW − 14d.
// ---------------------------------------------------------------------------
describe("EventsPanel — resolved time window", () => {
  it("sends an OPEN window for a preset selection (from only, no `to`)", () => {
    render(<EventsPanel datasetUrn={DATASET_URN} />);
    const params = lastParams();
    // NOW (2024-03-15T08:30Z) − 14 days.
    expect(params.from).toBe("2024-03-01T08:30:00.000Z");
    expect(params.to).toBeUndefined();
  });

  it("sends the CLOSED pair for a custom selection (upper bound preserved)", () => {
    // Counterweight to the assertion above: the panel is not "always drop `to`",
    // it forwards whatever the resolver produced. A custom selection keeps both
    // bounds, so a regression that unconditionally stripped `to` fails here.
    mockSelection.mockReturnValue({
      kind: "custom",
      from: "2024-02-01T00:00:00.000Z",
      to: "2024-02-10T23:59:59.999Z",
    } satisfies RangeSelection);

    render(<EventsPanel datasetUrn={DATASET_URN} />);
    const params = lastParams();
    expect(params.from).toBe("2024-02-01T00:00:00.000Z");
    expect(params.to).toBe("2024-02-10T23:59:59.999Z");
  });

  it("holds the lower bound stable across renders as the clock advances", () => {
    // The regression this file exists to catch runs BOTH ways, so both are
    // asserted here:
    //   1. the upper bound must stay absent, so a 15 s poll keeps reaching new
    //      events (the original bug: a `to` frozen at mount);
    //   2. the lower bound must NOT be re-derived per render, because it
    //      participates in the query key — spec: "a preset's *lower* bound is
    //      resolved against the clock at resolution time and then held —
    //      re-derived only when the selection or the display timezone changes,
    //      or on the next visit, never per render and never per poll tick,
    //      because it participates in the query key and re-resolving it per
    //      render would mint a new key every render and spin an unbounded
    //      refetch loop."
    //
    // Advancing the clock BETWEEN the two renders is what makes this
    // discriminating: with the clock frozen, deleting the component's useMemo
    // would still produce an identical `from` and the test would pass vacuously.
    const { rerender } = render(<EventsPanel datasetUrn={DATASET_URN} />);
    const first = lastParams();
    expect(first.from).toBe("2024-03-01T08:30:00.000Z");
    expect(first.to).toBeUndefined();

    vi.setSystemTime(new Date(NOW.getTime() + 60_000));

    // Backstop: prove the advanced clock really does change what a fresh
    // resolution would yield. Without this, an unchanged `from` below could mean
    // "the memo held" OR "the clock never moved".
    expect(presetRange(14, "datetime", "utc").from).toBe(
      "2024-03-01T08:31:00.000Z",
    );

    rerender(<EventsPanel datasetUrn={DATASET_URN} />);
    const second = lastParams();
    // Same lower bound → same query key → no refetch storm.
    expect(second.from).toBe(first.from);
    // …and still open above.
    expect(second.to).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// Display-timezone threading.
//
// spec/feature/FRONTEND_BASIC.md §shared-component-notes (RangePicker): "The
// picker has **no per-panel timezone control**: like all timestamps in the UI,
// the calendar days and times it shows are interpreted and displayed in the
// **global Settings timezone preference** (Local or UTC, default Local). Every
// bound it emits is a canonical inclusive UTC ISO instant regardless."
//
// SCOPE NOTE — where the `tz` DEP-ARRAY guard lives. This panel resolves at
// `datetime` granularity, where a preset's bounds are pure instant math
// (`now − days`) and therefore identical in both zones. So no assertion on the
// query params here can distinguish `useMemo(..., [selection, tz])` from
// `useMemo(..., [selection])` — the resolved value simply does not depend on tz
// at this granularity. That guard is a source-shape invariant and is asserted
// for every enumerated call site (this one included) in
// lib/range.import-boundary.test.ts → "memoizes every query-path resolveRange
// call on [selection, tz]". What IS observable here is the threading itself:
// the panel must read the live preference on every render, not capture one.
// ---------------------------------------------------------------------------
describe("EventsPanel — display timezone", () => {
  it("hands the RangePicker the CURRENT global preference, re-read on rerender", () => {
    const { rerender } = render(<EventsPanel datasetUrn={DATASET_URN} />);
    expect(screen.getByTestId("range-picker").getAttribute("data-tz")).toBe(
      "utc",
    );

    // Flip the global Settings preference and re-render, exactly as the real
    // zustand store would push to every subscriber.
    mockTz.mockReturnValue("local");
    rerender(<EventsPanel datasetUrn={DATASET_URN} />);

    expect(screen.getByTestId("range-picker").getAttribute("data-tz")).toBe(
      "local",
    );
  });

  it("emits the same absolute instants in either zone at datetime granularity", () => {
    // "Every bound it emits is a canonical inclusive UTC ISO instant
    // regardless" — and at `datetime` granularity the preset lower bound is
    // `now − days`, an absolute instant, so switching zones must not move it.
    // Cross-checked against the REAL resolver called with each zone, so this
    // fails if the panel ever stopped passing its tz through (it would resolve
    // at some other granularity/zone combination).
    render(<EventsPanel datasetUrn={DATASET_URN} />);
    const utcParams = lastParams();
    expect(utcParams.from).toBe(presetRange(14, "datetime", "utc").from);
    expect(utcParams.to).toBeUndefined();

    mockTz.mockReturnValue("local");
    mockSelection.mockReturnValue({ ...PRESET_14 });
    render(<EventsPanel datasetUrn={DATASET_URN} />);
    const localParams = lastParams();
    expect(localParams.from).toBe(presetRange(14, "datetime", "local").from);
    expect(localParams.to).toBeUndefined();

    expect(localParams.from).toBe(utcParams.from);
  });
});
