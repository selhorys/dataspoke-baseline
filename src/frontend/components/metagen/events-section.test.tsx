/**
 * Display-site integration test for the global timezone preference.
 *
 * EventsSection is a "use client" component that reads the global display
 * timezone via useDisplayTz() and renders each event's occurred_at through the
 * shared formatDateTime formatter. This test proves the global tz store
 * actually drives a real display site (not just the formatter in isolation).
 *
 * Spec trace:
 *   - spec/feature/FRONTEND_BASIC.md §/settings: the timezone preference is
 *     display-only and "governs how all dates and times are rendered across the
 *     app" (default Local).
 *
 * TZ stability: assertions compare the rendered text against the formatter's
 * own output (formatDateTime(iso, tz)) rather than hardcoded wall-clock values,
 * so they hold in any host timezone.
 */
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { EventsSection } from "./events-section";
import { formatDateTime } from "@/lib/format-time";
import { useTimezoneStore } from "@/lib/preferences/timezone";
import type { MetagenEvent } from "@/types/metagen";

const ISO = "2026-04-25T10:05:00.000Z";

function makeEvent(overrides: Partial<MetagenEvent> = {}): MetagenEvent {
  return {
    id: "evt-1",
    entity_type: "metagen_item",
    entity_id: "item-1",
    event_type: "candidate_generated",
    status: "success",
    detail: {},
    occurred_at: ISO,
    ...overrides,
  };
}

beforeEach(() => {
  localStorage.clear();
  useTimezoneStore.setState({ tz: "local" });
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  useTimezoneStore.setState({ tz: "local" });
});

describe("EventsSection — renders timestamps in the global display timezone", () => {
  it("renders the UTC wall-clock when the global tz is 'utc'", () => {
    useTimezoneStore.setState({ tz: "utc" });

    render(<EventsSection events={[makeEvent()]} />);

    // Exact, host-independent: utc getters.
    expect(screen.getByText("2026-04-25 10:05")).toBeInTheDocument();
    // And it equals what the shared formatter produces for tz="utc".
    expect(screen.getByText(formatDateTime(ISO, "utc"))).toBeInTheDocument();
  });

  it("renders the local wall-clock when the global tz is 'local'", () => {
    useTimezoneStore.setState({ tz: "local" });

    render(<EventsSection events={[makeEvent()]} />);

    // Offset-agnostic: compare against the formatter's local output.
    expect(
      screen.getByText(formatDateTime(ISO, "local")),
    ).toBeInTheDocument();
  });

  it("shows the empty message and no timestamp when there are no events", () => {
    render(<EventsSection events={[]} />);
    expect(screen.getByText("No events yet.")).toBeInTheDocument();
  });
});
