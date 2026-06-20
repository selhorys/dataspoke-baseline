/**
 * Validation domain types — derived from src/api/schemas/validation.py.
 */

export interface ValidationListItem {
  dataset_urn: string;
  description: string;
  variable_count: number;
  latest_data_time: string | null;
  latest_score: number | null;
  updated_at: string;
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

export interface ValidationConfResponse {
  dataset_urn: string;
  description: string;
  variables: ValidationVariable[];
  created_at: string;
  updated_at: string;
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
}
