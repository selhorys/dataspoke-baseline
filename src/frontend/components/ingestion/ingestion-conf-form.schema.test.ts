/**
 * Tests for ingestion-conf-form.schema.ts — Zod schema mode-gating and
 * serialization helpers (toInternal / fromInternal / defaultFormValues).
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_INGESTION.md §Page contracts:
 *     "active-custom shows all input fields; passive hides locator/auth/schedule_tier"
 *   - src/api/schemas/ingestion.py CreateIngestionConfigRequest.validate_fields:
 *     (a) passive forbids locator, auth, schedule_tier
 *     (b) active-custom requires locator
 *     (c) active-custom + is_enabled=true requires schedule_tier
 *     (d) auth required for postgres/mysql/oracle/snowflake (CredentialAuth platforms)
 *     (e) auth must NOT be present for bigquery/kafka (NoAuth platforms)
 *   - src/shared/models/ingestion.py PLATFORM_REGISTRY:
 *     CredentialAuth platforms = {postgres, mysql, oracle, snowflake}
 *     NoAuth platforms         = {bigquery, kafka}
 *   - types/ingestion.ts PLATFORMS_WITH_AUTH: checked for frontend/backend divergence
 */

import { describe, it, expect } from "vitest";
import {
  ingestionConfSchema,
  toInternal,
  fromInternal,
  defaultFormValues,
  SECRET_REF_NAME_PREFIX,
} from "./ingestion-conf-form.schema";
import type { IngestionConfFormValues } from "@/types/ingestion";
import type { IngestionConfigResponse } from "@/types/ingestion";
import { PLATFORMS_WITH_AUTH } from "@/types/ingestion";

// ── Spec-anchored platform sets (from src/shared/models/ingestion.py PLATFORM_REGISTRY) ──
//
// Backend CredentialAuth platforms: postgres, mysql, oracle, snowflake
// Backend NoAuth platforms:         bigquery, kafka
//
// If PLATFORMS_WITH_AUTH in types/ingestion.ts diverges from this, the
// "PLATFORMS_WITH_AUTH frontend/backend alignment" tests below will fail
// and surface the divergence.

const BACKEND_CREDENTIAL_AUTH_PLATFORMS = ["postgres", "mysql", "oracle", "snowflake"] as const;
const BACKEND_NO_AUTH_PLATFORMS = ["bigquery", "kafka"] as const;

// ── Shared valid base: active-custom postgres (all fields present) ─────────────

function makeValidActiveCustomPostgres(
  overrides: Partial<IngestionConfFormValues> = {},
): IngestionConfFormValues {
  return {
    mode: "active-custom",
    platform: "postgres",
    locator_host: "pg.imazon.internal",
    locator_port: "5432",
    locator_bootstrap_servers: "",
    locator_project_id: "",
    locator_account_id: "",
    identifier_database: "example_db",
    identifier_schema_name: "catalog",
    identifier_table: "title_master",
    identifier_topic: "",
    identifier_cluster: "",
    identifier_dataset: "",
    auth_username: "readonly",
    auth_password: "",
    auth_secret_ref_name: "dataspoke-source-cred-imazon-pg",
    auth_secret_ref_key: "password",
    is_enabled: true,
    schedule_tier: "daily",
    ...overrides,
  };
}

function makeValidPassiveKafka(
  overrides: Partial<IngestionConfFormValues> = {},
): IngestionConfFormValues {
  return {
    mode: "passive",
    platform: "kafka",
    locator_host: "",
    locator_port: "",
    locator_bootstrap_servers: "",
    locator_project_id: "",
    locator_account_id: "",
    identifier_database: "",
    identifier_schema_name: "",
    identifier_table: "",
    identifier_topic: "imazon.orders.events",
    identifier_cluster: "prod",
    identifier_dataset: "",
    auth_username: "",
    auth_password: "",
    auth_secret_ref_name: "",
    auth_secret_ref_key: "",
    is_enabled: false,
    schedule_tier: "",
    ...overrides,
  };
}

// ── 1. PLATFORMS_WITH_AUTH frontend/backend alignment ─────────────────────────
//
// If the frontend constant diverges from the backend set, these tests fail
// and surface the divergence explicitly. This is the canary for the finding.

