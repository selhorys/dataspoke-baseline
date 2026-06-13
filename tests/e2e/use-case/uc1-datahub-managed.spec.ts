/**
 * UC1 Case 1 — DATAHUB_MANAGED source sync: browser UI flow.
 *
 * Mirrors tests/integration/api_wired/test_uc1_datahub_managed.py step-for-step,
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
 *   5. Cleanup: delete the DataHub IngestionSource + Secret via GQL, re-run sync.
 *      Backend probe: source gone from DATAHUB_MANAGED list.
 *
 * spec: USE_CASE_en.md §UC1 Case 1
 * spec: spec/feature/FRONTEND_INGESTION.md §List View, §Source Detail (DATAHUB_MANAGED read-only)
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — dual confirmation
 */

import { test, expect } from "../fixtures/index";
import { apiBaseUrl } from "../fixtures/env";

// ── Constants (verbatim from api-wired test) ────────────────────────────────

const SECRET_NAME = "UC1_POSTGRES_PASSWORD";
const SECRET_REF = "${UC1_POSTGRES_PASSWORD}";
const PLAINTEXT_PW = "ExampleDev2024!"; // must NOT appear in any DataSpoke API response

const SCHEDULE_CRON = "0 0 * * *";

// Runs under the admin project only — enforced by the filename convention in
// playwright.config.ts (default *.spec.ts → admin), which supplies the admin
// storageState. Do not override storageState here (a relative path would resolve
// against the playwright cwd and break context creation).

// ── Skip-guard: requires a reachable DataHub GMS ─────────────────────────
// spec: test_uc1_datahub_managed.py fixture — skips when DATASPOKE_TEST_DATAHUB_GMS_URL absent.

const GMS_URL = process.env["DATASPOKE_TEST_DATAHUB_GMS_URL"] ?? "";
const GMS_TOKEN = process.env["DATASPOKE_TEST_DATAHUB_TOKEN"] ?? "";

if (!GMS_URL) {
  test.skip(true, "DATASPOKE_TEST_DATAHUB_GMS_URL not set; skipping DATAHUB_MANAGED UC1 E2E test.");
}

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
// spec: test_uc1_datahub_managed.py _managed_source_setup — createSecret + createIngestionSource
// ─────────────────────────────────────────────────────────────────────────────
test("UC1 Case 1 step 1 — seed DataHub Secret + IngestionSource", async () => {
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
// spec: test_uc1_datahub_managed.py step 2 — poll sync until source appears
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

  // -- UI assertion: a row with mode "DataHub-managed" and "read-only" badge visible --
  // spec: FRONTEND_INGESTION.md §List View — DATAHUB_MANAGED rows: mode badge + "read-only" badge
  await expect(page.getByText("DataHub-managed")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("read-only")).toBeVisible({ timeout: 10_000 });

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
  await expect(page.getByText("DataHub-managed")).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: "DataHub is the source of truth" explanatory note --
  // spec: FRONTEND_INGESTION.md §Source Detail §Recipe — DATAHUB_MANAGED: read-only note
  await expect(
    page.getByText(/DataHub is the source of truth/i)
  ).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: secret ref ${UC1_POSTGRES_PASSWORD} visible in recipe YAML (masked) --
  // spec: feature/BACKEND.md §Sync sweep step 1 — "${...} secret references preserved as-is"
  // The RecipeYamlEditor in read-only mode renders a <pre> with the masked ref highlighted.
  await expect(page.getByText(SECRET_REF)).toBeVisible({ timeout: 10_000 });

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
    await expect(page.getByText(firstUrn, { exact: false })).toBeVisible({ timeout: 30_000 });
  }
});
