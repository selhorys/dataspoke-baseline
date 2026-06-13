/**
 * UC1 Case 3 — PASSIVE kafka source: browser UI flow.
 *
 * Mirrors tests/integration/api_wired/test_uc1_passive_kafka.py step-for-step,
 * with dual confirmation at each mutating step:
 *   - UI assertion (toast, redirect, rendered table contents, unmanaged count)
 *   - Backend probe via adminApi (same REST read-back the api-wired step asserts)
 *
 * Steps (verbatim from USE_CASE_en.md §UC1 Case 3):
 *   0. Pre-source: /ingestion/unmanaged shows imazon.* topics before source creation.
 *      Backend probe: GET /spoke/ingestion/unmanaged confirms topics present.
 *   1. Navigate to /ingestion/sources/new; fill mode=PASSIVE, name, kafka recipe; Submit.
 *      Assert redirect to detail; backend: POST → 201, body shape (no schedule).
 *   2. On detail page, verify Run panel shows "not available" explanation (PASSIVE).
 *      Backend probe: POST /method/run → 409 INGESTION_RUN_NOT_APPLICABLE.
 *   3. After sync sweep: Datasets panel shows imazon.* topics with derivation=matched.
 *      Backend probe: GET /sources/{id}/datasets confirms both Kafka URNs with matched.
 *   4. After sync: /ingestion/unmanaged does NOT show imazon.* topics (now mapped).
 *      Backend probe: GET /spoke/ingestion/unmanaged confirms topics absent.
 *   5. Events panel accessible (may be empty for PASSIVE; 200 response).
 *      Backend probe: GET /sources/{id}/event → 200.
 *   6. Cleanup: Delete source via ConfirmDialog.
 *      Backend probe: GET /sources/{id} → 404.
 *
 * Design note: step 0's positive check relies on the global-setup --reset-seed having run
 * and the ES index having settled (≤180s budget). This test does NOT re-run the sync
 * internally — the api-wired step 0 / step 4 sync calls are backend-probed only
 * (no UI surface for /internal/activities). The UI reflects post-sync state via its
 * own polling.
 *
 * spec: USE_CASE_en.md §UC1 Case 3
 * spec: spec/feature/FRONTEND_INGESTION.md §Create View, §Source Detail, §Unmanaged View
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — dual confirmation
 */

import { test, expect } from "../fixtures/index";
import { apiBaseUrl } from "../fixtures/env";

// ── Constants (verbatim from api-wired test) ────────────────────────────────

const SOURCE_NAME = "dummy kafka topics";

// Kafka URNs (spec: TESTING.md §Imazon Dummy-Data Reference)
const ORDERS_URN =
  "urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.orders.events,DEV)";
const SHIPPING_URN =
  "urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.shipping.updates,DEV)";

const IMAZON_KAFKA_URNS = new Set([ORDERS_URN, SHIPPING_URN]);

// The YAML recipe for a PASSIVE kafka source (matching api-wired payload).
// spec: USE_CASE_en.md §UC1 Case 3 — mode: PASSIVE, no schedule,
//   recipe.source.type: kafka, topic_patterns.allow: ['^imazon\\..*$']
const RECIPE_YAML = `source:
  type: kafka
  config:
    topic_patterns:
      allow:
        - '^imazon\\..*$'
`;

// Runs under the admin project only — enforced by the filename convention in
// playwright.config.ts (default *.spec.ts → admin), which supplies the admin
// storageState. Do not override storageState here.

// ── Per-test state ────────────────────────────────────────────────────────

let sourceId: string | null = null;

// ── Cleanup: delete the created source after all steps ────────────────────