describe("PLATFORMS_WITH_AUTH alignment with backend CredentialAuth platforms (src/shared/models/ingestion.py PLATFORM_REGISTRY)", () => {
  it("includes all four backend CredentialAuth platforms: postgres, mysql, oracle, snowflake", () => {
    for (const p of BACKEND_CREDENTIAL_AUTH_PLATFORMS) {
      expect(PLATFORMS_WITH_AUTH).toContain(p);
    }
  });

  it("does NOT include bigquery (NoAuth backend platform)", () => {
    expect(PLATFORMS_WITH_AUTH).not.toContain("bigquery");
  });

  it("does NOT include kafka (NoAuth backend platform)", () => {
    expect(PLATFORMS_WITH_AUTH).not.toContain("kafka");
  });

  it("has exactly 4 members (no extra platforms added)", () => {
    expect(PLATFORMS_WITH_AUTH).toHaveLength(4);
  });
});

// ── 2. Active-custom: valid complete config passes ────────────────────────────

describe("schema — active-custom valid config (FRONTEND_INGESTION.md §Page contracts)", () => {
  it("accepts a complete active-custom postgres config with is_enabled=true and schedule_tier", () => {
    const result = ingestionConfSchema.safeParse(makeValidActiveCustomPostgres());
    expect(result.success).toBe(true);
  });

  it("accepts active-custom postgres with is_enabled=false and no schedule_tier", () => {
    const result = ingestionConfSchema.safeParse(
      makeValidActiveCustomPostgres({ is_enabled: false, schedule_tier: "" }),
    );
    expect(result.success).toBe(true);
  });

  it("accepts active-custom mysql with full locator/auth", () => {
    const result = ingestionConfSchema.safeParse(
      makeValidActiveCustomPostgres({
        platform: "mysql",
        locator_host: "mysql.imazon.internal",
        locator_port: "3306",
        identifier_database: "orders_db",
        identifier_schema_name: "orders",
        identifier_table: "shipments",
        auth_username: "reader",
        auth_secret_ref_name: "dataspoke-source-cred-mysql",
        auth_secret_ref_key: "password",
      }),
    );
    expect(result.success).toBe(true);
  });

  it("accepts active-custom kafka with bootstrap_servers and identifier topic/cluster", () => {
    const result = ingestionConfSchema.safeParse({
      ...defaultFormValues("active-custom"),
      platform: "kafka",
      locator_bootstrap_servers: "kafka:9092",
      identifier_topic: "imazon.orders.events",
      identifier_cluster: "prod",
    });
    expect(result.success).toBe(true);
  });

  it("accepts active-custom bigquery with project_id and identifier dataset/table", () => {
    const result = ingestionConfSchema.safeParse({
      ...defaultFormValues("active-custom"),
      platform: "bigquery",
      locator_project_id: "imazon-gcp-prod",
      identifier_dataset: "catalog",
      identifier_table: "books",
    });
    expect(result.success).toBe(true);
  });

  it("accepts active-custom snowflake with account_id and full identifier", () => {
    const result = ingestionConfSchema.safeParse({
      ...defaultFormValues("active-custom"),
      platform: "snowflake",
      locator_account_id: "imazon.snowflakecomputing.com",
      identifier_database: "IMAZON_DW",
      identifier_schema_name: "PUBLIC",
      identifier_table: "ORDERS",
      auth_username: "svc_dataspoke",
      auth_secret_ref_name: "dataspoke-source-cred-sf",
      auth_secret_ref_key: "password",
      is_enabled: false,
    });
    expect(result.success).toBe(true);
  });
});

// ── 3. Active-custom: locator required (ingestion.py validate_fields) ─────────

