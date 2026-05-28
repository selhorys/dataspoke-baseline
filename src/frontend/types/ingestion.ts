/**
 * Ingestion Control domain types — derived from src/api/schemas/ingestion.py
 * and src/shared/models/ingestion.py.
 */

export type IngestionMode = "active-custom" | "passive";
export type ScheduleTier = "hourly" | "daily" | "weekly";
export type Platform = "postgres" | "mysql" | "oracle" | "bigquery" | "snowflake" | "kafka";

/** Platforms that require auth (CredentialAuth). */
export const PLATFORMS_WITH_AUTH: Platform[] = ["postgres", "mysql", "oracle", "snowflake"];

/** Platforms that have a locator with host+port. */
export const RDBMS_PLATFORMS: Platform[] = ["postgres", "mysql", "oracle"];

export interface SecretRefSpec {
  name: string;
  key: string;
  force_overwrite?: boolean;
}

export interface AuthSpec {
  username: string;
  password?: string;
  secret_ref?: SecretRefSpec;
}

export interface IngestionConfigResponse {
  id: string;
  dataset_urn: string;
  mode: IngestionMode;
  platform: Platform;
  locator: Record<string, unknown> | null;
  identifier: Record<string, unknown>;
  auth: Record<string, unknown> | null;
  is_enabled: boolean;
  schedule_tier: ScheduleTier | null;
  workflow_dag_id: string | null;
  status: string;
  created_at: string;
  updated_at: string;
}

export interface IngestionConfigListResponse {
  offset: number;
  limit: number;
  total_count: number;
  configs: IngestionConfigResponse[];
}

export interface RunResultResponse {
  run_id: string;
  status: string;
  detail: Record<string, unknown>;
}

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

// ── Form types ─────────────────────────────────────────────────────────────────

/** Internal form shape used by react-hook-form for the ingestion config form. */
export interface IngestionConfFormValues {
  mode: IngestionMode;
  platform: Platform;
  // locator fields — active-custom only
  locator_host: string;
  locator_port: string;
  locator_bootstrap_servers: string;
  locator_project_id: string;
  locator_account_id: string;
  // identifier fields
  identifier_database: string;
  identifier_schema_name: string;
  identifier_table: string;
  identifier_topic: string;
  identifier_cluster: string;
  identifier_dataset: string;
  // auth fields — active-custom only (postgres/mysql/oracle/snowflake)
  auth_username: string;
  auth_password: string;
  auth_secret_ref_name: string;
  auth_secret_ref_key: string;
  // common
  is_enabled: boolean;
  schedule_tier: ScheduleTier | "";
}
