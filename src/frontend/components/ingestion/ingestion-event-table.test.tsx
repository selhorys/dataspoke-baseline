/**
 * Tests for IngestionEventTable — empty state, event rendering,
 * range filter control, pagination, and detail JSON display.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_INGESTION.md §Source Detail §Events:
 *     event history table, newest first, time-range filter, paginated.
 *   - spec/API.md §Ingestion: GET /spoke/ingestion/sources/{id}/event
 *     response shape: id, occurred_at, status, event_type, detail.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import React from "react";
import { IngestionEventTable } from "./ingestion-event-table";
import type { RangeSelection } from "@/lib/range";
import type { IngestionEvent } from "@/types/ingestion";

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function makeEvent(overrides: Partial<IngestionEvent> = {}): IngestionEvent {
  return {
    id: "evt-1",
    entity_type: "ingestion_source",
    entity_id: "src-1",
    event_type: "run_completed",
    status: "success",
    detail: {},
    occurred_at: "2024-03-15T10:30:00Z",
    ...overrides,
  };
}

const baseRange: RangeSelection = { kind: "preset", days: 14 };
const basePage = { offset: 0, limit: 20, totalCount: 0 };
const noop = () => {};

// ---------------------------------------------------------------------------
// 1. Empty state
// ---------------------------------------------------------------------------
describe("IngestionEventTable — empty state", () => {
  it("shows 'No events in this range' when events array is empty", () => {
    render(
      <IngestionEventTable
        events={[]}
        range={baseRange}
        onRangeChange={noop}
        tz="local"
        page={basePage}
        onPrev={noop}
        onNext={noop}
      />,
    );
    expect(screen.getByText(/no events in this range/i)).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// 2. Event row rendering
// ---------------------------------------------------------------------------
describe("IngestionEventTable — event rows", () => {
  it("renders each event's status, event_type, and occurred_at", () => {
    const events = [
      makeEvent({ id: "e1", status: "success", event_type: "run_completed" }),
      makeEvent({ id: "e2", status: "error", event_type: "run_failed" }),
    ];
    render(
      <IngestionEventTable
        events={events}
        range={baseRange}
        onRangeChange={noop}
        tz="local"
        page={{ offset: 0, limit: 20, totalCount: 2 }}
        onPrev={noop}
        onNext={noop}
      />,
    );
    expect(screen.getByText("success")).toBeTruthy();
    expect(screen.getByText("error")).toBeTruthy();
    expect(screen.getByText("run_completed")).toBeTruthy();
    expect(screen.getByText("run_failed")).toBeTruthy();
  });

  it("renders JSON-stringified detail when detail has keys", () => {
    const event = makeEvent({
      id: "e3",
      detail: { entities_ingested: 42, duration_ms: 1200 },
    });
    render(
      <IngestionEventTable
        events={[event]}
        range={baseRange}
        onRangeChange={noop}
        tz="local"
        page={{ offset: 0, limit: 20, totalCount: 1 }}
        onPrev={noop}
        onNext={noop}
      />,
    );
    // The component renders JSON.stringify(e.detail) in the detail cell
    expect(screen.getByText(/"entities_ingested":42/)).toBeTruthy();
  });

  it("renders an em-dash placeholder when detail is empty", () => {
    const event = makeEvent({ id: "e4", detail: {} });
    render(
      <IngestionEventTable
        events={[event]}
        range={baseRange}
        onRangeChange={noop}
        tz="local"
        page={{ offset: 0, limit: 20, totalCount: 1 }}
        onPrev={noop}
        onNext={noop}
      />,
    );
    // When detail is empty the component shows "—"
    expect(screen.getByText("—")).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// 3. Range filter control
// ---------------------------------------------------------------------------
describe("IngestionEventTable — range filter", () => {
  it("renders the range picker trigger showing the current range", () => {
    render(
      <IngestionEventTable
        events={[]}
        range={baseRange}
        onRangeChange={noop}
        tz="local"
        page={basePage}
        onPrev={noop}
        onNext={noop}
      />,
    );
    // The trigger button label is the active preset's label.
    expect(screen.getByText(/last 2 weeks/i)).toBeTruthy();
  });

  it("fires onRangeChange once with the staged preset on Apply", () => {
    // Per spec/feature/FRONTEND_BASIC.md §RangePicker, clicking a preset only
    // STAGES it — onRangeChange fires when Apply commits, not on the preset click.
    const onRangeChange = vi.fn();
    render(
      <IngestionEventTable
        events={[]}
        range={baseRange}
        onRangeChange={onRangeChange}
        tz="local"
        page={basePage}
        onPrev={noop}
        onNext={noop}
      />,
    );
    // Open the popover via the trigger, then click a preset (stages only).
    fireEvent.click(screen.getByText(/last 2 weeks/i));
    fireEvent.click(screen.getByText(/last 7 days/i));
    expect(onRangeChange).not.toHaveBeenCalled();
    // Apply commits the staged preset.
    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    expect(onRangeChange).toHaveBeenCalledTimes(1);
    expect(onRangeChange).toHaveBeenCalledWith({ kind: "preset", days: 7 });
  });
});

// ---------------------------------------------------------------------------
// 4. Pagination controls
// ---------------------------------------------------------------------------
describe("IngestionEventTable — pagination", () => {
  it("hides pagination when totalCount <= limit", () => {
    render(
      <IngestionEventTable
        events={[makeEvent()]}
        range={baseRange}
        onRangeChange={noop}
        tz="local"
        page={{ offset: 0, limit: 20, totalCount: 5 }}
        onPrev={noop}
        onNext={noop}
      />,
    );
    expect(screen.queryByRole("button", { name: /previous/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /next/i })).toBeNull();
  });

  it("shows page indicator and navigation when totalCount > limit", () => {
    render(
      <IngestionEventTable
        events={[makeEvent()]}
        range={baseRange}
        onRangeChange={noop}
        tz="local"
        page={{ offset: 0, limit: 20, totalCount: 45 }}
        onPrev={noop}
        onNext={noop}
      />,
    );
    expect(screen.getByRole("button", { name: /previous/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /next/i })).toBeTruthy();
    // Current page 1 of 3 (ceil(45/20)=3)
    expect(screen.getByText(/page 1 of 3/i)).toBeTruthy();
    expect(screen.getByText(/45 total/i)).toBeTruthy();
  });

  it("disables the Previous button on the first page", () => {
    render(
      <IngestionEventTable
        events={[makeEvent()]}
        range={baseRange}
        onRangeChange={noop}
        tz="local"
        page={{ offset: 0, limit: 20, totalCount: 45 }}
        onPrev={noop}
        onNext={noop}
      />,
    );
    expect(
      (screen.getByRole("button", { name: /previous/i }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it("disables the Next button on the last page", () => {
    render(
      <IngestionEventTable
        events={[makeEvent()]}
        range={baseRange}
        onRangeChange={noop}
        tz="local"
        // offset 40 + limit 20 = 60 >= totalCount 45 → last page
        page={{ offset: 40, limit: 20, totalCount: 45 }}
        onPrev={noop}
        onNext={noop}
      />,
    );
    expect(
      (screen.getByRole("button", { name: /next/i }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it("calls onPrev and onNext when the buttons are clicked", () => {
    const onPrev = vi.fn();
    const onNext = vi.fn();
    render(
      <IngestionEventTable
        events={[makeEvent()]}
        range={baseRange}
        onRangeChange={noop}
        tz="local"
        page={{ offset: 20, limit: 20, totalCount: 60 }}
        onPrev={onPrev}
        onNext={onNext}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /previous/i }));
    expect(onPrev).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    expect(onNext).toHaveBeenCalledTimes(1);
  });
});
