/**
 * Tests for lib/api/metagen.ts — the two time-window-bearing URL builders: the
 * per-conf run-history feed (`useMetagenConfEvents`) and the cross-conf event
 * feed (`useMetagenEvents`).
 *
 * Spec traces:
 *   - spec/API.md §Query Parameters: `from` — "Start of time-range filter,
 *     inclusive; used on `result` and `event` endpoints"; `to` — "End of
 *     time-range filter, inclusive; used on `result` and `event` endpoints.
 *     Optional — omitting it leaves the range unbounded above, so the filter
 *     reaches the newest record".
 *   - spec/API.md §Meta-Classifier Conventions (`event`): event endpoints
 *     "Supports `from`/`to` for time-range filtering."
 *   - spec/feature/FRONTEND_METAGEN.md §Conf create / detail: the conf
 *     run-history events table (`GET /spoke/metagen/conf/{conf_id}/event`)
 *     renders "with a `datetime`
 *     [RangePicker](FRONTEND_BASIC.md#shared-component-notes) driving
 *     `from`/`to`".
 *   - spec/feature/FRONTEND_METAGEN.md §Components: `MetagenEventTable` —
 *     "shared event table bound to a `…/event` route (conf-detail + cross-conf
 *     feeds), paired with a `datetime`
 *     [RangePicker](FRONTEND_BASIC.md#shared-component-notes) for the
 *     `from`/`to` window."
 *
 * These two feeds are the metagen half of the `from`/`to` consumer set: the
 * emitted param name has to be the one the router declares, since FastAPI drops
 * unknown query params silently and a mismatch turns both RangePickers into
 * decorations over an unfiltered feed. **Scope:** this file observes only the
 * client side — the URL these hooks emit. It cannot see the router, so it cannot
 * prove the pairing holds; what enforces that the emitted names are the ones the
 * routes declare is
 * `tests/unit/spec_conformance/test_time_range_params.py`, and what proves the
 * routes then act on them is
 * `tests/unit/api/routers/spoke/test_metagen.py::test_event_routes_filter_on_from_and_to_inclusively`
 * plus the spot tests in `tests/integration/spot/test_metagen_run.py`. The
 * open-window assertions here each sit
 * behind a both-bounds test in the same block, which is the injection backstop:
 * it proves the builder emits `to=` when one is supplied, so a missing `to=`
 * means the caller omitted it rather than the builder being incapable of
 * producing it.
 *
 * Strategy mirrors lib/api/governance.test.ts: mock @/lib/api/client so apiFetch
 * is a spy recording the URL, drive each hook via renderHook in a QueryClient
 * wrapper, and assert on the captured URL only (no network).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { mockApiFetch } = vi.hoisted(() => ({
  mockApiFetch: vi.fn(),
}));

vi.mock("@/lib/api/client", () => ({
  apiFetch: mockApiFetch,
  ApiError: class ApiError extends Error {
    constructor(
      public payload: { error_code: string; message: string },
      public status: number,
    ) {
      super(payload.message);
    }
  },
}));

import { useMetagenConfEvents, useMetagenEvents } from "./metagen";

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0 },
      mutations: { retry: false },
    },
  });
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(QueryClientProvider, { client: qc }, children);
  };
}

function lastUrl(): string {
  return mockApiFetch.mock.calls[mockApiFetch.mock.calls.length - 1][0] as string;
}

/** Parse the query string of a captured relative URL ("" when there is none). */
function queryOf(url: string): URLSearchParams {
  const q = url.indexOf("?");
  return new URLSearchParams(q === -1 ? "" : url.slice(q + 1));
}

const CONF_ID = "7c9f1b2e-0000-4000-8000-000000000abc";
const FROM = "2024-03-01T00:00:00.000Z";
const TO = "2024-03-15T23:59:59.999Z";

beforeEach(() => {
  vi.clearAllMocks();
  mockApiFetch.mockResolvedValue({
    events: [],
    total_count: 0,
    offset: 0,
    limit: 20,
  });
});

