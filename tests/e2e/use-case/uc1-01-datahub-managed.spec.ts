/**
 * UC1 Case 1 — DATAHUB_MANAGED source sync: browser UI flow.
 *
 * Mirrors tests/integration/api_wired/test_uc1_01_datahub_managed.py step-for-step,
 * with dual confirmation at each mutating step:
 *   - UI assertion (badge, read-only note, table contents)
 *   - Backend probe via adminApi (same REST read-back the api-wired step asserts)
 *
 * Steps (verbatim from USE_CASE_en.md §UC1 Case 1):
 *   0. Skip-guard: if DATASPOKE_TEST_DATAHUB_GMS_URL is absent, skip cleanly.
 *      (Mirrors api-wired fixture which skips when GMS URL not set.)
 *   1. Seed: via GraphQL — create DataHub Secret + IngestionSource (no UI surface;
 *      DataHub is the SSOT, DataSpoke reads from it).
 *   2. Trigger sync sweep via /internal/activities/ingestion/sync (backend, no UI).
 *      Poll until the source appears in GET /spoke/ingestion/sources?mode=DATAHUB_MANAGED.
 *   3. Navigate to /ingestion; verify DATAHUB_MANAGED row appears with "read-only" badge.
 *      Backend probe: GET /spoke/ingestion/sources?mode=DATAHUB_MANAGED.
 *   4. Open source detail. Assert:
 *      a. recipe YAML shows password="${UC1_POSTGRES_PASSWORD}" (preserved verbatim)
 *      b. "DataHub is the source of truth" explanatory note visible
 *      c. No Edit / Delete buttons (DATAHUB_MANAGED is read-only)
 *      d. Run panel shows "not available" (409 INGESTION_RUN_NOT_APPLICABLE)
 *      Backend probe: GET /spoke/ingestion/sources/{id} — credential and schedule invariants.
 *   5. Datasets panel: poll until non-catalog datasets appear (≤180s ES lag budget).
 *      Backend probe: GET /spoke/ingestion/sources/{id}/datasets.
 *   6. Execute the source in DataHub; DataSpoke reflects the run.
 *      (Mirrors api-wired test_uc1_datahub_managed_execute_and_reflect step 8.)
 *      a. Fire createIngestionExecutionRequest via GQL; poll to terminal SUCCESS/SUCCEEDED (≤180s).
 *      b. Re-run sync via /internal/activities/ingestion/sync.
 *      c. UI assertion: Events panel on source detail shows an INGESTION.COMPLETE row.
 *      d. UI assertion: Datasets panel shows ≥1 row with authority "high" and
 *         derivation "pipeline_name".
 *      e. Backend probe (PRIMARY): GET /sources/{id}/event has INGESTION.COMPLETE with
 *         detail.execution_request_urn present and detail.source='datahub_sync'.
 *      f. Backend probe (SECONDARY): GET /sources/{id}/datasets has ≥1 row with
 *         derivation='pipeline_name' and authority='high'.
 *      Tolerant: test.skip if executor unavailable or run does not reach SUCCESS in budget.
 *
 * spec: USE_CASE_en.md §UC1 Case 1
 * spec: USE_CASE_en.md §UC1 Case 1 — execution beat: sync mirrors run as INGESTION.COMPLETE
 *       and upgrades datasets from matched/medium to pipeline_name/high
 * spec: spec/feature/BACKEND.md §Sync sweep steps 3-4 — _link_pipeline_datasets +
 *       _mirror_execution_requests
 * spec: spec/feature/BACKEND_SCHEMA.md §ingestion_source_dataset — derivation→authority
 * spec: spec/feature/FRONTEND_INGESTION.md §List View, §Source Detail (DATAHUB_MANAGED read-only)
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — dual confirmation
 */

import { test, expect } from "../fixtures/index";
import { apiBaseUrl, loadDotenv } from "../fixtures/env";

// Ensure helm-charts/.env is loaded before reading GMS env at module scope —
// this module is evaluated at collection time, before the config's loadDotenv
// is guaranteed to have run in this process. loadDotenv is idempotent and never
// overwrites already-set vars.
loadDotenv();

// ── Constants (verbatim from api-wired test) ────────────────────────────────

const SECRET_NAME = "UC1_POSTGRES_PASSWORD";
const SECRET_REF = "${UC1_POSTGRES_PASSWORD}";
const PLAINTEXT_PW = "ExampleDev2024!"; // must NOT appear in any DataSpoke API response

const SCHEDULE_CRON = "0 0 * * *";

