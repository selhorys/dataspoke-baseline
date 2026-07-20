/**
 * Peripheral-config Zod schemas and pure helpers — extracted for testability.
 *
 * Mirrors src/api/schemas/admin.py field constraints for the DataHub and Langfuse
 * peripheral configurations. Each card owns its own schema, `toFormDefaults`, and
 * `buildPatch` so the page can PATCH each peripheral independently.
 *
 * Secrets (DataHub `token` and `kafka_sasl_password`, Langfuse `secret_key`) are
 * never echoed back: the masked indicator ("********") from the GET response is
 * dropped in `toFormDefaults` (the form starts blank), and `buildPatch` only
 * includes the secret when the user typed a non-empty value.
 * `kafka_sasl_password_version` is API-owned bookkeeping — never read, never sent.
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
  KafkaSaslMechanism,
  KafkaSecurityProtocol,
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

// ── Kafka security vocabulary ───────────────────────────────────────────────────

/** Mirrors `KAFKA_SECURITY_PROTOCOLS` in src/api/schemas/admin.py. */
export const KAFKA_SECURITY_PROTOCOLS = [
  "PLAINTEXT",
  "SSL",
  "SASL_PLAINTEXT",
  "SASL_SSL",
] as const satisfies readonly KafkaSecurityProtocol[];

/** Mechanisms authenticated with a typed username/password pair. */
export const KAFKA_CREDENTIAL_MECHANISMS = [
  "PLAIN",
  "SCRAM-SHA-256",
  "SCRAM-SHA-512",
] as const satisfies readonly KafkaSaslMechanism[];

/** Mirrors `KAFKA_SASL_MECHANISMS` in src/api/schemas/admin.py. */
export const KAFKA_SASL_MECHANISMS = [
  ...KAFKA_CREDENTIAL_MECHANISMS,
  "AWS_MSK_IAM",
] as const satisfies readonly KafkaSaslMechanism[];

/** The protocols that carry a SASL mechanism; the others reject one outright. */
export function isSaslProtocol(protocol: KafkaSecurityProtocol): boolean {
  return protocol === "SASL_PLAINTEXT" || protocol === "SASL_SSL";
}

/** True for the mechanisms that take a typed username/password pair. */
export function isCredentialMechanism(mechanism: KafkaSaslMechanism | ""): boolean {
  return (KAFKA_CREDENTIAL_MECHANISMS as readonly string[]).includes(mechanism);
}

/**
 * The mechanisms offerable under `protocol`.
 *
 * Empty for a non-SASL protocol (which rejects any mechanism), and
 * credential-only under `SASL_PLAINTEXT` — `AWS_MSK_IAM` requires `SASL_SSL`, so
 * offering it anywhere else would only produce a `422 INVALID_PARAMETER`.
 */
export function mechanismOptionsFor(
  protocol: KafkaSecurityProtocol,
): readonly KafkaSaslMechanism[] {
  if (!isSaslProtocol(protocol)) return [];
  return protocol === "SASL_SSL" ? KAFKA_SASL_MECHANISMS : KAFKA_CREDENTIAL_MECHANISMS;
}

/** Mirrors the `kafka_aws_region` pattern in src/api/schemas/admin.py. */
const AWS_REGION_RE = /^$|^[a-z0-9-]+$/;

/**
 * Mirrors `_MSK_BROKER_HOST_RE` in src/shared/datahub/kafka_security.py, serving
 * as both the rule-6 shape check and the rule-7 region extractor.
 *
 * The test is the MSK broker shape, not merely "an AWS host": an
 * attacker-controlled EC2 instance (`ec2-203-0-113-25.compute-1.amazonaws.com`)
 * and an S3 bucket endpoint are `*.amazonaws.com` too, and either would receive
 * a token signed with the consumer pod's IAM identity.
 *
 * Anchored at both ends for the same reason — an unanchored match would read
 * `b-1.mycluster.kafka.us-east-1.amazonaws.com.evil.tld` as region `us-east-1`
 * and hand the token to a host the attacker owns.
 */