describe("schema — active-custom requires locator (ingestion.py validate_fields: 'locator is required for active-custom mode')", () => {
  it("fails when locator_host is empty for postgres active-custom", () => {
    const result = ingestionConfSchema.safeParse(
      makeValidActiveCustomPostgres({ locator_host: "" }),
    );
    expect(result.success).toBe(false);
    if (!result.success) {
      const issue = result.error.issues.find((i) => i.path[0] === "locator_host");
      expect(issue).toBeDefined();
    }
  });

  it("fails when locator_port is empty for postgres active-custom", () => {
    const result = ingestionConfSchema.safeParse(
      makeValidActiveCustomPostgres({ locator_port: "" }),
    );
    expect(result.success).toBe(false);
    if (!result.success) {
      const issue = result.error.issues.find((i) => i.path[0] === "locator_port");
      expect(issue).toBeDefined();
    }
  });

  it("fails when locator_port is out of range (>65535)", () => {
    const result = ingestionConfSchema.safeParse(
      makeValidActiveCustomPostgres({ locator_port: "99999" }),
    );
    expect(result.success).toBe(false);
    if (!result.success) {
      expect(result.error.issues.some((i) => i.path[0] === "locator_port")).toBe(true);
    }
  });

  it("fails when locator_port is 0 (below valid range)", () => {
    const result = ingestionConfSchema.safeParse(
      makeValidActiveCustomPostgres({ locator_port: "0" }),
    );
    expect(result.success).toBe(false);
  });

  it("fails when locator_bootstrap_servers is empty for kafka active-custom", () => {
    const result = ingestionConfSchema.safeParse({
      ...defaultFormValues("active-custom"),
      platform: "kafka",
      locator_bootstrap_servers: "",
      identifier_topic: "imazon.orders.events",
      identifier_cluster: "prod",
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      expect(
        result.error.issues.some((i) => i.path[0] === "locator_bootstrap_servers"),
      ).toBe(true);
    }
  });

  it("fails when locator_project_id is empty for bigquery active-custom", () => {
    const result = ingestionConfSchema.safeParse({
      ...defaultFormValues("active-custom"),
      platform: "bigquery",
      locator_project_id: "",
      identifier_dataset: "catalog",
      identifier_table: "books",
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      expect(
        result.error.issues.some((i) => i.path[0] === "locator_project_id"),
      ).toBe(true);
    }
  });

  it("fails when locator_account_id is empty for snowflake active-custom", () => {
    const result = ingestionConfSchema.safeParse({
      ...defaultFormValues("active-custom"),
      platform: "snowflake",
      locator_account_id: "",
      identifier_database: "DW",
      identifier_schema_name: "PUBLIC",
      identifier_table: "ORDERS",
      auth_username: "svc",
      auth_secret_ref_name: "dataspoke-source-cred-sf",
      auth_secret_ref_key: "password",
    });
    expect(result.success).toBe(false);
    if (!result.success) {
      expect(
        result.error.issues.some((i) => i.path[0] === "locator_account_id"),
      ).toBe(true);
    }
  });
});

// ── 4. Active-custom: schedule_tier required when is_enabled=true ─────────────
//
// Backend rule: "schedule_tier is required when is_enabled is true and mode is active-custom"

describe("schema — active-custom + is_enabled=true requires schedule_tier (ingestion.py validate_fields)", () => {
  it("fails when is_enabled=true but schedule_tier is empty", () => {
    const result = ingestionConfSchema.safeParse(
      makeValidActiveCustomPostgres({ is_enabled: true, schedule_tier: "" }),
    );
    expect(result.success).toBe(false);
    if (!result.success) {
      const issue = result.error.issues.find((i) => i.path[0] === "schedule_tier");
      expect(issue).toBeDefined();
    }
  });

  it("passes when is_enabled=false and schedule_tier is empty (disabled active-custom)", () => {
    const result = ingestionConfSchema.safeParse(
      makeValidActiveCustomPostgres({ is_enabled: false, schedule_tier: "" }),
    );
    expect(result.success).toBe(true);
  });

  it("passes when is_enabled=true and schedule_tier='hourly'", () => {
    const result = ingestionConfSchema.safeParse(
      makeValidActiveCustomPostgres({ is_enabled: true, schedule_tier: "hourly" }),
    );
    expect(result.success).toBe(true);
  });

  it("passes when is_enabled=true and schedule_tier='weekly'", () => {
    const result = ingestionConfSchema.safeParse(
      makeValidActiveCustomPostgres({ is_enabled: true, schedule_tier: "weekly" }),
    );
    expect(result.success).toBe(true);
  });
});

// ── 5. Auth coupling: CredentialAuth platforms require auth fields ─────────────
//
// Backend rule: auth required for postgres/mysql/oracle/snowflake (PLATFORM_REGISTRY)

describe("schema — auth required for CredentialAuth platforms (src/shared/models/ingestion.py PLATFORM_REGISTRY)", () => {
  BACKEND_CREDENTIAL_AUTH_PLATFORMS.forEach((platform) => {
    it(`fails when auth_username is empty for active-custom ${platform}`, () => {
      const base =
        platform === "snowflake"
          ? {
              ...defaultFormValues("active-custom"),
              platform: "snowflake" as const,
              locator_account_id: "acct.snowflakecomputing.com",
              identifier_database: "DW",
              identifier_schema_name: "PUBLIC",
              identifier_table: "T",
              auth_secret_ref_name: "dataspoke-source-cred-sf",
              auth_secret_ref_key: "password",
            }
          : makeValidActiveCustomPostgres({ platform, auth_username: "" });

      const result = ingestionConfSchema.safeParse(
        platform === "snowflake" ? base : { ...base, auth_username: "" },
      );
      expect(result.success).toBe(false);
      if (!result.success) {
        expect(result.error.issues.some((i) => i.path[0] === "auth_username")).toBe(true);
      }
    });

    it(`fails when auth_secret_ref_name is empty for active-custom ${platform}`, () => {
      const base =
        platform === "snowflake"
          ? {
              ...defaultFormValues("active-custom"),
              platform: "snowflake" as const,
              locator_account_id: "acct.snowflakecomputing.com",
              identifier_database: "DW",
              identifier_schema_name: "PUBLIC",
              identifier_table: "T",
              auth_username: "svc",
              auth_secret_ref_name: "",
              auth_secret_ref_key: "password",
            }
          : makeValidActiveCustomPostgres({
              platform,
              auth_secret_ref_name: "",
            });
      const result = ingestionConfSchema.safeParse(base);
      expect(result.success).toBe(false);
      if (!result.success) {
        expect(
          result.error.issues.some((i) => i.path[0] === "auth_secret_ref_name"),
        ).toBe(true);
      }
    });

    it(`fails when auth_secret_ref_key is empty for active-custom ${platform}`, () => {
      const base =
        platform === "snowflake"
          ? {
              ...defaultFormValues("active-custom"),
              platform: "snowflake" as const,
              locator_account_id: "acct.snowflakecomputing.com",
              identifier_database: "DW",
              identifier_schema_name: "PUBLIC",
              identifier_table: "T",
              auth_username: "svc",
              auth_secret_ref_name: "dataspoke-source-cred-sf",
              auth_secret_ref_key: "",
            }
          : makeValidActiveCustomPostgres({
              platform,
              auth_secret_ref_key: "",
            });
      const result = ingestionConfSchema.safeParse(base);
      expect(result.success).toBe(false);
      if (!result.success) {
        expect(
          result.error.issues.some((i) => i.path[0] === "auth_secret_ref_key"),
        ).toBe(true);
      }
    });
  });
});

// ── 6. Auth coupling: NoAuth platforms do NOT require auth fields ──────────────
//
// Backend: bigquery/kafka use NoAuth — auth fields absent is valid

describe("schema — auth NOT required for NoAuth platforms (src/shared/models/ingestion.py PLATFORM_REGISTRY)", () => {
  // Canary: every backend NoAuth platform must be absent from PLATFORMS_WITH_AUTH.
  it("PLATFORMS_WITH_AUTH excludes all backend NoAuth platforms (bigquery, kafka)", () => {
    BACKEND_NO_AUTH_PLATFORMS.forEach((p) => {
      expect(PLATFORMS_WITH_AUTH).not.toContain(p);
    });
  });

  it("passes for active-custom kafka without auth fields", () => {
    const result = ingestionConfSchema.safeParse({
      ...defaultFormValues("active-custom"),
      platform: "kafka",
      locator_bootstrap_servers: "kafka:9092",
      identifier_topic: "imazon.orders.events",
      identifier_cluster: "prod",
      auth_username: "",
      auth_secret_ref_name: "",
      auth_secret_ref_key: "",
    });
    expect(result.success).toBe(true);
  });

  it("passes for active-custom bigquery without auth fields", () => {
    const result = ingestionConfSchema.safeParse({
      ...defaultFormValues("active-custom"),
      platform: "bigquery",
      locator_project_id: "imazon-gcp-prod",
      identifier_dataset: "catalog",
      identifier_table: "books",
      auth_username: "",
      auth_secret_ref_name: "",
      auth_secret_ref_key: "",
    });
    expect(result.success).toBe(true);
  });
});

// ── 6b. secret_ref.name prefix invariant ─────────────────────────────────────
//
// Backend rule (src/backend/ingestion/secret_resolver.py _NAME_PREFIX,
//               src/api/schemas/ingestion.py SecretRefSpec._validate_name_prefix):
//   secret_ref.name must start with 'dataspoke-source-cred-'; backend returns 422 otherwise.
// The frontend schema must enforce this to prevent a silent 422.

describe("schema — secret_ref.name prefix validation (mirrors secret_resolver.py _NAME_PREFIX)", () => {
  it("SECRET_REF_NAME_PREFIX matches the backend constant 'dataspoke-source-cred-'", () => {
    // This test encodes the backend invariant. If the prefix ever changes in the
    // backend, updating secret_resolver.py _NAME_PREFIX will make this test fail,
    // surfacing the divergence before a 422 can occur.
    expect(SECRET_REF_NAME_PREFIX).toBe("dataspoke-source-cred-");
  });

  it("fails when active-custom + credential platform has secret_ref.name without the required prefix", () => {
    const result = ingestionConfSchema.safeParse(
      makeValidActiveCustomPostgres({ auth_secret_ref_name: "wrong-name-no-prefix" }),
    );
    expect(result.success).toBe(false);
    if (!result.success) {
      const issue = result.error.issues.find((i) => i.path[0] === "auth_secret_ref_name");
      expect(issue).toBeDefined();
    }
  });

  it("passes when active-custom + credential platform has secret_ref.name with the correct prefix", () => {
    const result = ingestionConfSchema.safeParse(
      makeValidActiveCustomPostgres({
        auth_secret_ref_name: `${SECRET_REF_NAME_PREFIX}mydb-creds`,
      }),
    );
    expect(result.success).toBe(true);
  });

  it("passes when the secret_ref.name is exactly the prefix with a suffix (minimum valid name)", () => {
    const result = ingestionConfSchema.safeParse(
      makeValidActiveCustomPostgres({
        auth_secret_ref_name: `${SECRET_REF_NAME_PREFIX}x`,
      }),
    );
    expect(result.success).toBe(true);
  });

  it("no prefix requirement for passive mode (auth section is not active)", () => {
    // Passive mode ignores auth fields entirely — prefix rule does not apply.
    const result = ingestionConfSchema.safeParse(
      makeValidPassiveKafka({ auth_secret_ref_name: "wrong-prefix" }),
    );
    expect(result.success).toBe(true);
  });

  it("no prefix requirement for active-custom + NoAuth platform (kafka)", () => {
    // kafka is a NoAuth platform — auth fields are not validated.
    const result = ingestionConfSchema.safeParse({
      ...defaultFormValues("active-custom"),
      platform: "kafka",
      locator_bootstrap_servers: "kafka:9092",
      identifier_topic: "imazon.orders.events",
      identifier_cluster: "prod",
      auth_secret_ref_name: "wrong-prefix",
    });
    expect(result.success).toBe(true);
  });

  it("no prefix requirement for active-custom + NoAuth platform (bigquery)", () => {
    // bigquery is a NoAuth platform — auth fields are not validated.
    const result = ingestionConfSchema.safeParse({
      ...defaultFormValues("active-custom"),
      platform: "bigquery",
      locator_project_id: "imazon-gcp-prod",
      identifier_dataset: "catalog",
      identifier_table: "books",
      auth_secret_ref_name: "wrong-prefix",
    });
    expect(result.success).toBe(true);
  });
});

// ── 7. Passive mode: forbids active-only fields ───────────────────────────────
//
// Backend rule (ingestion.py validate_fields passive branch):
//   "schedule_tier is not allowed for passive mode"
//   "locator is not allowed for passive mode"
//   "auth is not allowed for passive mode"
//
// The frontend schema does not explicitly forbid providing these values
// in the raw input — it simply ignores them for passive mode in fromInternal.
// What matters for the schema is that passive mode passes without those fields.

describe("schema — passive mode: passes validation without locator/auth/schedule_tier (FRONTEND_INGESTION.md §Page contracts)", () => {
  it("accepts a minimal passive kafka config with is_enabled=false", () => {
    const result = ingestionConfSchema.safeParse(makeValidPassiveKafka());
    expect(result.success).toBe(true);
  });

  it("accepts passive mode with is_enabled=true (F5 fix: passive enabled is a real toggle)", () => {
    const result = ingestionConfSchema.safeParse(
      makeValidPassiveKafka({ is_enabled: true }),
    );
    expect(result.success).toBe(true);
  });

  it("accepts passive mode with is_enabled=false and schedule_tier empty (passive never needs schedule_tier)", () => {
    const result = ingestionConfSchema.safeParse(
      makeValidPassiveKafka({ is_enabled: false, schedule_tier: "" }),
    );
    expect(result.success).toBe(true);
  });

  it("does NOT require locator fields for passive mode (passive ingestor handles connectivity externally)", () => {
    const result = ingestionConfSchema.safeParse({
      ...makeValidPassiveKafka(),
      locator_host: "",
      locator_port: "",
      locator_bootstrap_servers: "",
    });
    expect(result.success).toBe(true);
  });

  it("does NOT require auth fields for passive mode", () => {
    const result = ingestionConfSchema.safeParse({
      ...makeValidPassiveKafka(),
      auth_username: "",
      auth_secret_ref_name: "",
      auth_secret_ref_key: "",
    });
    expect(result.success).toBe(true);
  });

  it("accepts passive postgres config without locator/auth", () => {
    const result = ingestionConfSchema.safeParse({
      mode: "passive" as const,
      platform: "postgres" as const,
      locator_host: "",
      locator_port: "",
      locator_bootstrap_servers: "",
      locator_project_id: "",
      locator_account_id: "",
      identifier_database: "example_db",
      identifier_schema_name: "catalog",
      identifier_table: "title_master",
      identifier_topic: "",
      identifier_cluster: "",
      identifier_dataset: "",
      auth_username: "",
      auth_password: "",
      auth_secret_ref_name: "",
      auth_secret_ref_key: "",
      is_enabled: false,
      schedule_tier: "",
    });
    expect(result.success).toBe(true);
  });
});

// ── 8. fromInternal: active-custom serialization (API body shape) ─────────────
//
// Spec: PUT .../attr/ingestion/conf body must include locator/auth/schedule_tier
// for active-custom (backend CreateIngestionConfigRequest).

describe("fromInternal — active-custom serializes locator/auth/schedule_tier (ingestion.py CreateIngestionConfigRequest)", () => {
  it("includes mode='active-custom' in body", () => {
    const body = fromInternal(makeValidActiveCustomPostgres());
    expect(body.mode).toBe("active-custom");
  });

  it("includes locator with host (string) and port (number) for RDBMS platforms", () => {
    const body = fromInternal(makeValidActiveCustomPostgres({ locator_port: "5432" }));
    expect(body.locator).toEqual({ host: "pg.imazon.internal", port: 5432 });
    expect(typeof (body.locator as Record<string, unknown>).port).toBe("number");
  });

  it("includes auth with username and secret_ref when auth_username is present", () => {
    const body = fromInternal(makeValidActiveCustomPostgres());
    const auth = body.auth as Record<string, unknown>;
    expect(auth).not.toBeNull();
    expect(auth.username).toBe("readonly");
    expect((auth.secret_ref as Record<string, unknown>).name).toBe(
      "dataspoke-source-cred-imazon-pg",
    );
    expect((auth.secret_ref as Record<string, unknown>).key).toBe("password");
  });

  it("includes password in auth only when auth_password is non-empty", () => {
    const withPw = fromInternal(
      makeValidActiveCustomPostgres({ auth_password: "s3cr3t" }),
    );
    expect((withPw.auth as Record<string, unknown>).password).toBe("s3cr3t");

    const withoutPw = fromInternal(makeValidActiveCustomPostgres({ auth_password: "" }));
    expect((withoutPw.auth as Record<string, unknown>).password).toBeUndefined();
  });

  it("includes schedule_tier as the string value when set", () => {
    const body = fromInternal(makeValidActiveCustomPostgres({ schedule_tier: "daily" }));
    expect(body.schedule_tier).toBe("daily");
  });

  it("serializes schedule_tier as null when schedule_tier is empty string", () => {
    const body = fromInternal(
      makeValidActiveCustomPostgres({ is_enabled: false, schedule_tier: "" }),
    );
    expect(body.schedule_tier).toBeNull();
  });

  it("includes is_enabled as boolean", () => {
    const body = fromInternal(makeValidActiveCustomPostgres({ is_enabled: true }));
    expect(body.is_enabled).toBe(true);
  });

  it("includes identifier with database/schema_name/table for postgres", () => {
    const body = fromInternal(makeValidActiveCustomPostgres());
    expect(body.identifier).toEqual({
      database: "example_db",
      schema_name: "catalog",
      table: "title_master",
    });
  });

  it("serializes kafka locator as { bootstrap_servers }", () => {
    const kafkaForm: IngestionConfFormValues = {
      ...defaultFormValues("active-custom"),
      platform: "kafka",
      locator_bootstrap_servers: "kafka:9092",
      identifier_topic: "imazon.orders.events",
      identifier_cluster: "prod",
    };
    const body = fromInternal(kafkaForm);
    expect(body.locator).toEqual({ bootstrap_servers: "kafka:9092" });
    expect(body.identifier).toEqual({ topic: "imazon.orders.events", cluster: "prod" });
  });

  it("serializes bigquery locator as { project_id }", () => {
    const bqForm: IngestionConfFormValues = {
      ...defaultFormValues("active-custom"),
      platform: "bigquery",
      locator_project_id: "imazon-gcp-prod",
      identifier_dataset: "catalog",
      identifier_table: "books",
    };
    const body = fromInternal(bqForm);
    expect(body.locator).toEqual({ project_id: "imazon-gcp-prod" });
    expect(body.identifier).toEqual({ dataset: "catalog", table: "books" });
    expect(body.auth).toBeNull();
  });

  it("serializes snowflake locator as { account_id }", () => {
    const sfForm: IngestionConfFormValues = {
      ...defaultFormValues("active-custom"),
      platform: "snowflake",
      locator_account_id: "imazon.snowflakecomputing.com",
      identifier_database: "DW",
      identifier_schema_name: "PUBLIC",
      identifier_table: "ORDERS",
      auth_username: "svc_dataspoke",
      auth_secret_ref_name: "dataspoke-source-cred-sf",
      auth_secret_ref_key: "password",
    };
    const body = fromInternal(sfForm);
    expect(body.locator).toEqual({ account_id: "imazon.snowflakecomputing.com" });
  });
});

// ── 9. fromInternal: passive serialization omits locator/auth ─────────────────
//
// Backend: passive mode must NOT have locator/auth/schedule_tier in the request body.
// fromInternal produces schedule_tier: null (backend accepts null to clear the field).

describe("fromInternal — passive mode omits locator and auth, schedule_tier is null (ingestion.py passive branch)", () => {
  it("produces mode='passive' in body", () => {
    const body = fromInternal(makeValidPassiveKafka());
    expect(body.mode).toBe("passive");
  });

  it("does not include locator key in passive body", () => {
    const body = fromInternal(makeValidPassiveKafka());
    expect(body).not.toHaveProperty("locator");
  });

  it("does not include auth key in passive body", () => {
    const body = fromInternal(makeValidPassiveKafka());
    expect(body).not.toHaveProperty("auth");
  });

  it("includes schedule_tier: null for passive (backend accepts null to clear)", () => {
    const body = fromInternal(makeValidPassiveKafka());
    expect(body.schedule_tier).toBeNull();
  });

  it("includes is_enabled as boolean for passive", () => {
    const body = fromInternal(makeValidPassiveKafka({ is_enabled: true }));
    expect(body.is_enabled).toBe(true);
  });

  it("includes platform in passive body", () => {
    const body = fromInternal(makeValidPassiveKafka());
    expect(body.platform).toBe("kafka");
  });

  it("includes identifier in passive body", () => {
    const body = fromInternal(makeValidPassiveKafka());
    expect(body.identifier).toEqual({
      topic: "imazon.orders.events",
      cluster: "prod",
    });
  });
});

// ── 10. toInternal: API response → form shape conversion ──────────────────────

describe("toInternal — converts IngestionConfigResponse into flat form shape (round-trip input)", () => {
  const activeResponse: IngestionConfigResponse = {
    id: "abc-123",
    dataset_urn: "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)",
    mode: "active-custom",
    platform: "postgres",
    locator: { host: "pg.imazon.internal", port: 5432 },
    identifier: { database: "example_db", schema_name: "catalog", table: "title_master" },
    auth: { username: "readonly", secret_ref: { name: "dataspoke-source-cred-imazon-pg", key: "password" } },
    is_enabled: true,
    schedule_tier: "daily",
    workflow_dag_id: "ingestion-dag-abc",
    status: "OK",
    created_at: "2026-04-01T00:00:00Z",
    updated_at: "2026-04-25T10:00:00Z",
  };

  it("maps mode correctly", () => {
    expect(toInternal(activeResponse).mode).toBe("active-custom");
  });

  it("maps platform correctly", () => {
    expect(toInternal(activeResponse).platform).toBe("postgres");
  });

  it("maps locator host and port as strings", () => {
    const form = toInternal(activeResponse);
    expect(form.locator_host).toBe("pg.imazon.internal");
    expect(form.locator_port).toBe("5432");
  });

  it("maps auth username and secret_ref", () => {
    const form = toInternal(activeResponse);
    expect(form.auth_username).toBe("readonly");
    expect(form.auth_secret_ref_name).toBe("dataspoke-source-cred-imazon-pg");
    expect(form.auth_secret_ref_key).toBe("password");
  });

  it("leaves auth_password empty (passwords are never returned by the API)", () => {
    expect(toInternal(activeResponse).auth_password).toBe("");
  });

  it("maps schedule_tier correctly", () => {
    expect(toInternal(activeResponse).schedule_tier).toBe("daily");
  });

  it("maps is_enabled correctly", () => {
    expect(toInternal(activeResponse).is_enabled).toBe(true);
  });

  it("maps null schedule_tier to empty string (for passive or disabled)", () => {
    const passiveResponse: IngestionConfigResponse = {
      ...activeResponse,
      mode: "passive",
      locator: null,
      auth: null,
      schedule_tier: null,
      is_enabled: false,
    };
    expect(toInternal(passiveResponse).schedule_tier).toBe("");
  });

  it("maps null locator fields to empty strings (passive response)", () => {
    const passiveResponse: IngestionConfigResponse = {
      ...activeResponse,
      mode: "passive",
      locator: null,
      auth: null,
      schedule_tier: null,
      is_enabled: false,
    };
    const form = toInternal(passiveResponse);
    expect(form.locator_host).toBe("");
    expect(form.locator_port).toBe("");
  });
});

// ── 11. Round-trip: toInternal → fromInternal preserves meaningful fields ──────

describe("round-trip toInternal(response) → fromInternal — preserves meaningful fields", () => {
  const activeResponse: IngestionConfigResponse = {
    id: "abc-123",
    dataset_urn: "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)",
    mode: "active-custom",
    platform: "postgres",
    locator: { host: "pg.imazon.internal", port: 5432 },
    identifier: { database: "example_db", schema_name: "catalog", table: "title_master" },
    auth: { username: "readonly", secret_ref: { name: "dataspoke-source-cred-imazon-pg", key: "password" } },
    is_enabled: true,
    schedule_tier: "daily",
    workflow_dag_id: "ingestion-dag-abc",
    status: "OK",
    created_at: "2026-04-01T00:00:00Z",
    updated_at: "2026-04-25T10:00:00Z",
  };

  it("round-trip preserves mode", () => {
    const body = fromInternal(toInternal(activeResponse));
    expect(body.mode).toBe("active-custom");
  });

  it("round-trip preserves platform", () => {
    const body = fromInternal(toInternal(activeResponse));
    expect(body.platform).toBe("postgres");
  });

  it("round-trip preserves locator host/port (port as number in body)", () => {
    const body = fromInternal(toInternal(activeResponse));
    expect(body.locator).toEqual({ host: "pg.imazon.internal", port: 5432 });
  });

  it("round-trip preserves identifier", () => {
    const body = fromInternal(toInternal(activeResponse));
    expect(body.identifier).toEqual({
      database: "example_db",
      schema_name: "catalog",
      table: "title_master",
    });
  });

  it("round-trip preserves schedule_tier", () => {
    const body = fromInternal(toInternal(activeResponse));
    expect(body.schedule_tier).toBe("daily");
  });

  it("round-trip preserves is_enabled", () => {
    const body = fromInternal(toInternal(activeResponse));
    expect(body.is_enabled).toBe(true);
  });

  it("round-trip for passive response: fromInternal omits locator/auth, schedule_tier is null", () => {
    const passiveResponse: IngestionConfigResponse = {
      id: "def-456",
      dataset_urn: "urn:li:dataset:(urn:li:dataPlatform:kafka,imazon.orders.events,DEV)",
      mode: "passive",
      platform: "kafka",
      locator: null,
      identifier: { topic: "imazon.orders.events", cluster: "prod" },
      auth: null,
      is_enabled: false,
      schedule_tier: null,
      workflow_dag_id: null,
      status: "OK",
      created_at: "2026-04-01T00:00:00Z",
      updated_at: "2026-04-25T10:00:00Z",
    };
    const body = fromInternal(toInternal(passiveResponse));
    expect(body.mode).toBe("passive");
    expect(body).not.toHaveProperty("locator");
    expect(body).not.toHaveProperty("auth");
    expect(body.schedule_tier).toBeNull();
    expect(body.identifier).toEqual({ topic: "imazon.orders.events", cluster: "prod" });
  });
});

// ── 12. defaultFormValues ──────────────────────────────────────────────────────

describe("defaultFormValues — blank form state for a new config", () => {
  it("defaults mode to 'active-custom'", () => {
    expect(defaultFormValues().mode).toBe("active-custom");
  });

  it("defaults platform to 'postgres'", () => {
    expect(defaultFormValues().platform).toBe("postgres");
  });

  it("all string fields are empty strings", () => {
    const v = defaultFormValues();
    const stringFields: (keyof IngestionConfFormValues)[] = [
      "locator_host", "locator_port", "locator_bootstrap_servers",
      "locator_project_id", "locator_account_id",
      "identifier_database", "identifier_schema_name", "identifier_table",
      "identifier_topic", "identifier_cluster", "identifier_dataset",
      "auth_username", "auth_password", "auth_secret_ref_name", "auth_secret_ref_key",
      "schedule_tier",
    ];
    stringFields.forEach((f) => {
      expect(v[f]).toBe("");
    });
  });

  it("is_enabled defaults to false", () => {
    expect(defaultFormValues().is_enabled).toBe(false);
  });

  it("accepts explicit 'passive' mode override", () => {
    expect(defaultFormValues("passive").mode).toBe("passive");
  });
});
