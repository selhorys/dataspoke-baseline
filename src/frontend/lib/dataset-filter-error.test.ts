/**
 * Tests for datasetFilterError — narrows a write error to the inline
 * `dataset_filter` error DatasetFilterEditor renders against the field.
 *
 * Spec traces:
 *   - spec/API.md §Error Catalogue → `INVALID_DATASET_FILTER` (422): "A
 *     `dataset_filter` string does not parse under the filter grammar, names an
 *     unknown column, or exceeds a payload cap. `detail` carries the character
 *     position of the error."
 *   - spec/API.md §Error Catalogue → `INVALID_DATASET_URN` (422): "A
 *     `dataset_urn` literal inside a `dataset_filter` is not a well-formed
 *     `urn:li:dataset:(…)` URN."
 *   - spec/feature/FRONTEND_BASIC.md §Shared component notes → DatasetFilterEditor:
 *     "Validation is server-side: a `422 INVALID_DATASET_FILTER` renders inline
 *     against the field, carrying the position the API reported."
 *
 * Mocked: nothing — ApiError is constructed directly from the real class, so the
 * envelope shape under test is the one lib/api/client.ts actually produces.
 */

import { describe, it, expect } from "vitest";
import { ApiError } from "@/lib/api/client";
import { datasetFilterError } from "./dataset-filter-error";

function apiError(
  error_code: string,
  message: string,
  detail?: Record<string, unknown>,
  status = 422,
): ApiError {
  return new ApiError(
    { error_code, message, trace_id: "trace-1", resp_time: "2026-01-01T00:00:00Z", detail },
    status,
  );
}

describe("datasetFilterError — which errors belong inline on the field", () => {
  it("claims INVALID_DATASET_FILTER and surfaces the reported position", () => {
    const info = datasetFilterError(
      apiError("INVALID_DATASET_FILTER", "unexpected token 'AND'", { position: 14 }),
    );
    expect(info).toBeDefined();
    expect(info!.message).toContain("INVALID_DATASET_FILTER");
    expect(info!.message).toContain("unexpected token 'AND'");
    expect(info!.position).toBe(14);
  });

  it("claims INVALID_DATASET_URN, which carries no position", () => {
    // spec/API.md §Metric: a malformed `dataset_urn` literal is reported without
    // a character offset, so the editor renders the message alone.
    const info = datasetFilterError(
      apiError("INVALID_DATASET_URN", "not a well-formed dataset URN"),
    );
    expect(info).toBeDefined();
    expect(info!.message).toContain("INVALID_DATASET_URN");
    expect(info!.position).toBeUndefined();
  });

  it("accepts position 0 — the first character is a real offset, not a falsy miss", () => {
    const info = datasetFilterError(
      apiError("INVALID_DATASET_FILTER", "unexpected token", { position: 0 }),
    );
    expect(info!.position).toBe(0);
  });

  it("leaves every other API error to the form's generic error slot", () => {
    // Backstop for the assertions above: the narrowing is by error_code, so a
    // different 422 from the same route must not be claimed.
    expect(datasetFilterError(apiError("INVALID_PARAMETER", "bad metric_type"))).toBeUndefined();
    expect(
      datasetFilterError(apiError("METRIC_NOT_FOUND", "no such metric", undefined, 404)),
    ).toBeUndefined();
  });

  it("ignores a non-ApiError rejection (network failure, thrown string)", () => {
    expect(datasetFilterError(new Error("Failed to fetch"))).toBeUndefined();
    expect(datasetFilterError("boom")).toBeUndefined();
    expect(datasetFilterError(undefined)).toBeUndefined();
  });
});

describe("datasetFilterError — an unusable position is dropped, the message is not", () => {
  it.each([
    ["absent detail", undefined],
    ["detail without position", { hint: "check the parens" }],
    ["non-numeric position", { position: "14" }],
    ["negative position", { position: -1 }],
    ["NaN position", { position: Number.NaN }],
  ])("still renders the message when the position is unusable (%s)", (_label, detail) => {
    const info = datasetFilterError(
      apiError("INVALID_DATASET_FILTER", "unexpected token", detail as Record<string, unknown>),
    );
    // The error must still reach the field — only the offset annotation is lost.
    expect(info).toBeDefined();
    expect(info!.message).toContain("unexpected token");
    expect(info!.position).toBeUndefined();
  });
});
