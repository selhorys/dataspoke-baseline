/**
 * Ground spec: /admin/peripherals page — narrow UI-flow tests.
 *
 * Concern: the DataHub + Langfuse peripheral panels render, are populated from
 * GET /admin/peripherals/{datahub,langfuse} (assert non-secret fields show their
 * current values and secret inputs render blank), and support a per-card save
 * round-trip on ONE benign non-secret field (DataHub `default_env`). The field is
 * edited → Save DataHub → persisted value confirmed via adminApi GET → reverted.
 *
 * Also covered: the DataHub Kafka security sub-form's progressive disclosure and
 * option scoping, and the read-only consumer-health badge.
 *
 * CRITICAL: do NOT touch the secret inputs (token, secret_key, kafka_sasl_password) —
 * they route to the K8s Secret and clearing them would break the dev cluster's
 * DataHub/Langfuse connectivity. The non-secret field edited here is reverted before the
 * test ends. The Kafka tests below are deliberately SAVE-FREE: every gesture is
 * form-local (a Select change re-renders the sub-form without issuing a PATCH), so the
 * live consumer configuration is never written and there is nothing to revert. A page
 * reload discards the unsaved state.
 *
 * spec: spec/feature/FRONTEND_BASIC.md §Peripherals (/admin/peripherals)
 * spec: spec/API.md §/admin/peripherals/datahub — GET returns non-secret
 *   service_corpuser_urn/default_env plain; PATCH (partial, changed fields only)
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — dual confirmation, selector guidance
 */

import { test, expect } from "../../fixtures/index";

// ── Types ─────────────────────────────────────────────────────────────────────

interface PeripheralHealth {
  status: "unknown" | "ok" | "error";
  last_error: string | null;
  last_ok_at: string | null;
  updated_at: string | null;
}

interface DatahubPeripheral {
  gms_url: string;
  kafka_brokers: string;
  kafka_security_protocol: string;
  kafka_sasl_mechanism: string;
  kafka_sasl_username: string;
  kafka_sasl_password: string;
  kafka_sasl_password_version: number;
  kafka_aws_region: string;
  token: string;
  service_corpuser_urn: string;
  default_env: string;
  is_configured: boolean;
  health: PeripheralHealth;
  updated_at: string | null;
}

interface LangfusePeripheral {
  host: string;
  public_key: string;
  secret_key: string;
  project_id: string;
  environment_tag: string;
  is_configured: boolean;
  updated_at: string | null;
}

// ── Module state ──────────────────────────────────────────────────────────────

/** Original DataHub default_env; used in afterAll for revert. */
let originalDefaultEnv: string | null = null;

// ── Cleanup ───────────────────────────────────────────────────────────────────

