/**
 * Tests for lib/toast-api-error.ts — toastApiError error classification.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §Authentication:
 *     "Token refresh on 401 — POST /auth/token/refresh"; auth client handles 401
 *     and redirects. toastApiError MUST suppress the toast for 401.
 *   - spec/API.md §Error Envelope: {error_code, message, trace_id} — non-2xx
 *     responses carry a structured ApiError. Toast body must include message and
 *     a short trace_id fragment so support can correlate logs.
 *   - lib/api/client.ts ApiError: instanceof check; .status, .error_code,
 *     .trace_id, .message.
 *   - toastApiError classification contract (four branches):
 *     1. ApiError + status 401 → NO toast (suppressed)
 *     2. ApiError + status != 401 → toast with error_code title, message + trace_id fragment
 *     3. TypeError("Failed to fetch") or TypeError("NetworkError…") → "Network error" toast
 *     4. Other unknown Error → "Unexpected error" toast
 *     Network-vs-unexpected is the contractual distinction — the two titles MUST differ.
 */

import { describe, it, expect, beforeEach, vi } from "vitest";
import { ApiError } from "@/lib/api/client";

// ---------------------------------------------------------------------------
// Mock the toast function before importing toastApiError.
// toast is exported from @/components/ui/use-toast alongside useToast.
// ---------------------------------------------------------------------------

const mockToast = vi.fn();

vi.mock("@/components/ui/use-toast", () => ({
  toast: (...args: unknown[]) => mockToast(...args),
  useToast: vi.fn(),
}));

// Import AFTER mock registration so the module picks up the mock.
import { toastApiError } from "./toast-api-error";

// ---------------------------------------------------------------------------
// Factory for ApiError — mirrors how ApiError is constructed in client.ts
// ---------------------------------------------------------------------------

function makeApiError(
  status: number,
  overrides: Partial<{ error_code: string; message: string; trace_id: string }> = {},
): ApiError {
  return new ApiError(
    {
      error_code: overrides.error_code ?? "SOME_ERROR",
      message: overrides.message ?? "Something went wrong",
      trace_id: overrides.trace_id ?? "ab12cd34-0000-0000-0000-000000000000",
      resp_time: "2026-05-01T00:00:00Z",
    },
    status,
  );
}

// ---------------------------------------------------------------------------
// Setup / teardown
// ---------------------------------------------------------------------------

beforeEach(() => {
  mockToast.mockClear();
});

// ---------------------------------------------------------------------------
// 1. 401 ApiError → NO toast (auth client handles it, redirects to /login)
// ---------------------------------------------------------------------------

