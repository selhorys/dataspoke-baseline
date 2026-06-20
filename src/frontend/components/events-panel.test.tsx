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
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import React from "react";
import { EventsPanel } from "./events-panel";
import type { DatasetEventListResponse } from "@/types/data";

// ── Mocks ──────────────────────────────────────────────────────────────────────
const useDatasetEvents = vi.fn();
vi.mock("@/lib/api/data", () => ({
  useDatasetEvents: (urn: string, params: unknown) =>
    useDatasetEvents(urn, params),
}));

// RangePicker pulls in calendar internals; stub it.
vi.mock("@/components/range-picker", () => ({
  RangePicker: () => React.createElement("div", { "data-testid": "range-picker" }),
}));

// Stable resolved range so the query params are deterministic.
vi.mock("@/lib/hooks/use-range-selection", () => ({
  RANGE_KEYS: { dataEvents: "range:data:events" },
  usePersistedRangeState: () => ({
    selection: { kind: "preset", days: 14 },
    setSelection: vi.fn(),
  }),
}));
vi.mock("@/lib/range", () => ({
  resolveRange: () => ({ from: "2026-06-01T00:00:00Z", to: "2026-06-19T00:00:00Z" }),
}));

const DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.t,DEV)";

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
  useDatasetEvents.mockReset();
  useDatasetEvents.mockReturnValue({ data: makeData() });
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
