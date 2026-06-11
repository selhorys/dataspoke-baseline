/**
 * Metadata Generation domain types — derived from src/api/schemas/metagen.py.
 */

export type ScheduleTier = "hourly" | "daily" | "weekly";
export type AllowedKind = "dataset.description" | "column.description";
export type ItemStatus = "pending" | "llm_approved" | "approved";
export type CandidateStatus = "llm_approved" | "approved" | "rejected";
export type ReviewVerdict = "approve" | "reject";

// ── Global conf ───────────────────────────────────────────────────────────────

export interface MetagenGlobalConf {
  is_enabled: boolean;
  schedule_tier: ScheduleTier | null;
  dataset_filter: Record<string, unknown>;
  result_limit: number;
  overwrite_pending: boolean;
  updated_at: string;
}

export interface MetagenGlobalConfPutBody {
  is_enabled: boolean;
  schedule_tier: ScheduleTier | null;
  dataset_filter: Record<string, unknown>;
  result_limit: number;
  overwrite_pending: boolean;
}

export interface MetagenGlobalConfPatchBody {
  is_enabled?: boolean;
  schedule_tier?: ScheduleTier | null;
  dataset_filter?: Record<string, unknown>;
  result_limit?: number;
  overwrite_pending?: boolean;
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
  status: ItemStatus;
  candidate_count: number;
  composite_id: string;
}

export interface MetagenItemListResponse {
  offset: number;
  limit: number;
  total_count: number;
  items: MetagenItemSummary[];
}

export interface MetagenCandidate {
  candidate_id: string;
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
