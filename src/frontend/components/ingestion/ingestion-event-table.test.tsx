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
        onOffset={noop}
        onLimit={noop}
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
        onOffset={noop}
        onLimit={noop}
      />,
    );
    expect(screen.getByText("success")).toBeTruthy();
    expect(screen.getByText("error")).toBeTruthy();
    expect(screen.getByText("run_completed")).toBeTruthy();
    expect(screen.getByText("run_failed")).toBeTruthy();
  });

  it("renders a click-to-expand detail trigger when detail has keys", () => {
    const event = makeEvent({
      id: "e3",
      detail: {
        dry_run: false,
        discovered_urns_count: 2,
        emitted_urns_count: 2,
        run_id: "run-e3",
      },
    });
    render(
      <IngestionEventTable
        events={[event]}
        range={baseRange}
        onRangeChange={noop}
        tz="local"
        page={{ offset: 0, limit: 20, totalCount: 1 }}
        onOffset={noop}
        onLimit={noop}
      />,
    );
    // The detail cell renders a truncated EventDetailCell trigger (button)
    // with a STABLE accessible name, not the full inline JSON. The compact
    // JSON is > 30 chars so the visible text ends in …
    const full = JSON.stringify(event.detail);
    expect(full.length).toBeGreaterThan(30);
    expect(screen.queryByText(full)).toBeNull();
    const trigger = screen.getByRole("button", { name: "View event detail" });
    expect(trigger.textContent).toContain("…");
    expect((trigger.textContent ?? "").length).toBeLessThanOrEqual(31);
    expect(trigger.textContent).not.toBe(full);
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
        onOffset={noop}
        onLimit={noop}
      />,
    );
    // When detail is empty the component shows "—"
    expect(screen.getByText("—")).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// 2b. Wrapper badge — present when event.wrapper is true
// ---------------------------------------------------------------------------
// Spec: spec/feature/FRONTEND_INGESTION.md §Source Detail §Events — a row whose
// `wrapper` flag is set carries a "wrapper" tag (runs booked on the source's
// internal DataHub CLI wrapper, surfaced on the regular parent).
describe("IngestionEventTable — wrapper badge", () => {
  it("renders a 'wrapper' badge for a wrapper:true event", () => {
    render(
      <IngestionEventTable
        events={[makeEvent({ id: "w1", wrapper: true })]}
        range={baseRange}
        onRangeChange={noop}
        tz="local"
        page={{ offset: 0, limit: 20, totalCount: 1 }}
        onOffset={noop}
        onLimit={noop}
      />,
    );
    expect(screen.getByText("wrapper")).toBeTruthy();
  });

  it("does NOT render a 'wrapper' badge when wrapper is false/undefined", () => {
    render(
      <IngestionEventTable
        events={[makeEvent({ id: "w2", wrapper: false })]}
        range={baseRange}
        onRangeChange={noop}
        tz="local"
        page={{ offset: 0, limit: 20, totalCount: 1 }}
        onOffset={noop}
        onLimit={noop}
      />,
    );
    expect(screen.queryByText("wrapper")).toBeNull();
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
        onOffset={noop}
        onLimit={noop}
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
        onOffset={noop}
        onLimit={noop}
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
// 4. Pagination controls (shared <Pagination>)
// ---------------------------------------------------------------------------
describe("IngestionEventTable — pagination", () => {
  it("renders the M–N of T label and Prev/Next controls", () => {
    render(
      <IngestionEventTable
        events={[makeEvent()]}
        range={baseRange}
        onRangeChange={noop}
        tz="local"
        page={{ offset: 0, limit: 20, totalCount: 45 }}
        onOffset={noop}
        onLimit={noop}
      />,
    );
    expect(screen.getByRole("button", { name: /previous/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /next/i })).toBeTruthy();
    // Standard "1–20 of 45" label from the shared control.
    expect(screen.getByText(/1.20 of 45/)).toBeTruthy();
  });

  it("disables the Previous button on the first page", () => {
    render(
      <IngestionEventTable
        events={[makeEvent()]}
        range={baseRange}
        onRangeChange={noop}
        tz="local"
        page={{ offset: 0, limit: 20, totalCount: 45 }}
        onOffset={noop}
        onLimit={noop}
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
        onOffset={noop}
        onLimit={noop}
      />,
    );
    expect(
      (screen.getByRole("button", { name: /next/i }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it("calls onOffset with the new offset when Prev/Next are clicked", () => {
    const onOffset = vi.fn();
    render(
      <IngestionEventTable
        events={[makeEvent()]}
        range={baseRange}
        onRangeChange={noop}
        tz="local"
        page={{ offset: 20, limit: 20, totalCount: 60 }}
        onOffset={onOffset}
        onLimit={noop}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: /previous/i }));
    expect(onOffset).toHaveBeenCalledWith(0);
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    expect(onOffset).toHaveBeenCalledWith(40);
  });
});
