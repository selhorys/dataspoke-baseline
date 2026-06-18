/**
 * Tests for lib/api/ontogen.ts — result-list URL construction, focused on the
 * new server-side `?sort=` param threaded through buildResultUrl + the result
 * hooks.
 *
 * Spec traces:
 *   - spec/API.md §UC3 Ontology Generation result rows
 *     (GET /spoke/ontogen/result/{node,edge,triple}): paginated; sortable by
 *     created_at via ?sort=created_at_asc|created_at_desc (default created_at_desc).
 *   - spec/API_DESIGN_PRINCIPLE_en.md §5: ?sort=<field>_asc|_desc convention.
 *
 * Strategy: mock @/lib/api/client so apiFetch records the URL; buildResultUrl is
 * a pure exported function tested directly, and the hooks are driven via
 * renderHook to confirm they thread `sort` into the request URL.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor } from "@testing-library/react";
import React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const { mockApiFetch } = vi.hoisted(() => ({ mockApiFetch: vi.fn() }));

vi.mock("@/lib/api/client", () => ({
  apiFetch: mockApiFetch,
  ApiError: class ApiError extends Error {},
}));

import { buildResultUrl, useOntogenNodes, useOntogenEdges, useOntogenTriples } from "./ontogen";

function makeWrapper() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return function Wrapper({ children }: { children: React.ReactNode }) {
    return React.createElement(QueryClientProvider, { client: qc }, children);
  };
}

function lastUrl(): string {
  return mockApiFetch.mock.calls[mockApiFetch.mock.calls.length - 1][0] as string;
}

beforeEach(() => {
  vi.clearAllMocks();
  mockApiFetch.mockResolvedValue({ nodes: [], edges: [], triples: [], total_count: 0, offset: 0, limit: 20 });
});

// ---------------------------------------------------------------------------
// buildResultUrl — pure URL construction
// ---------------------------------------------------------------------------
describe("buildResultUrl", () => {
  it("omits the query string entirely when no params are given", () => {
    expect(buildResultUrl("node", {})).toBe("/spoke/ontogen/result/node");
  });

  it("emits sort= when a sort is provided", () => {
    expect(buildResultUrl("node", { sort: "created_at_asc" })).toBe(
      "/spoke/ontogen/result/node?sort=created_at_asc",
    );
  });

  it("emits the desc sort for each result kind", () => {
    for (const kind of ["node", "edge", "triple"] as const) {
      expect(buildResultUrl(kind, { sort: "created_at_desc" })).toBe(
        `/spoke/ontogen/result/${kind}?sort=created_at_desc`,
      );
    }
  });

  it("combines offset, limit, status, and sort in the query string", () => {
    const url = buildResultUrl("triple", {
      offset: 40,
      limit: 50,
      status: "pending",
      sort: "created_at_asc",
    });
    expect(url.startsWith("/spoke/ontogen/result/triple?")).toBe(true);
    const qs = new URLSearchParams(url.split("?")[1]);
    expect(qs.get("offset")).toBe("40");
    expect(qs.get("limit")).toBe("50");
    expect(qs.get("status")).toBe("pending");
    expect(qs.get("sort")).toBe("created_at_asc");
  });

  it("omits sort= when sort is undefined", () => {
    expect(buildResultUrl("edge", { limit: 20 })).toBe(
      "/spoke/ontogen/result/edge?limit=20",
    );
  });
});

// ---------------------------------------------------------------------------
// Hooks thread `sort` into the request URL
// ---------------------------------------------------------------------------
describe("ontogen result hooks — sort threading", () => {
  it("useOntogenNodes passes sort= to apiFetch", async () => {
    renderHook(() => useOntogenNodes({ sort: "created_at_asc" }), { wrapper: makeWrapper() });
    await waitFor(() => expect(mockApiFetch).toHaveBeenCalled());
    expect(lastUrl()).toContain("sort=created_at_asc");
    expect(lastUrl()).toContain("/spoke/ontogen/result/node");
  });

  it("useOntogenEdges passes sort= to apiFetch", async () => {
    renderHook(() => useOntogenEdges({ sort: "created_at_desc" }), { wrapper: makeWrapper() });
    await waitFor(() => expect(mockApiFetch).toHaveBeenCalled());
    expect(lastUrl()).toContain("sort=created_at_desc");
    expect(lastUrl()).toContain("/spoke/ontogen/result/edge");
  });

  it("useOntogenTriples passes sort= to apiFetch", async () => {
    renderHook(() => useOntogenTriples({ sort: "created_at_asc" }), { wrapper: makeWrapper() });
    await waitFor(() => expect(mockApiFetch).toHaveBeenCalled());
    expect(lastUrl()).toContain("sort=created_at_asc");
    expect(lastUrl()).toContain("/spoke/ontogen/result/triple");
  });
});
