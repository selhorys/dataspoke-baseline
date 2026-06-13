/**
 * Ingestion domain types — derived 1:1 from src/api/schemas/ingestion.py and
 * src/api/schemas/events.py. Source of truth for the per-source ingestion model.
 *
 * Spec: spec/feature/FRONTEND_INGESTION.md, spec/API.md §Ingestion.
 */

export type IngestionMode =
  | "DATAHUB_MANAGED"
  | "ACTIVE_CUSTOM_MANAGED"
  | "PASSIVE";

/** Confidence in the source→dataset link. */
export type IngestionDatasetAuthority = "high" | "medium";

/** How the source→dataset link was established. */
export type IngestionDatasetDerivation = "emitted" | "pipeline_name" | "matched";

// ── Source ──────────────────────────────────────────────────────────────────────

/** Recipe is the DataHub-compatible {source: {type, config}} object. */
export type Recipe = Record<string, unknown>;

/** Full source record (GET/POST/PUT/PATCH /spoke/ingestion/sources/{id}). */
export interface IngestionSource {
  id: string;
  mode: IngestionMode;
  name: string;
  schedule: string | null;
  recipe: Recipe;
  platform: string;
  status: string;
  datahub_source_urn: string | null;
  created_at: string;
  updated_at: string;
}

export interface IngestionSourceListResponse {
  offset: number;
  limit: number;
  total_count: number;
  sources: IngestionSource[];
}

/** Request body for POST and PUT. */
export interface IngestionSourceBody {
  mode: IngestionMode;
  name: string;
  schedule: string | null;
  recipe: Recipe;
}

/** Request body for PATCH (all fields optional; mode is not patchable). */
export interface IngestionSourcePatchBody {
  name?: string;
  schedule?: string | null;
  recipe?: Recipe;
}

// ── Run ─────────────────────────────────────────────────────────────────────────

export interface IngestionRunResponse {
  run_id: string;
  status: string;
  detail: Record<string, unknown>;
}

// ── Source → dataset mapping ─────────────────────────────────────────────────────

export interface IngestionSourceDatasetRow {
  dataset_urn: string;
  authority: IngestionDatasetAuthority;
  derivation: IngestionDatasetDerivation;
  first_seen_at: string;
  last_seen_at: string;
}

export interface IngestionSourceDatasetsResponse {
  offset: number;
  limit: number;
  total_count: number;
  datasets: IngestionSourceDatasetRow[];
}

// ── Unmanaged bucket ─────────────────────────────────────────────────────────────

export interface IngestionUnmanagedResponse {
  offset: number;
  limit: number;
  total_count: number;
  dataset_urns: string[];
}

// ── Secret references ────────────────────────────────────────────────────────────

export interface SecretRefInfo {
  ref: string;
  secret_name: string;
  key: string;
}

export interface SecretRefListResponse {
  secrets: SecretRefInfo[];
}

// ── Events ───────────────────────────────────────────────────────────────────────

export interface IngestionEvent {
  id: string;
  entity_type: string;
  entity_id: string;
  event_type: string;
  status: string;
  detail: Record<string, unknown>;
  occurred_at: string;
}

export interface IngestionEventListResponse {
  offset: number;
  limit: number;
  total_count: number;
  events: IngestionEvent[];
}

// ── Per-dataset reverse-lookup ───────────────────────────────────────────────────

export interface IngestionLatestRunSummary {
  run_id: string | null;
  status: string;
  occurred_at: string;
}

export interface IngestionReverseLookupResponse {
  dataset_urn: string;
  source_id: string | null;
  mode: IngestionMode | null;
  name: string | null;
  latest_run: IngestionLatestRunSummary | null;
}
