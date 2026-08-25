/**
 * Validation domain types — derived from src/api/schemas/validation.py.
 */

/** Coverage filter for the validation list — covered (default), uncovered, or both. */
export type ValidationCoverage = "covered" | "uncovered" | "both";

export interface ValidationListItem {
  dataset_urn: string;
  // Uncovered rows (registered datasets with no validation conf) carry null
  // description / variable_count and null latest_* fields.
  description: string | null;
  variable_count: number | null;
  latest_data_time: string | null;
  latest_score: number | null;
  // Null for uncovered rows (no conf); registry timestamp otherwise.
  updated_at: string | null;
}

export interface ValidationListResponse {
  offset: number;
  limit: number;
  total_count: number;
  validations: ValidationListItem[];
}

export interface ValidationVariable {
  name: string;
  description: string;
}

/**
 * One declared pipeline hyperparameter. Its `value` is opaque string storage;
 * the name and description rules match variables, in its own namespace — a name may appear
 * in both lists. DataSpoke never interprets a parameter.
 */
export interface ValidationParameter {
  name: string;
  value: string;
  description: string;
}

/**
 * Declared data-arrival cadence of the dataset — the pair the governance
 * `validation-score` metric anchors its per-dataset window on. Always present
 * on a stored conf: a conf written without the section carries the defaults.
 */
export interface ValidationAttribute {
  /** Period, in seconds, at which the dataset's data is expected to arrive. */
  cadence_unit: number;
  /** How many `cadence_unit` periods the arriving data lags the arrival instant. */
  cadence_offset: number;
}

export interface ValidationConfResponse {
  dataset_urn: string;
  description: string;
  variables: ValidationVariable[];
  attribute: ValidationAttribute;
  /**
   * Absent by default. The API omits the key entirely rather than serializing
   * it as null, so `undefined` is the shape a caller actually sees; `null` is
   * admitted because it is the spelling a PATCH uses to clear the section.
   */
  parameter?: ValidationParameter[] | null;
  created_at: string;
  updated_at: string;
}

/**
 * Body of `PUT .../attr/validation/conf` — a full replace. `attribute` always
 * travels complete (the API replaces it wholesale) and `parameter` is omitted
 * when the section is absent, since an explicit `[]` is rejected with 422.
 */
export interface ValidationConfPutRequest {
  description: string;
  variables: ValidationVariable[];
  attribute: ValidationAttribute;
  parameter?: ValidationParameter[];
}

export interface ValidationResultRow {
  data_time: string;
  score: number;
  variables: Record<string, number>;
}

export interface ValidationResultListResponse {
  offset: number;
  limit: number;
  total_count: number;
  results: ValidationResultRow[];
}

export interface ValidationEvent {
  id: string;
  entity_type: string;
  entity_id: string;
  event_type: string;
  status: string;
  detail: Record<string, unknown>;
  occurred_at: string;
}

export interface ValidationEventListResponse {
  offset: number;
  limit: number;
  total_count: number;
  events: ValidationEvent[];
}

// ── Form types ─────────────────────────────────────────────────────────────────

export interface ValidationConfFormValues {
  description: string;
  variables: { name: string; description: string }[];
  /** Always complete — the API replaces `attribute` wholesale, never merges it. */
  attribute: { cadence_unit: number; cadence_offset: number };
  /**
   * The optional `parameter[]` section, flattened to "empty means absent": the
   * API rejects an explicit `[]`, so an empty list serializes as an omitted key
   * rather than as a value.
   */
  parameter: { name: string; value: string; description: string }[];
}
