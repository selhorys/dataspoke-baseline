/**
 * UC1 Case 2 — ACTIVE_CUSTOM_MANAGED postgres source: browser UI flow.
 *
 * Mirrors tests/integration/api_wired/test_uc1_02_active_custom_postgres.py
 * step-for-step, with dual confirmation at each mutating step:
 *   - UI assertion (toast, redirect, rendered table contents)
 *   - Backend probe via adminApi (same REST read-back the api-wired step asserts)
 *
 * Steps (verbatim from USE_CASE_en.md §UC1 Case 2):
 *   1. Navigate to /ingestion/sources/new; fill mode/name/schedule/recipe; Submit.
 *      Assert redirect to detail page; backend: POST → 201, body shape.
 *   2. On the detail page, enable dry_run toggle, trigger Run.
 *      Assert run result visible; backend: discovered_urns_count >= 2 and
 *      emitted_urns_count == 0 (dry-run discovers but emits nothing).
 *   3. Disable dry_run, trigger real Run.
 *      Assert run result; backend: emitted_urns_count >= 2 and emitted_urns ⊆ discovered_urns.
 *   4. Datasets panel lists ≥ 2 catalog rows with derivation=emitted.
 *      Backend: GET /sources/{id}/datasets.
 *   5. Events panel shows INGESTION.COMPLETE for this run.
 *      Backend: GET /sources/{id}/event.
 *   6. Navigate to /data/<catalog.title_master urn> (Ingestion summary card).
 *      Assert the Ingestion card names this source; backend: GET attr/ingestion.
 *   7. Cleanup: Delete the source via ConfirmDialog.
 *      Backend: GET /sources/{id} → 404.
 *
 * Secret precondition: beforeAll provisions dataspoke-source-cred-dummy-data-pg
 * idempotently via kubectl (create --dry-run=client -o yaml | apply -f -).
 * The test skips cleanly only if DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD is unset.
 *
 * spec: USE_CASE_en.md §UC1 Case 2
 * spec: spec/feature/FRONTEND_INGESTION.md §Create View, §Source Detail, §Per-dataset reverse-lookup
 * spec: spec/feature/SECRET_RESOLUTION.md §Reference-only model — out-of-band authoring
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — dual confirmation, selector guidance
 */

import { spawnSync } from "child_process";
import { test, expect, IMAZON_URNS } from "../fixtures/index";
import { required } from "../fixtures/env";

// ── Constants (verbatim from api-wired test) ────────────────────────────────

const SOURCE_NAME = "dummy postgres example_db in catalog schema";

// In-cluster host:port of the dummy-data postgres, read from the auto-populated env var
// so the dummy-data namespace isn't hardcoded (mirrors api-wired _PG_HOST_PORT); no
// hardcoded fallback so a wrong host fails the run loudly.
const PG_HOST_PORT = required("DATASPOKE_DEV_DUMMY_DATA_POSTGRES_HOST_PORT");

// Secret ref: ${dummy-data-pg__password} — the bare ref without ${...} wrapper.
const SECRET_REF_BARE = "dummy-data-pg__password";
const SECRET_REF = "${dummy-data-pg__password}";

// Catalog URNs (spec: project_datahub_resolvable_urns_catalog_only)
const CATALOG_TITLE_URN = IMAZON_URNS.titleMaster;
const CATALOG_EDITIONS_URN = IMAZON_URNS.editions;

// The YAML recipe to paste into the editor (matching api-wired payload exactly).
// spec: USE_CASE_en.md §UC1 Case 2 — ACTIVE_CUSTOM_MANAGED, catalog schema, secret ref.
const RECIPE_YAML = `source:
  type: postgres
  config:
    host_port: ${PG_HOST_PORT}
    database: example_db
    username: postgres
    password: \${dummy-data-pg__password}
    env: DEV
    schema_pattern:
      allow:
        - "^catalog$"
`;

// Write flows require Editor/Admin. Runs under the admin project only —
// enforced by the filename convention in playwright.config.ts (default
// *.spec.ts → admin), which supplies the admin storageState. Do not override
// storageState here. spec: spec/TESTING.md §E2E §Authentication — "Playwright projects are
// keyed on role (admin / editor / reader); role-gated tests select the matching project."

// ── Per-test state ────────────────────────────────────────────────────────

let sourceId: string | null = null;

