/**
 * datasetFilterError — narrows a mutation error to the inline `dataset_filter`
 * error DatasetFilterEditor renders against the field.
 *
 * The backend owns the grammar, so filter validation is server-side: a write
 * carrying a malformed clause comes back `422 INVALID_DATASET_FILTER` with
 * `detail.position` (the offending character index), or `422
 * INVALID_DATASET_URN` when a `dataset_urn` literal is not a well-formed URN
 * (no position). Every other error stays with the form's generic error slot.
 *
 * Spec: spec/API.md §Error Catalogue, spec/feature/FRONTEND_BASIC.md
 *       §Shared component notes (DatasetFilterEditor).
 */

import { ApiError } from "@/lib/api/client";

export interface DatasetFilterErrorInfo {
  message: string;
  /** Character offset of the syntax error, when the API reported one. */
  position?: number;
}

const FILTER_ERROR_CODES = new Set(["INVALID_DATASET_FILTER", "INVALID_DATASET_URN"]);

export function datasetFilterError(error: unknown): DatasetFilterErrorInfo | undefined {
  if (!(error instanceof ApiError)) return undefined;
  if (!FILTER_ERROR_CODES.has(error.error_code)) return undefined;

  const rawPosition = error.detail?.position;
  const position =
    typeof rawPosition === "number" && Number.isFinite(rawPosition) && rawPosition >= 0
      ? rawPosition
      : undefined;

  return { message: `${error.error_code}: ${error.message}`, position };
}
