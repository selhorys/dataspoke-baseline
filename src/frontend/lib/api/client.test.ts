/**
 * Tests for lib/api/client.ts — apiFetch and ensureFreshToken.
 *
 * Spec traces:
 *   - spec/API.md: all requests carry Authorization: Bearer <token>
 *   - spec/API.md §Error Envelope: non-2xx bodies are ApiError with error_code, message,
 *     trace_id, status
 *   - spec/feature/FRONTEND_BASIC.md: silent refresh on 401; single-flight dedup; store
 *     cleared on refresh failure
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import { ApiError } from "./client";

// ---------------------------------------------------------------------------
// Auth store mock — must be declared before any import that triggers
// getAuthStoreState(). Vitest hoists vi.mock() calls.
// ---------------------------------------------------------------------------
const mockStore = {
  accessToken: null as string | null,
  setToken: vi.fn((t: string) => { mockStore.accessToken = t; }),
  clear: vi.fn(() => { mockStore.accessToken = null; }),
};

vi.mock("@/lib/auth/store", () => ({
  useAuthStore: {
    getState: () => mockStore,
  },
}));

// We need to import apiFetch/ensureFreshToken AFTER the mock is set up.
// Use dynamic import inside each describe block via a module-level reference.
import { apiFetch, ensureFreshToken } from "./client";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Build a minimal Response-like object that fetch returns. */
function makeResponse(
  status: number,
  body: unknown,
  ok?: boolean,
): Response {
  const isOk = ok ?? (status >= 200 && status < 300);
  return {
    status,
    ok: isOk,
    statusText: status === 401 ? "Unauthorized" : "Error",
    json: () => Promise.resolve(body),
  } as unknown as Response;
}

function errorBody(overrides?: Partial<{ error_code: string; message: string; trace_id: string }>) {
  return {
    error_code: overrides?.error_code ?? "SOME_ERROR",
    message: overrides?.message ?? "Something went wrong",
    trace_id: overrides?.trace_id ?? "aaaaaaaa-0000-0000-0000-000000000000",
    resp_time: "2024-01-01T00:00:00Z",
  };
}

// ---------------------------------------------------------------------------
// Setup / teardown
// ---------------------------------------------------------------------------
beforeEach(() => {
  vi.restoreAllMocks();
  mockStore.accessToken = null;
  mockStore.setToken.mockClear();
  mockStore.clear.mockClear();
  // Restore setToken side-effect after restoreAllMocks (which resets mock state but keeps impl)
  mockStore.setToken.mockImplementation((t: string) => { mockStore.accessToken = t; });
  mockStore.clear.mockImplementation(() => { mockStore.accessToken = null; });
});

// ---------------------------------------------------------------------------
// 1. URL construction
// ---------------------------------------------------------------------------
describe("apiFetch — URL construction", () => {
  it("prepends /api/v1 to the path", async () => {
    const fetchMock = vi.fn().mockResolvedValue(makeResponse(200, { ok: true }));
    vi.stubGlobal("fetch", fetchMock);

    await apiFetch("/auth/me");

    const calledUrl: string = fetchMock.mock.calls[0][0];
    expect(calledUrl).toMatch(/\/api\/v1\/auth\/me$/);
  });

  it("does not produce double slashes when path starts with /", async () => {
    const fetchMock = vi.fn().mockResolvedValue(makeResponse(200, {}));
    vi.stubGlobal("fetch", fetchMock);

    await apiFetch("/spoke/ingestion/sources");

    const calledUrl: string = fetchMock.mock.calls[0][0];
    expect(calledUrl).not.toContain("//spoke");
    expect(calledUrl).toContain("/api/v1/spoke/ingestion/sources");
  });

  it("always includes the /api/v1 prefix in the outgoing URL", async () => {
    // NEXT_PUBLIC_API_BASE_URL is read at module load time so it cannot be
    // overridden in a running test. This test asserts the invariant that all
    // outgoing requests include the /api/v1 prefix regardless of base URL
    // configuration (spec/API.md: all routes start with /api/v1).
    const fetchMock = vi.fn().mockResolvedValue(makeResponse(200, {}));
    vi.stubGlobal("fetch", fetchMock);

    await apiFetch("/auth/token");

    const calledUrl: string = fetchMock.mock.calls[0][0];
    expect(calledUrl).toContain("/api/v1/auth/token");
  });
});

