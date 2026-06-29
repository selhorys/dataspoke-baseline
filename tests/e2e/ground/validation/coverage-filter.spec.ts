/**
 * Ground spec — Validation list coverage filter /validation.
 *
 * The spot-tier analogue: narrow, single-concern UI checks of the covered/
 * uncovered checkbox filter on the cross-dataset validation list.
 *
 * One concern per test:
 *   1. Default checkbox state: covered checked, uncovered unchecked.
 *   2. With rows present (uncovered checked), unchecking BOTH boxes lists no
 *      datasets — the count(0) is load-bearing because rows existed first.
 *   3. Checking uncovered surfaces a registered-no-conf dataset row; the row set
 *      is dual-confirmed against GET /spoke/validation?coverage=uncovered.
 *
 * Data setup: global-setup runs --reset-seed (seeded Imazon baseline). The
 * dataset_registry starts EMPTY after the reset and is populated only by the
 * ingestion sync sweep, so the beforeAll below sync-polls until a registered-no-conf
 * catalog dataset is in the uncovered set before the assertions run. The covered vs
 * uncovered split is read back from the API. Read-only page; no cleanup required.
 *
 * spec: spec/feature/FRONTEND_VALIDATION.md §List — covered (default checked) /
 *   uncovered (default unchecked) checkboxes mapping to the `coverage` query param.
 * spec: spec/API.md §Validation — GET /spoke/validation coverage=covered|uncovered|both.
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group; one concern per test.
 */

import { test, expect, IMAZON_URNS } from "../../fixtures/index";
import { apiBaseUrl } from "../../fixtures/env";

const VALIDATION_URL = "/validation";

// ── Registry sync preflight ─────────────────────────────────────────────────────
// dataset_registry starts EMPTY after the reset; POST /internal/activities/
// ingestion/sync is its sole writer (reconciles from DataHub). The uncovered set is
// the registry minus confs, so it is empty until sync runs. DataHub ES indexing lags
// ~2-3 min after reset-seed, so re-trigger sync each iteration until a known
// registered-no-conf catalog dataset appears in coverage=uncovered (180s budget).
// spec: project_es_indexing_lag_after_reset_seed — ES lag ~2-3 min after seed.
// spec: tests/e2e/use-case/uc1-01-datahub-managed.spec.ts:270-304 — sync-poll pattern.
test.beforeAll(async ({ adminApi }) => {
  const base = apiBaseUrl();
  const token = process.env["DATASPOKE_TEST_INTERNAL_TOKEN"] ?? "";
  const deadline = Date.now() + 180_000;
  let ready = false;
  while (Date.now() < deadline) {
    await fetch(`${base}/internal/activities/ingestion/sync`, {
      method: "POST",
      headers: { "X-Internal-Token": token, "Content-Type": "application/json" },
    }).catch(() => {});

    const resp = await adminApi.get(
      "/api/v1/spoke/validation?coverage=uncovered&limit=500",
    );
    if (resp.ok()) {
      const body = (await resp.json()) as {
        validations: { dataset_urn: string }[];
      };
      // editions is a seeded catalog dataset with no validation conf → uncovered.
      if (body.validations.some((v) => v.dataset_urn === IMAZON_URNS.editions)) {
        ready = true;
        break;
      }
    }
    await new Promise((r) => setTimeout(r, 5_000));
  }
  expect(
    ready,
    `Expected ${IMAZON_URNS.editions} to appear in GET /spoke/validation?coverage=` +
      "uncovered within 180s. The uncovered set is dataset_registry minus confs; the " +
      "registry is populated by ingestion sync and ES indexing may lag ~2-3 min. " +
      "spec: feature/BACKEND.md §Ingestion Service — Sync sweep.",
  ).toBe(true);
});

// ── Test 1 — default checkbox state ─────────────────────────────────────────────
// spec: FRONTEND_VALIDATION.md §List — covered checked by default, uncovered unchecked.

