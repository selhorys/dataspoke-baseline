/**
 * Tests for lib/api/data.ts — the unified per-dataset event timeline hook.
 *
 * Spec traces:
 *   - spec/API.md §Query Parameters: `to` — "End of time-range filter,
 *     inclusive; used on `result` and `event` endpoints. Optional — omitting it
 *     leaves the range unbounded above, so the filter reaches the newest record".
 *   - spec/feature/FRONTEND_BASIC.md §shared-component-notes (RangePicker):
 *     "endpoints whose end-bound param is `to` (events, governance metric
 *     `attr/result`) receive `from`/`to` directly" and "**A preset resolves to an
 *     open-ended window** — the lower bound only, with `to`/`until` omitted".
 *   - same section: "a preset's *lower* bound is a function of the selection and
 *     the display timezone alone — never of the render or the poll tick —
 *     because it participates in the query key, and re-resolving it per render
 *     would mint a new key every render and spin an unbounded refetch loop."
 *
 * The last trace is what the two render-churn tests below pin: this is the layer
 * where the loop would actually occur (`queryKey: ["data","events",urn,params]`
 * in lib/api/data.ts embeds the bounds), so the guard belongs here rather than
 * only at the component.
 *
 * Strategy mirrors lib/api/validation.test.ts: mock @/lib/api/client so apiFetch
 * is a spy recording the URL, drive the hook via renderHook in a QueryClient
 * wrapper, and assert on the captured URL / call count only (no network).
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

import { useDatasetEvents } from "./data";

/**
 * A QueryClient + its Provider wrapper. `gcTime` is a parameter because the
 * key-churn tests below count cache entries: with the default `gcTime: 0` an
 * abandoned key is evicted the instant its observer moves on, which would erase
 * the very evidence those tests read.
 */
function makeHarness(gcTime = 0) {
  const qc = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime },
      mutations: { retry: false },
    },
  });
  const wrapper = function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(QueryClientProvider, { client: qc }, children);
  };
  return { qc, wrapper };
}

function makeWrapper() {
  return makeHarness().wrapper;
}

/** How many distinct cache entries exist for this dataset's event feed. */
function eventKeyCount(qc: QueryClient): number {
  return qc.getQueryCache().findAll({ queryKey: ["data", "events", sampleUrn] })
    .length;
}

function lastUrl(): string {
  return mockApiFetch.mock.calls[mockApiFetch.mock.calls.length - 1][0] as string;
}

/** Parse the query string of a captured relative URL. */
function queryOf(url: string): URLSearchParams {
  return new URLSearchParams(url.slice(url.indexOf("?") + 1));
}

const sampleUrn =
  "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)";

const FROM = "2024-03-01T08:30:00.000Z";

beforeEach(() => {
  vi.clearAllMocks();
  mockApiFetch.mockResolvedValue({
    offset: 0,
    limit: 20,
    total_count: 0,
    events: [],
  });
});

// ---------------------------------------------------------------------------
// Bound mapping — an open window emits `from` and no `to`.
// ---------------------------------------------------------------------------
describe("useDatasetEvents — time bounds on the event URL", () => {
  it("emits from= and NO to= when the resolved window is open above", async () => {
    const { result } = renderHook(
      () => useDatasetEvents(sampleUrn, { from: FROM }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const sp = queryOf(lastUrl());
    expect(sp.get("from")).toBe(FROM);
    // spec/API.md §Query Parameters — omitting `to` leaves the range unbounded
    // above. Key absence on the URL is directly observable, unlike on the
    // resolved range object.
    expect(sp.has("to")).toBe(false);
    // Sanity: it targets the unified per-dataset event feed on the encoded URN.
    expect(lastUrl()).toContain(
      `/spoke/common/data/${encodeURIComponent(sampleUrn)}/event`,
    );
  });

  it("emits both bounds when the window is closed (custom range)", async () => {
    // Counterweight to the absence assertion above: the builder does forward a
    // `to` when one is supplied, so `has("to") === false` there means the caller
    // omitted it — not that the builder can never emit it.
    const to = "2024-03-10T23:59:59.999Z";
    const { result } = renderHook(
      () => useDatasetEvents(sampleUrn, { from: FROM, to }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const sp = queryOf(lastUrl());
    expect(sp.get("from")).toBe(FROM);
    expect(sp.get("to")).toBe(to);
  });

  it("does not fire when datasetUrn is empty", () => {
    renderHook(() => useDatasetEvents(""), { wrapper: makeWrapper() });
    expect(mockApiFetch).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Query-key stability — the loop guard.
//
// The primary assertion in both tests is the number of distinct cache entries
// the event key accumulates, which React Query materializes synchronously while
// building each render's observer. That makes the guard deterministic: it does
// not depend on whether an extra fetch has had time to land. The call counts are
// asserted alongside it as the observable consequence.
//
// The `to`-bearing case is asserted FIRST so it stands as the positive leg: it
// proves this harness genuinely observes key churn (and the refetch it causes),
// which is what makes the open-window assertions meaningful rather than
// trivially true.
// ---------------------------------------------------------------------------
describe("useDatasetEvents — refetch behaviour across renders", () => {
  it("mints a new key and refetches on every render when the upper bound moves", async () => {
    // This is the failure mode a per-render `to = now` would produce: each render
    // mints a new query key → cache miss → fetch → re-render.
    let tick = 0;
    const { qc, wrapper } = makeHarness(Infinity);
    const { rerender } = renderHook(
      () =>
        useDatasetEvents(sampleUrn, {
          from: FROM,
          to: `2024-03-15T08:30:0${tick}.000Z`,
        }),
      { wrapper },
    );
    await waitFor(() => expect(mockApiFetch).toHaveBeenCalledTimes(1));
    expect(eventKeyCount(qc)).toBe(1);

    for (let i = 1; i <= 5; i += 1) {
      tick = i;
      rerender();
      await waitFor(() => expect(mockApiFetch).toHaveBeenCalledTimes(i + 1));
    }

    // Six distinct keys in the cache — the churn is directly observable here.
    expect(eventKeyCount(qc)).toBe(6);
    expect(mockApiFetch).toHaveBeenCalledTimes(6);

    // …and every fetch went to a distinct URL.
    const urls = new Set(mockApiFetch.mock.calls.map((c) => c[0] as string));
    expect(urls.size).toBe(6);
  });

  it("keeps ONE key across five rerenders when the window is open above", async () => {
    // A stable open window (`from` memoized at the call site, `to` absent) hashes
    // to the same query key every render, so nothing is ever re-fetched.
    //
    // A FRESH object literal is passed on each render — that is what the real
    // call sites do (`{ from: range.from, to: range.to, ... }` is rebuilt in the
    // component body every render). Reusing one module-level object would prove
    // reference stability rather than the structural key hashing the guard
    // actually rests on.
    const { qc, wrapper } = makeHarness(Infinity);
    const { rerender } = renderHook(
      () => useDatasetEvents(sampleUrn, { from: FROM }),
      { wrapper },
    );
    await waitFor(() => expect(mockApiFetch).toHaveBeenCalledTimes(1));
    expect(eventKeyCount(qc)).toBe(1);

    for (let i = 0; i < 5; i += 1) {
      rerender();
      // Deterministic: a changed key materializes its cache entry during the
      // render that mints it, so this fails on the first churning render rather
      // than depending on a fetch landing inside some settle window.
      expect(eventKeyCount(qc)).toBe(1);
    }

    expect(mockApiFetch).toHaveBeenCalledTimes(1);
  });
});
