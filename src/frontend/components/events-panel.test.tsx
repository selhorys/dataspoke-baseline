/**
 * Tests for <EventsPanel> — the unified per-dataset event timeline body.
 *
 * Spec: spec/feature/FRONTEND_BASIC.md §Per-dataset page (Events panel):
 *   - default all major types checked → no `event_major_type` filter sent (all),
 *   - unchecking a type narrows the query to the remaining types,
 *   - the `wrapper` tag is rendered for wrapper rows.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
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
});
