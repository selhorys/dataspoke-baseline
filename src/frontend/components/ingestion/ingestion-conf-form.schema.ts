/**
 * IngestionConfForm Zod schema and serialization helpers — extracted for testability.
 *
 * Mode-gating invariants (mirrors src/api/schemas/ingestion.py):
 *
 *   passive mode:
 *     - locator, auth, schedule_tier must be absent / ignored
 *
 *   active-custom mode:
 *     - locator is required
 *     - when is_enabled=true, schedule_tier is required
 *     - auth required for postgres/mysql/oracle/snowflake (CredentialAuth platforms)
 *     - auth must not be present for bigquery/kafka (NoAuth platforms)
 *     - auth.secret_ref.name must start with SECRET_REF_NAME_PREFIX
 *       (mirrors src/backend/ingestion/secret_resolver.py _NAME_PREFIX)
 *
 * Spec: spec/feature/FRONTEND_INGESTION.md, src/api/schemas/ingestion.py.
 */

import { z } from "zod";
import type { IngestionConfFormValues, IngestionMode, Platform, ScheduleTier } from "@/types/ingestion";
import type { IngestionConfigResponse } from "@/types/ingestion";
import { PLATFORMS_WITH_AUTH, RDBMS_PLATFORMS } from "@/types/ingestion";

/**
 * Mirrors src/backend/ingestion/secret_resolver.py _NAME_PREFIX.
 * Backend rejects any secret_ref.name that does not start with this prefix (HTTP 422).
 */
export const SECRET_REF_NAME_PREFIX = "dataspoke-source-cred-";

export const ingestionConfSchema = z
  .object({
    mode: z.enum(["active-custom", "passive"]),
    platform: z.enum(["postgres", "mysql", "oracle", "bigquery", "snowflake", "kafka"]),
    // locator
    locator_host: z.string(),
    locator_port: z.string(),
    locator_bootstrap_servers: z.string(),
    locator_project_id: z.string(),
    locator_account_id: z.string(),
    // identifier
    identifier_database: z.string(),
    identifier_schema_name: z.string(),
    identifier_table: z.string(),
    identifier_topic: z.string(),
    identifier_cluster: z.string(),
    identifier_dataset: z.string(),
    // auth
    auth_username: z.string(),
    auth_password: z.string(),
    auth_secret_ref_name: z.string(),
    auth_secret_ref_key: z.string(),
    // common
    is_enabled: z.boolean(),
    schedule_tier: z.enum(["hourly", "daily", "weekly"]).or(z.literal("")),
  })
  .superRefine((data, ctx) => {
    const isActive = data.mode === "active-custom";
    const platform = data.platform as Platform;

    if (isActive) {
      // locator required for active-custom
      if (RDBMS_PLATFORMS.includes(platform)) {
        if (!data.locator_host.trim()) {
          ctx.addIssue({ code: z.ZodIssueCode.custom, message: "host is required", path: ["locator_host"] });
        }
        if (!data.locator_port.trim()) {
          ctx.addIssue({ code: z.ZodIssueCode.custom, message: "port is required", path: ["locator_port"] });
        } else {
          const port = Number(data.locator_port);
          if (!Number.isInteger(port) || port < 1 || port > 65535) {
            ctx.addIssue({ code: z.ZodIssueCode.custom, message: "port must be 1–65535", path: ["locator_port"] });
          }
        }
      }
      if (platform === "kafka") {
        if (!data.locator_bootstrap_servers.trim()) {
          ctx.addIssue({ code: z.ZodIssueCode.custom, message: "bootstrap_servers is required", path: ["locator_bootstrap_servers"] });
        }
      }
      if (platform === "bigquery") {
        if (!data.locator_project_id.trim()) {
          ctx.addIssue({ code: z.ZodIssueCode.custom, message: "project_id is required", path: ["locator_project_id"] });
        }
      }
      if (platform === "snowflake") {
        if (!data.locator_account_id.trim()) {
          ctx.addIssue({ code: z.ZodIssueCode.custom, message: "account_id is required", path: ["locator_account_id"] });
        }
      }

      // auth required for CredentialAuth platforms
      if (PLATFORMS_WITH_AUTH.includes(platform)) {
        if (!data.auth_username.trim()) {
          ctx.addIssue({ code: z.ZodIssueCode.custom, message: "username is required", path: ["auth_username"] });
        }
        if (!data.auth_secret_ref_name.trim()) {
          ctx.addIssue({ code: z.ZodIssueCode.custom, message: "secret_ref.name is required", path: ["auth_secret_ref_name"] });
        } else if (!data.auth_secret_ref_name.startsWith(SECRET_REF_NAME_PREFIX)) {
          ctx.addIssue({
            code: z.ZodIssueCode.custom,
            message: `secret_ref.name must start with '${SECRET_REF_NAME_PREFIX}'`,
            path: ["auth_secret_ref_name"],
          });
        }
        if (!data.auth_secret_ref_key.trim()) {
          ctx.addIssue({ code: z.ZodIssueCode.custom, message: "secret_ref.key is required", path: ["auth_secret_ref_key"] });
        }
      }

      // schedule_tier required when is_enabled=true
      if (data.is_enabled && !data.schedule_tier) {
        ctx.addIssue({
          code: z.ZodIssueCode.custom,
          message: "schedule_tier is required when is_enabled is true",
          path: ["schedule_tier"],
        });
      }
    }

    // identifier fields required for all modes
    if (RDBMS_PLATFORMS.includes(platform) || platform === "snowflake") {
      const needsDatabase =
        RDBMS_PLATFORMS.includes(platform) || platform === "snowflake";
      if (needsDatabase && !data.identifier_database.trim()) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, message: "database is required", path: ["identifier_database"] });
      }
      if (!data.identifier_schema_name.trim()) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, message: "schema_name is required", path: ["identifier_schema_name"] });
      }
      if (!data.identifier_table.trim()) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, message: "table is required", path: ["identifier_table"] });
      }
    }
    if (platform === "bigquery") {
      if (!data.identifier_dataset.trim()) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, message: "dataset is required", path: ["identifier_dataset"] });
      }
      if (!data.identifier_table.trim()) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, message: "table is required", path: ["identifier_table"] });
      }
    }
    if (platform === "kafka") {
      if (!data.identifier_topic.trim()) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, message: "topic is required", path: ["identifier_topic"] });
      }
      if (!data.identifier_cluster.trim()) {
        ctx.addIssue({ code: z.ZodIssueCode.custom, message: "cluster is required", path: ["identifier_cluster"] });
      }
    }
  });

