/**
 * Tests for app/(app)/admin/peripherals/page.tsx — Admin Peripherals page.
 *
 * Spec traces:
 *   - spec/feature/FRONTEND_BASIC.md §Admin Peripherals:
 *       two cards (DataHub, Langfuse), each its own form + Save button (per-card
 *       partial PATCH); non-secret fields (service_corpuser_urn, default_env,
 *       project_id, environment_tag) prefilled from GET and sent plain; secrets
 *       (token, secret_key) start blank, are blank-omitted from PATCH, never echo
 *       "********" back as a value; only changed fields are PATCHed; admin-gated.
 *   - spec/API.md §/admin/peripherals/datahub + /langfuse: the response/patch shapes.
 *
 * Mocked: useMe, the four admin hooks, toast, timezone — Vitest unit tier (no API).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import React from "react";
import type { DatahubPeripheral, LangfusePeripheral } from "@/lib/api/types";

// ---------------------------------------------------------------------------
// Shared mock factories
// ---------------------------------------------------------------------------

function makeDatahub(overrides: Partial<DatahubPeripheral> = {}): DatahubPeripheral {
  return {
    resp_time: "2026-06-26T00:00:00Z",
    gms_url: "http://datahub-gms:8080",
    // Browser-facing UI URL — deliberately differs from gms_url in host, port,
    // and scheme, mirroring the deployment shape the endpoint exists to serve.
    frontend_url: "https://datahub.example.com",
    kafka_brokers: "kafka:9092",
    kafka_security_protocol: "PLAINTEXT",
    kafka_sasl_mechanism: "",
    kafka_sasl_username: "",
    kafka_sasl_password: "",
    kafka_sasl_password_version: 0,
    kafka_aws_region: "",
    token: "********",
    service_corpuser_urn: "urn:li:corpuser:dataspoke",
    default_env: "DEV",
    is_configured: true,
    health: { status: "unknown", last_error: null, last_ok_at: null, updated_at: null },
    updated_at: "2026-06-26T10:00:00Z",
    ...overrides,
  };
}

/** A DataHub peripheral already secured with a SCRAM credential. */
function makeScramDatahub(overrides: Partial<DatahubPeripheral> = {}): DatahubPeripheral {
  return makeDatahub({
    kafka_security_protocol: "SASL_SSL",
    kafka_sasl_mechanism: "SCRAM-SHA-512",
    kafka_sasl_username: "dataspoke",
    kafka_sasl_password: "********",
    kafka_sasl_password_version: 3,
    ...overrides,
  });
}

