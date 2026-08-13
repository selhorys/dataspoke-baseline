/**
 * Tests for lib/api/governance.ts — the two end-bound-bearing URL builders:
 * the metric result timeseries (`buildResultsUrl`) and the metric event feed
 * (`buildMetricEventUrl`).
 *
 * Spec traces:
 *   - spec/API.md §Query Parameters: `to` — "End of time-range filter,
 *     inclusive; used on `result` and `event` endpoints. Optional — omitting it
 *     leaves the range unbounded above, so the filter reaches the newest record".
 *   - spec/feature/FRONTEND_BASIC.md §shared-component-notes (RangePicker):
 *     "endpoints whose end-bound param is `to` (events, governance metric
 *     `attr/result`) receive `from`/`to` directly" and "**A preset resolves to an
 *     open-ended window** — the lower bound only, with `to`/`until` omitted — so
 *     the read always reaches the present".
 *   - same section: the RangePicker drives "governance metric detail results +
 *     events" and the "governance dashboard" — the three surfaces whose windows
 *     land on these two builders.
 *
 * These are the remaining two of the four end-bound consumers (the other two are
 * covered by lib/api/data.test.ts and lib/api/validation.test.ts). The open-window
 * assertions each sit behind a both-bounds test in the same block, which is the
 * injection backstop: it proves the builder emits `to=` when one is supplied, so
 * a missing `to=` means the caller omitted it rather than the builder being
 * incapable of producing it.
 *
 * Strategy mirrors lib/api/data.test.ts: mock @/lib/api/client so apiFetch is a
 * spy recording the URL, drive each hook via renderHook in a QueryClient
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

import {
  useMetricResults,
  useLatestMetricResult,
  useMetricEvents,
  useMetricDatasets,
} from "./governance";

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

const METRIC_ID = "gdpr-pii-coverage";
const FROM = "2024-03-01T00:00:00.000Z";
const TO = "2024-03-15T23:59:59.999Z";

beforeEach(() => {
  vi.clearAllMocks();
  mockApiFetch.mockResolvedValue({
    results: [],
    events: [],
    total_count: 0,
    offset: 0,
    limit: 20,
  });
});

// ---------------------------------------------------------------------------
// Metric results — GET /spoke/governance/metric/{id}/attr/result
// (lib/api/governance.ts → buildResultsUrl)
// ---------------------------------------------------------------------------
describe("useMetricResults — time bounds on the result URL", () => {
  it("emits both bounds when the window is closed (custom range)", async () => {
    // Positive leg / injection backstop for the absence assertion below.
    const { result } = renderHook(
      () => useMetricResults(METRIC_ID, { from: FROM, to: TO, limit: 500 }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const sp = queryOf(lastUrl());
    expect(sp.get("from")).toBe(FROM);
    expect(sp.get("to")).toBe(TO);
    expect(sp.get("limit")).toBe("500");
    // Sanity: it targets the metric result timeseries route.
    expect(lastUrl()).toContain(
      `/spoke/governance/metric/${METRIC_ID}/attr/result`,
    );
  });

  it("emits from= and NO to= when the resolved window is open above", async () => {
    // The shape a preset actually produces at the metric-detail and dashboard
    // call sites (`to: resultRange.to` / `to: range.to`, absent under a preset).
    const { result } = renderHook(
      () => useMetricResults(METRIC_ID, { from: FROM, limit: 500 }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const sp = queryOf(lastUrl());
    expect(sp.get("from")).toBe(FROM);
    // spec/API.md §Query Parameters — omitting `to` leaves the range unbounded
    // above. Key absence on the URL is directly observable.
    expect(sp.has("to")).toBe(false);
    // …and the rest of the query survived, so "no to=" is not the builder
    // having bailed out.
    expect(sp.get("limit")).toBe("500");
  });

  it("emits no query string at all when no params are given", async () => {
    const { result } = renderHook(() => useMetricResults(METRIC_ID), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(lastUrl()).toBe(
      `/spoke/governance/metric/${METRIC_ID}/attr/result`,
    );
  });

  it("does not fire when the metric id is empty", () => {
    renderHook(() => useMetricResults(""), { wrapper: makeWrapper() });
    expect(mockApiFetch).not.toHaveBeenCalled();
  });
});

describe("useLatestMetricResult — single newest row", () => {
  it("requests limit=1 and no time bounds", async () => {
    // The dashboard card's "latest value" read shares buildResultsUrl; it is
    // unbounded on both ends by design, so it must not acquire a `to=`.
    const { result } = renderHook(() => useLatestMetricResult(METRIC_ID), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const sp = queryOf(lastUrl());
    expect(sp.get("limit")).toBe("1");
    expect(sp.has("from")).toBe(false);
    expect(sp.has("to")).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// Metric events — GET /spoke/governance/metric/{id}/event
// (lib/api/governance.ts → buildMetricEventUrl)
// ---------------------------------------------------------------------------
describe("useMetricEvents — time bounds on the event URL", () => {
  it("emits both bounds when the window is closed (custom range)", async () => {
    // Positive leg / injection backstop for the absence assertion below.
    const { result } = renderHook(
      () =>
        useMetricEvents(METRIC_ID, {
          from: FROM,
          to: TO,
          offset: 0,
          limit: 20,
          sort: "occurred_at_desc",
        }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const sp = queryOf(lastUrl());
    expect(sp.get("from")).toBe(FROM);
    expect(sp.get("to")).toBe(TO);
    expect(sp.get("sort")).toBe("occurred_at_desc");
    expect(lastUrl()).toContain(`/spoke/governance/metric/${METRIC_ID}/event`);
  });

  it("emits from= and NO to= when the resolved window is open above", async () => {
    // The metric-detail events panel maps `to: eventRange.to`, absent under a
    // preset — which is what keeps the 15 s-polled feed reaching new events.
    const { result } = renderHook(
      () =>
        useMetricEvents(METRIC_ID, {
          from: FROM,
          offset: 0,
          limit: 20,
          sort: "occurred_at_desc",
        }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const sp = queryOf(lastUrl());
    expect(sp.get("from")).toBe(FROM);
    expect(sp.has("to")).toBe(false);
    expect(sp.get("offset")).toBe("0");
    expect(sp.get("limit")).toBe("20");
    expect(sp.get("sort")).toBe("occurred_at_desc");
  });

  it("emits no query string at all when no params are given", async () => {
    const { result } = renderHook(() => useMetricEvents(METRIC_ID), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(lastUrl()).toBe(`/spoke/governance/metric/${METRIC_ID}/event`);
  });

  it("does not fire when the metric id is empty", () => {
    renderHook(() => useMetricEvents(""), { wrapper: makeWrapper() });
    expect(mockApiFetch).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Covered datasets — GET /spoke/governance/metric/{id}/dataset
// (lib/api/governance.ts → buildMetricDatasetUrl)
//
// spec/API.md §Metric — "Repeatable `met` query param (default: all three).
//   Paginated (`offset`/`limit`/`total_count`), sortable by `dataset_urn`
//   (default `dataset_urn_asc`)".
// spec/feature/FRONTEND_GOVERNANCE.md §Metric detail (Datasets panel) — "the
//   shared Pagination drives `offset`/`limit` with `sort=dataset_urn`" and "With
//   **zero** toggles selected the client … issues **no request**: an omitted
//   repeatable param and an empty one are the same HTTP request, which the API
//   reads as 'all three'".
// ---------------------------------------------------------------------------
describe("useMetricDatasets — the covered-dataset read", () => {
  it("emits one `met` pair per selected verdict, plus paging and sort", async () => {
    const { result } = renderHook(
      () =>
        useMetricDatasets(METRIC_ID, {
          met: ["true", "false", "unknown"],
          offset: 20,
          limit: 50,
          sort: "dataset_urn",
        }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const sp = queryOf(lastUrl());
    expect(sp.getAll("met")).toEqual(["true", "false", "unknown"]);
    expect(sp.get("offset")).toBe("20");
    expect(sp.get("limit")).toBe("50");
    expect(sp.get("sort")).toBe("dataset_urn");
    expect(lastUrl()).toContain(`/spoke/governance/metric/${METRIC_ID}/dataset`);
  });

  it("narrows to the verdicts still selected", async () => {
    const { result } = renderHook(
      () => useMetricDatasets(METRIC_ID, { met: ["false"], offset: 0, limit: 20 }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(queryOf(lastUrl()).getAll("met")).toEqual(["false"]);
  });

  it("sorts by dataset_urn even when the caller passes no sort", async () => {
    const { result } = renderHook(() => useMetricDatasets(METRIC_ID, { met: ["true"] }), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    expect(queryOf(lastUrl()).get("sort")).toBe("dataset_urn");
  });

  it("issues no request when the caller disables it (zero verdicts selected)", () => {
    // The panel's no-selection state cannot be expressed on the wire, so it is
    // resolved by not asking at all.
    renderHook(() => useMetricDatasets(METRIC_ID, { met: [] }, { enabled: false }), {
      wrapper: makeWrapper(),
    });
    expect(mockApiFetch).not.toHaveBeenCalled();
  });

  it("does not fire when the metric id is empty", () => {
    renderHook(() => useMetricDatasets(""), { wrapper: makeWrapper() });
    expect(mockApiFetch).not.toHaveBeenCalled();
  });
});