test("validation list defaults to covered checked / uncovered unchecked", async ({
  page,
}) => {
  await page.goto(VALIDATION_URL);
  await expect(page).not.toHaveURL(/\/login/);

  const covered = page.getByRole("checkbox", { name: "covered", exact: true });
  const uncovered = page.getByRole("checkbox", { name: "uncovered", exact: true });
  await expect(covered).toBeVisible({ timeout: 15_000 });
  await expect(covered).toHaveAttribute("aria-checked", "true");
  await expect(uncovered).toHaveAttribute("aria-checked", "false");
});

// ── Test 2 — neither checked → no datasets listed ──────────────────────────────
// spec: FRONTEND_VALIDATION.md §List — with neither box checked there is nothing
//   to fetch; the table lists no datasets.

test("unchecking both coverage boxes lists no datasets", async ({ page }) => {
  await page.goto(VALIDATION_URL);
  await expect(page).not.toHaveURL(/\/login/);

  const covered = page.getByRole("checkbox", { name: "covered", exact: true });
  const uncovered = page.getByRole("checkbox", { name: "uncovered", exact: true });
  await expect(covered).toBeVisible({ timeout: 15_000 });

  // First make the row set non-empty so the post-uncheck count(0) is load-bearing
  // (not vacuously passing on a broken/empty selector). Checking uncovered surfaces
  // the registered-no-conf datasets the sync preflight guaranteed are present.
  const rows = page
    .getByRole("main")
    .getByRole("link", { name: /^urn:li:dataset:/ });
  await uncovered.click();
  await expect(uncovered).toHaveAttribute("aria-checked", "true");
  await expect(rows.first()).toBeVisible({ timeout: 10_000 });
  expect(await rows.count()).toBeGreaterThan(0);

  // -- Now uncheck BOTH boxes → nothing selected → no datasets listed --
  // A dataset row links its URN to /data/[urn]; with no coverage selected none render.
  await covered.click();
  await uncovered.click();
  await expect(covered).toHaveAttribute("aria-checked", "false");
  await expect(uncovered).toHaveAttribute("aria-checked", "false");
  await expect(rows).toHaveCount(0);
});

// ── Test 3 — uncovered toggle surfaces registered-no-conf rows ──────────────────
// spec: FRONTEND_VALIDATION.md §List — checking uncovered lists registered datasets
//   with no validation conf; spec: API.md §Validation — coverage=uncovered.

test("checking uncovered surfaces a registered-no-conf dataset row", async ({
  page,
  adminApi,
}) => {
  // -- Backend dual-confirmation: pick an actually-uncovered dataset URN --
  const probe = await adminApi.get(
    "/api/v1/spoke/validation?coverage=uncovered&limit=200",
  );
  expect(probe.ok(), `uncovered probe failed: ${await probe.text()}`).toBeTruthy();
  const body = (await probe.json()) as {
    total_count: number;
    validations: { dataset_urn: string; description: string | null }[];
  };
  test.skip(body.total_count === 0, "no uncovered datasets in the seed to assert against");
  const target = body.validations[0];
  // Uncovered rows carry a null conf description.
  expect(target.description).toBeNull();

  await page.goto(VALIDATION_URL);
  await expect(page).not.toHaveURL(/\/login/);

  const uncovered = page.getByRole("checkbox", { name: "uncovered", exact: true });
  await expect(uncovered).toBeVisible({ timeout: 15_000 });
  await uncovered.click();
  await expect(uncovered).toHaveAttribute("aria-checked", "true");

  // -- UI assertion: the uncovered dataset row appears (URN link) --
  const enc = encodeURIComponent(target.dataset_urn);
  const row = page.getByRole("link", { name: target.dataset_urn });
  await expect(row.first()).toBeVisible({ timeout: 10_000 });
  await expect(row.first()).toHaveAttribute("href", `/data/${enc}`);
});