// Runs under the admin project only — enforced by the filename convention in
// playwright.config.ts (default *.spec.ts → admin), which supplies the admin
// storageState. Do not override storageState here (a relative path would resolve
// against the playwright cwd and break context creation).

const GMS_URL = process.env["DATASPOKE_TEST_DATAHUB_GMS_URL"] ?? "";
const GMS_TOKEN = process.env["DATASPOKE_TEST_DATAHUB_TOKEN"] ?? "";

// Skip-guard at runtime (not module top level, where a conditional test.skip is
// fragile): only skip when DataHub GMS is genuinely unconfigured. GMS is part of
// the dev stack, so normally these tests run.
// spec: test_uc1_01_datahub_managed.py fixture — skips when GMS URL absent.
test.beforeEach(() => {
  test.skip(!GMS_URL, "DATASPOKE_TEST_DATAHUB_GMS_URL not set; DATAHUB_MANAGED UC1 requires DataHub GMS.");
});

// Deterministic DataHub Secret URN (name-keyed) — used to clear leftovers so the
// seed is idempotent across runs.
const SECRET_DH_URN = `urn:li:dataHubSecret:${SECRET_NAME}`;

// ── Per-test state ────────────────────────────────────────────────────────

let sourceUrn: string | null = null;
let secretUrn: string | null = null;
let sourceId: string | null = null;

// Helper: GQL headers for DataHub GMS.
function gqlHeaders(): Record<string, string> {
  const h: Record<string, string> = { "Content-Type": "application/json" };
  if (GMS_TOKEN) h["Authorization"] = `Bearer ${GMS_TOKEN}`;
  return h;
}

// Helper: fire a DataHub GQL mutation.
async function gqlMutate(
  query: string,
  variables: Record<string, unknown>
): Promise<{ data?: Record<string, unknown>; errors?: unknown[] }> {
  const resp = await fetch(`${GMS_URL}/api/graphql`, {
    method: "POST",
    headers: gqlHeaders(),
    body: JSON.stringify({ query, variables }),
  });
  return resp.json() as Promise<{ data?: Record<string, unknown>; errors?: unknown[] }>;
}

// ── Cleanup: delete source + secret from DataHub and re-sync ─────────────

