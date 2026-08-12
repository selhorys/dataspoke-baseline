/**
 * Tests for lib/api/ingestion.ts — URL construction, query-param encoding,
 * and mutation request paths.
 *
 * Spec traces:
 *   - spec/API.md §Ingestion routes: GET /spoke/ingestion/sources,
 *     GET /spoke/ingestion/sources/{id}, POST /spoke/ingestion/sources/{id}/method/run,
 *     GET /spoke/ingestion/sources/{id}/datasets,
 *     GET /spoke/ingestion/sources/{id}/event,
 *     GET /spoke/ingestion/unmanaged,
 *     GET /spoke/common/data/{urn}/attr/ingestion,
 *     GET /spoke/common/data/{urn}/event/ingestion
 *   - spec/API.md §dry_run: dry_run is a query param on .../method/run (not a body field)
 *   - spec/feature/FRONTEND_INGESTION.md §Source Detail §Run: dry_run toggle posts as QP
 *
 * Strategy: mock @/lib/api/client so apiFetch is a spy that records the URL it
 * was called with. Each hook (or mutation fn) is driven via renderHook inside a
 * QueryClient wrapper so the queryFn fires.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// ---------------------------------------------------------------------------
// Mock @/lib/api/client before any ingestion module is imported.
// vi.hoisted() ensures mockApiFetch is available at the time vi.mock factory
// runs (vi.mock is hoisted to the top of the module by Vitest transforms).
// ---------------------------------------------------------------------------
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

import { ApiError } from "@/lib/api/client";
import { defaultQueryRetry } from "@/lib/api/error-policy";
import {
  useIngestionSources,
  useIngestionSource,
  useCreateIngestionSource,
  useReplaceIngestionSource,
  usePatchIngestionSource,
  useDeleteIngestionSource,
  useRunIngestionSource,
  useIngestionSourceDatasets,
  useIngestionSourceEvents,
  useIngestionUnmanaged,
  useIngestionSecrets,
  useIngestionReverseLookup,
  useIngestionDatasetEvents,
  useIngestionSourceDatasetCounts,
  useIngestionSourceLatestRuns,
  selectLatestRunEvent,
} from "./ingestion";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Wrap a hook with a fresh QueryClient so queries fire immediately. */
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

/** Captured URL from the most recent apiFetch call. */
function lastUrl(): string {
  return mockApiFetch.mock.calls[mockApiFetch.mock.calls.length - 1][0] as string;
}

/** Captured method option from the most recent apiFetch call. */
function lastMethod(): string | undefined {
  const opts = mockApiFetch.mock.calls[mockApiFetch.mock.calls.length - 1][1] as
    | { method?: string }
    | undefined;
  return opts?.method;
}

beforeEach(() => {
  vi.clearAllMocks();
  // Default resolution so hooks don't error out during URL-capture tests.
  mockApiFetch.mockResolvedValue({ sources: [], total_count: 0, offset: 0, limit: 20 });
});

