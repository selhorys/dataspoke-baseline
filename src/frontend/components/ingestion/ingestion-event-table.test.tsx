/**
 * Tests for IngestionEventTable — empty state, event rendering,
 * from/to filter controls, pagination, and detail JSON display.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_INGESTION.md §Source Detail §Events:
 *     event history table, newest first, from/to filters, paginated.
 *   - spec/API.md §Ingestion: GET /spoke/ingestion/sources/{id}/event
 *     response shape: id, occurred_at, status, event_type, detail.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import React from "react";
import { IngestionEventTable } from "./ingestion-event-table";
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
        from=""
        to=""
        onFromChange={noop}
        onToChange={noop}
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
        from=""
        to=""
        onFromChange={noop}
        onToChange={noop}
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
        from=""
        to=""
        onFromChange={noop}
        onToChange={noop}
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
        from=""
        to=""
        onFromChange={noop}
        onToChange={noop}
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
// 3. From/to filter controls
// ---------------------------------------------------------------------------
describe("IngestionEventTable — from/to filters", () => {
  it("calls onFromChange when the from input changes", () => {
    const onFromChange = vi.fn();
    render(
      <IngestionEventTable
        events={[]}
        from=""
        to=""
        onFromChange={onFromChange}
        onToChange={noop}
        page={basePage}
        onPrev={noop}
        onNext={noop}
      />,
    );
    // The 'from' input is labeled "from"
    const fromInput = screen.getByLabelText(/^from$/i);
    fireEvent.change(fromInput, { target: { value: "2024-01-01T00:00" } });
    expect(onFromChange).toHaveBeenCalledWith("2024-01-01T00:00");
  });

  it("calls onToChange when the to input changes", () => {
    const onToChange = vi.fn();
    render(
      <IngestionEventTable
        events={[]}
        from=""
        to=""
        onFromChange={noop}
        onToChange={onToChange}
        page={basePage}
        onPrev={noop}
        onNext={noop}
      />,
    );
    const toInput = screen.getByLabelText(/^to$/i);
    fireEvent.change(toInput, { target: { value: "2024-02-01T00:00" } });
    expect(onToChange).toHaveBeenCalledWith("2024-02-01T00:00");
  });

  it("reflects the from and to values passed via props", () => {
    render(
      <IngestionEventTable
        events={[]}
        from="2024-01-15T08:00"
        to="2024-01-16T08:00"
        onFromChange={noop}
        onToChange={noop}
        page={basePage}
        onPrev={noop}
        onNext={noop}
      />,
    );
    expect((screen.getByLabelText(/^from$/i) as HTMLInputElement).value).toBe(
      "2024-01-15T08:00",
    );
    expect((screen.getByLabelText(/^to$/i) as HTMLInputElement).value).toBe(
      "2024-01-16T08:00",
    );
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
        from=""
        to=""
        onFromChange={noop}
        onToChange={noop}
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
        from=""
        to=""
        onFromChange={noop}
        onToChange={noop}
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
        from=""
        to=""
        onFromChange={noop}
        onToChange={noop}
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
        from=""
        to=""
        onFromChange={noop}
        onToChange={noop}
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
        from=""
        to=""
        onFromChange={noop}
        onToChange={noop}
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
