/**
 * Ground spec: /admin/conf page — narrow UI-flow tests.
 *
 * Concern: the runtime-configuration form renders, is populated from
 * GET /admin/conf (assert two known numeric fields show their current values),
 * and supports a save round-trip on ONE benign numeric field
 * (validation_score_n_intervals). The field is edited → Save → persisted value
 * confirmed via adminApi GET → reverted to original.
 *
 * CRITICAL: do NOT touch stub_* toggles (other tests depend on stub mode) and do
 * NOT change the LLM API key. Any edit is reverted before the test ends.
 *
 * spec: spec/feature/FRONTEND_BASIC.md §Configurations (/admin/conf)
 * spec: spec/feature/FRONTEND_BASIC.md §Routing — /admin/conf: GET /admin/conf,
 *   PATCH /admin/conf (partial, changed fields only)
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — dual confirmation, selector guidance
 */

import { test, expect } from "../../fixtures/index";

// ── Types ─────────────────────────────────────────────────────────────────────

interface RuntimeConf {
  llm_provider: string;
  llm_model: string;
  validation_score_n_intervals: number;
  ontogen_llm_max_iterations: number;
  stub_redis_client: boolean;
  stub_llm_client: boolean;
  stub_pgvector_manager: boolean;
  stub_notification_service: boolean;
  updated_at: string | null;
}

// ── Module state ──────────────────────────────────────────────────────────────

/** Original value of the field we edit; used in afterAll for revert. */
let originalIntervals: number | null = null;

// ── Cleanup ───────────────────────────────────────────────────────────────────