// ---------------------------------------------------------------------------
// 1. Source list — buildSourceListUrl
// ---------------------------------------------------------------------------
describe("useIngestionSources — URL construction", () => {
  it("calls GET /spoke/ingestion/sources with no query string when params are empty", async () => {
    const { result } = renderHook(() => useIngestionSources(), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(lastUrl()).toBe("/spoke/ingestion/sources");
  });

  it("appends offset and limit as query params", async () => {
    mockApiFetch.mockResolvedValue({ sources: [], total_count: 0, offset: 20, limit: 20 });
    const { result } = renderHook(
      () => useIngestionSources({ offset: 20, limit: 20 }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const url = lastUrl();
    expect(url).toContain("offset=20");
    expect(url).toContain("limit=20");
  });

  it("appends mode filter when provided", async () => {
    const { result } = renderHook(
      () => useIngestionSources({ mode: "PASSIVE" }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(lastUrl()).toContain("mode=PASSIVE");
  });

  it("omits mode param when mode is undefined", async () => {
    const { result } = renderHook(
      () => useIngestionSources({ offset: 0, limit: 20 }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(lastUrl()).not.toContain("mode=");
  });

  it("produces the correct combined URL with all params", async () => {
    const { result } = renderHook(
      () =>
        useIngestionSources({
          offset: 40,
          limit: 20,
          mode: "ACTIVE_CUSTOM_MANAGED",
        }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const url = lastUrl();
    expect(url).toContain("/spoke/ingestion/sources?");
    expect(url).toContain("offset=40");
    expect(url).toContain("limit=20");
    expect(url).toContain("mode=ACTIVE_CUSTOM_MANAGED");
  });

  // The list never carries an ad_hoc query param — internal CLI wrapper sources
  // are hidden by the backend, not selected by a client filter.
  it("never appends an ad_hoc query param", async () => {
    const { result } = renderHook(
      () => useIngestionSources({ mode: "DATAHUB_MANAGED" }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(lastUrl()).not.toContain("ad_hoc");
  });
});

// ---------------------------------------------------------------------------
// 2. Single source — encodeURIComponent on id
// ---------------------------------------------------------------------------
describe("useIngestionSource — URL construction", () => {
  it("calls GET /spoke/ingestion/sources/{id}", async () => {
    mockApiFetch.mockResolvedValue({
      id: "src-1",
      mode: "ACTIVE_CUSTOM_MANAGED",
      name: "test",
      schedule: null,
      recipe: {},
      platform: "postgres",
      status: "OK",
      datahub_source_urn: null,
      created_at: "2024-01-01T00:00:00Z",
      updated_at: "2024-01-01T00:00:00Z",
    });
    const { result } = renderHook(() => useIngestionSource("src-1"), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(lastUrl()).toBe("/spoke/ingestion/sources/src-1");
  });

  it("URL-encodes source ids containing special characters", async () => {
    mockApiFetch.mockResolvedValue({
      id: "src/with/slash",
      mode: "ACTIVE_CUSTOM_MANAGED",
      name: "test",
      schedule: null,
      recipe: {},
      platform: "postgres",
      status: "OK",
      datahub_source_urn: null,
      created_at: "2024-01-01T00:00:00Z",
      updated_at: "2024-01-01T00:00:00Z",
    });
    const { result } = renderHook(
      () => useIngestionSource("src/with/slash"),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(lastUrl()).toBe(
      `/spoke/ingestion/sources/${encodeURIComponent("src/with/slash")}`,
    );
  });

  it("does not fire when id is empty string", () => {
    // enabled: !!id — empty string disables the query
    renderHook(() => useIngestionSource(""), { wrapper: makeWrapper() });
    // Give a tick for any async effects
    expect(mockApiFetch).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// 3. Run — dry_run as query param (spec/API.md §dry_run)
// ---------------------------------------------------------------------------
describe("useRunIngestionSource — dry_run query param", () => {
  it("posts to .../method/run without query param when dry_run=false", async () => {
    mockApiFetch.mockResolvedValue({ run_id: "r1", status: "started", detail: {} });
    const { result } = renderHook(() => useRunIngestionSource("src-1"), {
      wrapper: makeWrapper(),
    });
    await result.current.mutateAsync({ dry_run: false });
    expect(lastUrl()).toBe("/spoke/ingestion/sources/src-1/method/run");
    expect(lastMethod()).toBe("POST");
  });

  it("posts to .../method/run?dry_run=true when dry_run=true", async () => {
    mockApiFetch.mockResolvedValue({ run_id: "r2", status: "dry_run", detail: {} });
    const { result } = renderHook(() => useRunIngestionSource("src-1"), {
      wrapper: makeWrapper(),
    });
    await result.current.mutateAsync({ dry_run: true });
    expect(lastUrl()).toBe("/spoke/ingestion/sources/src-1/method/run?dry_run=true");
    expect(lastMethod()).toBe("POST");
  });

  it("defaults to dry_run=false when no args are passed", async () => {
    mockApiFetch.mockResolvedValue({ run_id: "r3", status: "started", detail: {} });
    const { result } = renderHook(() => useRunIngestionSource("src-1"), {
      wrapper: makeWrapper(),
    });
    await result.current.mutateAsync({});
    // No ?dry_run=true in the URL
    expect(lastUrl()).not.toContain("dry_run=true");
  });

  it("URL-encodes the source id in the run URL", async () => {
    mockApiFetch.mockResolvedValue({ run_id: "r4", status: "started", detail: {} });
    const { result } = renderHook(
      () => useRunIngestionSource("special id/with slash"),
      { wrapper: makeWrapper() },
    );
    await result.current.mutateAsync({});
    expect(lastUrl()).toContain(
      `/spoke/ingestion/sources/${encodeURIComponent("special id/with slash")}/method/run`,
    );
  });
});

// ---------------------------------------------------------------------------
// 4. Source datasets — buildPageQuery
// ---------------------------------------------------------------------------
describe("useIngestionSourceDatasets — URL construction", () => {
  it("calls GET /spoke/ingestion/sources/{id}/datasets with no QS when params empty", async () => {
    mockApiFetch.mockResolvedValue({
      datasets: [],
      total_count: 0,
      offset: 0,
      limit: 20,
    });
    const { result } = renderHook(
      () => useIngestionSourceDatasets("src-1"),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(lastUrl()).toBe("/spoke/ingestion/sources/src-1/datasets");
  });

  it("appends offset and limit as query params", async () => {
    mockApiFetch.mockResolvedValue({
      datasets: [],
      total_count: 0,
      offset: 10,
      limit: 10,
    });
    const { result } = renderHook(
      () => useIngestionSourceDatasets("src-1", { offset: 10, limit: 10 }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const url = lastUrl();
    expect(url).toContain("offset=10");
    expect(url).toContain("limit=10");
  });
});

// ---------------------------------------------------------------------------
// 5. Source events — buildEventQuery (always includes sort=occurred_at_desc)
// ---------------------------------------------------------------------------
describe("useIngestionSourceEvents — URL construction", () => {
  beforeEach(() => {
    mockApiFetch.mockResolvedValue({
      events: [],
      total_count: 0,
      offset: 0,
      limit: 20,
    });
  });

  it("always includes sort=occurred_at_desc", async () => {
    const { result } = renderHook(
      () => useIngestionSourceEvents("src-1"),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(lastUrl()).toContain("sort=occurred_at_desc");
  });

  it("appends from and to filter params when provided", async () => {
    const { result } = renderHook(
      () =>
        useIngestionSourceEvents("src-1", {
          from: "2024-01-01T00:00",
          to: "2024-02-01T00:00",
        }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const url = lastUrl();
    expect(url).toContain("from=");
    expect(url).toContain("to=");
  });

  it("emits from= and NO to= for an open window (preset)", async () => {
    // spec/feature/FRONTEND_BASIC.md §shared-component-notes (RangePicker):
    // "**A preset resolves to an open-ended window** — the lower bound only,
    // with `to`/`until` omitted — so the read always reaches the present".
    // spec/API.md §Query Parameters, `to`: "Optional — omitting it leaves the
    // range unbounded above, so the filter reaches the newest record".
    //
    // This is the shape the ingestion source-detail page actually produces
    // (`to: range.to`, absent under a preset). The both-bounds test above is the
    // backstop for this absence assertion: it proves the builder does emit `to=`
    // when one is supplied, so a missing `to=` here means the caller omitted it,
    // not that the builder can never produce it.
    const from = "2024-03-01T08:30:00.000Z";
    const { result } = renderHook(
      () => useIngestionSourceEvents("src-1", { from, offset: 0, limit: 20 }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const url = lastUrl();
    expect(url).toContain(`from=${encodeURIComponent(from)}`);
    expect(url).not.toContain("to=");
    // …and the rest of the query is intact, so "no to=" is not the side effect
    // of the builder having bailed out.
    expect(url).toContain("limit=20");
    expect(url).toContain("sort=occurred_at_desc");
  });

  it("omits from and to when not provided", async () => {
    const { result } = renderHook(
      () => useIngestionSourceEvents("src-1", { offset: 0, limit: 20 }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const url = lastUrl();
    expect(url).not.toContain("from=");
    expect(url).not.toContain("to=");
  });

  it("URL-encodes the source id", async () => {
    const { result } = renderHook(
      () => useIngestionSourceEvents("src/slash"),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(lastUrl()).toContain(
      `/spoke/ingestion/sources/${encodeURIComponent("src/slash")}/event`,
    );
  });
});

// ---------------------------------------------------------------------------
// 6. Unmanaged bucket — buildPageQuery
// ---------------------------------------------------------------------------
describe("useIngestionUnmanaged — URL construction", () => {
  it("calls GET /spoke/ingestion/unmanaged with no QS when params are empty", async () => {
    mockApiFetch.mockResolvedValue({
      dataset_urns: [],
      total_count: 0,
      offset: 0,
      limit: 50,
    });
    const { result } = renderHook(() => useIngestionUnmanaged(), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(lastUrl()).toBe("/spoke/ingestion/unmanaged");
  });

  it("appends pagination params when provided", async () => {
    mockApiFetch.mockResolvedValue({
      dataset_urns: [],
      total_count: 0,
      offset: 50,
      limit: 50,
    });
    const { result } = renderHook(
      () => useIngestionUnmanaged({ offset: 50, limit: 50 }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const url = lastUrl();
    expect(url).toContain("offset=50");
    expect(url).toContain("limit=50");
  });
});

// ---------------------------------------------------------------------------
// 7. Secrets — enabled guard
// ---------------------------------------------------------------------------
describe("useIngestionSecrets — enabled guard", () => {
  it("calls GET /spoke/ingestion/secrets when enabled=true", async () => {
    mockApiFetch.mockResolvedValue({ secrets: [] });
    const { result } = renderHook(
      () => useIngestionSecrets(true),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(lastUrl()).toBe("/spoke/ingestion/secrets");
  });

  it("does NOT call apiFetch when enabled=false", () => {
    renderHook(() => useIngestionSecrets(false), { wrapper: makeWrapper() });
    expect(mockApiFetch).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// 7b. Secrets — documented per-hook retry exception
// ---------------------------------------------------------------------------
describe("useIngestionSecrets — a 503 is the answer, not an obstacle", () => {
  // spec: spec/feature/FRONTEND_BASIC.md §Query Error Policy — "The ingestion
  //   secret-resolver read (GET /spoke/ingestion/secrets) treats any 503 as final.
  //   That read exists to report whether the resolver is reachable at all, so an
  //   unavailable resolver is the answer rather than an obstacle to it."
  // This is one of the two exceptions the spec grants to the global policy, which
  // retries a 503 twice; deleting the override on the grounds that the global rule
  // now covers this read would violate the spec silently.
  //
  // The wrapper below runs the app's own policy rather than `retry: false`: with
  // retries switched off wholesale, a one-attempt result would prove nothing about
  // the override.
  function makePolicyWrapper() {
    const qc = new QueryClient({
      defaultOptions: {
        queries: { retry: defaultQueryRetry, retryDelay: 0, gcTime: 0 },
        mutations: { retry: false },
      },
    });
    return function Wrapper({ children }: { children: React.ReactNode }) {
      return React.createElement(QueryClientProvider, { client: qc }, children);
    };
  }

  it("issues exactly one attempt on a 503", async () => {
    mockApiFetch.mockRejectedValue(
      new ApiError({
        error_code: "STORAGE_UNAVAILABLE",
        message: "resolver unreachable",
        trace_id: "aaaaaaaa-0000-0000-0000-000000000000",
        resp_time: "2026-07-01T00:00:00Z",
      }, 503),
    );

    const { result } = renderHook(() => useIngestionSecrets(true), {
      wrapper: makePolicyWrapper(),
    });
    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(mockApiFetch).toHaveBeenCalledTimes(1);
  });

  it("still defers to the global policy for every other class — a 500 is retried twice", async () => {
    // Backstop for the assertion above: under the same wrapper a transient
    // failure still costs three attempts, so the single attempt on a 503 comes
    // from the override rather than from retries being off.
    mockApiFetch.mockRejectedValue(
      new ApiError({
        error_code: "INTERNAL_ERROR",
        message: "boom",
        trace_id: "aaaaaaaa-0000-0000-0000-000000000000",
        resp_time: "2026-07-01T00:00:00Z",
      }, 500),
    );

    const { result } = renderHook(() => useIngestionSecrets(true), {
      wrapper: makePolicyWrapper(),
    });
    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(mockApiFetch).toHaveBeenCalledTimes(3);
  });

  it("does not retry a 403 either — Readers are not permitted this read", async () => {
    // spec/API.md §Ingestion: the route answers 403 READ_ONLY_ROLE for Readers.
    // The global 4xx rule covers this, which is why the hook's own override no
    // longer names 403.
    mockApiFetch.mockRejectedValue(
      new ApiError({
        error_code: "READ_ONLY_ROLE",
        message: "read-only",
        trace_id: "aaaaaaaa-0000-0000-0000-000000000000",
        resp_time: "2026-07-01T00:00:00Z",
      }, 403),
    );

    const { result } = renderHook(() => useIngestionSecrets(true), {
      wrapper: makePolicyWrapper(),
    });
    await waitFor(() => expect(result.current.isError).toBe(true));

    expect(mockApiFetch).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// 8. Per-dataset reverse-lookup — URN encoding
// ---------------------------------------------------------------------------
describe("useIngestionReverseLookup — URL construction", () => {
  const sampleUrn =
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)";

  it("calls GET /spoke/common/data/{urn}/attr/ingestion with encoded URN", async () => {
    mockApiFetch.mockResolvedValue({
      dataset_urn: sampleUrn,
      source_id: null,
      mode: null,
      name: null,
      latest_run: null,
    });
    const { result } = renderHook(
      () => useIngestionReverseLookup(sampleUrn),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(lastUrl()).toBe(
      `/spoke/common/data/${encodeURIComponent(sampleUrn)}/attr/ingestion`,
    );
  });

  it("does not fire when datasetUrn is empty", () => {
    renderHook(() => useIngestionReverseLookup(""), { wrapper: makeWrapper() });
    expect(mockApiFetch).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// 9. Per-dataset events — URN encoding + limit + sort
// ---------------------------------------------------------------------------
describe("useIngestionDatasetEvents — URL construction", () => {
  const sampleUrn =
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)";

  it("includes the limit param and sort=occurred_at_desc", async () => {
    mockApiFetch.mockResolvedValue({
      events: [],
      total_count: 0,
      offset: 0,
      limit: 10,
    });
    const { result } = renderHook(
      () => useIngestionDatasetEvents(sampleUrn, { limit: 10 }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const url = lastUrl();
    expect(url).toContain("limit=10");
    expect(url).toContain("sort=occurred_at_desc");
    expect(url).toContain(
      `/spoke/common/data/${encodeURIComponent(sampleUrn)}/event/ingestion`,
    );
  });

  it("appends from and to when provided", async () => {
    mockApiFetch.mockResolvedValue({
      events: [],
      total_count: 0,
      offset: 0,
      limit: 10,
    });
    const { result } = renderHook(
      () =>
        useIngestionDatasetEvents(sampleUrn, {
          limit: 10,
          from: "2024-01-01T00:00:00.000Z",
          to: "2024-02-01T00:00:00.000Z",
        }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    const url = lastUrl();
    expect(url).toContain("from=");
    expect(url).toContain("to=");
  });

  it("does not fire when datasetUrn is empty", () => {
    renderHook(() => useIngestionDatasetEvents("", { limit: 10 }), {
      wrapper: makeWrapper(),
    });
    expect(mockApiFetch).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// 10. useIngestionSourceDatasetCounts — per-id datasets?limit=… URL
// ---------------------------------------------------------------------------
describe("useIngestionSourceDatasetCounts — URL construction", () => {
  it("fires datasets?offset=0&limit=… with sort=occurred_at_desc for each source id", async () => {
    mockApiFetch.mockResolvedValue({
      datasets: [],
      total_count: 5,
      offset: 0,
      limit: 1,
    });
    const { result } = renderHook(
      () => useIngestionSourceDatasetCounts(["src-a"]),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current[0].isSuccess).toBe(true));
    const url = lastUrl();
    // Path and offset are stable; limit=1 is the impl's count-probe choice (not a spec contract).
    expect(url).toContain("/spoke/ingestion/sources/src-a/datasets");
    expect(url).toContain("offset=0");
    expect(url).toContain("limit=");
  });
});

// ---------------------------------------------------------------------------
// 11. useIngestionSourceLatestRuns — per-id event?limit=…&sort=occurred_at_desc
// ---------------------------------------------------------------------------
describe("useIngestionSourceLatestRuns — URL construction", () => {
  it("fires event?offset=0&limit=…&sort=occurred_at_desc for each source id", async () => {
    mockApiFetch.mockResolvedValue({
      events: [],
      total_count: 0,
      offset: 0,
      limit: 1,
    });
    const { result } = renderHook(
      () => useIngestionSourceLatestRuns(["src-b"]),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current[0].isSuccess).toBe(true));
    const url = lastUrl();
    // spec: spec/feature/FRONTEND_INGESTION.md §List View — "The status comes from
    //   GET /spoke/ingestion/sources/{id}/event?sort=occurred_at_desc requested at the
    //   route's maximum page size, because that feed carries more than run outcomes".
    //   That maximum is 1000 (the route declares limit as le=1000). The value is
    //   load-bearing, not cosmetic: at limit=1 the derivation sees only the newest event,
    //   so a source-lifecycle row or a per-dataset observation above the run outcome
    //   leaves the status column blank or wrong — the exact regression this hook fixes.
    expect(url).toContain("/spoke/ingestion/sources/src-b/event");
    expect(url).toContain("offset=0");
    expect(url).toContain("limit=1000");
    expect(url).toContain("sort=occurred_at_desc");
  });

  it("exposes the derived run outcome as each result's data, not the raw page", async () => {
    // The derivation is exhaustively covered below as a pure function, and the list-view
    // component test mocks this hook module wholesale — so neither can see whether the
    // hook actually APPLIES the derivation. Without that wiring the hook hands the
    // component a whole event page: `page.events[0]` is then the newest row of any kind,
    // which is precisely the inversion (#160) this hook exists to fix, and the status
    // column silently returns to reading the head of the feed.
    //
    // The page below is the inverted shape — a newer per-dataset observation above an
    // older run failure — so a hook that returned the page (or applied no `select`) yields
    // an object with an `events` array rather than the FAIL event, and the assertions
    // separate the two.
    //
    // spec: spec/feature/FRONTEND_INGESTION.md §List View — "Each row's status cell shows
    //   the newest **run outcome** … derived from that page by two predicates".
    mockApiFetch.mockResolvedValue({
      events: [
        {
          id: "ev-obs",
          entity_type: "ingestion_source",
          entity_id: "src-c",
          event_type: "INGESTION.COMPLETE",
          status: "success",
          detail: {
            source: "last_ingested_observation",
            dataset_urn:
              "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)",
          },
          occurred_at: "2026-01-02T12:00:00Z",
        },
        {
          id: "ev-fail",
          entity_type: "ingestion_source",
          entity_id: "src-c",
          event_type: "INGESTION.FAIL",
          status: "error",
          detail: { run_id: "run-42", platform: "postgres" },
          occurred_at: "2026-01-02T10:00:00Z",
        },
      ],
      total_count: 2,
      offset: 0,
      limit: 1000,
    });

    const { result } = renderHook(() => useIngestionSourceLatestRuns(["src-c"]), {
      wrapper: makeWrapper(),
    });
    await waitFor(() => expect(result.current[0].isSuccess).toBe(true));

    const data = result.current[0].data;
    expect(
      data && "events" in (data as object),
      "the hook must expose the derived event, not the raw page envelope",
    ).toBe(false);
    expect(data?.event_type).toBe("INGESTION.FAIL");
    expect(data?.status).toBe("error");
    expect(data?.detail.run_id).toBe("run-42");
  });
});

// ---------------------------------------------------------------------------
// 12. Mutations — HTTP method and URL
// ---------------------------------------------------------------------------
describe("useCreateIngestionSource — POST /spoke/ingestion/sources", () => {
  it("calls POST /spoke/ingestion/sources with the request body", async () => {
    const responseSource = {
      id: "src-new",
      mode: "ACTIVE_CUSTOM_MANAGED",
      name: "new",
      schedule: null,
      recipe: { source: { type: "postgres", config: {} } },
      platform: "postgres",
      status: "OK",
      datahub_source_urn: null,
      created_at: "2024-01-01T00:00:00Z",
      updated_at: "2024-01-01T00:00:00Z",
    };
    mockApiFetch.mockResolvedValue(responseSource);
    const { result } = renderHook(() => useCreateIngestionSource(), {
      wrapper: makeWrapper(),
    });
    await result.current.mutateAsync({
      mode: "ACTIVE_CUSTOM_MANAGED",
      name: "new",
      schedule: null,
      recipe: { source: { type: "postgres", config: {} } },
    });
    expect(lastUrl()).toBe("/spoke/ingestion/sources");
    expect(lastMethod()).toBe("POST");
  });
});

describe("useReplaceIngestionSource — PUT /spoke/ingestion/sources/{id}", () => {
  it("calls PUT with the correct URL including encoded id", async () => {
    mockApiFetch.mockResolvedValue({
      id: "src-1",
      mode: "ACTIVE_CUSTOM_MANAGED",
      name: "updated",
      schedule: "0 0 * * *",
      recipe: {},
      platform: "postgres",
      status: "OK",
      datahub_source_urn: null,
      created_at: "2024-01-01T00:00:00Z",
      updated_at: "2024-01-02T00:00:00Z",
    });
    const { result } = renderHook(
      () => useReplaceIngestionSource("src-1"),
      { wrapper: makeWrapper() },
    );
    await result.current.mutateAsync({
      mode: "ACTIVE_CUSTOM_MANAGED",
      name: "updated",
      schedule: "0 0 * * *",
      recipe: {},
    });
    expect(lastUrl()).toBe("/spoke/ingestion/sources/src-1");
    expect(lastMethod()).toBe("PUT");
  });
});

describe("usePatchIngestionSource — PATCH /spoke/ingestion/sources/{id}", () => {
  it("calls PATCH with the correct URL", async () => {
    mockApiFetch.mockResolvedValue({
      id: "src-1",
      mode: "ACTIVE_CUSTOM_MANAGED",
      name: "patched",
      schedule: null,
      recipe: {},
      platform: "postgres",
      status: "OK",
      datahub_source_urn: null,
      created_at: "2024-01-01T00:00:00Z",
      updated_at: "2024-01-02T00:00:00Z",
    });
    const { result } = renderHook(
      () => usePatchIngestionSource("src-1"),
      { wrapper: makeWrapper() },
    );
    await result.current.mutateAsync({ name: "patched" });
    expect(lastUrl()).toBe("/spoke/ingestion/sources/src-1");
    expect(lastMethod()).toBe("PATCH");
  });
});

describe("useDeleteIngestionSource — DELETE /spoke/ingestion/sources/{id}", () => {
  it("calls DELETE with the correct URL", async () => {
    mockApiFetch.mockResolvedValue(undefined);
    const { result } = renderHook(
      () => useDeleteIngestionSource("src-1"),
      { wrapper: makeWrapper() },
    );
    await result.current.mutateAsync();
    expect(lastUrl()).toBe("/spoke/ingestion/sources/src-1");
    expect(lastMethod()).toBe("DELETE");
  });
});

// ---------------------------------------------------------------------------
// 13. selectLatestRunEvent — the list view's run-status derivation
//
// The source event feed carries more than run outcomes: per-dataset ingestion
// observations and source-lifecycle events share it, and either can be newer than
// the run the status badge must report. The derivation applies two predicates in the
// backend's order — an event-type whitelist, then a null-safe producer blacklist.
//
// spec: spec/feature/FRONTEND_INGESTION.md §List View — "The newest **run outcome** is
//   derived from that page by two predicates … 1. **Event-type whitelist** — keep only
//   `INGESTION.COMPLETE` and `INGESTION.FAIL` … 2. **Producer blacklist, null-safe** —
//   of those, drop rows whose `detail.source` names a per-dataset observation producer.
//   A row carrying no `detail.source` is **kept**".
// spec: spec/feature/FRONTEND_INGESTION.md §List View — "Both predicates are required
//   and neither is sufficient alone — the whitelist alone lets a per-dataset observation
//   outrank an older failure, the blacklist alone lets a newer `SOURCE_UPDATE`
//   (`status="success"`) do the same."
// spec: spec/feature/BACKEND.md §Event Catalogue — the four `detail.source` producers.
// ---------------------------------------------------------------------------

/** One row of `GET /spoke/ingestion/sources/{id}/event`, newest-first. */
function evt(
  event_type: string,
  status: string,
  detail: Record<string, unknown>,
  occurred_at: string,
) {
  return {
    id: `ev-${occurred_at}`,
    entity_type: "ingestion_source",
    entity_id: "src-1",
    event_type,
    status,
    detail,
    occurred_at,
  };
}

/** A `GET …/event?sort=occurred_at_desc` page envelope. */
function page(events: ReturnType<typeof evt>[]) {
  return { events, total_count: events.length, offset: 0, limit: 1000 };
}

const OBSERVATION = {
  source: "last_ingested_observation",
  dataset_urn:
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)",
};

describe("selectLatestRunEvent — newest run outcome on a page", () => {
  it("reports an older FAIL over a newer per-dataset observation", () => {
    // The observation is newer AND carries status="success", so a derivation that took
    // the head of the feed would render a green badge over a failed run.
    const result = selectLatestRunEvent(
      page([
        evt("INGESTION.COMPLETE", "success", OBSERVATION, "2026-01-02T12:00:00Z"),
        evt(
          "INGESTION.FAIL",
          "error",
          { run_id: "run-42", platform: "postgres" },
          "2026-01-02T10:00:00Z",
        ),
      ]),
    );
    expect(result?.event_type).toBe("INGESTION.FAIL");
    expect(result?.status).toBe("error");
  });

  it("reports an older FAIL over a newer SOURCE_UPDATE lifecycle event", () => {
    // SOURCE_UPDATE carries status="success" and NO detail.source key, so the producer
    // blacklist alone keeps it — only the event-type whitelist excludes it. This is the
    // case the first fix attempt for this defect got wrong.
    const result = selectLatestRunEvent(
      page([
        evt(
          "INGESTION.SOURCE_UPDATE",
          "success",
          { operation: "PATCH", fields_changed: ["schedule"] },
          "2026-01-02T12:00:00Z",
        ),
        evt(
          "INGESTION.FAIL",
          "error",
          { run_id: "run-42" },
          "2026-01-02T10:00:00Z",
        ),
      ]),
    );
    expect(result?.event_type).toBe("INGESTION.FAIL");
  });

  it("keeps a key-less inline run event — the ACM record has no detail.source", () => {
    // The inline ACTIVE_CUSTOM_MANAGED run record carries no `source` key at all. A
    // blacklist that treated "not in the allowed set" as "drop" would remove exactly the
    // events the status column exists to report; this is the client-side mirror of the
    // backend's `detail->>'source' IS NULL` disjunct.
    const result = selectLatestRunEvent(
      page([
        evt(
          "INGESTION.COMPLETE",
          "success",
          {
            run_id: "run-inline",
            platform: "postgres",
            dry_run: false,
            emitted_urns_count: 2,
          },
          "2026-01-02T12:00:00Z",
        ),
      ]),
    );
    expect(result?.detail.run_id).toBe("run-inline");
    expect(result?.status).toBe("success");
  });

  it("keeps the datahub_sync mirror — it is a run-level producer", () => {
    // The mirror carries detail.source="datahub_sync", which is NOT an observation
    // producer. A blacklist keyed on "has a detail.source at all" would drop it and every
    // DATAHUB_MANAGED source would show no status.
    const result = selectLatestRunEvent(
      page([
        evt(
          "INGESTION.COMPLETE",
          "success",
          {
            source: "datahub_sync",
            execution_request_urn: "urn:li:dataHubExecutionRequest:run-1",
            duration_ms: 5000,
          },
          "2026-01-02T12:00:00Z",
        ),
      ]),
    );
    expect(result?.detail.source).toBe("datahub_sync");
  });

  it("drops passive_observation as well as last_ingested_observation", () => {
    // Both observation producers must be excluded; seeding only one would not catch a
    // blacklist that named a single producer.
    const result = selectLatestRunEvent(
      page([
        evt(
          "INGESTION.COMPLETE",
          "success",
          {
            source: "passive_observation",
            dataset_urn: OBSERVATION.dataset_urn,
            operation_type: "INSERT",
          },
          "2026-01-02T13:00:00Z",
        ),
        evt("INGESTION.COMPLETE", "success", OBSERVATION, "2026-01-02T12:00:00Z"),
        evt("INGESTION.COMPLETE", "success", { run_id: "run-real" }, "2026-01-02T09:00:00Z"),
      ]),
    );
    expect(result?.detail.run_id).toBe("run-real");
  });

  it("returns undefined for a page holding only observations", () => {
    // The PASSIVE reading: neither run-level producer covers that mode, so its feed holds
    // only per-dataset observations and the status cell renders its muted placeholder.
    // The page is deliberately non-empty, so `undefined` is a filtered result rather than
    // an empty feed.
    const observations = page([
      evt("INGESTION.COMPLETE", "success", OBSERVATION, "2026-01-02T12:00:00Z"),
      evt(
        "INGESTION.COMPLETE",
        "success",
        {
          source: "passive_observation",
          dataset_urn: OBSERVATION.dataset_urn,
          operation_type: "INSERT",
        },
        "2026-01-02T11:00:00Z",
      ),
    ]);
    expect(observations.events).toHaveLength(2);
    expect(selectLatestRunEvent(observations)).toBeUndefined();
  });

  it("returns undefined for an empty page", () => {
    expect(selectLatestRunEvent(page([]))).toBeUndefined();
  });

  it("takes the first surviving row of a newest-first page", () => {
    // The route is requested with sort=occurred_at_desc, so the first surviving row is
    // the newest run outcome. Two run outcomes are seeded so "first" and "any" differ.
    const result = selectLatestRunEvent(
      page([
        evt("INGESTION.COMPLETE", "success", OBSERVATION, "2026-01-02T14:00:00Z"),
        evt("INGESTION.FAIL", "error", { run_id: "run-newer" }, "2026-01-02T13:00:00Z"),
        evt("INGESTION.COMPLETE", "success", { run_id: "run-older" }, "2026-01-02T09:00:00Z"),
      ]),
    );
    expect(result?.detail.run_id).toBe("run-newer");
  });
});
