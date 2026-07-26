/**
 * Tests for lib/api/validation.ts — URL construction and query-param mapping.
 *
 * Spec traces:
 *   - spec/API.md §Route Catalogue → Data Resource (`/spoke/common/data`), row
 *     `GET /spoke/common/data/{dataset_urn}/attr/validation/result`: "this
 *     endpoint names its end-bound param `until` rather than the convention
 *     table's `to`" — `?from=…&until=…&limit=…`.
 *   - spec/feature/FRONTEND_BASIC.md §shared-component-notes (RangePicker):
 *     "the validation `attr/validation/result` endpoint — which names its
 *     end-bound `until` rather than `to` … — takes the upper bound in that slot
 *     instead". This file pins the invariant that the validation results URL
 *     emits `until=` (and `from=`), never `to=`.
 *   - same section: "**A preset resolves to an open-ended window** — the lower
 *     bound only, with `to`/`until` omitted — so the read always reaches the
 *     present". `until` is the one end-bound slot that is *not* named `to`, so
 *     the open-window case is pinned here as well as in lib/api/data.test.ts.
 *   - spec/feature/FRONTEND_VALIDATION.md §Page contracts: "In `date`
 *     granularity the RangePicker drives `?from=&until=&limit=` — this endpoint
 *     names its end-bound param `until` rather than `to`".
 *
 * Strategy mirrors lib/api/ingestion.test.ts: mock @/lib/api/client so apiFetch
 * is a spy recording the URL, drive the hook via renderHook in a QueryClient
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

import { useValidationResults } from "./validation";

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

const sampleUrn =
  "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)";

beforeEach(() => {
  vi.clearAllMocks();
  mockApiFetch.mockResolvedValue({ results: [], variables: [] });
});

// ---------------------------------------------------------------------------
// Validation results — end-bound param is `until`, never `to`.
// ---------------------------------------------------------------------------
describe("useValidationResults — until=to param mapping invariant", () => {
  it("emits from= and until= (URL-encoded) and never to= for the result endpoint", async () => {
    const from = "2024-01-01T00:00:00.000Z";
    const until = "2024-02-01T23:59:59.999Z";
    const { result } = renderHook(
      () => useValidationResults(sampleUrn, { from, until }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const url = lastUrl();
    // Both bounds present, URL-encoded.
    expect(url).toContain(`from=${encodeURIComponent(from)}`);
    expect(url).toContain(`until=${encodeURIComponent(until)}`);
    // The spec invariant: this endpoint uses `until`, not the convention `to`.
    expect(url).not.toContain("to=");
    // Sanity: it targets the validation result path on the encoded URN.
    expect(url).toContain(
      `/spoke/common/data/${encodeURIComponent(sampleUrn)}/attr/validation/result`,
    );
  });

  it("emits from= and limit= and NO until= for an open window (preset)", async () => {
    // The shape a preset actually produces at this call site:
    // validation-data-panel.tsx maps `until: resultRange.to`, and a preset
    // resolves with `to` absent. spec/feature/FRONTEND_BASIC.md
    // §shared-component-notes: "A preset resolves to an open-ended window — the
    // lower bound only, with `to`/`until` omitted".
    //
    // The both-bounds test above is the backstop this absence assertion needs:
    // it proves the builder does emit `until=` when one is supplied, so a
    // missing `until=` here means the caller omitted it — not that the builder
    // can never produce it.
    const from = "2024-03-01T00:00:00.000Z";
    const { result } = renderHook(
      () => useValidationResults(sampleUrn, { from, limit: 1000 }),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const url = lastUrl();
    expect(url).toContain(`from=${encodeURIComponent(from)}`);
    expect(url).toContain("limit=1000");
    expect(url).not.toContain("until=");
    // …and it does not silently fall back to the convention-table `to` name.
    expect(url).not.toContain("to=");
  });

  it("omits both bounds when neither from nor until is provided", async () => {
    const { result } = renderHook(
      () => useValidationResults(sampleUrn),
      { wrapper: makeWrapper() },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    const url = lastUrl();
    expect(url).not.toContain("from=");
    expect(url).not.toContain("until=");
    expect(url).not.toContain("to=");
  });

  it("does not fire when datasetUrn is empty", () => {
    renderHook(() => useValidationResults(""), { wrapper: makeWrapper() });
    expect(mockApiFetch).not.toHaveBeenCalled();
  });
});
