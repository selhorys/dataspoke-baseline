/**
 * Ground spec: /admin/conf page — narrow UI-flow tests.
 *
 * Concern: the runtime-configuration form renders, is populated from
 * GET /admin/conf (assert two known fields show their current values),
 * and supports a save round-trip on ONE numeric field
 * (metagen_ontology_rag_node_k). The field is edited → Save → persisted value
 * confirmed via adminApi GET → reverted to original.
 *
 * Why that field is safe to write here: it is the MetaGen ontology-RAG top-K, read
 * only on the LLM grounding path, and this suite runs against the dev profile with
 * stub_llm_client=true (seeded by helm-charts/bin/post-install/seed-runtime-config.sh),
 * so no run in the E2E session consumes it and its value has no observable effect on
 * any other spec. It is also bound-safe (0–20, both the original and the written value
 * are inside), always differs from whatever is loaded, and is restored twice — inline
 * at the end of the test and again in afterAll if the test aborts first.
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
  metagen_ontology_rag_node_k: number;
  ontogen_llm_max_iterations: number;
  stub_redis_client: boolean;
  stub_llm_client: boolean;
  stub_pgvector_manager: boolean;
  stub_notification_service: boolean;
  updated_at: string | null;
}

// ── Module state ──────────────────────────────────────────────────────────────

/** Original value of the field we edit; used in afterAll for revert. */
let originalNodeK: number | null = null;

// ── Cleanup ───────────────────────────────────────────────────────────────────

test.afterAll(async ({ adminApi }) => {
  // Revert metagen_ontology_rag_node_k to its original value if we changed it.
  if (originalNodeK !== null) {
    await adminApi.patch("/api/v1/admin/conf", {
      data: { metagen_ontology_rag_node_k: originalNodeK },
    });
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 1 — /admin/conf renders the form with values from GET /admin/conf
// spec: FRONTEND_BASIC.md §Configurations — GET /admin/conf populates form fields; the
//   sketch groups them as LLM / OntoGen / MetaGen / Stubs / Auth.
// spec: admin/conf/page.tsx — the rendered CardTitle strings for those groups are
//   "LLM", "Ontology Generation", "Metadata Generation", "Dependency stubs", "Auth".
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

  // -- UI assertion: metagen_ontology_rag_node_k shows current value --
  // spec: admin/conf/page.tsx — Input id="metagen_ontology_rag_node_k" type="number"
  const nodeKInput = page.locator("#metagen_ontology_rag_node_k");
  await expect(nodeKInput).toBeVisible({ timeout: 10_000 });
  const nodeKValue = await nodeKInput.inputValue();
  expect(Number(nodeKValue)).toBe(conf.metagen_ontology_rag_node_k);

  // -- UI assertion: "Save changes" submit button visible --
  // spec: admin/conf/page.tsx — Button type="submit" "Save changes"
  await expect(page.getByRole("button", { name: "Save changes" })).toBeVisible();
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 2 — Edit metagen_ontology_rag_node_k → Save → confirm persisted → revert
// spec: FRONTEND_BASIC.md §Configurations — PATCH /admin/conf (partial); save shows
//   "Saved · updated <timestamp>" text; reverted afterward.
// CRITICAL: stub_* fields are NOT touched.
// ─────────────────────────────────────────────────────────────────────────────

test("/admin/conf — edit metagen_ontology_rag_node_k → Save → persisted → reverted", async ({
  page,
  adminApi,
}) => {
  // Pre-flight: read current value so we can revert exactly.
  const preResp = await adminApi.get("/api/v1/admin/conf");
  expect(preResp.status()).toBe(200);
  const pre = (await preResp.json()) as RuntimeConf;
  originalNodeK = pre.metagen_ontology_rag_node_k;

  // Choose a value that is different from the current value (within valid range 0–20).
  // spec: FRONTEND_BASIC.md §Configurations — "`*_rag_k` and `metagen_ontology_rag_*_k` 0–20"
  const NEW_VALUE = originalNodeK === 8 ? 9 : 8;

  // Navigate to the page.
  await page.goto("/admin/conf");
  await expect(page).not.toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: "Admin — Configurations", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // Wait for form to be populated (useEffect fires after GET /admin/conf settles).
  const nodeKInput = page.locator("#metagen_ontology_rag_node_k");
  await expect(nodeKInput).toBeVisible({ timeout: 15_000 });
  // Wait until the field reflects the real conf value (not the RHF defaultValue).
  await expect(nodeKInput).toHaveValue(String(originalNodeK), { timeout: 10_000 });

  // -- UI gesture: clear and fill the new value --
  // spec: admin/conf/page.tsx — Input id="metagen_ontology_rag_node_k" type="number"
  await nodeKInput.fill(String(NEW_VALUE));

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
  await expect(nodeKInput).toHaveValue(String(NEW_VALUE), { timeout: 5_000 });

  // -- Backend probe (dual confirmation): GET /admin/conf → new value persisted --
  // spec: FRONTEND_BASIC.md §Configurations — PATCH writes the value; GET reflects it.
  const afterResp = await adminApi.get("/api/v1/admin/conf");
  expect(afterResp.status()).toBe(200);
  const after = (await afterResp.json()) as RuntimeConf;
  expect(after.metagen_ontology_rag_node_k).toBe(NEW_VALUE);

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
    data: { metagen_ontology_rag_node_k: originalNodeK },
  });
  expect(revertResp.status()).toBe(200);
  const reverted = (await revertResp.json()) as RuntimeConf;
  expect(reverted.metagen_ontology_rag_node_k).toBe(originalNodeK);
  // Clear the afterAll guard since we've already reverted.
  originalNodeK = null;
});
