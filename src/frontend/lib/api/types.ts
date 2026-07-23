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
  /** Error-code-specific context, e.g. `{ peripheral: "datahub" }`. */
  detail?: Record<string, unknown>;
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

// ── Peripheral Configuration ──────────────────────────────────────────────────

/** `security.protocol` the event consumer uses to reach Kafka. */
export type KafkaSecurityProtocol = "PLAINTEXT" | "SSL" | "SASL_PLAINTEXT" | "SASL_SSL";

/** `sasl.mechanism`; `""` means "no SASL", which is the only valid value under a non-SASL protocol. */
export type KafkaSaslMechanism = "PLAIN" | "SCRAM-SHA-256" | "SCRAM-SHA-512" | "AWS_MSK_IAM";

/**
 * A peripheral's last self-reported connection state.
 *
 * `unknown` covers both "never reported" and "no reporter deployed" — the API
 * does not distinguish them.
 */
export interface PeripheralHealth {
  status: "unknown" | "ok" | "error";
  last_error: string | null;
  last_ok_at: string | null;
  updated_at: string | null;
}

export interface DatahubPeripheral {
  resp_time: string;
  gms_url: string;
  /** Browser-facing DataHub UI base URL — never the GMS endpoint. */
  frontend_url: string;
  kafka_brokers: string;
  kafka_security_protocol: KafkaSecurityProtocol;
  kafka_sasl_mechanism: KafkaSaslMechanism | "";
  kafka_sasl_username: string;
  /** Masked indicator only: "" when unset, "********" when set. */
  kafka_sasl_password: string;
  /** API-owned bookkeeping, incremented on every password write. Never rendered, never sent. */
  kafka_sasl_password_version: number;
  kafka_aws_region: string;
  /** Masked indicator only: "" when unset, "********" when set. */
  token: string;
  service_corpuser_urn: string;
  default_env: string;
  /** Keys on `token` alone — the Kafka credential is optional and never participates. */
  is_configured: boolean;
  /** Read-only: whether the configuration actually works, as opposed to merely being present. */
  health: PeripheralHealth;
  updated_at: string | null;
}

export interface DatahubPeripheralPatch {
  gms_url?: string;
  /** Browser-facing DataHub UI base URL; constrained to a safe http(s) form. */
  frontend_url?: string;
  kafka_brokers?: string;
  kafka_security_protocol?: KafkaSecurityProtocol;
  /** `""` clears the mechanism, which is what a non-SASL protocol requires. */
  kafka_sasl_mechanism?: KafkaSaslMechanism | "";
  kafka_sasl_username?: string;
  /** Plaintext Kafka SASL password; omit to keep current, "" to clear. */
  kafka_sasl_password?: string;
  kafka_aws_region?: string;
  /** Plaintext token; omit to keep current, "" to clear. */
  token?: string;
  service_corpuser_urn?: string;
  default_env?: string;
}

/**
 * Peripheral display links for the app shell — GET /spoke/common/peripheral-links.
 *
 * Each field is `""` when its peripheral is unconfigured, which clients read as
 * "render no link". Carries only display links: no `gms_url`, `kafka_brokers`,
 * or corpuser URN, since this is a non-Admin surface.
 */
export interface PeripheralLinks {
  resp_time: string;
  /** From the DataHub peripheral's `frontend_url`, not `gms_url`. */
  datahub_url: string;
  /** From the Langfuse peripheral's `host`. */
  langfuse_url: string;
  langfuse_project_id: string;
}

export interface LangfusePeripheral {
  resp_time: string;
  host: string;
  public_key: string;
  /** Masked indicator only: "" when unset, "********" when set. */
  secret_key: string;
  project_id: string;
  environment_tag: string;
  is_configured: boolean;
  updated_at: string | null;
}

export interface LangfusePeripheralPatch {
  host?: string;
  public_key?: string;
  /** Plaintext secret key; omit to keep current, "" to clear. */
  secret_key?: string;
  project_id?: string;
  environment_tag?: string;
}

// ── Workflow schedules (DAG groups) ───────────────────────────────────────────

/** The five controllable DAG groups (operational schedule control via Airflow). */
export type DagGroup =
  | "datahub_sync"
  | "auth_role_sync"
  | "ingestion_active"
  | "ontogen"
  | "metagen"
  | "metrics";

/** Paused state of a single member DAG within a group. */
export interface DagDetail {
  dag_id: string;
  paused: boolean;
}

/**
 * Schedule (paused) status of one controllable DAG group.
 * `paused` is true only when all member DAGs are paused; `mixed` is true when
 * members disagree (some paused, some not).
 */
export interface DagGroupStatus {
  group: DagGroup;
  paused: boolean;
  mixed: boolean;
  dags: DagDetail[];
}

/** Response for GET /admin/dags. */
export interface DagGroupsResponse {
  resp_time: string;
  groups: DagGroupStatus[];
}

/** Request body for PATCH /admin/dags/{group}. */
export interface DagGroupPatch {
  paused: boolean;
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