function makeLangfuse(overrides: Partial<LangfusePeripheral> = {}): LangfusePeripheral {
  return {
    resp_time: "2026-06-26T00:00:00Z",
    host: "http://langfuse:3000",
    public_key: "pk-test",
    secret_key: "********",
    project_id: "imazon-metadata",
    environment_tag: "production",
    is_configured: true,
    updated_at: "2026-06-26T10:00:00Z",
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Browser API stubs — jsdom lacks ResizeObserver (used by Radix UI)
// ---------------------------------------------------------------------------
if (typeof global.ResizeObserver === "undefined") {
  global.ResizeObserver = class ResizeObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}
// jsdom implements neither pointer capture nor scrollIntoView, both of which the
// Radix Select trigger calls when it opens its listbox.
if (!Element.prototype.hasPointerCapture) {
  Element.prototype.hasPointerCapture = () => false;
  Element.prototype.setPointerCapture = () => {};
  Element.prototype.releasePointerCapture = () => {};
}
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

// ---------------------------------------------------------------------------
// Module mocks (hoisted by Vitest before imports)
// ---------------------------------------------------------------------------

const mockUseMeFn = vi.fn();
vi.mock("@/lib/auth/use-me", () => ({
  useMe: () => mockUseMeFn(),
}));

const mockUseDatahub = vi.fn();
const mockUseLangfuse = vi.fn();
const mockUpdateDatahub = vi.fn();
const mockUpdateLangfuse = vi.fn();
vi.mock("@/lib/api/admin", () => ({
  useDatahubPeripheral: () => mockUseDatahub(),
  useLangfusePeripheral: () => mockUseLangfuse(),
  useUpdateDatahubPeripheral: () => ({ mutateAsync: mockUpdateDatahub, isPending: false }),
  useUpdateLangfusePeripheral: () => ({ mutateAsync: mockUpdateLangfuse, isPending: false }),
}));

const mockToast = vi.fn();
vi.mock("@/components/ui/use-toast", () => ({
  toast: (...args: unknown[]) => mockToast(...args),
}));

vi.mock("@/lib/preferences/timezone", () => ({
  useDisplayTz: () => "UTC",
}));

// ApiError — mirror the real constructor signature (payload, status)
vi.mock("@/lib/api/client", () => {
  class ApiError extends Error {
    error_code: string;
    trace_id: string;
    status: number;
    constructor(
      payload: { error_code: string; message: string; trace_id: string },
      status: number,
    ) {
      super(payload.message);
      this.name = "ApiError";
      this.error_code = payload.error_code;
      this.trace_id = payload.trace_id;
      this.status = status;
    }
  }
  return { ApiError, apiFetch: vi.fn() };
});

// ---------------------------------------------------------------------------
// Import the page component + pure helpers AFTER mocks are registered
// ---------------------------------------------------------------------------
import AdminPeripheralsPage from "./page";
import {
  datahubSchema,
  datahubToFormDefaults,
  datahubBuildPatch,
  mechanismOptionsFor,
  nonMskBrokerHosts,
  deriveMskRegion,
  langfuseSchema,
  langfuseToFormDefaults,
  langfuseBuildPatch,
} from "./peripherals-form.schema";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function adminMe() {
  return {
    me: {
      id: "u1",
      email: "admin@example.com",
      name: "Admin",
      role: "Admin" as const,
      has_google: false,
      created_at: "",
      updated_at: "",
    },
    isAdmin: true,
    isEditor: false,
    canWrite: true,
    isLoading: false,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  mockUseMeFn.mockReturnValue(adminMe());
  mockUseDatahub.mockReturnValue({ data: makeDatahub(), isLoading: false });
  mockUseLangfuse.mockReturnValue({ data: makeLangfuse(), isLoading: false });
  mockUpdateDatahub.mockResolvedValue(makeDatahub({ updated_at: "2026-06-26T12:00:00Z" }));
  mockUpdateLangfuse.mockResolvedValue(makeLangfuse({ updated_at: "2026-06-26T12:00:00Z" }));
});

// ---------------------------------------------------------------------------
// 1. Page populates fields from GET (non-secret plain; secrets blank)
// ---------------------------------------------------------------------------
describe("AdminPeripheralsPage — populate from GET (FRONTEND_BASIC.md §Admin Peripherals)", () => {
  it("renders both card headings + Save buttons", async () => {
    render(<AdminPeripheralsPage />);
    expect(await screen.findByText("DataHub")).toBeTruthy();
    expect(screen.getByText("Langfuse")).toBeTruthy();
    expect(screen.getByRole("button", { name: /save datahub/i })).toBeTruthy();
    expect(screen.getByRole("button", { name: /save langfuse/i })).toBeTruthy();
  });

  it("prefills non-secret DataHub fields (service_corpuser_urn, default_env) from GET", async () => {
    render(<AdminPeripheralsPage />);
    await waitFor(() => {
      const urn = document.getElementById("datahub_service_corpuser_urn") as HTMLInputElement;
      expect(urn.value).toBe("urn:li:corpuser:dataspoke");
    });
    const env = document.getElementById("datahub_default_env") as HTMLInputElement;
    expect(env.value).toBe("DEV");
    const gms = document.getElementById("datahub_gms_url") as HTMLInputElement;
    expect(gms.value).toBe("http://datahub-gms:8080");
  });

  it("prefills non-secret Langfuse fields (project_id, environment_tag) from GET", async () => {
    render(<AdminPeripheralsPage />);
    await waitFor(() => {
      const proj = document.getElementById("langfuse_project_id") as HTMLInputElement;
      expect(proj.value).toBe("imazon-metadata");
    });
    const envTag = document.getElementById("langfuse_environment_tag") as HTMLInputElement;
    expect(envTag.value).toBe("production");
  });

  it("secret inputs (token, secret_key) ALWAYS render blank — never echo the masked indicator", async () => {
    // spec: peripherals-form.schema.ts toFormDefaults — secret blanked; never echo "********".
    render(<AdminPeripheralsPage />);
    await waitFor(() => {
      expect(document.getElementById("datahub_token")).toBeTruthy();
    });
    const token = document.getElementById("datahub_token") as HTMLInputElement;
    const secretKey = document.getElementById("langfuse_secret_key") as HTMLInputElement;
    expect(token.value).toBe("");
    expect(secretKey.value).toBe("");
  });

  it("the Kafka SASL password renders blank too — same masking rule as the token", async () => {
    mockUseDatahub.mockReturnValue({ data: makeScramDatahub(), isLoading: false });
    render(<AdminPeripheralsPage />);
    await waitFor(() => {
      expect(document.getElementById("datahub_kafka_sasl_password")).toBeTruthy();
    });
    const pw = document.getElementById("datahub_kafka_sasl_password") as HTMLInputElement;
    expect(pw.value).toBe("");
    // ...while the non-secret credential field IS prefilled.
    const user = document.getElementById("datahub_kafka_sasl_username") as HTMLInputElement;
    expect(user.value).toBe("dataspoke");
  });
});

// ---------------------------------------------------------------------------
// 2. Pure-helper validation + diff semantics (the real schema module)
// ---------------------------------------------------------------------------
describe("peripherals-form.schema — toFormDefaults blanks secrets, buildPatch diffs (FRONTEND_BASIC.md §Admin Peripherals)", () => {
  it("datahubToFormDefaults drops the masked token, keeps non-secret fields", () => {
    const defaults = datahubToFormDefaults(makeDatahub());
    expect(defaults.token).toBe("");
    expect(defaults.service_corpuser_urn).toBe("urn:li:corpuser:dataspoke");
    expect(defaults.default_env).toBe("DEV");
  });

  it("datahubToFormDefaults drops the masked kafka_sasl_password, keeps the Kafka settings", () => {
    const defaults = datahubToFormDefaults(makeScramDatahub());
    expect(defaults.kafka_sasl_password).toBe("");
    expect(defaults.kafka_security_protocol).toBe("SASL_SSL");
    expect(defaults.kafka_sasl_mechanism).toBe("SCRAM-SHA-512");
    expect(defaults.kafka_sasl_username).toBe("dataspoke");
    // API-owned bookkeeping is not part of the form at all.
    expect(defaults).not.toHaveProperty("kafka_sasl_password_version");
  });

  it("langfuseToFormDefaults drops the masked secret_key, keeps non-secret fields", () => {
    const defaults = langfuseToFormDefaults(makeLangfuse());
    expect(defaults.secret_key).toBe("");
    expect(defaults.project_id).toBe("imazon-metadata");
    expect(defaults.environment_tag).toBe("production");
  });

  it("datahubBuildPatch sends ONLY the changed non-secret field; omits blank token", () => {
    const loaded = makeDatahub();
    const values = { ...datahubToFormDefaults(loaded), default_env: "PROD" };
    const patch = datahubBuildPatch(values, loaded);
    expect(patch).toEqual({ default_env: "PROD" });
    // blank token (left untouched) must NOT appear → leave-current semantics.
    expect(patch).not.toHaveProperty("token");
    expect(patch).not.toHaveProperty("service_corpuser_urn");
  });

  it("datahubBuildPatch includes the token ONLY when the user typed a value", () => {
    const loaded = makeDatahub();
    const values = { ...datahubToFormDefaults(loaded), token: "new-pat-token" };
    const patch = datahubBuildPatch(values, loaded);
    expect(patch).toEqual({ token: "new-pat-token" });
  });

  it("langfuseBuildPatch sends ONLY changed non-secret fields; omits blank secret_key", () => {
    const loaded = makeLangfuse();
    const values = {
      ...langfuseToFormDefaults(loaded),
      environment_tag: "staging",
    };
    const patch = langfuseBuildPatch(values, loaded);
    expect(patch).toEqual({ environment_tag: "staging" });
    expect(patch).not.toHaveProperty("secret_key");
  });

  it("buildPatch returns {} when nothing changed (no-op save)", () => {
    const dh = makeDatahub();
    expect(datahubBuildPatch(datahubToFormDefaults(dh), dh)).toEqual({});
    const lf = makeLangfuse();
    expect(langfuseBuildPatch(langfuseToFormDefaults(lf), lf)).toEqual({});
  });

  it("datahubSchema accepts the form-default shape (all string fields)", () => {
    expect(datahubSchema.safeParse(datahubToFormDefaults(makeDatahub())).success).toBe(true);
    // ...and the secured shape, which carries the full Kafka tuple.
    expect(datahubSchema.safeParse(datahubToFormDefaults(makeScramDatahub())).success).toBe(true);
  });

  it("datahubBuildPatch includes kafka_sasl_password ONLY when the user typed a value", () => {
    const loaded = makeScramDatahub();
    const untouched = datahubToFormDefaults(loaded);
    expect(datahubBuildPatch(untouched, loaded)).toEqual({});

    const typed = { ...untouched, kafka_sasl_password: "rotated-secret" };
    expect(datahubBuildPatch(typed, loaded)).toEqual({ kafka_sasl_password: "rotated-secret" });
  });

  it("datahubBuildPatch NEVER sends kafka_sasl_password_version (API-owned bookkeeping)", () => {
    const loaded = makeScramDatahub();
    const values = {
      ...datahubToFormDefaults(loaded),
      kafka_sasl_username: "rotated-user",
      kafka_sasl_password: "rotated-secret",
    };
    const patch = datahubBuildPatch(values, loaded);
    expect(patch).not.toHaveProperty("kafka_sasl_password_version");
    expect(Object.keys(patch).sort()).toEqual(["kafka_sasl_password", "kafka_sasl_username"]);
  });

  it("datahubBuildPatch sends the cleared credential as '' when moving to AWS_MSK_IAM", () => {
    // The API rejects a username under AWS_MSK_IAM, so the move has to clear it
    // explicitly — omitting the field would leave the stored value in force.
    const loaded = makeScramDatahub();
    const values = {
      ...datahubToFormDefaults(loaded),
      kafka_sasl_mechanism: "AWS_MSK_IAM" as const,
      kafka_sasl_username: "",
    };
    expect(datahubBuildPatch(values, loaded)).toEqual({
      kafka_sasl_mechanism: "AWS_MSK_IAM",
      kafka_sasl_username: "",
    });
  });

  it("datahubBuildPatch sends frontend_url when the browser-facing URL changes", () => {
    const loaded = makeDatahub();
    const values = {
      ...datahubToFormDefaults(loaded),
      frontend_url: "https://datahub.corp.example.com",
    };
    // Only the changed key — gms_url is untouched even though both are URLs.
    expect(datahubBuildPatch(values, loaded)).toEqual({
      frontend_url: "https://datahub.corp.example.com",
    });
  });
});

// ---------------------------------------------------------------------------
// 2b. Client-side URL/slug validation mirrors the backend constraints, so an
//     operator gets an inline field error instead of a raw 422 toast.
//     Mirrors SAFE_DISPLAY_URL_PATTERN / SAFE_PROJECT_ID_PATTERN in
//     src/api/schemas/common.py.
// ---------------------------------------------------------------------------
describe("peripherals schemas — operator-supplied URL validation", () => {
  const HOSTILE = [
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "https://trusted.example.com@evil.com",
    "//protocol-relative.example.com",
    "datahub.example.com",
  ];

  it.each(HOSTILE)("datahubSchema rejects frontend_url %s", (url) => {
    const values = { ...datahubToFormDefaults(makeDatahub()), frontend_url: url };
    const parsed = datahubSchema.safeParse(values);
    expect(parsed.success).toBe(false);
    // The operator sees a usable message, not a bare "Invalid input".
    expect(parsed.error?.issues[0].message).toMatch(/http:\/\/ or https:\/\//);
  });

  it.each(HOSTILE)("langfuseSchema rejects host %s (same constraint as DataHub)", (url) => {
    const values = { ...langfuseToFormDefaults(makeLangfuse()), host: url };
    expect(langfuseSchema.safeParse(values).success).toBe(false);
  });

  it("both schemas accept a blank URL as 'unset'", () => {
    expect(
      datahubSchema.safeParse({ ...datahubToFormDefaults(makeDatahub()), frontend_url: "" })
        .success,
    ).toBe(true);
    expect(
      langfuseSchema.safeParse({ ...langfuseToFormDefaults(makeLangfuse()), host: "" }).success,
    ).toBe(true);
  });

  it.each(["../../etc/passwd", "proj/evil", "-leading-dash", "proj 1"])(
    "langfuseSchema rejects project_id %s",
    (id) => {
      const values = { ...langfuseToFormDefaults(makeLangfuse()), project_id: id };
      expect(langfuseSchema.safeParse(values).success).toBe(false);
    },
  );

  it("langfuseSchema accepts an ordinary host and project slug", () => {
    const values = {
      ...langfuseToFormDefaults(makeLangfuse()),
      host: "https://langfuse.example.com:3000",
      project_id: "dataspoke-project_1",
    };
    expect(langfuseSchema.safeParse(values).success).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 2c. Kafka security vocabulary + the five validation rules of
//     spec/API.md §DataHub Kafka security, mirrored client-side.
// ---------------------------------------------------------------------------
describe("datahubSchema — Kafka security rules (API.md §DataHub Kafka security)", () => {
  type KafkaOverrides = Partial<ReturnType<typeof datahubToFormDefaults>>;

  function kafkaValues(over: KafkaOverrides) {
    return { ...datahubToFormDefaults(makeDatahub()), ...over };
  }

  /** Parse an otherwise-valid AWS_MSK_IAM tuple with `over` applied. */
  function iamParse(over: KafkaOverrides) {
    return datahubSchema.safeParse(
      kafkaValues({
        kafka_security_protocol: "SASL_SSL",
        kafka_sasl_mechanism: "AWS_MSK_IAM",
        ...over,
      }),
    );
  }

  /**
   * Assert the parse failed on `field` and on nothing else, so the case cannot
   * pass for another rule's reason, and return the issue for message assertions.
   */
  function expectIssueAt(parsed: ReturnType<typeof datahubSchema.safeParse>, field: string) {
    expect(parsed.success).toBe(false);
    const issues = parsed.error?.issues ?? [];
    expect(issues.map((i) => i.path[0])).toEqual([field]);
    return issues[0];
  }

  it("mechanismOptionsFor offers AWS_MSK_IAM under SASL_SSL only", () => {
    expect(mechanismOptionsFor("PLAINTEXT")).toEqual([]);
    expect(mechanismOptionsFor("SSL")).toEqual([]);
    expect(mechanismOptionsFor("SASL_PLAINTEXT")).toEqual([
      "PLAIN",
      "SCRAM-SHA-256",
      "SCRAM-SHA-512",
    ]);
    expect(mechanismOptionsFor("SASL_SSL")).toContain("AWS_MSK_IAM");
  });

  it("rule 1 — a SASL protocol requires a mechanism, a non-SASL protocol rejects one", () => {
    const missing = datahubSchema.safeParse(
      kafkaValues({ kafka_security_protocol: "SASL_SSL", kafka_sasl_mechanism: "" }),
    );
    expect(missing.success).toBe(false);
    expect(missing.error?.issues[0].path).toEqual(["kafka_sasl_mechanism"]);

    const stray = datahubSchema.safeParse(
      kafkaValues({ kafka_security_protocol: "SSL", kafka_sasl_mechanism: "PLAIN" }),
    );
    expect(stray.success).toBe(false);
  });

  it("rule 2 — a credential mechanism requires a username", () => {
    const parsed = datahubSchema.safeParse(
      kafkaValues({
        kafka_security_protocol: "SASL_SSL",
        kafka_sasl_mechanism: "SCRAM-SHA-256",
        kafka_sasl_username: "",
      }),
    );
    expect(parsed.success).toBe(false);
    expect(parsed.error?.issues[0].path).toEqual(["kafka_sasl_username"]);
  });

  it("rule 3 — AWS_MSK_IAM rejects a username or a password", () => {
    // MSK brokers throughout, so each rejection is attributable to rule 3 alone.
    const iam = {
      kafka_security_protocol: "SASL_SSL" as const,
      kafka_sasl_mechanism: "AWS_MSK_IAM" as const,
      kafka_brokers: "b-1.mycluster.abc123.c2.kafka.us-east-1.amazonaws.com:9098",
    };

    const withUser = datahubSchema.safeParse(
      kafkaValues({ ...iam, kafka_sasl_username: "dataspoke" }),
    );
    expect(withUser.success).toBe(false);
    expect(withUser.error?.issues[0].path).toEqual(["kafka_sasl_username"]);

    const withPassword = datahubSchema.safeParse(
      kafkaValues({ ...iam, kafka_sasl_password: "typed" }),
    );
    expect(withPassword.success).toBe(false);
    expect(withPassword.error?.issues[0].path).toEqual(["kafka_sasl_password"]);

    // The same tuple with neither credential is valid.
    expect(datahubSchema.safeParse(kafkaValues(iam)).success).toBe(true);
  });

  it("rule 4 — AWS_MSK_IAM requires SASL_SSL, not SASL_PLAINTEXT", () => {
    const parsed = datahubSchema.safeParse(
      kafkaValues({
        kafka_security_protocol: "SASL_PLAINTEXT",
        kafka_sasl_mechanism: "AWS_MSK_IAM",
      }),
    );
    expect(parsed.success).toBe(false);
    expect(parsed.error?.issues.some((i) => i.path[0] === "kafka_security_protocol")).toBe(true);
  });

  it("rule 5 — an AWS region is accepted only with AWS_MSK_IAM", () => {
    expect(
      datahubSchema.safeParse(
        kafkaValues({
          kafka_security_protocol: "SASL_SSL",
          kafka_sasl_mechanism: "SCRAM-SHA-512",
          kafka_sasl_username: "dataspoke",
          kafka_aws_region: "ap-northeast-2",
        }),
      ).success,
    ).toBe(false);

    expect(
      datahubSchema.safeParse(
        kafkaValues({
          kafka_security_protocol: "SASL_SSL",
          kafka_sasl_mechanism: "AWS_MSK_IAM",
          kafka_aws_region: "ap-northeast-2",
          // Rule 6 applies to every AWS_MSK_IAM tuple, so a valid one needs MSK brokers.
          kafka_brokers: "b-1.mycluster.abc123.c2.kafka.ap-northeast-2.amazonaws.com:9098",
        }),
      ).success,
    ).toBe(true);
  });

  it("rule 6 — AWS_MSK_IAM accepts only MSK broker hosts", () => {
    expect(
      iamParse({
        kafka_brokers:
          "b-1.mycluster.abc123.c2.kafka.us-east-1.amazonaws.com:9098," +
          "b-2.mycluster.abc123.c2.kafka.us-east-1.amazonaws.com:9098",
      }).success,
    ).toBe(true);

    // Serverless brokers carry the same guarantee under a different label.
    expect(
      iamParse({
        kafka_brokers: "boot-abc123.c2.kafka-serverless.us-east-1.amazonaws.com:9098",
      }).success,
    ).toBe(true);

    expectIssueAt(iamParse({ kafka_brokers: "kafka:9092" }), "kafka_brokers");
  });

  it("rule 6 — an AWS host that is NOT an MSK broker is rejected", () => {
    // The suffix alone is insufficient: an EC2 host is routinely under a tenant's
    // own control, with a publicly-trusted cert obtainable for it, so accepting
    // it would leave the escalation intact one step removed. Both shapes satisfy
    // a bare *.amazonaws.com check.
    expectIssueAt(
      iamParse({ kafka_brokers: "ec2-203-0-113-25.compute-1.amazonaws.com:9098" }),
      "kafka_brokers",
    );
    expectIssueAt(iamParse({ kafka_brokers: "my-bucket.s3.amazonaws.com:9098" }), "kafka_brokers");
  });

  it("rule 6 — an *.amazonaws.com SUFFIX is not enough; the host must end there", () => {
    // This exact shape defeated a suffix-only check: the pod's IAM identity would
    // sign a token addressed to a host the attacker controls, ready to replay.
    expectIssueAt(
      iamParse({ kafka_brokers: "b-1.mycluster.kafka.us-east-1.amazonaws.com.evil.tld:9098" }),
      "kafka_brokers",
    );
  });

  it("rule 6 — one bad host among good ones still rejects, and is named in the message", () => {
    const parsed = iamParse({
      kafka_brokers:
        "b-1.mycluster.abc123.c2.kafka.us-east-1.amazonaws.com:9098, attacker.example.com:9098",
    });
    const issue = expectIssueAt(parsed, "kafka_brokers");
    expect(issue.message).toMatch(/attacker\.example\.com:9098/);
  });

  it("rule 6 — an empty broker list is rejected under AWS_MSK_IAM", () => {
    expectIssueAt(iamParse({ kafka_brokers: "   " }), "kafka_brokers");
  });

  it("rule 6 constrains AWS_MSK_IAM only — a SCRAM setup keeps any broker host", () => {
    expect(
      datahubSchema.safeParse(
        kafkaValues({
          kafka_security_protocol: "SASL_SSL",
          kafka_sasl_mechanism: "SCRAM-SHA-512",
          kafka_sasl_username: "dataspoke",
          kafka_brokers: "kafka.internal:9093",
        }),
      ).success,
    ).toBe(true);
  });

  it("rule 7 — an explicit region contradicting the brokers is rejected, on the region field", () => {
    const parsed = iamParse({
      kafka_brokers: "b-1.mycluster.abc123.c2.kafka.us-east-1.amazonaws.com:9098",
      kafka_aws_region: "ap-northeast-2",
    });
    const issue = expectIssueAt(parsed, "kafka_aws_region");
    expect(issue.message).toMatch(/us-east-1/);

    // The agreeing region is accepted.
    expect(
      iamParse({
        kafka_brokers: "b-1.mycluster.abc123.c2.kafka.us-east-1.amazonaws.com:9098",
        kafka_aws_region: "us-east-1",
      }).success,
    ).toBe(true);
  });

  it("rule 7 — a mixed-region broker list is rejected, on the brokers field", () => {
    // Underivable rather than resolved to one of them: signing for whichever
    // sorted first would silently pick an endpoint the operator did not choose.
    expectIssueAt(
      iamParse({
        kafka_brokers:
          "b-1.mycluster.abc123.c2.kafka.us-east-1.amazonaws.com:9098," +
          "b-2.mycluster.abc123.c2.kafka.eu-west-1.amazonaws.com:9098",
      }),
      "kafka_brokers",
    );
  });

  it("nonMskBrokerHosts anchors the host, tolerating whitespace and ports", () => {
    expect(nonMskBrokerHosts("b-1.kafka.us-east-1.amazonaws.com:9098")).toEqual([]);
    expect(
      nonMskBrokerHosts(" b-1.kafka.us-east-1.amazonaws.com , b-2.kafka.us-east-1.amazonaws.com "),
    ).toEqual([]);
    expect(nonMskBrokerHosts("evil-amazonaws.com")).toEqual(["evil-amazonaws.com"]);
    expect(nonMskBrokerHosts("b-1.amazonaws.com.evil.tld")).toEqual(["b-1.amazonaws.com.evil.tld"]);
    // AWS-hosted, but not a broker.
    expect(nonMskBrokerHosts("ec2-203-0-113-25.compute-1.amazonaws.com:9098")).toEqual([
      "ec2-203-0-113-25.compute-1.amazonaws.com:9098",
    ]);
  });

  it("deriveMskRegion reads the region only from a well-formed, agreeing list", () => {
    expect(deriveMskRegion("b-1.mycluster.abc.c2.kafka.us-east-1.amazonaws.com:9098")).toBe(
      "us-east-1",
    );
    expect(deriveMskRegion("boot-abc.c2.kafka-serverless.ap-northeast-2.amazonaws.com")).toBe(
      "ap-northeast-2",
    );
    // Disagreeing regions, a non-broker host, and the suffix attack are all
    // underivable rather than resolved to a plausible-looking region.
    expect(
      deriveMskRegion(
        "b-1.c.x.c2.kafka.us-east-1.amazonaws.com,b-2.c.x.c2.kafka.eu-west-1.amazonaws.com",
      ),
    ).toBeNull();
    expect(deriveMskRegion("kafka:9092")).toBeNull();
    expect(deriveMskRegion("b-1.c.x.c2.kafka.us-east-1.amazonaws.com.evil.tld")).toBeNull();
  });

  it("rejects a malformed AWS region slug", () => {
    const parsed = datahubSchema.safeParse(
      kafkaValues({
        kafka_security_protocol: "SASL_SSL",
        kafka_sasl_mechanism: "AWS_MSK_IAM",
        kafka_aws_region: "AP North East",
        kafka_brokers: "b-1.mycluster.abc123.c2.kafka.us-east-1.amazonaws.com:9098",
      }),
    );
    expect(parsed.success).toBe(false);
    expect(parsed.error?.issues.some((i) => i.path[0] === "kafka_aws_region")).toBe(true);
  });

  it("the PLAINTEXT default is valid with the whole Kafka tuple empty", () => {
    expect(datahubSchema.safeParse(datahubToFormDefaults(makeDatahub())).success).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// 2d. Progressive disclosure of the Kafka fields (FRONTEND_BASIC.md §Peripherals)
// ---------------------------------------------------------------------------
describe("DatahubCard — Kafka progressive disclosure (FRONTEND_BASIC.md §Peripherals)", () => {
  const AWS_NOTE = /pod IAM role/i;

  async function openSelect(user: ReturnType<typeof userEvent.setup>, id: string) {
    await user.click(document.getElementById(id)!);
    return await screen.findAllByRole("option");
  }

  it("PLAINTEXT shows nothing beyond the brokers — no mechanism, credentials, region, or note", async () => {
    render(<AdminPeripheralsPage />);
    await waitFor(() => {
      expect(document.getElementById("datahub_kafka_security_protocol")).toBeTruthy();
    });
    expect(document.getElementById("datahub_kafka_sasl_mechanism")).toBeNull();
    expect(document.getElementById("datahub_kafka_sasl_username")).toBeNull();
    expect(document.getElementById("datahub_kafka_sasl_password")).toBeNull();
    expect(document.getElementById("datahub_kafka_aws_region")).toBeNull();
    expect(screen.queryByText(AWS_NOTE)).toBeNull();
  });

  it("a SCRAM peripheral shows the mechanism + credential fields, but no region or note", async () => {
    mockUseDatahub.mockReturnValue({ data: makeScramDatahub(), isLoading: false });
    render(<AdminPeripheralsPage />);
    await waitFor(() => {
      expect(document.getElementById("datahub_kafka_sasl_mechanism")).toBeTruthy();
    });
    expect(document.getElementById("datahub_kafka_sasl_username")).toBeTruthy();
    expect(document.getElementById("datahub_kafka_sasl_password")).toBeTruthy();
    expect(document.getElementById("datahub_kafka_aws_region")).toBeNull();
    expect(screen.queryByText(AWS_NOTE)).toBeNull();
  });

  it("AWS_MSK_IAM shows the region + the IRSA note, and NO credential inputs", async () => {
    mockUseDatahub.mockReturnValue({
      data: makeDatahub({
        kafka_security_protocol: "SASL_SSL",
        kafka_sasl_mechanism: "AWS_MSK_IAM",
        kafka_aws_region: "ap-northeast-2",
      }),
      isLoading: false,
    });
    render(<AdminPeripheralsPage />);
    await waitFor(() => {
      expect(document.getElementById("datahub_kafka_aws_region")).toBeTruthy();
    });
    expect(document.getElementById("datahub_kafka_sasl_username")).toBeNull();
    expect(document.getElementById("datahub_kafka_sasl_password")).toBeNull();
    // The form alone is not sufficient — the note says so explicitly.
    const note = document.getElementById("datahub_kafka_aws_msk_iam_note");
    expect(note?.textContent).toMatch(AWS_NOTE);
    expect(note?.textContent).toMatch(/event-consumer\.serviceAccount/);
  });

  it("SASL_PLAINTEXT does NOT offer AWS_MSK_IAM — the API rejects that pairing", async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    render(<AdminPeripheralsPage />);
    await waitFor(() => {
      expect(document.getElementById("datahub_kafka_security_protocol")).toBeTruthy();
    });

    const protocols = await openSelect(user, "datahub_kafka_security_protocol");
    await user.click(protocols.find((o) => o.textContent === "SASL_PLAINTEXT")!);

    const mechanisms = await openSelect(user, "datahub_kafka_sasl_mechanism");
    const labels = mechanisms.map((o) => o.textContent);
    expect(labels).toEqual(["PLAIN", "SCRAM-SHA-256", "SCRAM-SHA-512"]);
    expect(labels).not.toContain("AWS_MSK_IAM");
  });

  it("switching away from a credential mechanism does not carry the typed password back", async () => {
    // Guards against React reusing a conditionally-rendered input's node across a
    // mechanism switch, which would smuggle a credential into a field the API rejects.
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    mockUseDatahub.mockReturnValue({ data: makeScramDatahub(), isLoading: false });
    render(<AdminPeripheralsPage />);

    await waitFor(() => {
      expect(document.getElementById("datahub_kafka_sasl_password")).toBeTruthy();
    });
    await user.type(document.getElementById("datahub_kafka_sasl_password")!, "typed-secret");

    const toIam = await openSelect(user, "datahub_kafka_sasl_mechanism");
    await user.click(toIam.find((o) => o.textContent === "AWS_MSK_IAM")!);
    await waitFor(() => {
      expect(document.getElementById("datahub_kafka_sasl_password")).toBeNull();
    });

    const back = await openSelect(user, "datahub_kafka_sasl_mechanism");
    await user.click(back.find((o) => o.textContent === "SCRAM-SHA-256")!);
    await waitFor(() => {
      expect(document.getElementById("datahub_kafka_sasl_password")).toBeTruthy();
    });
    const pw = document.getElementById("datahub_kafka_sasl_password") as HTMLInputElement;
    expect(pw.value).toBe("");
    const username = document.getElementById("datahub_kafka_sasl_username") as HTMLInputElement;
    expect(username.value).toBe("");
  });

  it("never renders the masked indicator for a stored Kafka password", async () => {
    mockUseDatahub.mockReturnValue({ data: makeScramDatahub(), isLoading: false });
    render(<AdminPeripheralsPage />);
    await waitFor(() => {
      expect(document.getElementById("datahub_kafka_sasl_password")).toBeTruthy();
    });
    expect(document.body.textContent).not.toContain("********");
  });

  it("switching to AWS_MSK_IAM saves a cleared credential and shows no password afterwards", async () => {
    // The API clears the stored password once the effective mechanism is
    // AWS_MSK_IAM, so the card must not keep implying one is in force.
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const loaded = makeScramDatahub();
    mockUseDatahub.mockReturnValue({ data: loaded, isLoading: false });
    mockUpdateDatahub.mockResolvedValue(
      makeDatahub({
        kafka_security_protocol: "SASL_SSL",
        kafka_sasl_mechanism: "AWS_MSK_IAM",
        kafka_sasl_username: "",
        kafka_sasl_password: "",
        kafka_sasl_password_version: 4,
        kafka_brokers: "b-1.mycluster.abc123.c2.kafka.us-east-1.amazonaws.com:9098",
        updated_at: "2026-06-26T12:00:00Z",
      }),
    );
    render(<AdminPeripheralsPage />);

    const brokers = (await waitFor(
      () => document.getElementById("datahub_kafka_brokers")!,
    )) as HTMLInputElement;
    await user.clear(brokers);
    await user.type(brokers, "b-1.mycluster.abc123.c2.kafka.us-east-1.amazonaws.com:9098");

    await user.click(document.getElementById("datahub_kafka_sasl_mechanism")!);
    const options = await screen.findAllByRole("option");
    await user.click(options.find((o) => o.textContent === "AWS_MSK_IAM")!);

    await user.click(screen.getByRole("button", { name: /save datahub/i }));

    await waitFor(() => {
      expect(mockUpdateDatahub).toHaveBeenCalled();
    });
    const patch = mockUpdateDatahub.mock.calls[0][0] as Record<string, unknown>;
    // The username is cleared explicitly — omitting it would leave it in force.
    expect(patch).toHaveProperty("kafka_sasl_username", "");
    expect(patch).toHaveProperty("kafka_sasl_mechanism", "AWS_MSK_IAM");
    // A blank password is still omitted; the API owns the clearing.
    expect(patch).not.toHaveProperty("kafka_sasl_password");
    expect(patch).not.toHaveProperty("kafka_sasl_password_version");

    await waitFor(() => {
      expect(document.getElementById("datahub_kafka_sasl_password")).toBeNull();
    });
    expect(document.body.textContent).not.toContain("********");
  });
});

// ---------------------------------------------------------------------------
// 2e. Read-only consumer health (FRONTEND_BASIC.md §Peripherals)
// ---------------------------------------------------------------------------
describe("DatahubCard — consumer health badge (FRONTEND_BASIC.md §Peripherals)", () => {
  it("renders 'unknown' as a neutral badge — the normal state with no consumer deployed", async () => {
    render(<AdminPeripheralsPage />);
    const badge = await waitFor(() => {
      const el = document.getElementById("datahub_health_status");
      expect(el).toBeTruthy();
      return el!;
    });
    expect(badge.dataset.status).toBe("unknown");
    expect(badge.textContent).toMatch(/unknown/i);
  });

  it("renders 'ok' with the last-OK timestamp", async () => {
    mockUseDatahub.mockReturnValue({
      data: makeDatahub({
        health: {
          status: "ok",
          last_error: null,
          last_ok_at: "2026-06-26T14:30:00Z",
          updated_at: "2026-06-26T14:30:00Z",
        },
      }),
      isLoading: false,
    });
    render(<AdminPeripheralsPage />);
    const badge = await waitFor(() => document.getElementById("datahub_health_status")!);
    expect(badge.dataset.status).toBe("ok");
    expect(screen.getByText(/Last OK/)).toBeTruthy();
  });

  it("renders 'error' with the last error visible — a SASL failure must not stay in pod logs", async () => {
    mockUseDatahub.mockReturnValue({
      data: makeScramDatahub({
        health: {
          status: "error",
          last_error: "SASL authentication failed: Invalid username or password",
          last_ok_at: null,
          updated_at: "2026-06-26T14:35:00Z",
        },
      }),
      isLoading: false,
    });
    render(<AdminPeripheralsPage />);
    const badge = await waitFor(() => document.getElementById("datahub_health_status")!);
    expect(badge.dataset.status).toBe("error");
    expect(document.getElementById("datahub_health_error")?.textContent).toMatch(
      /SASL authentication failed/,
    );
  });
});

// ---------------------------------------------------------------------------
// 3. Partial PATCH save round-trip (DataHub card)
// ---------------------------------------------------------------------------
describe("AdminPeripheralsPage — DataHub partial PATCH save (FRONTEND_BASIC.md §Admin Peripherals)", () => {
  it("editing default_env → Save DataHub → PATCHes ONLY default_env, omits token", async () => {
    const user = userEvent.setup();
    render(<AdminPeripheralsPage />);

    const envInput = (await waitFor(() => {
      const el = document.getElementById("datahub_default_env") as HTMLInputElement | null;
      expect(el?.value).toBe("DEV");
      return el!;
    })) as HTMLInputElement;

    await user.clear(envInput);
    await user.type(envInput, "PROD");

    await user.click(screen.getByRole("button", { name: /save datahub/i }));

    await waitFor(() => {
      expect(mockUpdateDatahub).toHaveBeenCalled();
    });
    const patchBody = mockUpdateDatahub.mock.calls[0][0] as Record<string, unknown>;
    expect(patchBody).toEqual({ default_env: "PROD" });
    // blank token must not be sent (leave-current); langfuse hook untouched.
    expect(patchBody).not.toHaveProperty("token");
    expect(mockUpdateLangfuse).not.toHaveBeenCalled();
  });

  it("typed token IS included in the DataHub PATCH alongside a changed field", async () => {
    const user = userEvent.setup();
    render(<AdminPeripheralsPage />);

    await waitFor(() => {
      expect(document.getElementById("datahub_token")).toBeTruthy();
    });
    const tokenInput = document.getElementById("datahub_token") as HTMLInputElement;
    await user.type(tokenInput, "fresh-pat");

    await user.click(screen.getByRole("button", { name: /save datahub/i }));

    await waitFor(() => {
      expect(mockUpdateDatahub).toHaveBeenCalled();
    });
    const patchBody = mockUpdateDatahub.mock.calls[0][0] as Record<string, unknown>;
    expect(patchBody).toHaveProperty("token", "fresh-pat");
  });

  it("Save with no changes does NOT call the mutation; shows a no-changes toast", async () => {
    const user = userEvent.setup();
    render(<AdminPeripheralsPage />);

    await waitFor(() => {
      expect(document.getElementById("datahub_default_env")).toBeTruthy();
    });
    await user.click(screen.getByRole("button", { name: /save datahub/i }));

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalled();
    });
    expect(mockUpdateDatahub).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// 4. Partial PATCH save round-trip (Langfuse card) + success toast
// ---------------------------------------------------------------------------
describe("AdminPeripheralsPage — Langfuse partial PATCH save (FRONTEND_BASIC.md §Admin Peripherals)", () => {
  it("editing environment_tag → Save Langfuse → PATCHes ONLY environment_tag; success toast", async () => {
    const user = userEvent.setup();
    render(<AdminPeripheralsPage />);

    const tagInput = (await waitFor(() => {
      const el = document.getElementById("langfuse_environment_tag") as HTMLInputElement | null;
      expect(el?.value).toBe("production");
      return el!;
    })) as HTMLInputElement;

    await user.clear(tagInput);
    await user.type(tagInput, "staging");

    await user.click(screen.getByRole("button", { name: /save langfuse/i }));

    await waitFor(() => {
      expect(mockUpdateLangfuse).toHaveBeenCalled();
    });
    const patchBody = mockUpdateLangfuse.mock.calls[0][0] as Record<string, unknown>;
    expect(patchBody).toEqual({ environment_tag: "staging" });
    expect(patchBody).not.toHaveProperty("secret_key");
    expect(mockUpdateDatahub).not.toHaveBeenCalled();

    // success toast fired
    await waitFor(() => {
      const titles = mockToast.mock.calls.map((c) => (c[0] as { title?: string })?.title);
      expect(titles.some((t) => /saved/i.test(t ?? ""))).toBe(true);
    });
  });
});

// ---------------------------------------------------------------------------
// 5. Admin gate
// ---------------------------------------------------------------------------
describe("AdminPeripheralsPage — admin gate (FRONTEND_BASIC.md §Routing)", () => {
  it("non-admin sees a permission message, not the cards", async () => {
    mockUseMeFn.mockReturnValue({ ...adminMe(), isAdmin: false });
    render(<AdminPeripheralsPage />);
    expect(await screen.findByText(/do not have permission/i)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /save datahub/i })).toBeNull();
  });
});