// ---------------------------------------------------------------------------
// Per-conf run history — GET /spoke/metagen/conf/{conf_id}/event
// (lib/api/metagen.ts → useMetagenConfEvents buildUrl)
// ---------------------------------------------------------------------------
describe("useMetagenConfEvents — time bounds on the per-conf event URL", () => {
  it("emits both bounds when the window is closed (custom range)", async () => {
    // Positive leg / injection backstop for the absence assertion below.
    const { result } = renderHook(
      () =>
        useMetagenConfEvents(CONF_ID, {
          from: FROM,
          to: TO,
          offset: 0,
          limit: 20,
        }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const sp = queryOf(lastUrl());
    // spec/API.md §Query Parameters — the time-range params are named `from`
    // and `to`; these are the names the router declares, and an unknown name
    // would be dropped silently by FastAPI.
    expect(sp.get("from")).toBe(FROM);
    expect(sp.get("to")).toBe(TO);
    expect(sp.get("offset")).toBe("0");
    expect(sp.get("limit")).toBe("20");
    // Sanity: it targets the per-conf run-history route.
    expect(lastUrl()).toContain(
      `/spoke/metagen/conf/${encodeURIComponent(CONF_ID)}/event`,
    );
  });

  it("emits from= and NO to= when the resolved window is open above", async () => {
    // The shape a RangePicker preset produces on the conf-detail events panel.
    const { result } = renderHook(
      () => useMetagenConfEvents(CONF_ID, { from: FROM, offset: 0, limit: 20 }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const sp = queryOf(lastUrl());
    expect(sp.get("from")).toBe(FROM);
    // spec/API.md §Query Parameters — omitting `to` leaves the range unbounded
    // above, which is what keeps the 15 s-polled feed reaching new events.
    expect(sp.has("to")).toBe(false);
    // …and the rest of the query survived, so "no to=" is not the builder
    // having bailed out.
    expect(sp.get("offset")).toBe("0");
    expect(sp.get("limit")).toBe("20");
  });

  it("emits no query string at all when no params are given", async () => {
    const { result } = renderHook(() => useMetagenConfEvents(CONF_ID), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(lastUrl()).toBe(
      `/spoke/metagen/conf/${encodeURIComponent(CONF_ID)}/event`,
    );
  });

  it("does not fire when the conf id is empty", () => {
    renderHook(() => useMetagenConfEvents(""), { wrapper: makeWrapper() });
    expect(mockApiFetch).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Cross-conf feed — GET /spoke/metagen/event
// (lib/api/metagen.ts → useMetagenEvents buildUrl)
// ---------------------------------------------------------------------------
describe("useMetagenEvents — time bounds on the cross-conf event URL", () => {
  it("emits both bounds when the window is closed (custom range)", async () => {
    // Positive leg / injection backstop for the absence assertion below.
    const { result } = renderHook(
      () => useMetagenEvents({ from: FROM, to: TO, offset: 0, limit: 20 }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const sp = queryOf(lastUrl());
    expect(sp.get("from")).toBe(FROM);
    expect(sp.get("to")).toBe(TO);
    expect(sp.get("offset")).toBe("0");
    expect(sp.get("limit")).toBe("20");
    expect(lastUrl()).toContain("/spoke/metagen/event");
  });

  it("emits from= and NO to= when the resolved window is open above", async () => {
    // The shape a RangePicker preset produces on the cross-conf events feed.
    const { result } = renderHook(
      () => useMetagenEvents({ from: FROM, offset: 0, limit: 20 }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const sp = queryOf(lastUrl());
    expect(sp.get("from")).toBe(FROM);
    // spec/API.md §Query Parameters — the range stays unbounded above.
    expect(sp.has("to")).toBe(false);
    expect(sp.get("offset")).toBe("0");
    expect(sp.get("limit")).toBe("20");
  });

  it("emits no query string at all when no params are given", async () => {
    const { result } = renderHook(() => useMetagenEvents(), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(lastUrl()).toBe("/spoke/metagen/event");
  });
});
