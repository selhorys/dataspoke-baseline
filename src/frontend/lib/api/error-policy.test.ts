/**
 * Tests for lib/api/error-policy.ts — the single interpretation of a failed read
 * that the global retry rule, the inline render point and the toast all share.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §Query Error Policy:
 *     "Non-transient — no retry, fail immediately. Any 4xx, plus
 *     PERIPHERAL_NOT_CONFIGURED regardless of its 503 status … 429 needs no
 *     separate rule — the 4xx rule already covers it".
 *   - spec/feature/FRONTEND_BASIC.md §Query Error Policy:
 *     "Everything else — retried up to twice, then surfaced to the render site."
 *   - spec/feature/FRONTEND_BASIC.md §Query Error Policy: "One retry policy is set
 *     globally on the query client and governs every read".
 *   - spec/feature/FRONTEND_BASIC.md §Shared Component Notes (QueryErrorState):
 *     "When the error is PERIPHERAL_NOT_CONFIGURED, it names the peripheral from
 *     detail.peripheral".
 *   - spec/API.md §Application Error Codes: PERIPHERAL_NOT_CONFIGURED | 503 |
 *     "detail.peripheral identifies which one ("smtp" for
 *     /auth/password/reset/request; "datahub" for any DataHub-requiring endpoint)".
 *
 * Where a case has no spec line of its own it is called out inline.
 *
 * On copy: the spec constrains only that the peripheral be *named*. The display
 * labels ("DataHub", "SMTP") and the unnamed stand-in are this client's copy, and
 * this file is the one deliberate place they are pinned as exact strings — the
 * render sites (components/query-error-state.test.tsx, lib/toast-api-error.test.ts)
 * assert only that the peripheral is named, so the copy has a single owner rather
 * than being restated at every call site.
 */
import { describe, it, expect } from "vitest";
import { ApiError } from "@/lib/api/client";
import {
  PERIPHERAL_NOT_CONFIGURED,
  defaultQueryRetry,
  isNonTransient,
  isPeripheralNotConfigured,
  peripheralDisplayName,
  unconfiguredPeripheral,
} from "./error-policy";

// ---------------------------------------------------------------------------
// Fixtures — ApiError is constructed exactly as client.ts constructs it
// (payload: ApiErrorPayload, status: number).
// ---------------------------------------------------------------------------

function apiError(
  status: number,
  overrides: Partial<{
    error_code: string;
    message: string;
    trace_id: string;
    detail: Record<string, unknown>;
  }> = {},
): ApiError {
  return new ApiError(
    {
      error_code: overrides.error_code ?? "SOME_ERROR",
      message: overrides.message ?? "Something went wrong",
      trace_id: overrides.trace_id ?? "aaaaaaaa-0000-0000-0000-000000000000",
      resp_time: "2026-07-01T00:00:00Z",
      ...(overrides.detail !== undefined ? { detail: overrides.detail } : {}),
    },
    status,
  );
}

/** 503 PERIPHERAL_NOT_CONFIGURED with detail.peripheral, as the API sends it. */
function peripheralError(peripheral = "datahub"): ApiError {
  return apiError(503, {
    error_code: PERIPHERAL_NOT_CONFIGURED,
    message: "DataHub is not configured",
    detail: { peripheral },
  });
}

// ---------------------------------------------------------------------------
// 1. Non-transient classification: any 4xx never retries
// ---------------------------------------------------------------------------

/** Every status in [from, to). Sweeps close the 4xx/5xx boundary by construction. */
function statusRange(from: number, to: number): number[] {
  return Array.from({ length: to - from }, (_, i) => from + i);
}

