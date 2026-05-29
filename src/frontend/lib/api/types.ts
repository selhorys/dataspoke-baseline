/**
 * Hand-written API types for the current phase.
 *
 * Run `pnpm codegen` against a live backend to regenerate `lib/api/types.generated.ts`
 * from the OpenAPI schema. Until then, only the types currently needed are defined here.
 */

export type UserRole = "Admin" | "Editor" | "Reader";

export interface Me {
  id: string;
  email: string;
  name: string;
  role: UserRole;
  has_google: boolean;
  created_at: string;
  updated_at: string;
}

export interface TokenResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
}

export interface ApiErrorPayload {
  error_code: string;
  message: string;
  trace_id: string;
  resp_time: string;
}

// ── API Tokens ────────────────────────────────────────────────────────────────

export interface ApiTokenItem {
  id: string;
  name: string;
  role_snapshot: string;
  created_at: string;
  last_used_at: string | null;
  expires_at: string | null;
}

export interface ApiTokenListResponse {
  tokens: ApiTokenItem[];
  total: number;
}

export interface ApiTokenMintResponse {
  id: string;
  name: string;
  role_snapshot: string;
  token: string;
  created_at: string;
  expires_at: string | null;
}

// ── Admin Users ───────────────────────────────────────────────────────────────

export interface AdminUser {
  id: string;
  email: string;
  name: string;
  role: UserRole;
  has_google: boolean;
  created_at: string;
  updated_at: string;
}

export interface UsersListResponse {
  users: AdminUser[];
  total: number;
}

// ── Runtime Configuration ─────────────────────────────────────────────────────

export interface RuntimeConf {
  resp_time: string;
  llm_provider: string;
  llm_model: string;
  llm_api_key: string;
  ontogen_llm_max_iterations: number;
  ontogen_debate_max_turns: number;
  ontogen_debate_rag_k: number;
  ontogen_debate_reviewer_model: string | null;
  metagen_llm_max_iterations: number;
  metagen_debate_max_turns: number;
  metagen_debate_rag_k: number;
  metagen_debate_reviewer_model: string | null;
  metagen_confidence_threshold: number;
  metagen_ontology_rag_node_k: number;
  metagen_ontology_rag_edge_k: number;
  metagen_ontology_rag_triple_k: number;
  validation_score_n_intervals: number;
  stub_redis_client: boolean;
  stub_llm_client: boolean;
  stub_pgvector_manager: boolean;
  stub_notification_service: boolean;
  auth_datahub_corp_group: string;
  updated_at: string | null;
}

export interface RuntimeConfPatch {
  llm_provider?: string;
  llm_model?: string;
  llm_api_key?: string;
  ontogen_llm_max_iterations?: number;
  ontogen_debate_max_turns?: number;
  ontogen_debate_rag_k?: number;
  ontogen_debate_reviewer_model?: string | null;
  metagen_llm_max_iterations?: number;
  metagen_debate_max_turns?: number;
  metagen_debate_rag_k?: number;
  metagen_debate_reviewer_model?: string | null;
  metagen_confidence_threshold?: number;
  metagen_ontology_rag_node_k?: number;
  metagen_ontology_rag_edge_k?: number;
  metagen_ontology_rag_triple_k?: number;
  validation_score_n_intervals?: number;
  stub_redis_client?: boolean;
  stub_llm_client?: boolean;
  stub_pgvector_manager?: boolean;
  stub_notification_service?: boolean;
  auth_datahub_corp_group?: string;
}
