/**
 * Ontology Generation domain types — derived from src/api/schemas/ontogen.py.
 */

export type ScheduleTier = "hourly" | "daily" | "weekly";

/** Lifecycle status values for nodes, edges, and triples. */
export type OntogenStatus = "llm_pending" | "llm_approved" | "approved" | "rejected";

export type ReviewVerdict = "approve" | "reject";

// ── Conf ──────────────────────────────────────────────────────────────────────

export interface OntogenConf {
  is_enabled: boolean;
  schedule_tier: ScheduleTier | null;
  dataset_filter: Record<string, unknown>;
  default_run_prompt: string | null;
  updated_at: string | null;
}

export interface OntogenConfPutBody {
  is_enabled: boolean;
  schedule_tier: ScheduleTier | null;
  dataset_filter: Record<string, unknown>;
  default_run_prompt: string | null;
}

export interface OntogenConfPatchBody {
  is_enabled?: boolean;
  schedule_tier?: ScheduleTier | null;
  dataset_filter?: Record<string, unknown>;
  default_run_prompt?: string | null;
}

// ── Seeds ─────────────────────────────────────────────────────────────────────

export interface SeedListItem {
  seed_id: string;
  updated_at: string;
  preview: string;
}

export interface SeedListResponse {
  offset: number;
  limit: number;
  total_count: number;
  seeds: SeedListItem[];
}

export interface SeedCreateResponse {
  seed_id: string;
  updated_at: string;
}

// ── Run ───────────────────────────────────────────────────────────────────────

export interface OntogenRunResponse {
  status: string;
  dry_run: boolean;
  unresolved_urns: string[];
  counts: Record<string, number>;
}

// ── Node ─────────────────────────────────────────────────────────────────────

export interface OntogenNode {
  id: string;
  name: string;
  description: string;
  confidence_score: number;
  status: OntogenStatus;
  created_at: string;
  updated_at: string;
}

export interface NodeListResponse {
  offset: number;
  limit: number;
  total_count: number;
  nodes: OntogenNode[];
}

// ── Edge ─────────────────────────────────────────────────────────────────────

export interface OntogenEdge {
  id: string;
  label: string;
  semantics: string | null;
  confidence_score: number;
  status: OntogenStatus;
  created_at: string;
  updated_at: string;
}

export interface EdgeListResponse {
  offset: number;
  limit: number;
  total_count: number;
  edges: OntogenEdge[];
}

// ── Triple ────────────────────────────────────────────────────────────────────

export interface OntogenTriple {
  id: string;
  subject_node_id: string;
  edge_id: string;
  object_node_id: string;
  confidence_score: number;
  status: OntogenStatus;
  created_at: string;
  updated_at: string;
}

export interface TripleListResponse {
  offset: number;
  limit: number;
  total_count: number;
  triples: OntogenTriple[];
}

// ── Item attr (evidence) ───────────────────────────────────────────────────────

/**
 * Per-item detail from GET /spoke/ontogen/result/{kind}/{id}/attr. `evidence` is
 * free-form JSON (it carries the adversarial-debate transcript under `debate`)
 * and is rendered as-is. Mirrors {Node,Edge,Triple}AttrResponse in
 * src/api/schemas/ontogen.py; the per-kind id field is unused by the UI.
 */
export interface OntogenItemAttrResponse {
  confidence_score: number;
  evidence: Record<string, unknown>;
}

// ── Review ────────────────────────────────────────────────────────────────────

export interface ReviewRequest {
  verdict: ReviewVerdict;
  reason?: string;
}

// ── Events ────────────────────────────────────────────────────────────────────

export interface OntogenEvent {
  id: string;
  entity_type: string;
  entity_id: string;
  event_type: string;
  status: string;
  detail: Record<string, unknown>;
  occurred_at: string;
}

export interface OntogenEventListResponse {
  offset: number;
  limit: number;
  total_count: number;
  events: OntogenEvent[];
}