test.afterAll(async ({ adminApi }) => {
  if (sourceId) {
    await adminApi.delete(`/api/v1/spoke/ingestion/sources/${sourceId}`);
    sourceId = null;
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 0 — Pre-source positive check: imazon.* topics appear in /unmanaged
// spec: USE_CASE_en.md §UC1 Case 3 — "Datasets covered by no source appear in
//   GET /spoke/ingestion/unmanaged" (positive invariant before source creation)
// spec: FRONTEND_INGESTION.md §Unmanaged View — /ingestion/unmanaged page
// ─────────────────────────────────────────────────────────────────────────────
test("UC1 Case 3 step 0 — imazon Kafka topics appear in /unmanaged before source creation", async ({
  page,
  adminApi,
}) => {
  // Trigger the sync sweep via the internal API to populate the dataset registry.
  // spec: test_uc1_passive_kafka.py step 0 — re-trigger sync each iteration so newly-ES-indexed URNs surface.
  // This step has no UI surface — fired via adminApi with the internal token.
  // spec: TESTING.md §E2E — "[API-fired, no UI surface]" steps are probed via backend, not gestures.
  const base = apiBaseUrl();
  const token = process.env["DATASPOKE_TEST_INTERNAL_TOKEN"] ?? "";

  // Poll until both imazon topics appear in /unmanaged (≤180s, ES lag budget).
  // spec: project_es_indexing_lag_after_reset_seed — ES lags ~2-3 min after seed.
  const deadline = Date.now() + 180_000;
  let beforeUrns: string[] = [];
  while (Date.now() < deadline) {
    // Trigger sync (best-effort; tolerate non-200 during ES lag)
    try {
      await fetch(`${base}/internal/activities/ingestion/sync`, {
        method: "POST",
        headers: { "X-Internal-Token": token, "Content-Type": "application/json" },
      });
    } catch {
      // transient
    }

    const unmanResp = await adminApi.get("/api/v1/spoke/ingestion/unmanaged?limit=500");
    if (unmanResp.ok()) {
      const body = (await unmanResp.json()) as { dataset_urns: string[] };
      beforeUrns = body.dataset_urns;
      const allPresent = [...IMAZON_KAFKA_URNS].every((u) => beforeUrns.includes(u));
      if (allPresent) break;
    }
    await new Promise((r) => setTimeout(r, 5_000));
  }

  // Positive presence assertion (mirrors api-wired step 0).
  for (const urn of IMAZON_KAFKA_URNS) {
    expect(
      beforeUrns.includes(urn),
      `Before PASSIVE source creation, ${urn} must appear in /unmanaged. ` +
        "spec: USE_CASE_en.md §UC1 Case 3 — unmanaged bucket contains unmapped datasets."
    ).toBe(true);
  }

  // -- UI check: /ingestion/unmanaged page renders these URNs --
  // spec: FRONTEND_INGESTION.md §Unmanaged View — UnmanagedDatasetTable rows show URN links
  //
  // The API poll above confirmed both Kafka URNs are in /unmanaged; the page should
  // reflect the same state after the data fetch completes. Use expect(...).toBeVisible()
  // with a 30s budget (covers page load + API fetch + render) rather than the
  // synchronous isVisible() which races the component's data fetch.
  await page.goto("/ingestion/unmanaged");
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page.getByRole("heading", { name: "Unmanaged datasets" })).toBeVisible({
    timeout: 15_000,
  });
  // At least one imazon Kafka URN must be visible on the page. Try the orders URN first;
  // if it's not rendered (e.g. pagination below fold), check the shipping URN.
  // spec: FRONTEND_INGESTION.md §Unmanaged View — UnmanagedDatasetTable rows show URN text
  const ordersLocator = page.getByText(ORDERS_URN, { exact: false });
  const shippingLocator = page.getByText(SHIPPING_URN, { exact: false });
  // Wait up to 30s for the table to render (covers API fetch + React render).
  // spec: TESTING.md §E2E — bounded expect.poll / toBeVisible rather than fixed sleeps.
  await expect(ordersLocator.or(shippingLocator).first()).toBeVisible({ timeout: 30_000 });
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 1 — Create PASSIVE kafka source via /ingestion/sources/new
// spec: USE_CASE_en.md §UC1 Case 3 step 1
// spec: FRONTEND_INGESTION.md §Create View — mode=PASSIVE, no schedule, kafka recipe
// ─────────────────────────────────────────────────────────────────────────────
test("UC1 Case 3 step 1 — create PASSIVE kafka source", async ({ page, adminApi }) => {
  // Navigate to the Create source page.
  await page.goto("/ingestion/sources/new");
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI gesture: mode selector → PASSIVE --
  // spec: FRONTEND_INGESTION.md §Create View — mode selector includes PASSIVE
  // The mode selector has id="create-mode"; click to open the Radix Select dropdown.
  const modeTrigger = page.locator("#create-mode");
  await expect(modeTrigger).toBeVisible();
  await modeTrigger.click();
  await page.getByRole("option", { name: "Passive" }).click();

  // -- UI assertion: schedule field should NOT appear for PASSIVE --
  // spec: FRONTEND_INGESTION.md §Create View — PASSIVE has no schedule selector
  await expect(page.locator("#create-schedule")).not.toBeVisible();

  // -- UI gesture: name field --
  await page.locator("#create-name").fill(SOURCE_NAME);

  // -- UI gesture: paste YAML recipe --
  // spec: FRONTEND_INGESTION.md §Create View — YAML editor (RecipeYamlEditor), recipeOnly mode
  // The Textarea in RecipeYamlEditor carries aria-label="recipe YAML"; use getByLabel to
  // distinguish it from the name <Input> (also a textbox) that is also on this page.
  // fill() clears the textarea before writing; no selectAll() needed.
  const recipeEditor = page.getByLabel("recipe YAML");
  await recipeEditor.fill(RECIPE_YAML);

  // -- UI gesture: click Save --
  await page.getByRole("button", { name: "Save" }).click();

  // -- UI assertion: redirect to source detail page --
  // spec: FRONTEND_INGESTION.md §Create View — on success, redirect to /ingestion/sources/[id]
  // Exclude the create page itself (/sources/new) so a failed create (which stays
  // on /new) is caught here instead of silently matching the loose pattern.
  await page.waitForURL(/\/ingestion\/sources\/(?!new$)[^/]+$/, { timeout: 30_000 });
  const url = page.url();
  const idMatch = /\/ingestion\/sources\/([^/?#]+)$/.exec(url);
  expect(idMatch, "Expected source ID in URL after redirect").toBeTruthy();
  sourceId = decodeURIComponent(idMatch![1]!);

  // -- UI assertion: source name heading visible --
  await expect(page.getByRole("heading", { name: SOURCE_NAME })).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: mode badge "Passive" visible --
  // spec: FRONTEND_INGESTION.md §Source Detail — mode badge in header; modeLabel(PASSIVE) = "Passive"
  await expect(page.getByText("Passive", { exact: true })).toBeVisible();

  // -- Backend probe: GET /spoke/ingestion/sources/{id} --
  // spec: USE_CASE_en.md §UC1 Case 3 step 2 — body shape: mode=PASSIVE, schedule=null,
  //   no schedule_tier, platform='kafka', datahub_source_urn=null
  const getResp = await adminApi.get(`/api/v1/spoke/ingestion/sources/${sourceId}`);
  expect(getResp.status()).toBe(200);
  const source = (await getResp.json()) as {
    id: string;
    mode: string;
    name: string;
    schedule: string | null;
    platform: string;
    datahub_source_urn: string | null;
  };
  expect(source.mode).toBe("PASSIVE");
  expect(source.name).toBe(SOURCE_NAME);
  expect(source.schedule).toBeNull();
  expect(Object.keys(source)).not.toContain("schedule_tier");
  expect(source.platform).toBe("kafka");
  expect(source.datahub_source_urn).toBeNull();
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 2 — Run panel shows "not available" for PASSIVE source
// spec: USE_CASE_en.md §UC1 Case 3 step 3 — PASSIVE cannot be run
// spec: FRONTEND_INGESTION.md §Source Detail §Run — PASSIVE shows explanatory disabled state
// ─────────────────────────────────────────────────────────────────────────────
test("UC1 Case 3 step 2 — Run panel shows disabled state for PASSIVE source", async ({
  page,
  adminApi,
}) => {
  if (!sourceId) test.skip();

  await page.goto(`/ingestion/sources/${encodeURIComponent(sourceId!)}`);
  await expect(page.getByRole("heading", { name: SOURCE_NAME })).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: Run section visible --
  await expect(page.getByRole("heading", { name: "Run" })).toBeVisible();

  // -- UI assertion: "not available" explanation shown (PASSIVE has no run button) --
  // spec: FRONTEND_INGESTION.md §Source Detail §Run — IngestionRunPanel: PASSIVE shows explanatory text
  // The IngestionRunPanel for non-ACTIVE_CUSTOM_MANAGED renders:
  //   "Run is not available for this source. {modeDescription(mode)}"
  await expect(page.getByText(/not available/i)).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: no Run or Dry Run button visible for PASSIVE --
  await expect(page.getByRole("button", { name: "Run" })).not.toBeVisible();
  await expect(page.getByRole("button", { name: "Dry Run" })).not.toBeVisible();

  // -- Backend probe: POST /method/run → 409 INGESTION_RUN_NOT_APPLICABLE --
  // spec: API.md §Ingestion — PASSIVE: 409 INGESTION_RUN_NOT_APPLICABLE
  const runResp = await adminApi.post(`/api/v1/spoke/ingestion/sources/${sourceId}/method/run`);
  expect(runResp.status()).toBe(409);
  const runBody = (await runResp.json()) as { error_code: string };
  expect(runBody.error_code).toBe("INGESTION_RUN_NOT_APPLICABLE");

  // dry_run=true also returns 409 (PASSIVE cannot run regardless)
  // spec: API.md §Ingestion — INGESTION_RUN_NOT_APPLICABLE for all PASSIVE run attempts
  const dryRunResp = await adminApi.post(
    `/api/v1/spoke/ingestion/sources/${sourceId}/method/run?dry_run=true`
  );
  expect(dryRunResp.status()).toBe(409);
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 3 — After sync: Datasets panel shows imazon.* topics with derivation=matched
// spec: USE_CASE_en.md §UC1 Case 3 step 4
// spec: FRONTEND_INGESTION.md §Source Detail §Datasets — SourceDatasetTable; matched derivation
// ─────────────────────────────────────────────────────────────────────────────
test("UC1 Case 3 step 3 — datasets panel shows imazon Kafka topics with matched derivation", async ({
  page,
  adminApi,
}) => {
  if (!sourceId) test.skip();

  // Trigger sync sweeps (backend, no UI surface) until both Kafka URNs appear in datasets.
  // spec: test_uc1_passive_kafka.py step 4 — re-trigger sync each iteration.
  const base = apiBaseUrl();
  const token = process.env["DATASPOKE_TEST_INTERNAL_TOKEN"] ?? "";
  const deadline = Date.now() + 180_000;
  let datasetsUrns: string[] = [];

  while (Date.now() < deadline) {
    try {
      await fetch(`${base}/internal/activities/ingestion/sync`, {
        method: "POST",
        headers: { "X-Internal-Token": token, "Content-Type": "application/json" },
      });
    } catch {
      // transient
    }
    const resp = await adminApi.get(`/api/v1/spoke/ingestion/sources/${sourceId}/datasets`);
    if (resp.ok()) {
      const body = (await resp.json()) as {
        datasets: Array<{ dataset_urn: string; derivation: string; authority: string }>;
      };
      datasetsUrns = body.datasets.map((d) => d.dataset_urn);
      const allMapped = [...IMAZON_KAFKA_URNS].every((u) => datasetsUrns.includes(u));
      if (allMapped) break;
    }
    await new Promise((r) => setTimeout(r, 5_000));
  }

  // -- Backend probe: both imazon Kafka URNs mapped with derivation='matched' --
  // spec: USE_CASE_en.md §UC1 Case 3 step 4 — imazon.* topics mapped; derivation=matched
  for (const urn of IMAZON_KAFKA_URNS) {
    expect(
      datasetsUrns.includes(urn),
      `${urn} must be mapped to the PASSIVE source after sync. ` +
        "spec: feature/BACKEND.md §Sync sweep step 2 — AllowDenyPattern matcher."
    ).toBe(true);
  }

  // Verify derivation and authority.
  const datasetsResp = await adminApi.get(
    `/api/v1/spoke/ingestion/sources/${sourceId}/datasets`
  );
  const datasetsBody = (await datasetsResp.json()) as {
    datasets: Array<{ dataset_urn: string; derivation: string; authority: string }>;
  };
  for (const d of datasetsBody.datasets) {
    if (IMAZON_KAFKA_URNS.has(d.dataset_urn)) {
      expect(d.derivation).toBe("matched");
      expect(d.authority).toBe("medium");
    }
  }

  // -- UI assertion: navigate to detail and check datasets table --
  // The page renders SourceDatasetTable with the matched URNs.
  await page.goto(`/ingestion/sources/${encodeURIComponent(sourceId!)}`);
  await expect(page.getByRole("heading", { name: SOURCE_NAME })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("heading", { name: "Datasets" })).toBeVisible();

  // At least one Kafka URN must appear in the table. Use an auto-waiting
  // assertion (not isVisible(), which is a zero-wait race against the panel's
  // async data fetch); the backend probe above already confirmed the mapping.
  const ordersText = page.getByText(ORDERS_URN, { exact: false });
  const shippingText = page.getByText(SHIPPING_URN, { exact: false });
  await expect(ordersText.or(shippingText).first()).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: authority badge "medium (matched)" present --
  // spec: FRONTEND_INGESTION.md §Source Detail §Datasets — authority rendered as "medium (matched)"
  // Both mapped Kafka topics render this badge; assert at least one is present.
  await expect(page.getByText("medium (matched)").first()).toBeVisible();
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 4 — After sync: imazon.* topics absent from /unmanaged (now mapped)
// spec: USE_CASE_en.md §UC1 Case 3 step 5
// spec: FRONTEND_INGESTION.md §Unmanaged View — mapped datasets absent from unmanaged
// ─────────────────────────────────────────────────────────────────────────────
test("UC1 Case 3 step 4 — imazon Kafka topics absent from /unmanaged after source maps them", async ({
  page,
  adminApi,
}) => {
  if (!sourceId) test.skip();

  // Poll until both imazon topics leave /unmanaged (≤120s).
  // spec: test_uc1_passive_kafka.py step 5 — poll ≤120s for mapping propagation.
  const deadline = Date.now() + 120_000;
  let afterUrns: string[] = [];
  while (Date.now() < deadline) {
    const resp = await adminApi.get("/api/v1/spoke/ingestion/unmanaged?limit=500");
    if (resp.ok()) {
      const body = (await resp.json()) as { dataset_urns: string[] };
      afterUrns = body.dataset_urns;
      const stillUnmanaged = [...IMAZON_KAFKA_URNS].filter((u) => afterUrns.includes(u));
      if (stillUnmanaged.length === 0) break;
    }
    await new Promise((r) => setTimeout(r, 5_000));
  }

  // -- Backend probe: mapped URNs absent from /unmanaged --
  // spec: USE_CASE_en.md §UC1 — "Datasets covered by no source appear in /unmanaged"
  for (const urn of IMAZON_KAFKA_URNS) {
    expect(
      !afterUrns.includes(urn),
      `${urn} must NOT appear in /unmanaged after being mapped to the PASSIVE source. ` +
        "spec: USE_CASE_en.md §UC1 Case 3 — mapped datasets absent from unmanaged bucket."
    ).toBe(true);
  }

  // -- UI assertion: /ingestion/unmanaged page does not show imazon Kafka URNs --
  // spec: FRONTEND_INGESTION.md §Unmanaged View — mapped datasets not rendered
  await page.goto("/ingestion/unmanaged");
  await expect(page.getByRole("heading", { name: "Unmanaged datasets" })).toBeVisible({
    timeout: 15_000,
  });

  // Neither URN should appear (they are now mapped).
  await expect(page.getByText(ORDERS_URN, { exact: false })).not.toBeVisible({ timeout: 10_000 });
  await expect(page.getByText(SHIPPING_URN, { exact: false })).not.toBeVisible({ timeout: 10_000 });
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 5 — Events panel accessible (200; may be empty for PASSIVE)
// spec: USE_CASE_en.md §UC1 Case 3 step 6
// spec: FRONTEND_INGESTION.md §Source Detail §Events — IngestionEventTable
// ─────────────────────────────────────────────────────────────────────────────
test("UC1 Case 3 step 5 — events panel is accessible for PASSIVE source", async ({
  page,
  adminApi,
}) => {
  if (!sourceId) test.skip();

  await page.goto(`/ingestion/sources/${encodeURIComponent(sourceId!)}`);
  await expect(page.getByRole("heading", { name: SOURCE_NAME })).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: Events section rendered --
  // spec: FRONTEND_INGESTION.md §Source Detail §Events — always rendered (even when empty)
  await expect(page.getByRole("heading", { name: "Events" })).toBeVisible();

  // -- Backend probe: GET /sources/{id}/event → 200, events is a list --
  // spec: API.md §Ingestion — GET /sources/{id}/event returns event history; 200 even when empty
  const eventResp = await adminApi.get(`/api/v1/spoke/ingestion/sources/${sourceId}/event`);
  expect(eventResp.status()).toBe(200);
  const eventBody = (await eventResp.json()) as { events: unknown[] };
  expect(Array.isArray(eventBody.events)).toBe(true);
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 6 — Cleanup: delete PASSIVE source via ConfirmDialog
// spec: FRONTEND_INGESTION.md §Source Detail — Delete button → ConfirmDialog
// ─────────────────────────────────────────────────────────────────────────────
test("UC1 Case 3 step 6 — delete PASSIVE source; source gone from list", async ({
  page,
  adminApi,
}) => {
  if (!sourceId) test.skip();

  await page.goto(`/ingestion/sources/${encodeURIComponent(sourceId!)}`);
  await expect(page.getByRole("heading", { name: SOURCE_NAME })).toBeVisible({ timeout: 15_000 });

  // -- UI gesture: Delete → ConfirmDialog → confirm --
  await page.getByRole("button", { name: "Delete" }).click();
  await page.getByRole("button", { name: "Delete", exact: true }).last().click();

  // -- UI assertion: redirected to /ingestion --
  await page.waitForURL(/\/ingestion$/, { timeout: 30_000 });
  await expect(page.getByText(SOURCE_NAME)).not.toBeVisible({ timeout: 10_000 });

  // -- Backend probe: GET /sources/{id} → 404 --
  const checkResp = await adminApi.get(`/api/v1/spoke/ingestion/sources/${sourceId}`);
  expect(checkResp.status()).toBe(404);

  sourceId = null;
});