// ── Secret provisioning: create-if-absent before any test step ────────────
// spec: feature/SECRET_RESOLUTION.md §Reference-only model — out-of-band authoring
// spec: spec/TESTING.md §E2E §Execution discipline — "Setup is idempotent and lives in hooks":
// state-mutating setup "belongs in `beforeAll` / `beforeEach` / fixtures, never inline in a
// step that also asserts".
//
// Provisions dataspoke-source-cred-dummy-data-pg idempotently via kubectl
// (--dry-run=client -o yaml | apply -f - is idempotent on repeated runs).
// Skips cleanly only when DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD is unset.

let _skipReason: string | null = null;

test.beforeAll(async () => {
  const password = process.env["DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD"] ?? "";
  if (!password) {
    _skipReason =
      "DATASPOKE_DEV_DUMMY_DATA_POSTGRES_PASSWORD is not set. " +
      "Source helm-charts/.env.dev before running this test. " +
      "spec: feature/SECRET_RESOLUTION.md §Admin authoring guide.";
    return;
  }

  // Resolve namespace from env (mirrors _resolve_namespace() in util/k8s.py).
  // required, not defaulted — a baked example namespace would silently target the
  // wrong namespace.
  const namespace = required("DATASPOKE_KUBE_DATASPOKE_NAMESPACE");

  // Idempotent kubectl create: --dry-run=client -o yaml | apply -f -
  // "apply" is idempotent; the --dry-run+pipe pattern avoids the "already exists"
  // error path. spawnSync with ["sh", "-c", ...] avoids the boolean shell: option
  // that causes TS overload resolution to fail with execSync.
  // spec: spec/feature/SECRET_RESOLUTION.md §Admin authoring guide.
  // spec: spec/TESTING.md §E2E §Execution discipline — "Cluster-side setup reuses the existing
  //   tooling": provisioning a source-credential Secret "shells out to `kubectl`"; "E2E adds no
  //   TypeScript Kubernetes client".
  const cmd =
    `kubectl create secret generic dataspoke-source-cred-dummy-data-pg` +
    ` --from-literal=password=${password}` +
    ` -n ${namespace}` +
    ` --dry-run=client -o yaml | kubectl apply -f -`;
  const result = spawnSync("sh", ["-c", cmd], { encoding: "utf-8" });
  if (result.status !== 0) {
    _skipReason =
      `kubectl failed to provision dataspoke-source-cred-dummy-data-pg: ` +
      `${result.stderr ?? result.error}. ` +
      "Ensure kubectl is on PATH and the cluster is reachable.";
  }
});

test.beforeEach(async ({ adminApi }) => {
  if (_skipReason) {
    test.skip(true, _skipReason);
  }
  // Re-derive sourceId from durable backend state when module state was lost
  // (each worker/spec file may start fresh). The source is identified by its
  // unique SOURCE_NAME among ACTIVE_CUSTOM_MANAGED sources. If it cannot be
  // found, leave sourceId null and let the dependent step proceed (no skip).
  if (sourceId === null) {
    const r = await adminApi.get(
      "/api/v1/spoke/ingestion/sources?mode=ACTIVE_CUSTOM_MANAGED&limit=100"
    );
    if (r.ok()) {
      const b = (await r.json()) as { sources?: Array<{ id: string; name: string }> };
      const f = (b.sources ?? []).find((s) => s.name === SOURCE_NAME);
      if (f) sourceId = f.id;
    }
  }
});

// Group-retry idempotency: on a serial group-retry the source created in the
// failed attempt may still exist (afterAll runs once after the whole group, not
// per failed attempt), so the step-1 UI create would 409 on a duplicate name.
// Pre-delete any source matching SOURCE_NAME once before the group runs so the
// re-create lands cleanly with a 201. (beforeAll runs again on a group-retry.)
test.beforeAll(async ({ adminApi }) => {
  if (_skipReason) return;
  const r = await adminApi.get(
    "/api/v1/spoke/ingestion/sources?mode=ACTIVE_CUSTOM_MANAGED&limit=100"
  );
  if (r.ok()) {
    const b = (await r.json()) as { sources?: Array<{ id: string; name: string }> };
    for (const s of (b.sources ?? []).filter((x) => x.name === SOURCE_NAME)) {
      await adminApi.delete(`/api/v1/spoke/ingestion/sources/${s.id}`);
    }
  }
  sourceId = null;
});

// ── Cleanup: delete the created source after all steps ────────────────────