describe("isNonTransient / defaultQueryRetry — any 4xx is non-transient (no retry)", () => {
  // spec §Query Error Policy: "Any 4xx … no retry, fail immediately". The rule is
  // stated over the whole class, so it is asserted over the whole class rather
  // than over chosen examples — that also pins the 499/500 boundary.

  it("no status in 400–499 is ever retried", () => {
    const retried = statusRange(400, 500).filter((status) => defaultQueryRetry(0, apiError(status)));
    expect(retried).toEqual([]);
  });

  it("every status in 400–499 is classified non-transient", () => {
    const transient = statusRange(400, 500).filter((status) => !isNonTransient(apiError(status)));
    expect(transient).toEqual([]);
  });

  it("429 is covered by the 4xx rule rather than a Retry-After backoff", () => {
    // spec §Query Error Policy: "429 needs no separate rule — the 4xx rule
    // already covers it … the query layer's blind backoff is the wrong
    // instrument to honour it with."
    expect(defaultQueryRetry(0, apiError(429, { error_code: "RATE_LIMIT_EXCEEDED" }))).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 2. Transient classification: 5xx retries
// ---------------------------------------------------------------------------

describe("isNonTransient / defaultQueryRetry — 5xx is transient (retried)", () => {
  // spec §Query Error Policy: "Everything else — retried up to twice". Swept over
  // the whole class for the same reason as the 4xx rule above.

  it("every status in 500–599 is retried when it carries no peripheral code", () => {
    const notRetried = statusRange(500, 600).filter(
      (status) => !defaultQueryRetry(0, apiError(status)),
    );
    expect(notRetried).toEqual([]);
  });

  it("every status in 500–599 is classified transient when it carries no peripheral code", () => {
    const nonTransient = statusRange(500, 600).filter((status) => isNonTransient(apiError(status)));
    expect(nonTransient).toEqual([]);
  });

  it("the peripheral code overrides the 5xx class at every 5xx status", () => {
    // spec §Query Error Policy: PERIPHERAL_NOT_CONFIGURED is non-transient
    // "regardless of its 503 status" — the code, not the status, decides.
    const retried = statusRange(500, 600).filter((status) =>
      defaultQueryRetry(0, apiError(status, { error_code: PERIPHERAL_NOT_CONFIGURED })),
    );
    expect(retried).toEqual([]);
  });

  it("502 DATAHUB_UNAVAILABLE (configured but unreachable) is retried", () => {
    // spec/API.md §Application Error Codes: DATAHUB_UNAVAILABLE | 502 |
    // "DataHub GMS is configured but did not respond" — a fault, not a
    // configuration state, so a retry can change the answer.
    expect(defaultQueryRetry(0, apiError(502, { error_code: "DATAHUB_UNAVAILABLE" }))).toBe(true);
  });

  it("503 STORAGE_UNAVAILABLE is retried — only the peripheral code is carved out", () => {
    expect(defaultQueryRetry(0, apiError(503, { error_code: "STORAGE_UNAVAILABLE" }))).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 3. PERIPHERAL_NOT_CONFIGURED is non-transient despite its 503 status
// ---------------------------------------------------------------------------

describe("PERIPHERAL_NOT_CONFIGURED — non-transient regardless of its 503 status", () => {
  // spec §Query Error Policy: "plus PERIPHERAL_NOT_CONFIGURED regardless of its
  // 503 status … the peripheral stays unconfigured until an operator wires it, so
  // a retry chain cannot change the answer".

  it("isNonTransient true", () => {
    expect(isNonTransient(peripheralError())).toBe(true);
  });

  it("defaultQueryRetry false on the very first failure", () => {
    expect(defaultQueryRetry(0, peripheralError())).toBe(false);
  });

  it("stays non-transient for the smtp peripheral too", () => {
    // spec/API.md §Application Error Codes: "smtp" for /auth/password/reset/request.
    expect(defaultQueryRetry(0, peripheralError("smtp"))).toBe(false);
  });

  it("is classified by the error code alone — an envelope with no detail still counts", () => {
    // This is the invariant that keeps the retry rule and the render rule from
    // disagreeing: QueryErrorState's onboarding branch and this retry carve-out
    // must fire on exactly the same set of errors, and the render branch cannot
    // depend on a detail the retry branch never reads.
    const noDetail = apiError(503, { error_code: PERIPHERAL_NOT_CONFIGURED });

    expect(isPeripheralNotConfigured(noDetail)).toBe(true);
    expect(isNonTransient(noDetail)).toBe(true);
    expect(defaultQueryRetry(0, noDetail)).toBe(false);
  });

  it("a malformed detail does not demote it to a retryable failure", () => {
    const badDetail = apiError(503, {
      error_code: PERIPHERAL_NOT_CONFIGURED,
      detail: { peripheral: 42 },
    });

    expect(isPeripheralNotConfigured(badDetail)).toBe(true);
    expect(defaultQueryRetry(0, badDetail)).toBe(false);
  });

  it("a 503 with a different error code is NOT peripheral-not-configured", () => {
    // Backstop for the assertions above: the predicate is not simply true for
    // every 503.
    expect(isPeripheralNotConfigured(apiError(503, { error_code: "STORAGE_UNAVAILABLE" }))).toBe(
      false,
    );
  });

  it("the code carried on a non-503 status is still recognised (code, not status, decides)", () => {
    expect(isPeripheralNotConfigured(apiError(500, { error_code: PERIPHERAL_NOT_CONFIGURED }))).toBe(
      true,
    );
  });

  it("a non-ApiError throwable is never peripheral-not-configured", () => {
    expect(isPeripheralNotConfigured(new Error(PERIPHERAL_NOT_CONFIGURED))).toBe(false);
    expect(isPeripheralNotConfigured(null)).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// 4. Non-ApiError throwables are transient
// ---------------------------------------------------------------------------

describe("defaultQueryRetry — throwables that carry no API envelope are transient", () => {
  // spec §Query Error Policy classifies only "any 4xx" and the peripheral code as
  // non-transient; "everything else" — which includes anything that never reached
  // the API — is retried.

  it("a network TypeError is retried", () => {
    const err = new TypeError("Failed to fetch");
    expect(isNonTransient(err)).toBe(false);
    expect(defaultQueryRetry(0, err)).toBe(true);
  });

  it("a plain Error is retried", () => {
    expect(defaultQueryRetry(0, new Error("boom"))).toBe(true);
  });

  it("a thrown non-Error value is retried and does not throw the policy", () => {
    expect(() => defaultQueryRetry(0, "some string")).not.toThrow();
    expect(defaultQueryRetry(0, "some string")).toBe(true);
    expect(defaultQueryRetry(0, undefined)).toBe(true);
    expect(defaultQueryRetry(0, null)).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 5. The retry budget: "retried up to twice"
// ---------------------------------------------------------------------------

describe("defaultQueryRetry — a transient failure is retried up to twice, then surfaced", () => {
  // spec §Query Error Policy: "Everything else — retried up to twice, then
  // surfaced to the render site." TanStack calls retry(failureCount, error) with
  // failureCount = the number of retries already spent, starting at 0 for the
  // first failure, so the budget is the number of consecutive `true` answers.

  it("grants exactly two retries for a 500 (three attempts in total)", () => {
    const error = apiError(500, { error_code: "INTERNAL_ERROR" });

    let granted = 0;
    // The bound is a runaway guard, not the expectation — a policy that never
    // stops would exit the loop at 10 and fail the assertion below.
    while (granted < 10 && defaultQueryRetry(granted, error)) {
      granted += 1;
    }

    expect(granted).toBe(2);
  });

  it("grants exactly two retries for a network failure", () => {
    const error = new TypeError("Failed to fetch");

    let granted = 0;
    while (granted < 10 && defaultQueryRetry(granted, error)) {
      granted += 1;
    }

    expect(granted).toBe(2);
  });

  it("grants zero retries for a non-transient failure", () => {
    // Backstop pairing with the two cases above: the same loop over a
    // non-transient error must terminate immediately.
    let granted = 0;
    while (granted < 10 && defaultQueryRetry(granted, peripheralError())) {
      granted += 1;
    }

    expect(granted).toBe(0);
  });
});

// ---------------------------------------------------------------------------
// 6. unconfiguredPeripheral — reading detail.peripheral
// ---------------------------------------------------------------------------

describe("unconfiguredPeripheral — the peripheral named by detail.peripheral", () => {
  // spec §Shared Component Notes: QueryErrorState "names the peripheral from
  // detail.peripheral". spec/API.md §Application Error Codes enumerates the two
  // identifiers the API sends today.

  it("returns the name the envelope carries", () => {
    expect(unconfiguredPeripheral(peripheralError("datahub"))).toBe("datahub");
    expect(unconfiguredPeripheral(peripheralError("smtp"))).toBe("smtp");
  });

  it("returns null when the envelope carries no detail at all", () => {
    expect(unconfiguredPeripheral(apiError(503, { error_code: PERIPHERAL_NOT_CONFIGURED }))).toBeNull();
  });

  it("returns null when detail.peripheral is not a string", () => {
    expect(
      unconfiguredPeripheral(
        apiError(503, { error_code: PERIPHERAL_NOT_CONFIGURED, detail: { peripheral: 7 } }),
      ),
    ).toBeNull();
    expect(
      unconfiguredPeripheral(
        apiError(503, { error_code: PERIPHERAL_NOT_CONFIGURED, detail: { other: "datahub" } }),
      ),
    ).toBeNull();
  });

  it("returns null for an API error carrying a different code, even with a peripheral detail", () => {
    expect(
      unconfiguredPeripheral(
        apiError(502, { error_code: "DATAHUB_UNAVAILABLE", detail: { peripheral: "datahub" } }),
      ),
    ).toBeNull();
  });

  it("returns null for a throwable that is not an ApiError", () => {
    expect(unconfiguredPeripheral(new Error("boom"))).toBeNull();
    expect(unconfiguredPeripheral(undefined)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 7. peripheralDisplayName — the subject of the onboarding sentence
// ---------------------------------------------------------------------------

describe("peripheralDisplayName — human-readable subject for the onboarding message", () => {
  // The labels themselves are copy, not spec text; what the spec requires is that
  // the peripheral be *named*, so these assert the naming, its casing for the two
  // identifiers the API documents, and that an unknown or absent name still
  // yields a usable sentence subject.

  it("renders the datahub identifier as DataHub", () => {
    expect(peripheralDisplayName(peripheralError("datahub"))).toBe("DataHub");
  });

  it("renders the smtp identifier as SMTP", () => {
    expect(peripheralDisplayName(peripheralError("smtp"))).toBe("SMTP");
  });

  it("passes an unrecognised identifier through verbatim rather than hiding it", () => {
    expect(peripheralDisplayName(peripheralError("langfuse"))).toBe("langfuse");
  });

  it("falls back to a generic subject when the envelope names no peripheral", () => {
    const name = peripheralDisplayName(apiError(503, { error_code: PERIPHERAL_NOT_CONFIGURED }));

    expect(name.length).toBeGreaterThan(0);
    expect(name).not.toMatch(/undefined|null/);
    // The stand-in must not masquerade as a named peripheral.
    expect(name).not.toBe("DataHub");
    expect(name).not.toBe("SMTP");
  });

  it("falls back to the same generic subject for a non-peripheral error", () => {
    const generic = peripheralDisplayName(apiError(503, { error_code: PERIPHERAL_NOT_CONFIGURED }));
    expect(peripheralDisplayName(new Error("boom"))).toBe(generic);
  });
});