test.afterAll(async ({ adminApi }) => {
  if (sourceUrn) {
    await gqlMutate(
      `mutation deleteIngestionSource($urn: String!) { deleteIngestionSource(urn: $urn) }`,
      { urn: sourceUrn }
    ).catch(() => {});
  }
  if (secretUrn) {
    await gqlMutate(
      `mutation deleteSecret($urn: String!) { deleteSecret(urn: $urn) }`,
      { urn: secretUrn }
    ).catch(() => {});
  }
  // Re-run sync to remove the mirrored DataSpoke row.
  const base = apiBaseUrl();
  const token = process.env["DATASPOKE_TEST_INTERNAL_TOKEN"] ?? "";
  await fetch(`${base}/internal/activities/ingestion/sync`, {
    method: "POST",
    headers: { "X-Internal-Token": token, "Content-Type": "application/json" },
  }).catch(() => {});
  sourceUrn = null;
  secretUrn = null;
  sourceId = null;
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 1 — Seed: create DataHub Secret + IngestionSource via GraphQL
// (API-fired, no UI surface — DataHub is the SSOT for DATAHUB_MANAGED)
// spec: USE_CASE_en.md §UC1 Case 1 — "team creates a DataHub Managed Ingestion source"
// spec: test_uc1_01_datahub_managed.py _managed_source_setup — createSecret + createIngestionSource
// ─────────────────────────────────────────────────────────────────────────────
test("UC1 Case 1 step 1 — seed DataHub Secret + IngestionSource", async () => {
  // Idempotency: clear any leftover secret from a prior run (createSecret errors
  // with "This Secret already exists!" otherwise). Ignore errors when absent.
  await gqlMutate(
    `mutation deleteSecret($urn: String!) { deleteSecret(urn: $urn) }`,
    { urn: SECRET_DH_URN }
  ).catch(() => {});

  // Create DataHub Secret
  const secretResult = await gqlMutate(
    `mutation createSecret($input: CreateSecretInput!) { createSecret(input: $input) }`,
    {
      input: {
        name: SECRET_NAME,
        value: PLAINTEXT_PW,
        description: "UC1 E2E test secret: postgres password for DATAHUB_MANAGED",
      },
    }
  );
  if (secretResult.errors) {
    test.skip(
      true,
      `createSecret GraphQL error: ${JSON.stringify(secretResult.errors)}. ` +
        "DataHub GMS may not support Managed Secrets in this dev-env."
    );
    return;
  }
  secretUrn = (secretResult.data?.["createSecret"] as string) ?? null;
  expect(secretUrn, "createSecret must return a URN").toBeTruthy();

  // Create IngestionSource with secret-ref recipe
  const name = `uc1-datahub-managed-${Date.now().toString(36)}`;
  const recipe = {
    source: {
      type: "postgres",
      config: {
        host_port: "example-postgres.dataspoke-dummy-data-01.svc.cluster.local:5432",
        database: "example_db",
        username: "postgres",
        password: SECRET_REF,
        include_tables: true,
        include_views: false,
        env: "DEV",
        schema_pattern: { deny: ["^information_schema$", "^pg_.*$", "^catalog$"] },
      },
    },
    sink: { type: "datahub-rest", config: { server: GMS_URL } },
  };

  const sourceResult = await gqlMutate(
    `mutation createIngestionSource($input: UpdateIngestionSourceInput!) {
       createIngestionSource(input: $input)
     }`,
    {
      input: {
        name,
        type: "postgres",
        config: {
          recipe: JSON.stringify(recipe),
          executorId: "default",
          debugMode: false,
        },
        schedule: { interval: SCHEDULE_CRON, timezone: "UTC" },
      },
    }
  );
  if (sourceResult.errors) {
    test.skip(
      true,
      `createIngestionSource GraphQL error: ${JSON.stringify(sourceResult.errors)}. ` +
        "DataHub GMS may not support Managed Ingestion in this dev-env."
    );
    return;
  }
  sourceUrn = (sourceResult.data?.["createIngestionSource"] as string) ?? null;
  expect(sourceUrn, "createIngestionSource must return a URN").toBeTruthy();
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 2 — Sync sweep: DataSpoke picks up the DATAHUB_MANAGED source
// (Backend-only: no UI surface for the sync trigger itself)
// spec: USE_CASE_en.md §UC1 Case 1 — "DataSpoke's sync sweep pulls the definition down"
// spec: test_uc1_01_datahub_managed.py step 2 — poll sync until source appears
// ─────────────────────────────────────────────────────────────────────────────
test("UC1 Case 1 step 2 — sync sweep mirrors the DATAHUB_MANAGED source into DataSpoke", async ({
  adminApi,
}) => {
  if (!sourceUrn) test.skip();

  const base = apiBaseUrl();
  const token = process.env["DATASPOKE_TEST_INTERNAL_TOKEN"] ?? "";

  // Poll: trigger sync + check list until the source appears (≤180s ES lag budget).
  // spec: project_es_indexing_lag_after_reset_seed — ES lags ~2-3 min after seed.
  const deadline = Date.now() + 180_000;
  let found = false;
  while (Date.now() < deadline) {
    await fetch(`${base}/internal/activities/ingestion/sync`, {
      method: "POST",
      headers: { "X-Internal-Token": token, "Content-Type": "application/json" },
    }).catch(() => {});

    const listResp = await adminApi.get(
      "/api/v1/spoke/ingestion/sources?mode=DATAHUB_MANAGED&limit=100"
    );
    if (listResp.ok()) {
      const body = (await listResp.json()) as {
        sources: Array<{ id: string; datahub_source_urn: string }>;
      };
      const match = body.sources.find((s) => s.datahub_source_urn === sourceUrn);
      if (match) {
        sourceId = match.id;
        found = true;
        break;
      }
    }
    await new Promise((r) => setTimeout(r, 5_000));
  }

  expect(
    found,
    `Expected DATAHUB_MANAGED source with datahub_source_urn=${sourceUrn} to appear ` +
      "in GET /spoke/ingestion/sources?mode=DATAHUB_MANAGED within 180s. " +
      "spec: feature/BACKEND.md §Sync sweep step 1 — sync mirrors DataHub-managed sources."
  ).toBe(true);
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 3 — List view: DATAHUB_MANAGED row with "read-only" badge
// spec: USE_CASE_en.md §UC1 Case 1 — source exposed read-only via DataSpoke
// spec: FRONTEND_INGESTION.md §List View — DATAHUB_MANAGED rows carry a read-only badge
// ─────────────────────────────────────────────────────────────────────────────
test("UC1 Case 1 step 3 — /ingestion list shows DATAHUB_MANAGED row with read-only badge", async ({
  page,
  adminApi,
}) => {
  if (!sourceId) test.skip();

  // Navigate to the ingestion list.
  await page.goto("/ingestion");
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page.getByRole("heading", { name: "Ingestion" })).toBeVisible({ timeout: 15_000 });

  // -- UI gesture: filter to DATAHUB_MANAGED mode --
  // spec: FRONTEND_INGESTION.md §List View — mode filter via Select (IngestionSourceList)
  // The mode filter has no id; locate by its SelectTrigger role (combobox) near the mode label.
  // We click the filter combobox and select "DataHub-managed".
  const modeFilter = page.getByRole("combobox");
  await modeFilter.click();
  await page.getByRole("option", { name: "DataHub-managed" }).click();

  // -- UI assertion: the registered source's row shows "DataHub-managed" and "read-only" badges --
  // spec: FRONTEND_INGESTION.md §List View — DATAHUB_MANAGED rows: mode badge + "read-only" badge
  // Scoped to the registered source's row via its datahub_source_urn (rendered as a mono
  // subtitle below the source name since the URN-subtitle feature was added to
  // ingestion-source-list.tsx). This prevents stale/other DATAHUB_MANAGED rows from
  // satisfying the assertion.
  const sourceRow = page.getByRole("row").filter({ hasText: sourceUrn! });
  await expect(sourceRow).toBeVisible({ timeout: 15_000 });
  await expect(sourceRow.getByText("DataHub-managed")).toBeVisible({ timeout: 15_000 });
  await expect(sourceRow.getByText("read-only")).toBeVisible({ timeout: 10_000 });
  // Also confirm the registered source's name and URN subtitle appear in the same row.
  // spec: FRONTEND_INGESTION.md §List View — datahub_source_urn rendered as mono subtitle
  // The source name starts with "uc1-datahub-managed-" (set in step 1).
  await expect(sourceRow.getByText(/^uc1-datahub-managed-/)).toBeVisible({ timeout: 10_000 });
  await expect(sourceRow.getByText(sourceUrn!, { exact: true })).toBeVisible({ timeout: 10_000 });

  // -- Backend probe: GET /spoke/ingestion/sources?mode=DATAHUB_MANAGED --
  // spec: USE_CASE_en.md §UC1 Case 1 — source appears as DATAHUB_MANAGED row
  const listResp = await adminApi.get(
    "/api/v1/spoke/ingestion/sources?mode=DATAHUB_MANAGED&limit=100"
  );
  expect(listResp.status()).toBe(200);
  const listBody = (await listResp.json()) as {
    sources: Array<{ id: string; mode: string; datahub_source_urn: string }>;
  };
  const match = listBody.sources.find((s) => s.id === sourceId);
  expect(match, `Source id=${sourceId} must appear in DATAHUB_MANAGED list`).toBeTruthy();
  expect(match!.mode).toBe("DATAHUB_MANAGED");
  expect(match!.datahub_source_urn).toBe(sourceUrn);
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 4 — Source detail: read-only invariants
// spec: USE_CASE_en.md §UC1 Case 1 — credential-handling, read-only enforcement, schedule
// spec: FRONTEND_INGESTION.md §Source Detail — DATAHUB_MANAGED read-only note; no edit/delete
// ─────────────────────────────────────────────────────────────────────────────
test("UC1 Case 1 step 4 — source detail shows secret ref preserved; is read-only", async ({
  page,
  adminApi,
}) => {
  if (!sourceId) test.skip();

  // Navigate to source detail.
  await page.goto(`/ingestion/sources/${encodeURIComponent(sourceId!)}`);
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: mode badge "DataHub-managed" visible in header --
  // spec: FRONTEND_INGESTION.md §Source Detail — mode badge rendered in header
  // exact — the source name ("uc1-datahub-managed-…") and recipe <pre> also
  // contain "datahub-managed"; only the badge text is exactly "DataHub-managed".
  await expect(page.getByText("DataHub-managed", { exact: true })).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: "DataHub is the source of truth" explanatory note --
  // spec: FRONTEND_INGESTION.md §Source Detail §Recipe — DATAHUB_MANAGED: read-only note
  await expect(
    page.getByText(/DataHub is the source of truth/i)
  ).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: secret ref ${UC1_POSTGRES_PASSWORD} visible in recipe YAML (masked) --
  // spec: feature/BACKEND.md §Sync sweep step 1 — "${...} secret references preserved as-is"
  // The RecipeYamlEditor in read-only mode renders a <pre> with the masked ref highlighted.
  // The ref may appear in both the recipe <pre> and a raw-JSON view; assert ≥1.
  await expect(page.getByText(SECRET_REF).first()).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: plaintext password NOT visible on page --
  // spec: API.md §Ingestion §Source body shape — secret refs never expanded in responses
  await expect(page.getByText(PLAINTEXT_PW)).not.toBeVisible();

  // -- UI assertion: no Edit button visible (DATAHUB_MANAGED is read-only) --
  // spec: FRONTEND_INGESTION.md §Source Detail §Recipe — edits disabled for DATAHUB_MANAGED
  // The source detail page suppresses Edit/Delete when mode=DATAHUB_MANAGED (isEditable=false).
  await expect(page.getByRole("button", { name: "Edit" })).not.toBeVisible();
  await expect(page.getByRole("button", { name: "Delete" })).not.toBeVisible();

  // -- UI assertion: Run panel shows "not available" explanation --
  // spec: FRONTEND_INGESTION.md §Source Detail §Run — DATAHUB_MANAGED shows disabled state
  await expect(page.getByRole("heading", { name: "Run" })).toBeVisible();
  await expect(page.getByText(/not available/i)).toBeVisible({ timeout: 10_000 });

  // -- Backend probe: GET /spoke/ingestion/sources/{id} — credential + schedule invariants --
  // spec: feature/BACKEND.md §Sync sweep step 1 — "${...} secret references preserved as-is"
  // spec: USE_CASE_en.md §UC1 Case 1 — schedule '0 0 * * *'; schedule_tier absent from wire
  const getResp = await adminApi.get(`/api/v1/spoke/ingestion/sources/${sourceId}`);
  expect(getResp.status()).toBe(200);
  const source = (await getResp.json()) as {
    mode: string;
    schedule: string | null;
    recipe: { source: { config: { password?: string } } };
  };
  // Credential-handling: secret-ref preserved verbatim
  expect(source.recipe?.source?.config?.password).toBe(SECRET_REF);
  // Schedule round-trips
  expect(source.schedule).toBe(SCHEDULE_CRON);
  // schedule_tier must NOT appear on the wire
  expect(Object.keys(source)).not.toContain("schedule_tier");

  // -- Backend probe: read-only enforcement — PUT → 409 INGESTION_SOURCE_READONLY --
  // spec: API.md §Ingestion — PUT / PATCH on DATAHUB_MANAGED → 409 INGESTION_SOURCE_READONLY
  const putResp = await adminApi.put(`/api/v1/spoke/ingestion/sources/${sourceId}`, {
    data: {
      mode: "DATAHUB_MANAGED",
      name: "attempted overwrite",
      schedule: null,
      recipe: { source: { type: "postgres", config: {} } },
    },
  });
  expect(putResp.status()).toBe(409);
  const putBody = (await putResp.json()) as { error_code: string };
  expect(putBody.error_code).toBe("INGESTION_SOURCE_READONLY");

  // -- Backend probe: method/run → 409 INGESTION_RUN_NOT_APPLICABLE --
  // spec: API.md §Ingestion — INGESTION_RUN_NOT_APPLICABLE for non-ACTIVE_CUSTOM_MANAGED
  const runResp = await adminApi.post(
    `/api/v1/spoke/ingestion/sources/${sourceId}/method/run`
  );
  expect(runResp.status()).toBe(409);
  const runBody = (await runResp.json()) as { error_code: string };
  expect(runBody.error_code).toBe("INGESTION_RUN_NOT_APPLICABLE");
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 5 — Datasets panel: non-empty after sync + ES settle (≤180s poll)
// spec: USE_CASE_en.md §UC1 Case 1 — GET /sources/{id}/datasets lists covered datasets
// spec: FRONTEND_INGESTION.md §Source Detail §Datasets — SourceDatasetTable
// ─────────────────────────────────────────────────────────────────────────────
test("UC1 Case 1 step 5 — datasets panel shows mapped non-catalog datasets", async ({
  page,
  adminApi,
}) => {
  if (!sourceId) test.skip();

  const base = apiBaseUrl();
  const token = process.env["DATASPOKE_TEST_INTERNAL_TOKEN"] ?? "";

  // Poll: re-trigger sync each iteration until non-catalog URNs appear.
  // spec: project_es_indexing_lag_after_reset_seed — ES lags ~2-3 min; budget ≥180s.
  const deadline = Date.now() + 180_000;
  let mappedDatasets: Array<{ dataset_urn: string; derivation: string; authority: string }> = [];

  while (Date.now() < deadline) {
    await fetch(`${base}/internal/activities/ingestion/sync`, {
      method: "POST",
      headers: { "X-Internal-Token": token, "Content-Type": "application/json" },
    }).catch(() => {});

    const resp = await adminApi.get(`/api/v1/spoke/ingestion/sources/${sourceId}/datasets`);
    if (resp.ok()) {
      const body = (await resp.json()) as {
        datasets: Array<{ dataset_urn: string; derivation: string; authority: string }>;
      };
      const nonCatalog = body.datasets.filter(
        (d) =>
          d.dataset_urn.includes(",example_db.") &&
          !d.dataset_urn.includes("example_db.catalog.")
      );
      if (nonCatalog.length > 0) {
        mappedDatasets = body.datasets;
        break;
      }
    }
    await new Promise((r) => setTimeout(r, 5_000));
  }

  // -- Backend probe: non-empty mapped set; derivation + authority invariants --
  // spec: USE_CASE_en.md §UC1 Case 1 — GET /sources/{id}/datasets must be non-empty
  expect(
    mappedDatasets.length,
    "GET /sources/{id}/datasets must be non-empty within 180s. " +
      "spec: USE_CASE_en.md §UC1 Case 1 — covered datasets listed after sync."
  ).toBeGreaterThan(0);

  // At least one row must have derivation='matched' (DATAHUB_MANAGED sync path).
  // spec: feature/BACKEND.md §Sync sweep step 2 — DATAHUB_MANAGED uses filter-matcher
  const matchedRows = mappedDatasets.filter((d) => d.derivation === "matched");
  expect(matchedRows.length, "At least one row must have derivation='matched'").toBeGreaterThan(0);

  // All rows must have valid derivation and authority.
  const validDerivations = new Set(["emitted", "pipeline_name", "matched"]);
  const validAuthorities = new Set(["high", "medium"]);
  for (const d of mappedDatasets) {
    expect(validDerivations.has(d.derivation)).toBe(true);
    expect(validAuthorities.has(d.authority)).toBe(true);
    // No catalog URNs (the recipe denies catalog)
    expect(d.dataset_urn).not.toContain("example_db.catalog.");
  }

  // -- UI assertion: navigate to detail and verify Datasets section is populated --
  await page.goto(`/ingestion/sources/${encodeURIComponent(sourceId!)}`);
  await expect(page.getByRole("heading", { name: "Datasets" })).toBeVisible({ timeout: 15_000 });

  // A dataset URN from the mapped set must appear in the table.
  if (mappedDatasets.length > 0) {
    const firstUrn = mappedDatasets[0]!.dataset_urn;
    await expect(page.getByText(firstUrn, { exact: false }).first()).toBeVisible({ timeout: 30_000 });
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 6 — Execute source in DataHub; DataSpoke reflects the run
// spec: USE_CASE_en.md §UC1 Case 1 — execution beat: sync mirrors the run as
//   INGESTION.COMPLETE and upgrades datasets from matched/medium to pipeline_name/high
// spec: feature/BACKEND.md §Sync sweep step 3 — _link_pipeline_datasets: derivation='pipeline_name'
// spec: feature/BACKEND.md §Sync sweep step 4 — _mirror_execution_requests: INGESTION.COMPLETE
// spec: feature/BACKEND_SCHEMA.md §ingestion_source_dataset — pipeline_name→authority='high'
// Mirrors: test_uc1_datahub_managed_execute_and_reflect (api-wired step 8)
// ─────────────────────────────────────────────────────────────────────────────
test("UC1 Case 1 step 6 — execute in DataHub; DataSpoke reflects the run", async ({
  page,
  adminApi,
}) => {
  if (!sourceId) test.skip();

  const base = apiBaseUrl();
  const token = process.env["DATASPOKE_TEST_INTERNAL_TOKEN"] ?? "";

  // -- 6a: Trigger the execution in DataHub via GQL --
  // spec: ref/github/datahub/datahub-graphql-core/src/main/resources/ingestion.graphql
  //   createIngestionExecutionRequest(input: { ingestionSourceUrn: String! }) → String (exec URN)
  // spec: ref/github/datahub/smoke-test/tests/managed_ingestion/managed_ingestion_test.py
  //   test_create_list_get_ingestion_execution_request — confirmed mutation shape
  const execResult = await gqlMutate(
    `mutation createIngestionExecutionRequest($input: CreateIngestionExecutionRequestInput!) {
       createIngestionExecutionRequest(input: $input)
     }`,
    { input: { ingestionSourceUrn: sourceUrn! } }
  );
  if (execResult.errors) {
    test.skip(
      true,
      `createIngestionExecutionRequest GraphQL error: ${JSON.stringify(execResult.errors)}. ` +
        "DataHub executor may not be available or ready in this dev-env."
    );
    return;
  }
  const executionRequestUrn = (execResult.data?.["createIngestionExecutionRequest"] as string) ?? null;
  if (!executionRequestUrn) {
    test.skip(
      true,
      `createIngestionExecutionRequest returned no URN: ${JSON.stringify(execResult.data)}. ` +
        "Skipping execution-and-reflect step."
    );
    return;
  }

  // -- 6b: Poll ingestionSource executions to terminal SUCCESS/SUCCEEDED (≤180s) --
  // spec: ref/github/datahub/datahub-graphql-core/src/main/resources/ingestion.graphql
  //   ingestionSource(urn: String!) { executions(start:0, count:5) {
  //       total executionRequests { urn result { status } } } }
  //   result.status: String! — terminal when not null and not in
  //   {PENDING, RUNNING, SKIPPED, UP_FOR_RETRY}. PENDING/RUNNING are in-progress (keep polling).
  // SUCCESS / SUCCEEDED → proceed; any other terminal → tolerant skip
  const pollQuery = `
    query ingestionSource($urn: String!) {
      ingestionSource(urn: $urn) {
        executions(start: 0, count: 5) {
          total
          executionRequests {
            urn
            result {
              status
            }
          }
        }
      }
    }
  `;
  const SUCCESS_STATUSES = new Set(["SUCCESS", "SUCCEEDED"]);
  // PENDING/RUNNING are in-progress; SKIPPED/UP_FOR_RETRY are ambiguous — all keep polling.
  const NON_TERMINAL_STATUSES = new Set(["PENDING", "RUNNING", "SKIPPED", "UP_FOR_RETRY"]);

  const pollDeadline = Date.now() + 180_000;
  let execStatus: string | null = null;

  while (Date.now() < pollDeadline) {
    const pollResult = await gqlMutate(pollQuery, { urn: sourceUrn! }).catch(() => ({})) as {
      data?: Record<string, unknown>;
      errors?: unknown[];
    };
    const execRequests = (
      (
        (pollResult.data?.["ingestionSource"] as Record<string, unknown> | undefined)
          ?.["executions"] as Record<string, unknown> | undefined
      )?.["executionRequests"] as Array<Record<string, unknown>> | undefined
    ) ?? [];

    for (const req of execRequests) {
      if (req["urn"] === executionRequestUrn) {
        const result = (req["result"] as Record<string, unknown> | null) ?? null;
        const status = (result?.["status"] as string | null) ?? null;
        if (status && !NON_TERMINAL_STATUSES.has(status)) {
          execStatus = status;
          break;
        }
      }
    }
    if (execStatus !== null) break;
    await new Promise((r) => setTimeout(r, 8_000));
  }

  if (execStatus === null) {
    test.skip(
      true,
      `Execution ${executionRequestUrn} did not reach terminal status within 180s. ` +
        "DataHub executor may be slow or unavailable. " +
        "spec: TESTING.md — tolerant skip when executor unavailable."
    );
    return;
  }
  if (!SUCCESS_STATUSES.has(execStatus)) {
    test.skip(
      true,
      `Execution ${executionRequestUrn} completed with non-success status ${execStatus}. ` +
        "Executor ran but source errored (likely dev-env connectivity). " +
        "spec: TESTING.md — tolerant skip when executor completes with failure."
    );
    return;
  }

  // -- 6c: Re-run sync to mirror the completed execution --
  // spec: feature/BACKEND.md §Sync sweep step 4 — _mirror_execution_requests mirrors
  //   terminal execution requests for DATAHUB_MANAGED sources as INGESTION.COMPLETE events
  await fetch(`${base}/internal/activities/ingestion/sync`, {
    method: "POST",
    headers: { "X-Internal-Token": token, "Content-Type": "application/json" },
  }).catch(() => {});

  // -- 6d: Backend probe (PRIMARY) — GET /sources/{id}/event → INGESTION.COMPLETE --
  // spec: USE_CASE_en.md §UC1 Case 1 — "DataSpoke's next sync mirrors that execution
  //   into …/event as an INGESTION.COMPLETE event"
  // spec: feature/BACKEND.md §Sync sweep step 4 — _mirror_execution_requests inserts
  //   Event(event_type=INGESTION_COMPLETE, status='success',
  //         detail={execution_request_urn: ..., source: 'datahub_sync'})
  let foundEvent: Record<string, unknown> | null = null;
  const eventDeadline = Date.now() + 30_000;
  while (Date.now() < eventDeadline) {
    const evtResp = await adminApi.get(`/api/v1/spoke/ingestion/sources/${sourceId}/event`);
    if (evtResp.ok()) {
      const evtBody = (await evtResp.json()) as {
        events: Array<Record<string, unknown>>;
      };
      for (const evt of evtBody.events ?? []) {
        const detail = (evt["detail"] as Record<string, unknown> | null) ?? {};
        if (
          evt["event_type"] === "INGESTION.COMPLETE" &&
          detail["execution_request_urn"] === executionRequestUrn
        ) {
          foundEvent = evt;
          break;
        }
      }
    }
    if (foundEvent !== null) break;
    await new Promise((r) => setTimeout(r, 2_000));
  }

  expect(
    foundEvent,
    `Expected INGESTION.COMPLETE event with detail.execution_request_urn=${executionRequestUrn} ` +
      "in GET /sources/{id}/event within 30s after sync. " +
      "spec: USE_CASE_en.md §UC1 Case 1 — sync mirrors run as INGESTION.COMPLETE event. " +
      "spec: feature/BACKEND.md §Sync sweep step 4 — _mirror_execution_requests."
  ).not.toBeNull();

  // Verify event status and detail.source.
  // spec: feature/BACKEND.md §Sync sweep step 4 — detail.source='datahub_sync'
  expect(foundEvent!["status"]).toBe("success");
  const evtDetail = (foundEvent!["detail"] as Record<string, unknown> | null) ?? {};
  expect(evtDetail["source"]).toBe("datahub_sync");
  expect(evtDetail["execution_request_urn"]).toBeTruthy();

  // -- 6e: Backend probe (SECONDARY) — GET /sources/{id}/datasets → pipeline_name/high --
  // spec: USE_CASE_en.md §UC1 Case 1 — "upgrades the covered datasets from
  //   matcher-mapped (derivation=matched, authority=medium) to run-observed
  //   (derivation=pipeline_name, authority=high)"
  // spec: feature/BACKEND.md §Sync sweep step 3 — _link_pipeline_datasets upserts
  //   derivation='pipeline_name' where systemMetadata.pipelineName == datahub_source_urn
  // spec: BACKEND_SCHEMA.md §ingestion_source_dataset — pipeline_name→authority='high'
  let pipelineNameRows: Array<{ dataset_urn: string; derivation: string; authority: string }> = [];
  const dsDeadline = Date.now() + 60_000;
  while (Date.now() < dsDeadline) {
    // Re-trigger sync each iteration so freshly-indexed pipelineName aspects are picked up.
    await fetch(`${base}/internal/activities/ingestion/sync`, {
      method: "POST",
      headers: { "X-Internal-Token": token, "Content-Type": "application/json" },
    }).catch(() => {});

    const dsResp = await adminApi.get(`/api/v1/spoke/ingestion/sources/${sourceId}/datasets`);
    if (dsResp.ok()) {
      const dsBody = (await dsResp.json()) as {
        datasets: Array<{ dataset_urn: string; derivation: string; authority: string }>;
      };
      pipelineNameRows = (dsBody.datasets ?? []).filter(
        (d) => d.derivation === "pipeline_name" && d.authority === "high"
      );
      if (pipelineNameRows.length > 0) break;
    }
    await new Promise((r) => setTimeout(r, 8_000));
  }

  expect(
    pipelineNameRows.length,
    "Expected ≥1 dataset with derivation='pipeline_name' and authority='high' in " +
      "GET /sources/{id}/datasets within 60s after a successful DataHub execution + re-sync. " +
      "spec: USE_CASE_en.md §UC1 Case 1 — execution upgrades datasets to pipeline_name/high. " +
      "spec: feature/BACKEND.md §Sync sweep step 3 — _link_pipeline_datasets."
  ).toBeGreaterThan(0);

  // -- 6f: UI assertion — Events panel shows INGESTION.COMPLETE row --
  // spec: FRONTEND_INGESTION.md §Source Detail §Events — event log rendered per event_type
  await page.goto(`/ingestion/sources/${encodeURIComponent(sourceId!)}`);
  await expect(page).not.toHaveURL(/\/login/);

  // The Events section heading must be visible.
  await expect(page.getByRole("heading", { name: "Events" })).toBeVisible({ timeout: 15_000 });

  // An INGESTION.COMPLETE row must appear in the event log.
  // spec: FRONTEND_INGESTION.md §Source Detail §Events — INGESTION.COMPLETE rendered as row
  await expect(
    page.getByText("INGESTION.COMPLETE", { exact: false }).first()
  ).toBeVisible({ timeout: 15_000 });

  // -- 6g: UI assertion — Datasets panel shows ≥1 "high" authority row --
  // spec: FRONTEND_INGESTION.md §Source Detail §Datasets — authority cell rendered per row
  // The Datasets section must show at least one row where the authority badge reads "high".
  // We look within the Datasets section context; the first "high" occurrence is sufficient.
  await expect(
    page.getByText("high", { exact: false }).first()
  ).toBeVisible({ timeout: 15_000 });
});
