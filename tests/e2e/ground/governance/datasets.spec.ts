/**
 * Ground spec — Governance Dataset catalog page /governance/datasets.
 *
 * The spot-tier analogue: narrow, single-concern UI checks of the cross-feature
 * dataset list and its navigation from the Governance sidebar group.
 *
 * One concern per test:
 *   1. Navigate from the Governance sidebar group → Datasets lands on
 *      /governance/datasets with the five column headers (incl. validation).
 *   2. The dataset_urn cell links to the per-dataset hub /data/[urn], and the
 *      datahub cell exposes an external DataHub deep-link (datahubUrl configured
 *      in the dev cluster). Dual-confirmed against GET /spoke/common/data.
 *
 * Data setup: global-setup runs --reset-seed (seeded Imazon baseline). The
 * dataset_registry starts EMPTY after the reset and is populated only by the
 * ingestion sync sweep, so the beforeAll below sync-polls until the catalog
 * datasets are registered before the read-only assertions run. No cleanup required.
 *
 * spec: spec/feature/FRONTEND_GOVERNANCE.md §Datasets — table (dataset_urn →
 *   /data/[urn], datahub → external, ingestion → covering sources, validation →
 *   Covered/Uncovered, metagen → confs) + Governance sidebar entry.
 * spec: spec/API.md §Data Resource — GET /spoke/common/data (collection root).
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group; one concern per test.
 */

import { test, expect, IMAZON_URNS } from "../../fixtures/index";
import { apiBaseUrl } from "../../fixtures/env";

const DATASETS_URL = "/governance/datasets";

// ── Registry sync preflight ─────────────────────────────────────────────────────
// dataset_registry starts EMPTY after the reset; POST /internal/activities/
// ingestion/sync is its sole writer (reconciles from DataHub). DataHub ES indexing
// lags ~2-3 min after reset-seed, so re-trigger sync each iteration until the
// catalog datasets are registered (180s budget / 5s interval) before any test runs.
// spec: project_es_indexing_lag_after_reset_seed — ES lag ~2-3 min after seed.
// spec: tests/e2e/use-case/uc1-01-datahub-managed.spec.ts:270-304 — sync-poll pattern.
test.beforeAll(async ({ adminApi }) => {
  const base = apiBaseUrl();
  const token = process.env["DATASPOKE_TEST_INTERNAL_TOKEN"] ?? "";
  const deadline = Date.now() + 180_000;
  let registered = false;
  while (Date.now() < deadline) {
    await fetch(`${base}/internal/activities/ingestion/sync`, {
      method: "POST",
      headers: { "X-Internal-Token": token, "Content-Type": "application/json" },
    }).catch(() => {});

    const resp = await adminApi.get("/api/v1/spoke/common/data?limit=500");
    if (resp.ok()) {
      const body = (await resp.json()) as { datasets: { dataset_urn: string }[] };
      if (body.datasets.some((d) => d.dataset_urn === IMAZON_URNS.titleMaster)) {
        registered = true;
        break;
      }
    }
    await new Promise((r) => setTimeout(r, 5_000));
  }
  expect(
    registered,
    `Expected ${IMAZON_URNS.titleMaster} to be registered in GET /spoke/common/data ` +
      "within 180s. dataset_registry is populated by ingestion sync; ES indexing may " +
      "lag ~2-3 min. spec: feature/BACKEND.md §Ingestion Service — Sync sweep.",
  ).toBe(true);
});

// ── Test 1 — navigate from the Governance menu ─────────────────────────────────
// spec: FRONTEND_GOVERNANCE.md §Datasets — sidebar entry under the Governance group.

test("navigates to the Datasets page from the Governance sidebar group", async ({
  page,
}) => {
  await page.goto("/governance/dashboard");
  await expect(page).not.toHaveURL(/\/login/);

  // Expand the Governance group if collapsed, then click the Datasets entry.
  const governanceToggle = page
    .getByRole("button", { name: /governance/i })
    .first();
  await expect(governanceToggle).toBeVisible({ timeout: 15_000 });
  if ((await governanceToggle.getAttribute("aria-expanded")) === "false") {
    await governanceToggle.click();
  }

  await page.getByRole("link", { name: /^datasets$/i }).click();

  // -- UI assertion: landed on /governance/datasets with the page heading --
  await page.waitForURL(/\/governance\/datasets$/, { timeout: 15_000 });
  await expect(
    page.getByRole("heading", { name: "Datasets", exact: true }),
  ).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: the five column headers render (validation added) --
  // spec: FRONTEND_GOVERNANCE.md §Datasets — columns dataset_urn, datahub,
  // ingestion, validation (Covered/Uncovered), metagen.
  for (const col of ["dataset_urn", "datahub", "ingestion", "validation", "metagen"]) {
    await expect(page.getByRole("columnheader", { name: col })).toBeVisible({
      timeout: 10_000,
    });
  }
});

// ── Test 2 — column links resolve ──────────────────────────────────────────────
// spec: FRONTEND_GOVERNANCE.md §Datasets — dataset_urn → /data/[urn]; datahub →
//   external DataHub deep-link.

test("dataset_urn links to its hub and datahub exposes a deep-link", async ({
  page,
  adminApi,
}) => {
  await page.goto(DATASETS_URL);
  await expect(page).not.toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: "Datasets", exact: true }),
  ).toBeVisible({ timeout: 15_000 });

  // -- Backend dual-confirmation: the catalog lists registered datasets --
  // spec: API.md §Data Resource — GET /spoke/common/data envelope + rows.
  const probe = await adminApi.get("/api/v1/spoke/common/data?limit=200");
  expect(probe.ok(), `catalog probe failed: ${await probe.text()}`).toBeTruthy();
  const body = (await probe.json()) as {
    total_count: number;
    datasets: { dataset_urn: string }[];
  };
  expect(body.total_count).toBeGreaterThan(0);
  const urns = body.datasets.map((d) => d.dataset_urn);
  expect(urns).toContain(IMAZON_URNS.titleMaster);

  // -- UI assertion: the title_master row links to its per-dataset hub --
  const enc = encodeURIComponent(IMAZON_URNS.titleMaster);
  const urnLink = page.getByRole("link", { name: IMAZON_URNS.titleMaster });
  await expect(urnLink.first()).toBeVisible({ timeout: 10_000 });
  await expect(urnLink.first()).toHaveAttribute("href", `/data/${enc}`);

  // -- UI assertion: a DataHub deep-link is present in the table --
  const datahubLink = page.getByRole("main").getByRole("link", { name: "DataHub" });
  await expect(datahubLink.first()).toBeVisible({ timeout: 10_000 });
  await expect(datahubLink.first()).toHaveAttribute("href", /\/dataset\//);

  // -- UI assertion: clicking the URN navigates to the per-dataset hub --
  await urnLink.first().click();
  await page.waitForURL(new RegExp(`/data/${enc.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}`), {
    timeout: 15_000,
  });
  await expect(
    page.getByRole("heading", { name: IMAZON_URNS.titleMaster, exact: true }),
  ).toBeVisible({ timeout: 15_000 });
});