const MSK_BROKER_HOST_RE = /^[A-Za-z0-9.-]+\.kafka(?:-serverless)?\.([a-z0-9-]+)\.amazonaws\.com$/;

/** Split a librdkafka `bootstrap.servers` string into its host[:port] entries. */
export function splitBrokers(brokers: string): string[] {
  return brokers
    .split(",")
    .map((entry) => entry.trim())
    .filter((entry) => entry !== "");
}

function stripPort(host: string): string {
  return host.includes(":") ? host.slice(0, host.lastIndexOf(":")) : host;
}

/** The broker entries rule 6 rejects under `AWS_MSK_IAM` — hosts that are not MSK brokers. */
export function nonMskBrokerHosts(brokers: string): string[] {
  return splitBrokers(brokers).filter((entry) => !MSK_BROKER_HOST_RE.test(stripPort(entry)));
}

/**
 * The AWS region every MSK broker host encodes, or `null` when they disagree.
 *
 * A mixed list is underivable rather than resolved to one of its regions, so the
 * caller fails loudly instead of signing for whichever happened to sort first.
 */
export function deriveMskRegion(brokers: string): string | null {
  const regions = new Set<string>();
  for (const entry of splitBrokers(brokers)) {
    const match = MSK_BROKER_HOST_RE.exec(stripPort(entry));
    if (match === null) return null;
    regions.add(match[1]);
  }
  return regions.size === 1 ? [...regions][0] : null;
}

export const datahubSchema = z
  .object({
    gms_url: z.string(),
    frontend_url: safeDisplayUrlField,
    kafka_brokers: z.string(),
    kafka_security_protocol: z.enum(KAFKA_SECURITY_PROTOCOLS),
    kafka_sasl_mechanism: z.enum([...KAFKA_SASL_MECHANISMS, ""]),
    kafka_sasl_username: z.string().max(256, "Must be at most 256 characters."),
    kafka_sasl_password: z.string(),
    kafka_aws_region: z
      .string()
      .max(64, "Must be at most 64 characters.")
      .refine((v) => AWS_REGION_RE.test(v), {
        message: "Must be a lowercase AWS region slug (e.g. ap-northeast-2). Leave blank to derive.",
      }),
    token: z.string(),
    service_corpuser_urn: z.string(),
    default_env: z.string(),
  })
  // The five rules of spec/API.md §DataHub Kafka security, mirrored so an
  // operator gets an inline field error instead of a raw 422 toast. The API
  // remains the authority — a rejection it raises still surfaces as a toast.
  .superRefine((v, ctx) => {
    const sasl = isSaslProtocol(v.kafka_security_protocol);

    // Rule 1 — the mechanism belongs to the SASL protocols and only to them.
    if (sasl && !v.kafka_sasl_mechanism) {
      ctx.addIssue({
        code: "custom",
        path: ["kafka_sasl_mechanism"],
        message: `A SASL mechanism is required when the protocol is ${v.kafka_security_protocol}.`,
      });
    }
    if (!sasl && v.kafka_sasl_mechanism) {
      ctx.addIssue({
        code: "custom",
        path: ["kafka_sasl_mechanism"],
        message: `A SASL mechanism is not allowed when the protocol is ${v.kafka_security_protocol}.`,
      });
    }

    // Rule 2 — credential mechanisms need a user to authenticate as.
    if (isCredentialMechanism(v.kafka_sasl_mechanism) && !v.kafka_sasl_username) {
      ctx.addIssue({
        code: "custom",
        path: ["kafka_sasl_username"],
        message: `A SASL username is required for ${v.kafka_sasl_mechanism}.`,
      });
    }

    if (v.kafka_sasl_mechanism === "AWS_MSK_IAM") {
      // Rule 3 — AWS_MSK_IAM authenticates with the consumer pod's IAM identity,
      // so a supplied credential is rejected rather than dropped.
      if (v.kafka_sasl_username) {
        ctx.addIssue({
          code: "custom",
          path: ["kafka_sasl_username"],
          message: "A SASL username is not accepted with AWS_MSK_IAM.",
        });
      }
      if (v.kafka_sasl_password) {
        ctx.addIssue({
          code: "custom",
          path: ["kafka_sasl_password"],
          message: "A SASL password is not accepted with AWS_MSK_IAM.",
        });
      }
      // Rule 4 — the stored protocol is always the one the consumer uses, so the
      // mismatch is rejected rather than silently upgraded to SASL_SSL.
      if (v.kafka_security_protocol !== "SASL_SSL") {
        ctx.addIssue({
          code: "custom",
          path: ["kafka_security_protocol"],
          message: "AWS_MSK_IAM requires the SASL_SSL protocol.",
        });
      }
      // Rule 6 — the pod's IAM identity is a deploy-time grant an Admin must not
      // be able to redirect: a host outside the MSK broker shape would receive a
      // SigV4 token minted from that identity and could replay it against the
      // real cluster.
      const hosts = splitBrokers(v.kafka_brokers);
      const offending = nonMskBrokerHosts(v.kafka_brokers);
      if (hosts.length === 0) {
        ctx.addIssue({
          code: "custom",
          path: ["kafka_brokers"],
          message: "At least one broker is required with AWS_MSK_IAM.",
        });
      } else if (offending.length > 0) {
        ctx.addIssue({
          code: "custom",
          path: ["kafka_brokers"],
          message:
            "AWS_MSK_IAM requires every broker to be an MSK broker of the form " +
            `<broker>.kafka[-serverless].<region>.amazonaws.com; rejected: ${offending.join(", ")}`,
        });
      } else {
        // Rule 7 — the host allowlist pins which cluster is reachable and the
        // region pins which account's endpoint the token is signed for; they must
        // describe the same place.
        const hostRegion = deriveMskRegion(v.kafka_brokers);
        if (hostRegion === null) {
          ctx.addIssue({
            code: "custom",
            path: ["kafka_brokers"],
            message: "Every broker must encode the same AWS region.",
          });
        } else if (v.kafka_aws_region && v.kafka_aws_region !== hostRegion) {
          ctx.addIssue({
            code: "custom",
            path: ["kafka_aws_region"],
            message: `Contradicts the region encoded in the brokers ('${hostRegion}').`,
          });
        }
      }
    } else if (v.kafka_aws_region) {
      // Rule 5 — the region exists only to sign an MSK IAM token.
      ctx.addIssue({
        code: "custom",
        path: ["kafka_aws_region"],
        message: "An AWS region is accepted only with AWS_MSK_IAM.",
      });
    }
  });