// ── Serialization helpers ──────────────────────────────────────────────────────

/**
 * toInternal: convert an IngestionConfigResponse into the flat internal form shape.
 * Falls back to empty strings for fields not present for this mode/platform.
 */
export function toInternal(conf: IngestionConfigResponse): IngestionConfFormValues {
  const locator = conf.locator ?? {};
  const identifier = conf.identifier ?? {};
  const auth = conf.auth ?? {};
  const secretRef = (auth.secret_ref ?? {}) as Record<string, unknown>;

  return {
    mode: conf.mode,
    platform: conf.platform,
    locator_host: String(locator.host ?? ""),
    locator_port: String(locator.port ?? ""),
    locator_bootstrap_servers: String(locator.bootstrap_servers ?? ""),
    locator_project_id: String(locator.project_id ?? ""),
    locator_account_id: String(locator.account_id ?? ""),
    identifier_database: String(identifier.database ?? ""),
    identifier_schema_name: String(identifier.schema_name ?? ""),
    identifier_table: String(identifier.table ?? ""),
    identifier_topic: String(identifier.topic ?? ""),
    identifier_cluster: String(identifier.cluster ?? ""),
    identifier_dataset: String(identifier.dataset ?? ""),
    auth_username: String(auth.username ?? ""),
    auth_password: "",
    auth_secret_ref_name: String(secretRef.name ?? ""),
    auth_secret_ref_key: String(secretRef.key ?? ""),
    is_enabled: conf.is_enabled,
    schedule_tier: (conf.schedule_tier ?? "") as ScheduleTier | "",
  };
}

/** Default blank form values for a new config. */
export function defaultFormValues(mode: IngestionMode = "active-custom"): IngestionConfFormValues {
  return {
    mode,
    platform: "postgres",
    locator_host: "",
    locator_port: "",
    locator_bootstrap_servers: "",
    locator_project_id: "",
    locator_account_id: "",
    identifier_database: "",
    identifier_schema_name: "",
    identifier_table: "",
    identifier_topic: "",
    identifier_cluster: "",
    identifier_dataset: "",
    auth_username: "",
    auth_password: "",
    auth_secret_ref_name: "",
    auth_secret_ref_key: "",
    is_enabled: false,
    schedule_tier: "",
  };
}

/**
 * fromInternal: convert the flat form shape into the API request body for PUT.
 * Fields not applicable to the mode/platform are omitted.
 */
export function fromInternal(v: IngestionConfFormValues): Record<string, unknown> {
  const platform = v.platform as Platform;
  const isActive = v.mode === "active-custom";

  // Build identifier
  let identifier: Record<string, unknown> = {};
  if (RDBMS_PLATFORMS.includes(platform) || platform === "snowflake") {
    identifier = {
      database: v.identifier_database,
      schema_name: v.identifier_schema_name,
      table: v.identifier_table,
    };
  } else if (platform === "bigquery") {
    identifier = {
      dataset: v.identifier_dataset,
      table: v.identifier_table,
    };
  } else if (platform === "kafka") {
    identifier = {
      topic: v.identifier_topic,
      cluster: v.identifier_cluster,
    };
  }

  if (!isActive) {
    return {
      mode: "passive",
      platform,
      identifier,
      is_enabled: v.is_enabled,
      schedule_tier: null,
    };
  }

  // Build locator
  let locator: Record<string, unknown> = {};
  if (RDBMS_PLATFORMS.includes(platform)) {
    locator = { host: v.locator_host, port: Number(v.locator_port) };
  } else if (platform === "kafka") {
    locator = { bootstrap_servers: v.locator_bootstrap_servers };
  } else if (platform === "bigquery") {
    locator = { project_id: v.locator_project_id };
  } else if (platform === "snowflake") {
    locator = { account_id: v.locator_account_id };
  }

  // Build auth
  let auth: Record<string, unknown> | null = null;
  if (PLATFORMS_WITH_AUTH.includes(platform) && v.auth_username) {
    const secretRef: Record<string, unknown> = {
      name: v.auth_secret_ref_name,
      key: v.auth_secret_ref_key,
    };
    auth = {
      username: v.auth_username,
      secret_ref: secretRef,
    };
    if (v.auth_password) {
      auth.password = v.auth_password;
    }
  }

  return {
    mode: "active-custom",
    platform,
    locator,
    identifier,
    auth,
    is_enabled: v.is_enabled,
    schedule_tier: v.schedule_tier || null,
  };
}
