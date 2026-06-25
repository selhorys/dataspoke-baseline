/**
 * Ground spec: /admin/peripherals page — narrow UI-flow tests.
 *
 * Concern: the DataHub + Langfuse peripheral panels render, are populated from
 * GET /admin/peripherals/{datahub,langfuse} (assert non-secret fields show their
 * current values and secret inputs render blank), and support a per-card save
 * round-trip on ONE benign non-secret field (DataHub `default_env`). The field is
 * edited → Save DataHub → persisted value confirmed via adminApi GET → reverted.
 *
 * CRITICAL: do NOT touch the secret inputs (token, secret_key) — they route to the
 * K8s Secret and clearing them would break the dev cluster's DataHub/Langfuse
 * connectivity. The non-secret field edited here is reverted before the test ends.
 *
 * spec: spec/feature/FRONTEND_BASIC.md §Admin Peripherals (/admin/peripherals)
 * spec: spec/API.md §/admin/peripherals/datahub — GET returns non-secret
 *   service_corpuser_urn/default_env plain; PATCH (partial, changed fields only)
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — dual confirmation, selector guidance
 */

import { test, expect } from "../../fixtures/index";

// ── Types ─────────────────────────────────────────────────────────────────────

interface DatahubPeripheral {
  gms_url: string;
  kafka_brokers: string;
  token: string;
  service_corpuser_urn: string;
  default_env: string;
  is_configured: boolean;
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
// spec: FRONTEND_BASIC.md §Admin Peripherals — DataHub + Langfuse cards; non-secret
//   fields prefilled from GET; secret inputs render blank.
// ─────────────────────────────────────────────────────────────────────────────

test("/admin/peripherals — cards render with values from GET; secrets blank", async ({
  page,
  adminApi,
}) => {
  // Backend probe: read current peripheral config to know expected field values.
  // spec: TESTING.md §E2E — poll adminApi until present, THEN assert UI.
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
// spec: FRONTEND_BASIC.md §Admin Peripherals — per-card partial PATCH; save shows
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
  // spec: FRONTEND_BASIC.md §Admin Peripherals — PATCH writes the value; GET reflects it.
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