export type DatahubFormValues = z.infer<typeof datahubSchema>;

/** Convert a DataHub peripheral response into form default values (secrets blanked). */
export function datahubToFormDefaults(p: DatahubPeripheral): DatahubFormValues {
  return {
    gms_url: p.gms_url,
    frontend_url: p.frontend_url,
    kafka_brokers: p.kafka_brokers,
    kafka_security_protocol: p.kafka_security_protocol,
    kafka_sasl_mechanism: p.kafka_sasl_mechanism,
    kafka_sasl_username: p.kafka_sasl_username,
    // Never echo the masked indicator back as an editable value.
    kafka_sasl_password: "",
    kafka_aws_region: p.kafka_aws_region,
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
  if (values.kafka_security_protocol !== loaded.kafka_security_protocol) {
    patch.kafka_security_protocol = values.kafka_security_protocol;
  }
  if (values.kafka_sasl_mechanism !== loaded.kafka_sasl_mechanism) {
    patch.kafka_sasl_mechanism = values.kafka_sasl_mechanism;
  }
  if (values.kafka_sasl_username !== loaded.kafka_sasl_username) {
    patch.kafka_sasl_username = values.kafka_sasl_username;
  }
  // Only include the Kafka password when the user typed something (blank → keep current).
  if (values.kafka_sasl_password !== "") {
    patch.kafka_sasl_password = values.kafka_sasl_password;
  }
  if (values.kafka_aws_region !== loaded.kafka_aws_region) {
    patch.kafka_aws_region = values.kafka_aws_region;
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