test.afterAll(async ({ adminApi }) => {
  // Revert DataHub default_env to its original value if we changed it.
  if (originalDefaultEnv !== null) {
    await adminApi.patch("/api/v1/admin/peripherals/datahub", {
      data: { default_env: originalDefaultEnv },
    });
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 1 — /admin/peripherals renders both cards populated from GET
// spec: FRONTEND_BASIC.md §Peripherals — DataHub + Langfuse cards; non-secret
//   fields prefilled from GET; secret inputs render blank.
// ─────────────────────────────────────────────────────────────────────────────

test("/admin/peripherals — cards render with values from GET; secrets blank", async ({
  page,
  adminApi,
}) => {
  // Backend probe: read current peripheral config to know expected field values.
  // spec: TESTING.md §E2E §Execution discipline — "Gate data-dependent UI assertions on
  //   confirmed backend state".
  const dhResp = await adminApi.get("/api/v1/admin/peripherals/datahub");
  expect(dhResp.status()).toBe(200);
  const dh = (await dhResp.json()) as DatahubPeripheral;

  const lfResp = await adminApi.get("/api/v1/admin/peripherals/langfuse");
  expect(lfResp.status()).toBe(200);
  const lf = (await lfResp.json()) as LangfusePeripheral;

  // Navigate to the page.
  await page.goto("/admin/peripherals");
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: page heading --
  // spec: admin/peripherals/page.tsx — h1 "Admin — Peripherals"
  await expect(
    page.getByRole("heading", { name: "Admin — Peripherals", exact: true }),
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: both card titles --
  // spec: admin/peripherals/page.tsx — CardTitle "DataHub" / "Langfuse"
  await expect(page.getByText("DataHub", { exact: true })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("Langfuse", { exact: true })).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: non-secret DataHub fields prefilled from GET --
  // spec: admin/peripherals/page.tsx — Input id="datahub_gms_url" / "datahub_default_env"
  const gmsInput = page.locator("#datahub_gms_url");
  await expect(gmsInput).toBeVisible({ timeout: 10_000 });
  expect(await gmsInput.inputValue(), "gms_url must be populated from GET").toBe(dh.gms_url);

  const defaultEnvInput = page.locator("#datahub_default_env");
  await expect(defaultEnvInput).toHaveValue(dh.default_env, { timeout: 10_000 });

  const corpuserInput = page.locator("#datahub_service_corpuser_urn");
  await expect(corpuserInput).toHaveValue(dh.service_corpuser_urn, { timeout: 10_000 });

  // -- UI assertion: secret input renders BLANK (never echoes "********") --
  // spec: peripherals-form.schema.ts — toFormDefaults blanks the secret.
  await expect(page.locator("#datahub_token")).toHaveValue("");
  await expect(page.locator("#langfuse_secret_key")).toHaveValue("");

  // -- UI assertion: non-secret Langfuse fields prefilled from GET --
  await expect(page.locator("#langfuse_project_id")).toHaveValue(lf.project_id, {
    timeout: 10_000,
  });
  await expect(page.locator("#langfuse_environment_tag")).toHaveValue(lf.environment_tag, {
    timeout: 10_000,
  });

  // -- UI assertion: per-card Save buttons visible --
  // spec: admin/peripherals/page.tsx — Button "Save DataHub" / "Save Langfuse"
  await expect(page.getByRole("button", { name: "Save DataHub" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Save Langfuse" })).toBeVisible();
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 2 — Edit DataHub default_env → Save → confirm persisted → revert
// spec: FRONTEND_BASIC.md §Peripherals — per-card partial PATCH; save shows
//   "Saved · updated <timestamp>" + a success toast; reverted afterward.
// CRITICAL: the secret inputs (token, secret_key) are NOT touched.
// ─────────────────────────────────────────────────────────────────────────────

test("/admin/peripherals — edit DataHub default_env → Save → persisted → reverted", async ({
  page,
  adminApi,
}) => {
  // Pre-flight: read current value so we can revert exactly.
  const preResp = await adminApi.get("/api/v1/admin/peripherals/datahub");
  expect(preResp.status()).toBe(200);
  const pre = (await preResp.json()) as DatahubPeripheral;
  originalDefaultEnv = pre.default_env;

  // Choose a fabric value different from the current one (valid DataHub FabricType).
  // spec: API.md §/admin/peripherals — default_env is the fabric/env (PROD/DEV/QA/TEST…).
  const NEW_VALUE = originalDefaultEnv === "QA" ? "TEST" : "QA";

  // Navigate to the page.
  await page.goto("/admin/peripherals");
  await expect(page).not.toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: "Admin — Peripherals", exact: true }),
  ).toBeVisible({ timeout: 15_000 });

  // Wait until the field reflects the real value (useEffect fires after GET settles).
  const defaultEnvInput = page.locator("#datahub_default_env");
  await expect(defaultEnvInput).toHaveValue(originalDefaultEnv, { timeout: 15_000 });

  // -- UI gesture: replace the value --
  await defaultEnvInput.fill(NEW_VALUE);

  // -- UI gesture: click "Save DataHub" --
  // spec: admin/peripherals/page.tsx — Button type="submit" form=DATAHUB_FORM_ID
  await page.getByRole("button", { name: "Save DataHub" }).click();

  // -- UI assertion: success toast --
  // spec: admin/peripherals/page.tsx — toast({ title: "DataHub configuration saved." })
  await expect(
    page.getByText("DataHub configuration saved.", { exact: true }).first(),
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: "Saved · updated ..." text appears (savedAt rendered) --
  // spec: admin/peripherals/page.tsx — p "Saved · updated {formatDateTime(savedAt)}"
  await expect(page.getByText(/Saved · updated/)).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: the field now shows the new value --
  await expect(defaultEnvInput).toHaveValue(NEW_VALUE, { timeout: 5_000 });

  // -- Backend probe (dual confirmation): GET → new value persisted, secret intact --
  // spec: FRONTEND_BASIC.md §Peripherals — PATCH writes the value; GET reflects it.
  const afterResp = await adminApi.get("/api/v1/admin/peripherals/datahub");
  expect(afterResp.status()).toBe(200);
  const after = (await afterResp.json()) as DatahubPeripheral;
  expect(after.default_env).toBe(NEW_VALUE);

  // CRITICAL: the DataHub token must remain configured (secret untouched).
  // spec: API.md §/admin/peripherals/datahub — is_configured reflects the K8s Secret.
  expect(after.is_configured).toBe(pre.is_configured);
  expect(after.gms_url).toBe(pre.gms_url);

  // Revert the field via adminApi (mirrors afterAll, but ensures revert immediately).
  const revertResp = await adminApi.patch("/api/v1/admin/peripherals/datahub", {
    data: { default_env: originalDefaultEnv },
  });
  expect(revertResp.status()).toBe(200);
  const reverted = (await revertResp.json()) as DatahubPeripheral;
  expect(reverted.default_env).toBe(originalDefaultEnv);
  // Clear the afterAll guard since we've already reverted.
  originalDefaultEnv = null;
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 3 — Kafka security sub-form: progressive disclosure by protocol
// spec: FRONTEND_BASIC.md §Peripherals — the DataHub card's Kafka security fields
// spec: API.md §DataHub Kafka security rule 1 — a mechanism is required with
//   SASL_PLAINTEXT / SASL_SSL and rejected with PLAINTEXT / SSL
// SAVE-FREE: only Select gestures; no PATCH is issued.
// ─────────────────────────────────────────────────────────────────────────────

test("/admin/peripherals — Kafka fields appear only for the protocols that accept them", async ({
  page,
}) => {
  await page.goto("/admin/peripherals");
  await expect(page).not.toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: "Admin — Peripherals", exact: true }),
  ).toBeVisible({ timeout: 15_000 });

  const protocolSelect = page.locator("#datahub_kafka_security_protocol");
  await expect(protocolSelect).toBeVisible({ timeout: 15_000 });

  // -- PLAINTEXT: no SASL surface at all.
  // spec: API.md §DataHub Kafka security — "PLAINTEXT (default)"; rule 1 rejects a
  //   mechanism under it, so offering one would invite a 422.
  await protocolSelect.click();
  await page.getByRole("option", { name: "PLAINTEXT", exact: true }).click();
  await expect(page.locator("#datahub_kafka_sasl_mechanism")).toHaveCount(0);
  await expect(page.locator("#datahub_kafka_sasl_username")).toHaveCount(0);
  await expect(page.locator("#datahub_kafka_sasl_password")).toHaveCount(0);
  await expect(page.locator("#datahub_kafka_aws_region")).toHaveCount(0);

  // -- SSL: transport security, still no SASL surface.
  await protocolSelect.click();
  await page.getByRole("option", { name: "SSL", exact: true }).click();
  await expect(page.locator("#datahub_kafka_sasl_mechanism")).toHaveCount(0);
  await expect(page.locator("#datahub_kafka_sasl_username")).toHaveCount(0);

  // -- SASL_SSL: the mechanism select appears.
  // spec: API.md §DataHub Kafka security rule 1 — "kafka_sasl_mechanism is required
  //   when kafka_security_protocol is SASL_PLAINTEXT or SASL_SSL".
  await protocolSelect.click();
  await page.getByRole("option", { name: "SASL_SSL", exact: true }).click();
  await expect(page.locator("#datahub_kafka_sasl_mechanism")).toBeVisible({ timeout: 10_000 });

  // -- Back to PLAINTEXT: the sub-form collapses again (the disclosure is reversible).
  await protocolSelect.click();
  await page.getByRole("option", { name: "PLAINTEXT", exact: true }).click();
  await expect(page.locator("#datahub_kafka_sasl_mechanism")).toHaveCount(0);
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 4 — AWS_MSK_IAM is offered only under SASL_SSL (option scoping)
// spec: API.md §DataHub Kafka security rule 4 — "kafka_sasl_mechanism = AWS_MSK_IAM
//   requires kafka_security_protocol = SASL_SSL; any other protocol is rejected"
// SAVE-FREE.
// ─────────────────────────────────────────────────────────────────────────────

test("/admin/peripherals — AWS_MSK_IAM is offered under SASL_SSL only", async ({ page }) => {
  await page.goto("/admin/peripherals");
  await expect(
    page.getByRole("heading", { name: "Admin — Peripherals", exact: true }),
  ).toBeVisible({ timeout: 15_000 });

  const protocolSelect = page.locator("#datahub_kafka_security_protocol");
  await expect(protocolSelect).toBeVisible({ timeout: 15_000 });

  // -- SASL_PLAINTEXT: only the three credential mechanisms are offered.
  // Rule 4 rejects AWS_MSK_IAM here, and the form declines to offer a choice the API
  // would refuse rather than letting the operator discover it through a 422.
  await protocolSelect.click();
  await page.getByRole("option", { name: "SASL_PLAINTEXT", exact: true }).click();
  const mechanismSelect = page.locator("#datahub_kafka_sasl_mechanism");
  await expect(mechanismSelect).toBeVisible({ timeout: 10_000 });

  await mechanismSelect.click();
  await expect(page.getByRole("option", { name: "PLAIN", exact: true })).toBeVisible();
  await expect(page.getByRole("option", { name: "SCRAM-SHA-256", exact: true })).toBeVisible();
  await expect(page.getByRole("option", { name: "SCRAM-SHA-512", exact: true })).toBeVisible();
  await expect(page.getByRole("option", { name: "AWS_MSK_IAM", exact: true })).toHaveCount(0);
  await page.keyboard.press("Escape");

  // -- SASL_SSL: AWS_MSK_IAM joins the list.
  await protocolSelect.click();
  await page.getByRole("option", { name: "SASL_SSL", exact: true }).click();
  await expect(mechanismSelect).toBeVisible({ timeout: 10_000 });
  await mechanismSelect.click();
  await expect(page.getByRole("option", { name: "AWS_MSK_IAM", exact: true })).toBeVisible();
  await page.keyboard.press("Escape");

  // Leave the form on the unsecured default; nothing was saved.
  await protocolSelect.click();
  await page.getByRole("option", { name: "PLAINTEXT", exact: true }).click();
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 5 — AWS_MSK_IAM hides the credential inputs and explains why
// spec: API.md §DataHub Kafka security rule 3 — "kafka_sasl_username and
//   kafka_sasl_password are rejected when kafka_sasl_mechanism is AWS_MSK_IAM";
//   "AWS_MSK_IAM is not a typable credential — it authenticates with the consumer
//   pod's IAM identity, attached at deploy time by the chart plane"
// spec: API.md §DataHub Kafka security — kafka_aws_region is "AWS_MSK_IAM only.
//   Optional — falls back to derivation from the broker hostname"
// SAVE-FREE.
// ─────────────────────────────────────────────────────────────────────────────

test("/admin/peripherals — AWS_MSK_IAM swaps credentials for the region field + IAM note", async ({
  page,
}) => {
  await page.goto("/admin/peripherals");
  await expect(
    page.getByRole("heading", { name: "Admin — Peripherals", exact: true }),
  ).toBeVisible({ timeout: 15_000 });

  const protocolSelect = page.locator("#datahub_kafka_security_protocol");
  await expect(protocolSelect).toBeVisible({ timeout: 15_000 });
  await protocolSelect.click();
  await page.getByRole("option", { name: "SASL_SSL", exact: true }).click();

  const mechanismSelect = page.locator("#datahub_kafka_sasl_mechanism");
  await expect(mechanismSelect).toBeVisible({ timeout: 10_000 });

  // -- A credential mechanism shows username + password, and no region/IAM note.
  // spec: API.md §DataHub Kafka security rule 2 — a username is required for SCRAM.
  await mechanismSelect.click();
  await page.getByRole("option", { name: "SCRAM-SHA-512", exact: true }).click();
  await expect(page.locator("#datahub_kafka_sasl_username")).toBeVisible({ timeout: 10_000 });
  await expect(page.locator("#datahub_kafka_sasl_password")).toBeVisible();
  await expect(page.locator("#datahub_kafka_aws_region")).toHaveCount(0);
  await expect(page.locator("#datahub_kafka_aws_msk_iam_note")).toHaveCount(0);

  // -- AWS_MSK_IAM removes both credential inputs, adds the region field and the note.
  await mechanismSelect.click();
  await page.getByRole("option", { name: "AWS_MSK_IAM", exact: true }).click();
  await expect(page.locator("#datahub_kafka_sasl_username")).toHaveCount(0);
  await expect(page.locator("#datahub_kafka_sasl_password")).toHaveCount(0);
  await expect(page.locator("#datahub_kafka_aws_region")).toBeVisible({ timeout: 10_000 });
  await expect(page.locator("#datahub_kafka_aws_msk_iam_note")).toBeVisible();

  // The note must say the identity is a deploy-time grant, not something this form sets.
  // spec: API.md §DataHub Kafka security — the IAM role is "attached at deploy time by
  //   the chart plane"; feature/BACKEND.md §Kafka connection — "a deployment whose
  //   ServiceAccount carries no IAM role cannot be fixed from the admin API".
  await expect(page.locator("#datahub_kafka_aws_msk_iam_note")).toContainText("IAM role");

  // Leave the form on the unsecured default; nothing was saved.
  await protocolSelect.click();
  await page.getByRole("option", { name: "PLAINTEXT", exact: true }).click();
  await expect(page.locator("#datahub_kafka_aws_msk_iam_note")).toHaveCount(0);
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 6 — the consumer-health badge mirrors GET health (dual confirmation)
// spec: API.md §DataHub Kafka security — "The health object on GET reports whether
//   that configuration actually works … status is unknown when the consumer has
//   never reported — including every deployment that runs no consumer at all"
// spec: FRONTEND_BASIC.md §Peripherals — the DataHub card renders consumer health
// SAVE-FREE (read-only).
// ─────────────────────────────────────────────────────────────────────────────

test("/admin/peripherals — consumer health badge matches GET /admin/peripherals/datahub", async ({
  page,
  adminApi,
}) => {
  // Backend probe first: the badge must mirror whatever the API reports, not a
  // hard-coded expectation — the dev cluster ships the consumer disabled, so the
  // normal value is "unknown", but the assertion holds for any of the three states.
  const resp = await adminApi.get("/api/v1/admin/peripherals/datahub");
  expect(resp.status()).toBe(200);
  const dh = (await resp.json()) as DatahubPeripheral;
  expect(["unknown", "ok", "error"]).toContain(dh.health.status);

  await page.goto("/admin/peripherals");
  await expect(
    page.getByRole("heading", { name: "Admin — Peripherals", exact: true }),
  ).toBeVisible({ timeout: 15_000 });

  const badge = page.locator("#datahub_health_status");
  await expect(badge).toBeVisible({ timeout: 15_000 });
  await expect(badge).toHaveAttribute("data-status", dh.health.status);

  if (dh.health.status === "error") {
    // The failure message is the actionable part; is_configured cannot express it.
    await expect(page.locator("#datahub_health_error")).toContainText(dh.health.last_error ?? "");
  } else {
    await expect(page.locator("#datahub_health_error")).toHaveCount(0);
  }

  // Health is a report about the connection, never an input to is_configured.
  // spec: API.md §DataHub Kafka security — "is_configured only states that values are
  //   present"; the Kafka credential "never affects the flag".
  expect(typeof dh.is_configured).toBe("boolean");
});
