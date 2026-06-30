/**
 * Tests for MetricEventTable — the metrics-detail run/event log.
 *
 * Mirrors the ingestion event-table pattern: empty state, event-row rendering,
 * click-to-expand detail popup (EventDetailCell), range filter, and pagination.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_GOVERNANCE.md §Metrics detail event log — event
 *     history table (occurred_at, status, event_type, detail), newest first,
 *     time-range filter, paginated.
 *   - spec/API.md §Metric — GET /spoke/governance/metric/{id}/event response
 *     shape: id, occurred_at, status, event_type, detail.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import React from "react";
import { MetricEventTable } from "./metric-event-table";
import type { RangeSelection } from "@/lib/range";
import type { MetricEvent } from "@/types/governance";

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) =>
    React.createElement("a", { href, ...rest }, children),
}));

// jsdom lacks ResizeObserver (used by Radix popover/dialog).
if (typeof global.ResizeObserver === "undefined") {
  global.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

function makeEvent(overrides: Partial<MetricEvent> = {}): MetricEvent {
  return {
    id: "evt-1",
    entity_type: "metric",
    entity_id: "ingestion-freshness",
    event_type: "METRIC.RUN_COMPLETE",
    status: "success",
    detail: {},
    occurred_at: "2026-03-15T10:30:00Z",
    ...overrides,
  };
}

const baseRange: RangeSelection = { kind: "preset", days: 14 };
const basePage = { offset: 0, limit: 20, totalCount: 0 };
const noop = () => {};

// ── Empty state ─────────────────────────────────────────────────────────────────
describe("MetricEventTable — empty state", () => {
  it("shows 'No events in this range' when events array is empty", () => {
    render(
      <MetricEventTable
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

// ── Event rows ──────────────────────────────────────────────────────────────────
describe("MetricEventTable — event rows", () => {
  it("renders each event's status badge and event_type", () => {
    const events = [
      makeEvent({ id: "e1", status: "success", event_type: "METRIC.RUN_COMPLETE" }),
      makeEvent({ id: "e2", status: "error", event_type: "METRIC.CONFIG_UPDATE" }),
    ];
    render(
      <MetricEventTable
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
    expect(screen.getByText("METRIC.RUN_COMPLETE")).toBeTruthy();
    expect(screen.getByText("METRIC.CONFIG_UPDATE")).toBeTruthy();
  });

  it("renders the four column headers", () => {
    render(
      <MetricEventTable
        events={[makeEvent()]}
        range={baseRange}
        onRangeChange={noop}
        tz="local"
        page={{ offset: 0, limit: 20, totalCount: 1 }}
        onOffset={noop}
        onLimit={noop}
      />,
    );
    expect(screen.getByRole("columnheader", { name: "occurred_at" })).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "status" })).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "event_type" })).toBeTruthy();
    expect(screen.getByRole("columnheader", { name: "detail" })).toBeTruthy();
  });

  it("renders an em-dash placeholder when detail is empty", () => {
    render(
      <MetricEventTable
        events={[makeEvent({ id: "e4", detail: {} })]}
        range={baseRange}
        onRangeChange={noop}
        tz="local"
        page={{ offset: 0, limit: 20, totalCount: 1 }}
        onOffset={noop}
        onLimit={noop}
      />,
    );
    expect(screen.getByText("—")).toBeTruthy();
  });
});

// ── Detail popup ──────────────────────────────────────────────────────────────
describe("MetricEventTable — detail popup", () => {
  it("renders a click-to-expand trigger that opens the pretty-printed detail dialog", () => {
    const detail = {
      run_id: "run-7",
      metric_id: "ingestion-freshness",
      dry_run: false,
      values: { total: 10, ingested_in_time: 8 },
    };
    render(
      <MetricEventTable
        events={[makeEvent({ id: "e5", detail })]}
        range={baseRange}
        onRangeChange={noop}
        tz="local"
        page={{ offset: 0, limit: 20, totalCount: 1 }}
        onOffset={noop}
        onLimit={noop}
      />,
    );
    // The cell shows a truncated trigger, not the full inline JSON.
    const full = JSON.stringify(detail);
    expect(full.length).toBeGreaterThan(30);
    expect(screen.queryByText(full)).toBeNull();
    const trigger = screen.getByRole("button", { name: "View event detail" });
    expect(trigger.textContent).toContain("…");

    // Clicking opens the dialog with the pretty-printed JSON (run_id visible).
    fireEvent.click(trigger);
    const dialog = screen.getByRole("dialog");
    expect(dialog.textContent).toContain("Event detail");
    expect(dialog.textContent).toContain("run-7");
  });
});

// ── Range filter ──────────────────────────────────────────────────────────────
describe("MetricEventTable — range filter", () => {
  it("renders the range picker trigger showing the current range", () => {
    render(
      <MetricEventTable
        events={[]}
        range={baseRange}
        onRangeChange={noop}
        tz="local"
        page={basePage}
        onOffset={noop}
        onLimit={noop}
      />,
    );
    expect(screen.getByText(/last 2 weeks/i)).toBeTruthy();
  });

  it("fires onRangeChange once with the staged preset on Apply", () => {
    const onRangeChange = vi.fn();
    render(
      <MetricEventTable
        events={[]}
        range={baseRange}
        onRangeChange={onRangeChange}
        tz="local"
        page={basePage}
        onOffset={noop}
        onLimit={noop}
      />,
    );
    fireEvent.click(screen.getByText(/last 2 weeks/i));
    fireEvent.click(screen.getByText(/last 7 days/i));
    expect(onRangeChange).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    expect(onRangeChange).toHaveBeenCalledTimes(1);
    expect(onRangeChange).toHaveBeenCalledWith({ kind: "preset", days: 7 });
  });
});

// ── Pagination ──────────────────────────────────────────────────────────────────
describe("MetricEventTable — pagination", () => {
  it("renders the M–N of T label and Prev/Next controls", () => {
    render(
      <MetricEventTable
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
    expect(screen.getByText(/1.20 of 45/)).toBeTruthy();
  });

  it("calls onOffset with the new offset when Prev/Next are clicked", () => {
    const onOffset = vi.fn();
    render(
      <MetricEventTable
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
