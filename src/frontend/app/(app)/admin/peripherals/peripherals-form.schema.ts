/**
 * Peripheral-config Zod schemas and pure helpers — extracted for testability.
 *
 * Mirrors src/api/schemas/admin.py field constraints for the DataHub and Langfuse
 * peripheral configurations. Each card owns its own schema, `toFormDefaults`, and
 * `buildPatch` so the page can PATCH each peripheral independently.
 *
 * Secrets (DataHub `token`, Langfuse `secret_key`) are never echoed back: the
 * masked indicator ("********") from the GET response is dropped in `toFormDefaults`
 * (the form starts blank), and `buildPatch` only includes the secret when the user
 * typed a non-empty value.
 *
 * Spec: spec/feature/FRONTEND_BASIC.md §Admin Peripherals.
 */

import { z } from "zod";
import {
  SAFE_DISPLAY_URL_MAX_LENGTH,
  SAFE_DISPLAY_URL_RE,
  SAFE_PROJECT_ID_MAX_LENGTH,
  SAFE_PROJECT_ID_RE,
} from "@/lib/safe-url";
import type {
  DatahubPeripheral,
  DatahubPeripheralPatch,
  LangfusePeripheral,
  LangfusePeripheralPatch,
} from "@/lib/api/types";

// ── DataHub ─────────────────────────────────────────────────────────────────────

/**
 * Mirrors `SAFE_DISPLAY_URL_PATTERN` (defined in src/api/schemas/common.py and
 * imported by src/api/schemas/admin.py) so a malformed URL surfaces as an inline
 * field error instead of a raw 422 toast.
 *
 * Applied to every operator-supplied URL the backend constrains this way: the
 * DataHub `frontend_url` and the Langfuse `host`.
 */
export const safeDisplayUrlField = z
  .string()
  .max(SAFE_DISPLAY_URL_MAX_LENGTH, `Must be at most ${SAFE_DISPLAY_URL_MAX_LENGTH} characters.`)
  .refine((v) => SAFE_DISPLAY_URL_RE.test(v), {
    message:
      "Must be an http:// or https:// URL with no credentials, spaces, or control " +
      "characters (e.g. https://datahub.example.com). Leave blank to unset.",
  });

/**
 * Mirrors `SAFE_PROJECT_ID_PATTERN` in src/api/schemas/common.py. The project id
 * is interpolated into a Langfuse deep-link path segment, so it is constrained
 * to an opaque slug.
 */
export const safeProjectIdField = z
  .string()
  .max(SAFE_PROJECT_ID_MAX_LENGTH, `Must be at most ${SAFE_PROJECT_ID_MAX_LENGTH} characters.`)
  .refine((v) => SAFE_PROJECT_ID_RE.test(v), {
    message:
      "Must start with a letter or digit and contain only letters, digits, " +
      "hyphens, and underscores. Leave blank to unset.",
  });

export const datahubSchema = z.object({
  gms_url: z.string(),
  frontend_url: safeDisplayUrlField,
  kafka_brokers: z.string(),
  token: z.string(),
  service_corpuser_urn: z.string(),
  default_env: z.string(),
});

export type DatahubFormValues = z.infer<typeof datahubSchema>;

/** Convert a DataHub peripheral response into form default values (secret blanked). */
export function datahubToFormDefaults(p: DatahubPeripheral): DatahubFormValues {
  return {
    gms_url: p.gms_url,
    frontend_url: p.frontend_url,
    kafka_brokers: p.kafka_brokers,
    // Never echo the masked indicator back as an editable value.
    token: "",
    service_corpuser_urn: p.service_corpuser_urn,
    default_env: p.default_env,
  };
}

/** Diff form values against the loaded peripheral, returning only changed keys. */
export function datahubBuildPatch(
  values: DatahubFormValues,
  loaded: DatahubPeripheral,
): DatahubPeripheralPatch {
  const patch: DatahubPeripheralPatch = {};

  if (values.gms_url !== loaded.gms_url) {
    patch.gms_url = values.gms_url;
  }
  if (values.frontend_url !== loaded.frontend_url) {
    patch.frontend_url = values.frontend_url;
  }
  if (values.kafka_brokers !== loaded.kafka_brokers) {
    patch.kafka_brokers = values.kafka_brokers;
  }
  // Only include the token when the user typed something (blank → keep current).
  if (values.token !== "") {
    patch.token = values.token;
  }
  if (values.service_corpuser_urn !== loaded.service_corpuser_urn) {
    patch.service_corpuser_urn = values.service_corpuser_urn;
  }
  if (values.default_env !== loaded.default_env) {
    patch.default_env = values.default_env;
  }

  return patch;
}

// ── Langfuse ────────────────────────────────────────────────────────────────────

export const langfuseSchema = z.object({
  host: safeDisplayUrlField,
  public_key: z.string(),
  secret_key: z.string(),
  project_id: safeProjectIdField,
  environment_tag: z.string(),
});

export type LangfuseFormValues = z.infer<typeof langfuseSchema>;

/** Convert a Langfuse peripheral response into form default values (secret blanked). */
export function langfuseToFormDefaults(p: LangfusePeripheral): LangfuseFormValues {
  return {
    host: p.host,
    public_key: p.public_key,
    // Never echo the masked indicator back as an editable value.
    secret_key: "",
    project_id: p.project_id,
    environment_tag: p.environment_tag,
  };
}

/** Diff form values against the loaded peripheral, returning only changed keys. */
export function langfuseBuildPatch(
  values: LangfuseFormValues,
  loaded: LangfusePeripheral,
): LangfusePeripheralPatch {
  const patch: LangfusePeripheralPatch = {};

  if (values.host !== loaded.host) {
    patch.host = values.host;
  }
  if (values.public_key !== loaded.public_key) {
    patch.public_key = values.public_key;
  }
  // Only include the secret key when the user typed something (blank → keep current).
  if (values.secret_key !== "") {
    patch.secret_key = values.secret_key;
  }
  if (values.project_id !== loaded.project_id) {
    patch.project_id = values.project_id;
  }
  if (values.environment_tag !== loaded.environment_tag) {
    patch.environment_tag = values.environment_tag;
  }

  return patch;
}