test.afterAll(async ({ adminApi }) => {
  if (sourceId) {
    await adminApi.delete(`/api/v1/spoke/ingestion/sources/${sourceId}`);
    sourceId = null;
  }
});

// Serial mode: the steps below form one ordered, stateful scenario (each step
// depends on module state + backend resources established by the prior step).
// In serial mode the file's tests run as one group; if a step fails, the WHOLE
// group is retried together — re-running every step in order and re-establishing
// both module state and backend state. The create step is idempotent across a
// group-retry: beforeEach re-derives sourceId and pre-deletes any leftover source
// by name, so the UI create lands cleanly on retry.
// spec: spec/TESTING.md §E2E §Execution discipline — "Ordered scenarios run serial…
// Playwright retries a failed serial group from the first step, so a file either makes
// every step re-runnable or sets `retries: 0`".
test.describe.configure({ mode: "serial" });

// ─────────────────────────────────────────────────────────────────────────────
// Step 1 — Create ACTIVE_CUSTOM_MANAGED source via /ingestion/sources/new
// spec: USE_CASE_en.md §UC1 Case 2 step 1
// spec: FRONTEND_INGESTION.md §Create View — mode selector, name field, YAML editor, Submit
// ─────────────────────────────────────────────────────────────────────────────
test("UC1 Case 2 step 1 — create ACTIVE_CUSTOM_MANAGED postgres source", async ({
  page,
  adminApi,
}) => {
  // Navigate to the Create source page.
  await page.goto("/ingestion/sources/new");
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI gesture: mode selector → ACTIVE_CUSTOM_MANAGED (default; verify rendered) --
  // spec: FRONTEND_INGESTION.md §Create View — mode selector (ACTIVE_CUSTOM_MANAGED / PASSIVE)
  // The mode selector has id="create-mode" in the page source.
  await expect(page.locator("#create-mode")).toBeVisible();

  // -- UI gesture: name field --
  // spec: FRONTEND_INGESTION.md §Create View — name field required
  await page.locator("#create-name").fill(SOURCE_NAME);

  // Schedule dropdown — select "daily" (resolves to cron '0 0 * * *')
  // spec: USE_CASE_en.md §UC1 Case 2 — "schedule: '0 0 * * *'"
  // spec: FRONTEND_INGESTION.md §Create View — schedule selector (hourly/daily/weekly)
  // The schedule field has id="create-schedule"; select "daily" which is the default.
  // We verify the trigger exists, then use selectOption on the underlying select.
  const scheduleTrigger = page.locator("#create-schedule");
  await expect(scheduleTrigger).toBeVisible();
  // The schedule selector is a Radix Select — click the trigger, then pick the "daily" item.
  await scheduleTrigger.click();
  await page.getByRole("option", { name: "daily" }).click();

  // -- UI gesture: paste YAML recipe into the textarea editor --
  // spec: FRONTEND_INGESTION.md §Create View — YAML recipe editor (RecipeYamlEditor)
  // The Textarea in RecipeYamlEditor carries aria-label="recipe YAML"; use getByLabel to
  // distinguish it from the name <Input> (also a textbox) that is also on this page.
  // fill() clears the textarea before writing; no selectAll() needed.
  const recipeEditor = page.getByLabel("recipe YAML");
  await recipeEditor.fill(RECIPE_YAML);

  // -- UI gesture: click Save (in the RecipeYamlEditor the submit button is "Save") --
  // spec: FRONTEND_INGESTION.md §Create View — RecipeYamlEditor Save button triggers POST
  await page.getByRole("button", { name: "Save" }).click();

  // -- UI assertion: redirect to source detail page --
  // spec: FRONTEND_INGESTION.md §Create View — on success, redirect to /ingestion/sources/[id]
  // Exclude the create page itself (/sources/new) so a failed create (which stays
  // on /new) is caught here instead of silently matching the loose pattern.
  await page.waitForURL(/\/ingestion\/sources\/(?!new$)[^/]+$/, { timeout: 30_000 });

  // Capture the source id from the URL for subsequent steps.
  const url = page.url();
  const idMatch = /\/ingestion\/sources\/([^/?#]+)$/.exec(url);
  expect(idMatch, "Expected source ID in URL after redirect").toBeTruthy();
  sourceId = decodeURIComponent(idMatch![1]!);

  // -- UI assertion: toast "Source created" --
  // spec: FRONTEND_INGESTION.md §Create View — toast on success
  // The toast may auto-dismiss quickly; we check for the heading text or the source name.
  // The detail page header renders the source name as an h1.
  await expect(page.getByRole("heading", { name: SOURCE_NAME })).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: mode badge ACTIVE_CUSTOM_MANAGED visible --
  // spec: FRONTEND_INGESTION.md §Source Detail — mode badge rendered in header
  // modeLabel("ACTIVE_CUSTOM_MANAGED") === "Active"
  // (ingestion-mode-variant.ts modeLabel switch, line 35)
  await expect(page.getByText("Active", { exact: true })).toBeVisible();

  // -- UI assertion: recipe YAML shows masked secret ref, not plaintext --
  // spec: FRONTEND_INGESTION.md §Source Detail §Recipe — secrets masked in YAML view
  await expect(page.getByText(SECRET_REF)).toBeVisible();

  // -- Backend probe (dual confirmation): GET /spoke/ingestion/sources/{id} --
  // spec: USE_CASE_en.md §UC1 Case 2 — POST → 201; body.mode=ACTIVE_CUSTOM_MANAGED,
  //   body.schedule='0 0 * * *', NO schedule_tier, recipe.password == secret ref verbatim.
  const getResp = await adminApi.get(`/api/v1/spoke/ingestion/sources/${sourceId}`);
  expect(getResp.status()).toBe(200);
  const source = (await getResp.json()) as {
    id: string;
    mode: string;
    name: string;
    schedule: string | null;
    recipe: { source: { config: { password?: string } } };
    status: string;
  };
  expect(source.mode).toBe("ACTIVE_CUSTOM_MANAGED");
  expect(source.name).toBe(SOURCE_NAME);
  expect(source.schedule).toBe("0 0 * * *");
  // spec: API.md §Ingestion §Source body shape — no schedule_tier on the wire
  expect(Object.keys(source)).not.toContain("schedule_tier");
  // spec: USE_CASE_en.md §UC1 Case 2 — password stored as masked ref verbatim
  expect(source.recipe?.source?.config?.password).toBe(SECRET_REF);
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 2 — Dry run: connection check, no datasets emitted
// spec: USE_CASE_en.md §UC1 Case 2 step 3
// spec: FRONTEND_INGESTION.md §Source Detail §Run — dry_run toggle + Run button
// ─────────────────────────────────────────────────────────────────────────────
test("UC1 Case 2 step 2 — dry_run emits nothing", async ({ page, adminApi }) => {
  // Budget: the dry run's result panel is given 60s to appear — the whole project ceiling
  // on its own — after a 15s header wait, plus the backend dry-run probe that follows.
  test.setTimeout(180_000);

  // Navigate to source detail.
  await page.goto(`/ingestion/sources/${encodeURIComponent(sourceId!)}`);
  await expect(page.getByRole("heading", { name: SOURCE_NAME })).toBeVisible({ timeout: 15_000 });

  // -- UI gesture: enable dry_run checkbox --
  // spec: FRONTEND_INGESTION.md §Source Detail §Run — Checkbox id="ingestion-dry-run"
  const dryRunCheckbox = page.locator("#ingestion-dry-run");
  await expect(dryRunCheckbox).toBeVisible();
  await dryRunCheckbox.check();

  // -- UI gesture: click "Dry Run" button --
  // spec: FRONTEND_INGESTION.md §Source Detail §Run — Run panel button label changes to "Dry Run"
  await page.getByRole("button", { name: "Dry Run" }).click();

  // -- UI assertion: run result appears (run_id rendered, status not "fail") --
  // spec: FRONTEND_INGESTION.md §Source Detail §Run — result panel shows run_id and status badge
  // The run panel renders a badge with status text and a "run_id" mono span.
  // We use a testid here because the status badge variant and run_id span have no unique
  // semantic anchors -- the badge text is the status value itself (e.g. "success").
  // spec: TESTING.md §E2E §Selectors — prefer user-facing locators; add "a `data-testid` to a
  //   component only where a semantic locator is insufficient (recharts widgets, dynamic table
  //   rows, status badges)".
  // REQUIRED data-testid: ingestion-run-panel (component: IngestionRunPanel, element: result container)
  // For now we wait for the run_id span text "run_id" to appear as a proxy for the result panel.
  await expect(page.getByText(/^run_id\s/)).toBeVisible({ timeout: 60_000 });

  // -- Backend probe: POST /sources/{id}/method/run?dry_run=true --
  // spec: USE_CASE_en.md §UC1 Case 2 step 3 — dry_run discovers but emits nothing
  // spec: API.md §method/run — discovered_urns present on dry-run; emitted_urns empty
  const dryRunResp = await adminApi.post(
    `/api/v1/spoke/ingestion/sources/${sourceId}/method/run?dry_run=true`
  );
  expect(dryRunResp.status()).toBe(200);
  const dryBody = (await dryRunResp.json()) as {
    run_id: string;
    status: string;
    detail: {
      dry_run: boolean;
      discovered_urns: string[];
      discovered_urns_count: number;
      emitted_urns: string[];
      emitted_urns_count: number;
    };
  };
  expect(dryBody.detail.dry_run).toBe(true);
  // Dry-run discovers the catalog datasets (the "would emit" plan).
  expect(dryBody.detail.discovered_urns_count).toBeGreaterThanOrEqual(2);
  expect(dryBody.detail.discovered_urns).toContain(CATALOG_TITLE_URN);
  expect(dryBody.detail.discovered_urns).toContain(CATALOG_EDITIONS_URN);
  // Dry-run emits nothing.
  expect(dryBody.detail.emitted_urns_count).toBe(0);
  expect(dryBody.detail.emitted_urns).toEqual([]);
  const failStatuses = new Set(["fail", "failed", "failure", "error", "errored"]);
  expect(failStatuses.has(dryBody.status.toLowerCase())).toBe(false);
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 3 — Real run: emits catalog datasets to DataHub
// spec: USE_CASE_en.md §UC1 Case 2 step 4
// spec: FRONTEND_INGESTION.md §Source Detail §Run — dry_run OFF, Run button
// ─────────────────────────────────────────────────────────────────────────────
test("UC1 Case 2 step 3 — real run emits ≥ 2 catalog datasets", async ({ page, adminApi }) => {
  // Budget: the real run's result panel is given 120s to appear, plus a 15s header wait
  // and a second real run fired as the backend probe.
  test.setTimeout(300_000);

  await page.goto(`/ingestion/sources/${encodeURIComponent(sourceId!)}`);
  await expect(page.getByRole("heading", { name: SOURCE_NAME })).toBeVisible({ timeout: 15_000 });

  // -- UI gesture: ensure dry_run is unchecked (may be checked from previous navigate) --
  const dryRunCheckbox = page.locator("#ingestion-dry-run");
  await expect(dryRunCheckbox).toBeVisible();
  // Uncheck if checked
  if (await dryRunCheckbox.isChecked()) {
    await dryRunCheckbox.uncheck();
  }

  // -- UI gesture: click "Run" button --
  // spec: FRONTEND_INGESTION.md §Source Detail §Run — label is "Run" when dry_run is off
  // exact: true so this never substring-matches another button's accessible name.
  await page.getByRole("button", { name: "Run", exact: true }).click();

  // -- UI assertion: run_id appears in result panel --
  await expect(page.getByText(/^run_id\s/)).toBeVisible({ timeout: 120_000 });

  // -- Backend probe: POST /sources/{id}/method/run (no dry_run) --
  // spec: USE_CASE_en.md §UC1 Case 2 step 4 — real run: emitted_urns_count >= 2
  // spec: API.md §method/run — emitted_urns ⊆ discovered_urns; both populated on a real run
  const runResp = await adminApi.post(`/api/v1/spoke/ingestion/sources/${sourceId}/method/run`);
  expect(runResp.status()).toBe(200);
  const runBody = (await runResp.json()) as {
    run_id: string;
    status: string;
    detail: {
      dry_run: boolean;
      discovered_urns: string[];
      discovered_urns_count: number;
      emitted_urns: string[];
      emitted_urns_count: number;
    };
  };
  expect(runBody.detail.dry_run).toBe(false);
  expect(runBody.detail.discovered_urns_count).toBeGreaterThanOrEqual(2);
  expect(runBody.detail.emitted_urns_count).toBeGreaterThanOrEqual(2);
  // emitted_urns ⊆ discovered_urns.
  const discovered = new Set(runBody.detail.discovered_urns);
  for (const u of runBody.detail.emitted_urns) {
    expect(discovered.has(u)).toBe(true);
  }
  const failStatuses = new Set(["fail", "failed", "failure", "error", "errored"]);
  expect(failStatuses.has(runBody.status.toLowerCase())).toBe(false);
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 4 — Datasets panel: ≥ 2 catalog rows with derivation=emitted
// spec: USE_CASE_en.md §UC1 Case 2 step 5
// spec: FRONTEND_INGESTION.md §Source Detail §Datasets — SourceDatasetTable
// ─────────────────────────────────────────────────────────────────────────────
test("UC1 Case 2 step 4 — datasets panel shows catalog rows with emitted derivation", async ({
  page,
  adminApi,
}) => {
  // Budget: a 180s ES-lag readiness poll chained with the 120s toPass render block.
  test.setTimeout(360_000);

  // Readiness poll: datasets here are derived from DataHub ES, which lags the
  // real run by ~2-3 min. Poll the backend until the catalog dataset URN is
  // present before navigating + asserting the UI table.
  // spec: TESTING.md §E2E §Execution discipline — "Never sleep for a fixed duration"; a
  //   hand-rolled polling loop "declares its deadline and asserts the awaited condition
  //   after the loop". §Execution discipline also requires gating a data-dependent UI
  //   assertion on confirmed backend state.
  const deadline = Date.now() + 180_000;
  let datasetsReady = false;
  while (Date.now() < deadline) {
    const r = await adminApi.get(`/api/v1/spoke/ingestion/sources/${sourceId}/datasets`);
    if (r.ok()) {
      const body = (await r.json()) as { datasets: Array<{ dataset_urn: string }> };
      const urns = new Set(body.datasets.map((d) => d.dataset_urn));
      if (urns.has(CATALOG_TITLE_URN)) {
        datasetsReady = true;
        break;
      }
    }
    await new Promise((res) => setTimeout(res, 3_000));
  }
  expect(datasetsReady, "catalog dataset URN not present in /datasets within deadline").toBe(true);

  // Navigate + assert the data-dependent UI, retrying the whole block to absorb
  // residual client-side render lag after the backend is ready.
  await expect(async () => {
    await page.goto(`/ingestion/sources/${encodeURIComponent(sourceId!)}`);
    await expect(page.getByRole("heading", { name: SOURCE_NAME })).toBeVisible({ timeout: 10_000 });

    // -- UI assertion: Datasets section visible --
    await expect(page.getByRole("heading", { name: "Datasets" })).toBeVisible({ timeout: 10_000 });

    // -- UI assertion: catalog URNs appear in the datasets table --
    // spec: FRONTEND_INGESTION.md §Source Detail §Datasets — SourceDatasetTable renders URN links
    // The SourceDatasetTable renders each dataset_urn as a link with the URN text.
    // We check for the catalog.title_master URN specifically (the most discriminating).
    await expect(page.getByText(CATALOG_TITLE_URN, { exact: false })).toBeVisible({ timeout: 10_000 });

    // -- UI assertion: authority badge "high (emitted)" appears at least once --
    // spec: FRONTEND_INGESTION.md §Source Detail §Datasets — authority rendered as "high (emitted)"
    // Multiple catalog rows render this badge; assert at least one is present.
    await expect(page.getByText("high (emitted)").first()).toBeVisible({ timeout: 10_000 });
  }).toPass({ timeout: 120_000, intervals: [2_000, 3_000, 5_000, 10_000] });

  // -- Backend probe: GET /sources/{id}/datasets --
  // spec: USE_CASE_en.md §UC1 Case 2 step 5 — dataset_urns subset includes catalog tables;
  //   derivation='emitted', authority='high' for real-run rows.
  const datasetsResp = await adminApi.get(`/api/v1/spoke/ingestion/sources/${sourceId}/datasets`);
  expect(datasetsResp.status()).toBe(200);
  const datasetsBody = (await datasetsResp.json()) as {
    datasets: Array<{ dataset_urn: string; derivation: string; authority: string }>;
    total_count: number;
  };
  const urns = new Set(datasetsBody.datasets.map((d) => d.dataset_urn));
  expect(urns.size).toBeGreaterThanOrEqual(2);
  // Catalog tables must be present.
  expect(urns.has(CATALOG_TITLE_URN)).toBe(true);
  expect(urns.has(CATALOG_EDITIONS_URN)).toBe(true);
  // All rows must have derivation='emitted' with authority='high'.
  for (const d of datasetsBody.datasets) {
    if (d.derivation === "emitted") {
      expect(d.authority).toBe("high");
    }
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 5 — Events panel: INGESTION.COMPLETE event for the real run
// spec: USE_CASE_en.md §UC1 Case 2 step 6
// spec: FRONTEND_INGESTION.md §Source Detail §Events — IngestionEventTable
// ─────────────────────────────────────────────────────────────────────────────
test("UC1 Case 2 step 5 — events panel shows INGESTION.COMPLETE for the real run", async ({
  page,
  adminApi,
}) => {
  await page.goto(`/ingestion/sources/${encodeURIComponent(sourceId!)}`);
  await expect(page.getByRole("heading", { name: SOURCE_NAME })).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: Events section visible --
  await expect(page.getByRole("heading", { name: "Events" })).toBeVisible();

  // -- UI assertion: INGESTION.COMPLETE event visible in the event table --
  // spec: FRONTEND_INGESTION.md §Source Detail §Events — event_type rendered as text
  // Multiple runs (dry + real) log events; assert at least one is present.
  // No re-mount loop: the source-detail events query resolves its preset window open
  // above (`from` only, `to` omitted), so an event booked a step earlier is inside the
  // window however long ago the page mounted. The run completed behind a bounded
  // readiness poll, so its event is already confirmed present in the backend; what
  // remains is the panel's own 15 s poll tick, waited out in place.
  // spec: FRONTEND_BASIC.md §shared-component-notes (RangePicker) — "A preset resolves
  //   to an open-ended window — the lower bound only, with `to`/`until` omitted — so the
  //   read always reaches the present, which is what lets a 15 s-polled panel (see Live
  //   Updates) surface records written after page load."
  // spec: TESTING.md §E2E §Execution discipline — "Never sleep for a fixed duration":
  //   wait with a bounded construct — "expect(locator).toBeVisible({ timeout })".
  await expect(page.getByText("INGESTION.COMPLETE").first()).toBeVisible({
    timeout: 30_000,
  });

  // -- Backend probe: GET /sources/{id}/event → INGESTION.COMPLETE with status='success' --
  // spec: USE_CASE_en.md §UC1 Case 2 step 6 — INGESTION.COMPLETE carries status='success'
  const eventResp = await adminApi.get(`/api/v1/spoke/ingestion/sources/${sourceId}/event`);
  expect(eventResp.status()).toBe(200);
  const eventBody = (await eventResp.json()) as {
    events: Array<{ event_type: string; status: string; detail?: Record<string, unknown> }>;
  };
  const completeEvent = eventBody.events.find((e) => e.event_type === "INGESTION.COMPLETE");
  expect(completeEvent, "INGESTION.COMPLETE event must be present after real run").toBeTruthy();
  expect(completeEvent!.status).toBe("success");
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 6 — Per-dataset reverse-lookup: /data/<catalog.title_master urn>
// spec: USE_CASE_en.md §UC1 Case 2 step 7
// spec: FRONTEND_INGESTION.md §Per-dataset reverse-lookup (moved to /data/[urn])
// ─────────────────────────────────────────────────────────────────────────────
test("UC1 Case 2 step 6 — per-dataset reverse-lookup shows owning source", async ({
  page,
  adminApi,
}) => {
  // Budget: a 180s ES-lag readiness poll chained with the 120s toPass render block.
  test.setTimeout(360_000);

  const encodedUrnPre = encodeURIComponent(CATALOG_TITLE_URN);

  // Readiness poll: the reverse-lookup attr/ingestion is keyed off the emitted
  // dataset in DataHub ES (~2-3 min lag). Poll until source_id is resolved (and
  // matches this source) before navigating + asserting the Ingestion panel.
  // spec: TESTING.md §E2E §Execution discipline — "Never sleep for a fixed duration"; a
  //   hand-rolled polling loop "declares its deadline and asserts the awaited condition
  //   after the loop". §Execution discipline also requires gating a data-dependent UI
  //   assertion on confirmed backend state.
  const deadline = Date.now() + 180_000;
  let reverseReady = false;
  while (Date.now() < deadline) {
    const r = await adminApi.get(
      `/api/v1/spoke/common/data/${encodedUrnPre}/attr/ingestion`
    );
    if (r.ok()) {
      const body = (await r.json()) as { source_id: string | null };
      if (body.source_id !== null && body.source_id === sourceId) {
        reverseReady = true;
        break;
      }
    }
    await new Promise((res) => setTimeout(res, 3_000));
  }
  expect(reverseReady, "reverse-lookup source_id not resolved within deadline").toBe(true);

  // Navigate to the unified per-dataset hub; the reverse-lookup folds into the
  // Ingestion summary card (no standalone Ingestion panel). Retry the whole block
  // to absorb residual client-side render lag after the backend is ready.
  // spec: FRONTEND_BASIC.md §Per-dataset page; FRONTEND_INGESTION.md §Per-dataset reverse-lookup
  await expect(async () => {
    await page.goto(`/data/${encodeURIComponent(CATALOG_TITLE_URN)}`);
    await expect(page).not.toHaveURL(/\/login/);

    // -- UI assertion: the owning-source link is visible in the Ingestion summary card --
    // The IngestionSummaryCard renders the resolved source name as a Link to the
    // source detail page, so the link's visibility proves the reverse-lookup resolved.
    // spec: FRONTEND_BASIC.md §Per-dataset page — Ingestion summary card (owning-source link).
    await expect(page.getByRole("link", { name: SOURCE_NAME })).toBeVisible({ timeout: 10_000 });

    // -- UI assertion: mode badge "Active" visible (modeLabel("ACTIVE_CUSTOM_MANAGED") === "Active") --
    // ingestion-mode-variant.ts modeLabel switch, line 35
    await expect(page.getByText("Active", { exact: true }).first()).toBeVisible({ timeout: 10_000 });
  }).toPass({ timeout: 120_000, intervals: [2_000, 3_000, 5_000, 10_000] });

  // -- Backend probe: GET /spoke/common/data/{urn}/attr/ingestion --
  // spec: USE_CASE_en.md §UC1 Case 2 step 7 — source_id matches, mode=ACTIVE_CUSTOM_MANAGED,
  //   latest_run.status='success'
  const encodedUrn = encodeURIComponent(CATALOG_TITLE_URN);
  const reverseResp = await adminApi.get(
    `/api/v1/spoke/common/data/${encodedUrn}/attr/ingestion`
  );
  expect(reverseResp.status()).toBe(200);
  const reverseBody = (await reverseResp.json()) as {
    source_id: string | null;
    mode: string | null;
    dataset_urn: string;
    latest_run: { status: string } | null;
  };
  expect(reverseBody.source_id).toBe(sourceId);
  expect(reverseBody.mode).toBe("ACTIVE_CUSTOM_MANAGED");
  expect(reverseBody.dataset_urn).toBe(CATALOG_TITLE_URN);
  expect(reverseBody.latest_run?.status).toBe("success");
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 7 — Cleanup: delete source via ConfirmDialog
// spec: FRONTEND_INGESTION.md §Source Detail — Delete button → ConfirmDialog
// ─────────────────────────────────────────────────────────────────────────────
test("UC1 Case 2 step 7 — delete source; source gone from list", async ({
  page,
  adminApi,
}) => {
  await page.goto(`/ingestion/sources/${encodeURIComponent(sourceId!)}`);
  await expect(page.getByRole("heading", { name: SOURCE_NAME })).toBeVisible({ timeout: 15_000 });

  // -- UI gesture: click Delete button (triggers ConfirmDialog) --
  // spec: FRONTEND_INGESTION.md §Source Detail — Delete behind ConfirmDialog
  await page.getByRole("button", { name: "Delete" }).click();

  // -- UI gesture: confirm in the ConfirmDialog --
  // spec: FRONTEND_BASIC.md §ConfirmDialog — the confirm button has label matching confirmLabel
  // The ConfirmDialog in this page has confirmLabel="Delete".
  await page.getByRole("button", { name: "Delete", exact: true }).last().click();

  // -- UI assertion: redirected to /ingestion/conf list (source list moved to /ingestion/conf) --
  await page.waitForURL(/\/ingestion\/conf$/, { timeout: 30_000 });

  // -- UI assertion: source name no longer visible in list --
  await expect(page.getByText(SOURCE_NAME)).not.toBeVisible({ timeout: 10_000 });

  // -- Backend probe: GET /sources/{id} → 404 (or error_code INGESTION_SOURCE_NOT_FOUND) --
  // spec: USE_CASE_en.md §UC1 Case 2 cleanup
  const deleteCheckResp = await adminApi.get(`/api/v1/spoke/ingestion/sources/${sourceId}`);
  expect(deleteCheckResp.status()).toBe(404);

  // Mark as cleaned up so afterAll does not double-delete.
  sourceId = null;
});
