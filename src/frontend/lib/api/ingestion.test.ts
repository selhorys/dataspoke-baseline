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
    // Path and sort are stable spec contracts; limit=1 is the impl's latest-run probe
    // choice (fetching one event to get its status) — not a spec contract.
    expect(url).toContain("/spoke/ingestion/sources/src-b/event");
    expect(url).toContain("offset=0");
    expect(url).toContain("limit=");
    expect(url).toContain("sort=occurred_at_desc");
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