// ---------------------------------------------------------------------------
// 2. Authorization header
// ---------------------------------------------------------------------------
describe("apiFetch — Authorization header", () => {
  it("attaches Bearer token when store has an access token", async () => {
    mockStore.accessToken = "test-access-token-abc";
    const fetchMock = vi.fn().mockResolvedValue(makeResponse(200, {}));
    vi.stubGlobal("fetch", fetchMock);

    await apiFetch("/auth/me");

    const calledHeaders = fetchMock.mock.calls[0][1].headers as Record<string, string>;
    // normalizeHeaders lowercases all keys; check lowercase authorization
    expect(calledHeaders["authorization"]).toBe("Bearer test-access-token-abc");
  });

  it("omits Authorization header when store has no token", async () => {
    mockStore.accessToken = null;
    const fetchMock = vi.fn().mockResolvedValue(makeResponse(200, {}));
    vi.stubGlobal("fetch", fetchMock);

    await apiFetch("/auth/token");

    const calledHeaders = fetchMock.mock.calls[0][1].headers as Record<string, string>;
    expect(calledHeaders["authorization"]).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// 3. X-Trace-Id header
// ---------------------------------------------------------------------------
describe("apiFetch — X-Trace-Id header", () => {
  it("generates an x-trace-id when the caller does not supply one", async () => {
    const fetchMock = vi.fn().mockResolvedValue(makeResponse(200, {}));
    vi.stubGlobal("fetch", fetchMock);

    await apiFetch("/auth/me");

    const calledHeaders = fetchMock.mock.calls[0][1].headers as Record<string, string>;
    // After normalization all keys are lowercase
    expect(calledHeaders["x-trace-id"]).toBeTruthy();
    expect(calledHeaders["x-trace-id"]).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i,
    );
  });

  it("preserves a caller-supplied x-trace-id over the generated one", async () => {
    const fetchMock = vi.fn().mockResolvedValue(makeResponse(200, {}));
    vi.stubGlobal("fetch", fetchMock);

    const callerTraceId = "cccccccc-1111-2222-3333-444444444444";
    await apiFetch("/auth/me", {
      // Caller supplies the header; normalizeHeaders lowercases it, overriding the generated one
      headers: { "X-Trace-Id": callerTraceId },
    });

    const calledHeaders = fetchMock.mock.calls[0][1].headers as Record<string, string>;
    expect(calledHeaders["x-trace-id"]).toBe(callerTraceId);
  });
});

// ---------------------------------------------------------------------------
// 4. Header normalization (F8 fix)
// ---------------------------------------------------------------------------
describe("apiFetch — header normalization", () => {
  it("accepts a plain object for init.headers and merges entries", async () => {
    const fetchMock = vi.fn().mockResolvedValue(makeResponse(200, {}));
    vi.stubGlobal("fetch", fetchMock);

    await apiFetch("/auth/me", {
      headers: { "X-Custom-Header": "custom-value" },
    });

    const calledHeaders = fetchMock.mock.calls[0][1].headers as Record<string, string>;
    // normalizeHeaders lowercases all keys
    expect(calledHeaders["x-custom-header"]).toBe("custom-value");
    // Content-Type must still be present (also lowercased after normalization)
    expect(calledHeaders["content-type"]).toBeTruthy();
  });

  it("accepts a Headers instance for init.headers and preserves entries", async () => {
    const fetchMock = vi.fn().mockResolvedValue(makeResponse(200, {}));
    vi.stubGlobal("fetch", fetchMock);

    const hdrs = new Headers({ "X-Request-Source": "test-suite" });
    await apiFetch("/auth/me", { headers: hdrs });

    const calledHeaders = fetchMock.mock.calls[0][1].headers as Record<string, string>;
    expect(calledHeaders["x-request-source"]).toBe("test-suite");
  });

  it("accepts a string[][] for init.headers and preserves entries", async () => {
    const fetchMock = vi.fn().mockResolvedValue(makeResponse(200, {}));
    vi.stubGlobal("fetch", fetchMock);

    await apiFetch("/auth/me", {
      headers: [["X-Array-Header", "array-value"]],
    });

    const calledHeaders = fetchMock.mock.calls[0][1].headers as Record<string, string>;
    expect(calledHeaders["x-array-header"]).toBe("array-value");
  });
});

// ---------------------------------------------------------------------------
// 5. Error envelope
// ---------------------------------------------------------------------------
describe("apiFetch — error envelope", () => {
  it("throws ApiError with typed fields on a non-2xx response", async () => {
    const body = errorBody({ error_code: "NOT_FOUND", message: "Dataset not found", trace_id: "trace-001" });
    const fetchMock = vi.fn().mockResolvedValue(makeResponse(404, body));
    vi.stubGlobal("fetch", fetchMock);

    await expect(apiFetch("/spoke/validation/datasets/unknown")).rejects.toSatisfy(
      (err: unknown) => {
        expect(err).toBeInstanceOf(ApiError);
        const apiErr = err as ApiError;
        expect(apiErr.error_code).toBe("NOT_FOUND");
        expect(apiErr.message).toBe("Dataset not found");
        expect(apiErr.trace_id).toBe("trace-001");
        expect(apiErr.status).toBe(404);
        return true;
      },
    );
  });

  it("falls back to UNKNOWN_ERROR when response body is not valid JSON", async () => {
    const badResponse = {
      status: 500,
      ok: false,
      statusText: "Internal Server Error",
      json: () => Promise.reject(new SyntaxError("not json")),
    } as unknown as Response;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(badResponse));

    await expect(apiFetch("/auth/me")).rejects.toSatisfy((err: unknown) => {
      expect(err).toBeInstanceOf(ApiError);
      const apiErr = err as ApiError;
      expect(apiErr.error_code).toBe("UNKNOWN_ERROR");
      expect(apiErr.status).toBe(500);
      return true;
    });
  });

  it("returns the parsed body on a 200 response", async () => {
    const payload = { id: "u1", email: "admin@example.com", name: "Admin", role: "Admin" as const };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(makeResponse(200, payload)));

    const result = await apiFetch<typeof payload>("/auth/me");
    expect(result).toEqual(payload);
  });

  it("returns undefined on a 204 No Content response", async () => {
    const resp = { status: 204, ok: true, statusText: "No Content", json: () => Promise.resolve() } as unknown as Response;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(resp));

    const result = await apiFetch("/auth/token/revoke", { method: "POST" });
    expect(result).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// 6. 401 single-flight refresh — critical path
// ---------------------------------------------------------------------------
describe("apiFetch — 401 refresh and retry", () => {
  it("retries the original request with the new token after a successful refresh", async () => {
    mockStore.accessToken = "old-token";
    const newToken = "new-token-after-refresh";

    let callCount = 0;
    const fetchMock = vi.fn().mockImplementation((url: string, opts: RequestInit) => {
      callCount++;

      // First call: the original request returns 401
      if (callCount === 1) {
        return Promise.resolve(makeResponse(401, errorBody({ error_code: "TOKEN_EXPIRED" })));
      }
      // Second call: the refresh request succeeds
      if (callCount === 2 && typeof url === "string" && url.includes("/auth/token/refresh")) {
        // Simulate refresh updating the store
        mockStore.accessToken = newToken;
        return Promise.resolve(makeResponse(200, { access_token: newToken }));
      }
      // Third call: the retry of the original request — must carry the new token
      if (callCount === 3) {
        const authHeader = (opts.headers as Record<string, string>)["authorization"];
        expect(authHeader).toBe(`Bearer ${newToken}`);
        return Promise.resolve(makeResponse(200, { id: "u1" }));
      }
      throw new Error(`Unexpected fetch call #${callCount}: ${url as string}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await apiFetch<{ id: string }>("/auth/me");
    expect(result).toEqual({ id: "u1" });
    // Three fetch calls: original, refresh, retry
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("clears the store and rejects when refresh fails", async () => {
    mockStore.accessToken = "old-token";

    let callCount = 0;
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      callCount++;
      if (callCount === 1) {
        return Promise.resolve(makeResponse(401, errorBody({ error_code: "TOKEN_EXPIRED" })));
      }
      // Refresh call fails with 401
      if (callCount === 2 && typeof url === "string" && url.includes("/auth/token/refresh")) {
        return Promise.resolve(makeResponse(401, errorBody({ error_code: "REFRESH_INVALID" })));
      }
      throw new Error(`Unexpected fetch call #${callCount}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(apiFetch("/auth/me")).rejects.toBeInstanceOf(ApiError);

    // Store must be cleared
    expect(mockStore.clear).toHaveBeenCalledTimes(1);
    expect(mockStore.accessToken).toBeNull();
  });

  it("does NOT retry when there is no access token (no 401 retry loop)", async () => {
    // When the store has no token, 401 means there's nothing to refresh with.
    mockStore.accessToken = null;

    const fetchMock = vi.fn().mockResolvedValue(
      makeResponse(401, errorBody({ error_code: "UNAUTHENTICATED" })),
    );
    vi.stubGlobal("fetch", fetchMock);

    // Should throw, but only make one fetch call (no retry because no token was sent)
    await expect(apiFetch("/auth/me")).rejects.toBeInstanceOf(ApiError);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

// ---------------------------------------------------------------------------
// 7. Refresh deduplication — concurrent 401s
// ---------------------------------------------------------------------------
describe("apiFetch — refresh deduplication", () => {
  it("calls POST /auth/token/refresh exactly once for N concurrent 401s", async () => {
    // Distinguish initial requests from retries by the Authorization header:
    // - Initial requests carry the OLD token → return 401
    // - Retry requests carry the NEW token → return 200
    //
    // This is the end-to-end happy path: N concurrent 401s recover to one
    // refresh and each retries with the new token. Under the current execution
    // model the dynamic store import serializes the callers, so this test does
    // NOT by itself prove the single-flight guard. Authoritative single-flight
    // bug-sensitivity (refreshCallCount === 1 even when callers truly overlap)
    // is asserted by the "ensureFreshToken — deduplicates concurrent calls"
    // test below, which calls ensureFreshToken directly without the import gap.
    const oldToken = "old-token-dedup";
    const newToken = "dedup-refreshed-token";
    mockStore.accessToken = oldToken;
    let refreshCallCount = 0;

    const fetchMock = vi.fn().mockImplementation((url: string, opts: RequestInit) => {
      const urlStr = url as string;

      if (urlStr.includes("/auth/token/refresh")) {
        refreshCallCount++;
        return new Promise<Response>((resolve) =>
          setTimeout(() => {
            mockStore.accessToken = newToken;
            resolve(makeResponse(200, { access_token: newToken }));
          }, 50),
        );
      }

      if (urlStr.includes("/target-resource")) {
        const authHeader = ((opts as RequestInit).headers as Record<string, string>)["authorization"];
        if (authHeader === `Bearer ${oldToken}`) {
          // Initial request — stale token → 401
          return Promise.resolve(makeResponse(401, errorBody({ error_code: "TOKEN_EXPIRED" })));
        }
        // Retry with new token → 200
        return Promise.resolve(makeResponse(200, { id: "ok" }));
      }

      throw new Error(`Unexpected URL: ${urlStr}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const N = 3;
    const results = await Promise.all(
      Array.from({ length: N }, () => apiFetch<{ id: string }>("/target-resource")),
    );

    // With the guard: one refresh total regardless of whether the N callers
    // overlap at the gate or execute sequentially.
    // Without the guard: if callers overlap, each would start its own refresh.
    expect(refreshCallCount).toBe(1);
    expect(results).toHaveLength(N);
    results.forEach((r) => expect(r).toEqual({ id: "ok" }));
  });
});

// ---------------------------------------------------------------------------
// 8. ensureFreshToken
// ---------------------------------------------------------------------------
describe("ensureFreshToken", () => {
  it("returns true and stores the new token on a successful refresh", async () => {
    const fetchMock = vi.fn().mockResolvedValue(makeResponse(200, { access_token: "brand-new-token" }));
    vi.stubGlobal("fetch", fetchMock);

    const result = await ensureFreshToken();

    expect(result).toBe(true);
    expect(mockStore.setToken).toHaveBeenCalledWith("brand-new-token");
  });

  it("returns false when the refresh endpoint returns non-2xx", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(makeResponse(401, errorBody({ error_code: "REFRESH_INVALID" }))),
    );

    const result = await ensureFreshToken();
    expect(result).toBe(false);
  });

  it("returns false when the refresh body lacks a string access_token", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(makeResponse(200, { access_token: null })),
    );

    const result = await ensureFreshToken();
    expect(result).toBe(false);
  });

  it("returns false when the fetch itself throws (network error)", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Network error")));

    const result = await ensureFreshToken();
    expect(result).toBe(false);
  });

  it("deduplicates concurrent calls — refresh fires exactly once", async () => {
    let callCount = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn().mockImplementation(() => {
        callCount++;
        return new Promise((resolve) =>
          setTimeout(() => resolve(makeResponse(200, { access_token: "dedup-token" })), 10),
        );
      }),
    );

    const [r1, r2, r3] = await Promise.all([
      ensureFreshToken(),
      ensureFreshToken(),
      ensureFreshToken(),
    ]);

    expect(callCount).toBe(1);
    expect(r1).toBe(true);
    expect(r2).toBe(true);
    expect(r3).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 9. responseType: "text" path
// ---------------------------------------------------------------------------
// Spec traces:
//   - spec/feature/FRONTEND_ONTOGEN.md §Page contracts: seed Markdown body is fetched
//     with accept: text/markdown; the raw Markdown string is the response (not JSON).
//   - lib/api/ontogen.ts useOntogenSeed: apiFetch(…, { responseType: "text",
//     headers: { accept: "text/markdown" } })
//   - lib/api/client.ts: responseType === "text" path calls response.text() not
//     response.json(); does NOT force content-type/accept: application/json headers.
// ---------------------------------------------------------------------------

describe("apiFetch — responseType: text — returns raw text body", () => {
  it("returns the raw string from response.text() instead of parsed JSON", async () => {
    const markdown = "# Ontogen Seed\n\nSome *markdown* content.";
    const textResponse = {
      status: 200,
      ok: true,
      statusText: "OK",
      text: () => Promise.resolve(markdown),
      json: () => { throw new Error("should not call json() on text response"); },
    } as unknown as Response;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(textResponse));

    const result = await apiFetch<string>("/spoke/ontogen/attr/seed/abc123", {
      responseType: "text",
      headers: { accept: "text/markdown" },
    });

    expect(result).toBe(markdown);
  });

  it("does NOT force content-type: application/json when responseType is text", async () => {
    const textResponse = {
      status: 200,
      ok: true,
      statusText: "OK",
      text: () => Promise.resolve("some text"),
      json: () => Promise.resolve({}),
    } as unknown as Response;
    const fetchMock = vi.fn().mockResolvedValue(textResponse);
    vi.stubGlobal("fetch", fetchMock);

    await apiFetch<string>("/spoke/ontogen/attr/seed/abc123", {
      responseType: "text",
    });

    const calledHeaders = fetchMock.mock.calls[0][1].headers as Record<string, string>;
    expect(calledHeaders["content-type"]).toBeUndefined();
  });

  it("passes a caller-supplied accept: text/markdown header through unchanged", async () => {
    const textResponse = {
      status: 200,
      ok: true,
      statusText: "OK",
      text: () => Promise.resolve("# Seed"),
      json: () => Promise.resolve({}),
    } as unknown as Response;
    const fetchMock = vi.fn().mockResolvedValue(textResponse);
    vi.stubGlobal("fetch", fetchMock);

    await apiFetch<string>("/spoke/ontogen/attr/seed/abc123", {
      responseType: "text",
      headers: { accept: "text/markdown" },
    });

    const calledHeaders = fetchMock.mock.calls[0][1].headers as Record<string, string>;
    expect(calledHeaders["accept"]).toBe("text/markdown");
  });

  it("204 on a text request returns undefined (same behaviour as JSON path)", async () => {
    const noContentResponse = {
      status: 204,
      ok: true,
      statusText: "No Content",
      text: () => Promise.resolve(""),
      json: () => Promise.resolve(),
    } as unknown as Response;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(noContentResponse));

    const result = await apiFetch<string>("/spoke/ontogen/attr/seed/gone", {
      method: "DELETE",
      responseType: "text",
    });

    expect(result).toBeUndefined();
  });
});

describe("apiFetch — responseType: text — 401-retry returns text (not a JSON parse error)", () => {
  // This is the subtle invariant that motivated adding responseType support:
  // a text request that 401s → refresh → retry must return the retried body
  // via response.text(), not via response.json() (which would throw on markdown).

  it("after 401 refresh, retried text request returns the text body", async () => {
    mockStore.accessToken = "old-token";
    const newToken = "new-token-for-text-retry";
    const expectedMarkdown = "# Refreshed Seed\n\nContent after refresh.";

    let callCount = 0;
    const fetchMock = vi.fn().mockImplementation((url: string, opts: RequestInit) => {
      callCount++;

      // First call: original text request returns 401
      if (callCount === 1) {
        return Promise.resolve({
          status: 401,
          ok: false,
          statusText: "Unauthorized",
          json: () => Promise.resolve({
            error_code: "TOKEN_EXPIRED",
            message: "Token expired",
            trace_id: "00000000-0000-0000-0000-000000000001",
            resp_time: new Date().toISOString(),
          }),
          text: () => Promise.resolve("Unauthorized"),
        } as unknown as Response);
      }

      // Second call: refresh succeeds
      if (callCount === 2 && (url as string).includes("/auth/token/refresh")) {
        mockStore.accessToken = newToken;
        return Promise.resolve({
          status: 200,
          ok: true,
          statusText: "OK",
          json: () => Promise.resolve({ access_token: newToken }),
          text: () => Promise.resolve(JSON.stringify({ access_token: newToken })),
        } as unknown as Response);
      }

      // Third call: retry of the original text request — must return text, not JSON
      if (callCount === 3) {
        const authHeader = (opts.headers as Record<string, string>)["authorization"];
        expect(authHeader).toBe(`Bearer ${newToken}`);
        return Promise.resolve({
          status: 200,
          ok: true,
          statusText: "OK",
          text: () => Promise.resolve(expectedMarkdown),
          json: () => { throw new Error("should not call json() on text retry response"); },
        } as unknown as Response);
      }

      throw new Error(`Unexpected fetch call #${callCount}: ${url as string}`);
    });
    vi.stubGlobal("fetch", fetchMock);

    const result = await apiFetch<string>("/spoke/ontogen/attr/seed/abc123", {
      responseType: "text",
      headers: { accept: "text/markdown" },
    });

    expect(result).toBe(expectedMarkdown);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});

describe("apiFetch — responseType: json (default) unchanged", () => {
  // Regression guard: the existing JSON path must be unaffected by the text-path addition.

  it("default (no responseType) calls response.json() on a 200 response", async () => {
    const payload = { id: "node-123", status: "approved" };
    const jsonSpy = vi.fn().mockResolvedValue(payload);
    const fetchMock = vi.fn().mockResolvedValue({
      status: 200,
      ok: true,
      statusText: "OK",
      json: jsonSpy,
      text: () => { throw new Error("should not call text() on json response"); },
    } as unknown as Response);
    vi.stubGlobal("fetch", fetchMock);

    const result = await apiFetch<typeof payload>("/spoke/ontogen/result/node/node-123");

    expect(result).toEqual(payload);
    expect(jsonSpy).toHaveBeenCalledTimes(1);
  });

  it('explicit responseType: "json" forces content-type: application/json on the request', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      status: 200,
      ok: true,
      statusText: "OK",
      json: () => Promise.resolve({}),
    } as unknown as Response);
    vi.stubGlobal("fetch", fetchMock);

    await apiFetch("/some/json/endpoint", { responseType: "json" });

    const calledHeaders = fetchMock.mock.calls[0][1].headers as Record<string, string>;
    expect(calledHeaders["content-type"]).toBe("application/json");
  });
});
