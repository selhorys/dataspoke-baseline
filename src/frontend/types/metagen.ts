/**
 * Metadata Generation domain types — derived from src/api/schemas/metagen.py.
 *
 * Confs are a managed collection: many named confs coexist, each with its own
 * dataset_filter / schedule_tier / generation budget. All confs feed one global
 * cross-dataset review queue; each candidate carries the conf_id/conf_name that
 * produced it.
 */

export type ScheduleTier = "hourly" | "daily" | "weekly";
export type AllowedKind = "dataset.description" | "column.description";
export type ItemStatus = "pending" | "llm_approved" | "approved";
export type CandidateStatus = "llm_approved" | "approved" | "rejected";
export type ReviewVerdict = "approve" | "reject";
export type UncoveredReason = "no_conf_match" | "boundary_blocked";

// ── Conf collection ─────────────────────────────────────────────────────────

export interface MetagenConf {
  id: string;
  name: string;
  is_enabled: boolean;
  schedule_tier: ScheduleTier | null;
  dataset_filter: Record<string, unknown>;
  result_limit: number;
  overwrite_pending: boolean;
  created_at: string;
  updated_at: string;
}

export interface MetagenConfListResponse {
  offset: number;
  limit: number;
  total_count: number;
  confs: MetagenConf[];
}

export interface MetagenConfCreateBody {
  name: string;
  is_enabled: boolean;
  schedule_tier: ScheduleTier | null;
  dataset_filter: Record<string, unknown>;
  result_limit: number;
  overwrite_pending: boolean;
}

/** PUT — full replacement. Same shape as create minus that the id is in the path. */
export type MetagenConfPutBody = MetagenConfCreateBody;

export interface MetagenConfPatchBody {
  name?: string;
  is_enabled?: boolean;
  schedule_tier?: ScheduleTier | null;
  dataset_filter?: Record<string, unknown>;
  result_limit?: number;
  overwrite_pending?: boolean;
}

// ── Uncovered ─────────────────────────────────────────────────────────────────

export interface MetagenUncoveredRow {
  dataset_urn: string;
  reason: UncoveredReason;
}

export interface MetagenUncoveredResponse {
  offset: number;
  limit: number;
  total_count: number;
  datasets: MetagenUncoveredRow[];
}

// ── Covered datasets (per-conf) ─────────────────────────────────────────────────

export interface MetagenCoveredDatasetSummary {
  dataset_urn: string;
  is_enabled: boolean;
  allowed: AllowedKind[];
  owner: string | null;
  blocked: boolean;
  reason: string | null;
}

export interface MetagenCoveredDatasetResponse {
  offset: number;
  limit: number;
  total_count: number;
  datasets: MetagenCoveredDatasetSummary[];
}

// ── Per-dataset boundary ──────────────────────────────────────────────────────

export interface MetagenBoundary {
  dataset_urn: string;
  is_enabled: boolean;
  allowed: AllowedKind[];
  owner: string | null;
  created_at: string;
  updated_at: string;
}

export interface MetagenBoundaryPutBody {
  is_enabled: boolean;
  allowed: AllowedKind[];
  owner?: string | null;
}

export interface MetagenBoundaryPatchBody {
  is_enabled?: boolean;
  allowed?: AllowedKind[];
  owner?: string | null;
}

// ── Item & candidate ──────────────────────────────────────────────────────────

export interface MetagenItemSummary {
  dataset_urn: string;
  item_id: string;
  kind: AllowedKind;
  field_path: string | null;
  /**
   * Derived over NON-rejected candidates: `pending` when no non-rejected
   * candidate exists, `llm_approved` when at least one does, `approved` once a
   * candidate has been human-approved (_metagen_mappers.py §item_status).
   */
  status: ItemStatus;
  candidate_count: number;
  composite_id: string;
  created_at: string;
}

export interface MetagenItemListResponse {
  offset: number;
  limit: number;
  total_count: number;
  items: MetagenItemSummary[];
}

export interface MetagenCandidate {
  candidate_id: string;
  /** The conf that produced this candidate; null after the conf was deleted. */
  conf_id: string | null;
  conf_name: string | null;
  /** Creating run's id; doubles as the Langfuse session id for the Evidence link. */
  run_id: string | null;
  item_id: string;
  dataset_urn: string;
  value: string;
  confidence_score: number;
  status: CandidateStatus;
  evidence: Record<string, unknown>;
  created_at: string;
  reviewed_at: string | null;
  reviewer_id: string | null;
}

export interface MetagenItemDetail extends MetagenItemSummary {
  candidates: MetagenCandidate[];
}

// ── Run ───────────────────────────────────────────────────────────────────────

export interface MetagenRunBody {
  dataset_urns?: string[] | null;
  dry_run?: boolean; // passed as ?dry_run=true query param; not sent in request body
}

export interface MetagenRunResponse {
  run_id: string;
  conf_id: string;
  status: "success" | "failure";
  dry_run: boolean;
  unresolved_urns: string[];
  counts: Record<string, number>;
  producer_iterations: number | null;
  debate_outcome: "accept" | "turns_exhausted" | "cycle_detected" | null;
}

// ── Review ────────────────────────────────────────────────────────────────────

export interface MetagenReviewBody {
  verdict: ReviewVerdict;
  reason?: string;
}

// ── Events ────────────────────────────────────────────────────────────────────

export interface MetagenEvent {
  id: string;
  entity_type: string;
  entity_id: string;
  event_type: string;
  status: string;
  detail: Record<string, unknown>;
  occurred_at: string;
}

export interface MetagenEventListResponse {
  offset: number;
  limit: number;
  total_count: number;
  events: MetagenEvent[];
}