describe("toastApiError — 401 ApiError → NO toast (suppressed; auth client handles redirect)", () => {
  // Contract: 401 is the silent-refresh signal. Showing a toast would confuse
  // the user — the refresh or redirect is the correct UX response.
  // spec/feature/FRONTEND_BASIC.md §Authentication §Token refresh on 401.

  it("does NOT call toast for a 401 ApiError", () => {
    toastApiError(makeApiError(401, { error_code: "TOKEN_EXPIRED" }));
    expect(mockToast).not.toHaveBeenCalled();
  });

  it("does NOT call toast for a 401 even when message is non-empty", () => {
    toastApiError(makeApiError(401, { message: "Token has expired, please log in again" }));
    expect(mockToast).not.toHaveBeenCalled();
  });

  it("does NOT call toast for a 401 with a trace_id present", () => {
    toastApiError(makeApiError(401, { trace_id: "ffffffff-0000-0000-0000-000000000000" }));
    expect(mockToast).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// 2. Non-401 ApiError → toast fires with error_code title + message + trace fragment
// ---------------------------------------------------------------------------

describe("toastApiError — non-401 ApiError → toast fires with message and trace_id fragment", () => {
  // Contract: API errors other than 401 must surface to the user.
  // Toast body: title = error_code; description includes message + trace_id prefix.
  // spec/API.md §Error Envelope.

  // Table-driven for common HTTP status codes that must all produce a toast.
  type Row = { status: number; error_code: string; label: string };

  const table: Row[] = [
    { status: 403, error_code: "READ_ONLY_ROLE",   label: "403 Forbidden" },
    { status: 409, error_code: "CONFLICT_ERROR",   label: "409 Conflict" },
    { status: 422, error_code: "VALIDATION_ERROR", label: "422 Unprocessable" },
    { status: 500, error_code: "INTERNAL_ERROR",   label: "500 Internal Server Error" },
    { status: 503, error_code: "SERVICE_UNAVAILABLE", label: "503 Service Unavailable" },
  ];

  table.forEach(({ status, error_code, label }) => {
    it(`${label} → toast called once`, () => {
      toastApiError(makeApiError(status, { error_code }));
      expect(mockToast).toHaveBeenCalledTimes(1);
    });
  });

  it("toast title is the error_code from the ApiError envelope", () => {
    toastApiError(makeApiError(500, { error_code: "INTERNAL_ERROR" }));
    const call = mockToast.mock.calls[0][0] as { title: string };
    expect(call.title).toBe("INTERNAL_ERROR");
  });

  it("toast description includes the message from the ApiError envelope", () => {
    toastApiError(makeApiError(500, { message: "Database connection failed" }));
    const call = mockToast.mock.calls[0][0] as { description: string };
    expect(call.description).toContain("Database connection failed");
  });

  it("toast description includes a trace_id fragment (first 8 chars) for support correlation", () => {
    // Contract: trace fragment allows support engineers to correlate log entries.
    // The description includes "(trace: <first-8-chars>)" so a short prefix is sufficient.
    toastApiError(makeApiError(500, { trace_id: "ab12cd34-ef56-7890-abcd-ef1234567890" }));
    const call = mockToast.mock.calls[0][0] as { description: string };
    // First 8 chars of the trace_id
    expect(call.description).toContain("ab12cd34");
  });

  it("toast description with trace_id does not expose the full UUID (only a short prefix)", () => {
    // The full UUID adds noise; spec contract is a SHORT trace fragment.
    toastApiError(makeApiError(500, { trace_id: "ab12cd34-ef56-7890-abcd-ef1234567890" }));
    const call = mockToast.mock.calls[0][0] as { description: string };
    // Should NOT contain the full trace_id beyond the prefix.
    expect(call.description).not.toContain("ab12cd34-ef56-7890-abcd-ef1234567890");
  });

  it("toast fires with variant='destructive' for API errors", () => {
    toastApiError(makeApiError(500, { error_code: "INTERNAL_ERROR" }));
    const call = mockToast.mock.calls[0][0] as { variant: string };
    expect(call.variant).toBe("destructive");
  });

  it("403 READ_ONLY_ROLE → toast with title READ_ONLY_ROLE and description containing message", () => {
    toastApiError(
      makeApiError(403, {
        error_code: "READ_ONLY_ROLE",
        message: "Your role does not permit this action",
        trace_id: "cccccccc-1234-5678-9abc-def012345678",
      }),
    );

    const call = mockToast.mock.calls[0][0] as { title: string; description: string; variant: string };
    expect(call.title).toBe("READ_ONLY_ROLE");
    expect(call.description).toContain("Your role does not permit this action");
    expect(call.description).toContain("cccccccc");
    expect(call.variant).toBe("destructive");
  });

  it("500 with no trace_id → toast fires without a trace fragment (description is just the message)", () => {
    // ApiError.trace_id is always set by client.ts (fallback is 00000000-…).
    // But if somehow it were empty/falsy, the impl falls back to just the message.
    const err = makeApiError(500, { message: "Server error", trace_id: "" });
    // Patch trace_id to empty string to simulate missing trace.
    Object.assign(err, { trace_id: "" });

    toastApiError(err);

    expect(mockToast).toHaveBeenCalledTimes(1);
    const call = mockToast.mock.calls[0][0] as { description: string };
    // Without trace_id, description should just be the message
    expect(call.description).toBe("Server error");
  });
});

// ---------------------------------------------------------------------------
// 3. Network error (TypeError "Failed to fetch") → "Network error" toast
// ---------------------------------------------------------------------------

describe("toastApiError — network error → 'Network error' toast (distinguished from unknown error)", () => {
  // Contract: TypeError with message "Failed to fetch" is the browser's network
  // error signal. It must produce a recognisably distinct toast from generic
  // unknown errors so the user knows to check their connection, not the app.
  // The network-vs-unexpected distinction IS the contract.

  it("TypeError('Failed to fetch') → toast called once", () => {
    toastApiError(new TypeError("Failed to fetch"));
    expect(mockToast).toHaveBeenCalledTimes(1);
  });

  it("TypeError('Failed to fetch') → title is 'Network error'", () => {
    toastApiError(new TypeError("Failed to fetch"));
    const call = mockToast.mock.calls[0][0] as { title: string };
    expect(call.title).toBe("Network error");
  });

  it("TypeError('NetworkError when attempting to fetch resource') → title is 'Network error'", () => {
    // Firefox uses this message prefix for network errors.
    toastApiError(new TypeError("NetworkError when attempting to fetch resource"));
    const call = mockToast.mock.calls[0][0] as { title: string };
    expect(call.title).toBe("Network error");
  });

  it("network error toast title is NOT 'Unexpected error' (the two titles are distinct)", () => {
    toastApiError(new TypeError("Failed to fetch"));
    const call = mockToast.mock.calls[0][0] as { title: string };
    expect(call.title).not.toBe("Unexpected error");
  });

  it("network error description advises checking connection (not a generic message)", () => {
    toastApiError(new TypeError("Failed to fetch"));
    const call = mockToast.mock.calls[0][0] as { description: string };
    // Description must communicate connectivity, not just "error occurred".
    expect(call.description.toLowerCase()).toMatch(/server|connection|network|reach/);
  });

  it("network error toast fires with variant='destructive'", () => {
    toastApiError(new TypeError("Failed to fetch"));
    const call = mockToast.mock.calls[0][0] as { variant: string };
    expect(call.variant).toBe("destructive");
  });
});

// ---------------------------------------------------------------------------
// 4. Other unknown errors → "Unexpected error" toast
// ---------------------------------------------------------------------------

describe("toastApiError — unknown/unexpected errors → 'Unexpected error' toast", () => {
  // Contract: non-ApiError, non-network errors get the generic unexpected-error toast.
  // The user knows something went wrong; they should report it via trace.

  it("plain Error('boom') → toast called once", () => {
    toastApiError(new Error("boom"));
    expect(mockToast).toHaveBeenCalledTimes(1);
  });

  it("plain Error('boom') → title is 'Unexpected error'", () => {
    toastApiError(new Error("boom"));
    const call = mockToast.mock.calls[0][0] as { title: string };
    expect(call.title).toBe("Unexpected error");
  });

  it("plain Error('boom') → title is NOT 'Network error' (distinct from network branch)", () => {
    toastApiError(new Error("boom"));
    const call = mockToast.mock.calls[0][0] as { title: string };
    expect(call.title).not.toBe("Network error");
  });

  it("TypeError with a non-network message → title is 'Unexpected error'", () => {
    // A TypeError that is NOT a network error (e.g., a JS type mismatch in app code).
    toastApiError(new TypeError("Cannot read properties of undefined (reading 'id')"));
    const call = mockToast.mock.calls[0][0] as { title: string };
    expect(call.title).toBe("Unexpected error");
  });

  it("plain string thrown as error → toast called once (does not throw)", () => {
    // Defensive: some libraries throw strings.
    expect(() => toastApiError("plain string error")).not.toThrow();
    expect(mockToast).toHaveBeenCalledTimes(1);
  });

  it("null thrown → toast called once without throwing", () => {
    expect(() => toastApiError(null)).not.toThrow();
    expect(mockToast).toHaveBeenCalledTimes(1);
  });

  it("undefined thrown → toast called once without throwing", () => {
    expect(() => toastApiError(undefined)).not.toThrow();
    expect(mockToast).toHaveBeenCalledTimes(1);
  });

  it("unknown error toast fires with variant='destructive'", () => {
    toastApiError(new Error("random error"));
    const call = mockToast.mock.calls[0][0] as { variant: string };
    expect(call.variant).toBe("destructive");
  });

  it("Error message appears in description for plain Error", () => {
    toastApiError(new Error("something exploded"));
    const call = mockToast.mock.calls[0][0] as { description: string };
    expect(call.description).toContain("something exploded");
  });
});

// ---------------------------------------------------------------------------
// 5. Network-vs-unexpected contract: the two titles MUST differ
// ---------------------------------------------------------------------------

describe("toastApiError — network-vs-unexpected title distinction is the classification contract", () => {
  // The spec-level contract: two error classes produce visually distinct toasts.
  // Users need to know whether the problem is connectivity (network) or app logic
  // (unexpected). Asserting both titles from a single test makes the pairing explicit.

  it("network error and unknown error produce different toast titles", () => {
    toastApiError(new TypeError("Failed to fetch"));
    const networkTitle = (mockToast.mock.calls[0][0] as { title: string }).title;
    mockToast.mockClear();

    toastApiError(new Error("boom"));
    const unknownTitle = (mockToast.mock.calls[0][0] as { title: string }).title;

    expect(networkTitle).not.toBe(unknownTitle);
    expect(networkTitle).toBe("Network error");
    expect(unknownTitle).toBe("Unexpected error");
  });
});

// ---------------------------------------------------------------------------
// 6. Classification contract table — exhaustive drive across all four branches
// ---------------------------------------------------------------------------

describe("toastApiError — exhaustive classification table (all four branches)", () => {
  type Row =
    | { label: string; input: unknown; expectToast: false }
    | { label: string; input: unknown; expectToast: true; titleContains: string };

  const table: Row[] = [
    {
      label: "401 ApiError → suppressed",
      input: makeApiError(401, { error_code: "TOKEN_EXPIRED" }),
      expectToast: false,
    },
    {
      label: "403 ApiError → toast with error_code title",
      input: makeApiError(403, { error_code: "READ_ONLY_ROLE" }),
      expectToast: true,
      titleContains: "READ_ONLY_ROLE",
    },
    {
      label: "500 ApiError → toast with error_code title",
      input: makeApiError(500, { error_code: "INTERNAL_ERROR" }),
      expectToast: true,
      titleContains: "INTERNAL_ERROR",
    },
    {
      label: "TypeError('Failed to fetch') → Network error",
      input: new TypeError("Failed to fetch"),
      expectToast: true,
      titleContains: "Network error",
    },
    {
      label: "TypeError('NetworkError…') → Network error",
      input: new TypeError("NetworkError when attempting to fetch resource"),
      expectToast: true,
      titleContains: "Network error",
    },
    {
      label: "plain Error('boom') → Unexpected error",
      input: new Error("boom"),
      expectToast: true,
      titleContains: "Unexpected error",
    },
  ];

  table.forEach(({ label, input, expectToast, ...rest }) => {
    it(label, () => {
      toastApiError(input);

      if (!expectToast) {
        expect(mockToast).not.toHaveBeenCalled();
      } else {
        expect(mockToast).toHaveBeenCalledTimes(1);
        const call = mockToast.mock.calls[0][0] as { title: string };
        expect(call.title).toContain((rest as { titleContains: string }).titleContains);
      }
    });
  });
});