test.afterAll(async ({ adminApi }) => {
  // Revert validation_score_n_intervals to its original value if we changed it.
  if (originalIntervals !== null) {
    await adminApi.patch("/api/v1/admin/conf", {
      data: { validation_score_n_intervals: originalIntervals },
    });
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 1 — /admin/conf renders the form with values from GET /admin/conf
// spec: FRONTEND_BASIC.md §Configurations — GET /admin/conf populates form fields;
//   field groups: LLM, OntoGen, MetaGen, Validation, Stubs, Auth.
// ─────────────────────────────────────────────────────────────────────────────

test("/admin/conf — form renders with current config values from GET /admin/conf", async ({
  page,
  adminApi,
}) => {
  // Backend probe: read current conf to know expected field values.
  // spec: TESTING.md §E2E §Execution discipline — "Gate data-dependent UI assertions on
  //   confirmed backend state".
  const confResp = await adminApi.get("/api/v1/admin/conf");
  expect(confResp.status()).toBe(200);
  const conf = (await confResp.json()) as RuntimeConf;

  // Navigate to the page.
  await page.goto("/admin/conf");
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: page heading --
  // spec: admin/conf/page.tsx — h1 "Admin — Configurations"
  await expect(
    page.getByRole("heading", { name: "Admin — Configurations", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: LLM card heading --
  // spec: admin/conf/page.tsx — CardTitle "LLM"
  await expect(page.getByRole("heading", { name: "LLM", exact: true })).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: llm_provider field shows the current value (non-empty) --
  // spec: admin/conf/page.tsx — Input id="llm_provider" populated via useEffect(reset())
  const providerInput = page.locator("#llm_provider");
  await expect(providerInput).toBeVisible({ timeout: 10_000 });
  // The field should reflect the current conf value; assert it is not blank.
  const providerValue = await providerInput.inputValue();
  expect(providerValue, "llm_provider must be populated from GET /admin/conf").toBe(conf.llm_provider);

  // -- UI assertion: validation_score_n_intervals shows current value --
  // spec: admin/conf/page.tsx — Input id="validation_score_n_intervals" type="number"
  const intervalsInput = page.locator("#validation_score_n_intervals");
  await expect(intervalsInput).toBeVisible({ timeout: 10_000 });
  const intervalsValue = await intervalsInput.inputValue();
  expect(Number(intervalsValue)).toBe(conf.validation_score_n_intervals);

  // -- UI assertion: "Save changes" submit button visible --
  // spec: admin/conf/page.tsx — Button type="submit" "Save changes"
  await expect(page.getByRole("button", { name: "Save changes" })).toBeVisible();
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 2 — Edit validation_score_n_intervals → Save → confirm persisted → revert
// spec: FRONTEND_BASIC.md §Configurations — PATCH /admin/conf (partial); save shows
//   "Saved · updated <timestamp>" text; reverted afterward.
// CRITICAL: stub_* fields are NOT touched.
// ─────────────────────────────────────────────────────────────────────────────

test("/admin/conf — edit validation_score_n_intervals → Save → persisted → reverted", async ({
  page,
  adminApi,
}) => {
  // Pre-flight: read current value so we can revert exactly.
  const preResp = await adminApi.get("/api/v1/admin/conf");
  expect(preResp.status()).toBe(200);
  const pre = (await preResp.json()) as RuntimeConf;
  originalIntervals = pre.validation_score_n_intervals;

  // Choose a value that is different from the current value (within valid range ≥ 1).
  // spec: FRONTEND_BASIC.md §Configurations — validation_score_n_intervals: integer ≥ 1
  const NEW_VALUE = originalIntervals === 3 ? 4 : 3;

  // Navigate to the page.
  await page.goto("/admin/conf");
  await expect(page).not.toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: "Admin — Configurations", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // Wait for form to be populated (useEffect fires after GET /admin/conf settles).
  const intervalsInput = page.locator("#validation_score_n_intervals");
  await expect(intervalsInput).toBeVisible({ timeout: 15_000 });
  // Wait until the field reflects the real conf value (not the RHF defaultValue).
  await expect(intervalsInput).toHaveValue(String(originalIntervals), { timeout: 10_000 });

  // -- UI gesture: clear and fill the new value --
  // spec: admin/conf/page.tsx — Input id="validation_score_n_intervals" type="number"
  await intervalsInput.fill(String(NEW_VALUE));

  // -- UI gesture: click "Save changes" --
  // spec: admin/conf/page.tsx — Button type="submit" "Save changes"
  await page.getByRole("button", { name: "Save changes" }).click();

  // -- UI assertion: toast "Configuration saved." --
  // spec: admin/conf/page.tsx — toast({ title: "Configuration saved." })
  // Toasts render twice (visual + aria-live span) → .first() on the toast text.
  await expect(
    page.getByText("Configuration saved.", { exact: true }).first()
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: "Saved · updated ..." text appears (savedAt rendered) --
  // spec: admin/conf/page.tsx — p.text-sm "Saved · updated {new Date(savedAt).toLocaleString()}"
  await expect(page.getByText(/Saved · updated/)).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: the field now shows the new value --
  await expect(intervalsInput).toHaveValue(String(NEW_VALUE), { timeout: 5_000 });

  // -- Backend probe (dual confirmation): GET /admin/conf → new value persisted --
  // spec: FRONTEND_BASIC.md §Configurations — PATCH writes the value; GET reflects it.
  const afterResp = await adminApi.get("/api/v1/admin/conf");
  expect(afterResp.status()).toBe(200);
  const after = (await afterResp.json()) as RuntimeConf;
  expect(after.validation_score_n_intervals).toBe(NEW_VALUE);

  // CRITICAL: stub_* fields must remain unchanged.
  // They are dev-env-wide settings owned by the profile seed
  // (helm-charts/bin/post-install/seed-runtime-config.sh PATCHes all four for the dev
  // profile) and by the operator — not by anything in the E2E run; global-setup never
  // touches them.
  // spec: TESTING.md §E2E §Execution discipline — "Never flip the stub toggles… A test
  //   may read them to gate an LLM variant and must assert them unchanged after any
  //   `/admin/conf` write, but never sets them."
  // spec: TESTING.md §Stub Toggles (RuntimeConfig) — "The dev profile's
  //   `helm-charts/bin/post-install/seed-runtime-config.sh` PATCHes all four to `true`".
  expect(after.stub_redis_client).toBe(pre.stub_redis_client);
  expect(after.stub_llm_client).toBe(pre.stub_llm_client);
  expect(after.stub_pgvector_manager).toBe(pre.stub_pgvector_manager);
  expect(after.stub_notification_service).toBe(pre.stub_notification_service);

  // Revert the field via adminApi (mirrors the afterAll, but ensures revert even if
  // the next test runs immediately after).
  const revertResp = await adminApi.patch("/api/v1/admin/conf", {
    data: { validation_score_n_intervals: originalIntervals },
  });
  expect(revertResp.status()).toBe(200);
  const reverted = (await revertResp.json()) as RuntimeConf;
  expect(reverted.validation_score_n_intervals).toBe(originalIntervals);
  // Clear the afterAll guard since we've already reverted.
  originalIntervals = null;
});
