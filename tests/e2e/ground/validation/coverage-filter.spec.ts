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
 *   3. The coverage filter discriminates: a dataset WITH a validation conf appears
 *      under `covered` and is absent under `uncovered`, while a registered-no-conf
 *      dataset does the reverse. Both sides are dual-confirmed against
 *      GET /spoke/validation?coverage=covered|uncovered.
 *
 * Data setup: global-setup runs --reset-seed (seeded Imazon baseline). The
 * dataset_registry starts EMPTY after the reset and is populated only by the
 * ingestion sync sweep, so the beforeAll below sync-polls until the two probe
 * datasets are in the uncovered set. It then creates ONE validation conf (on
 * COVERED_URN) so the filter has rows on both sides — seeding only uncovered rows
 * could not catch an over-broad predicate that returns every registered dataset.
 * The conf is hard-deleted in afterAll and the deletion is asserted.
 * spec: spec/TESTING.md §Assertion Discipline — "Filter/query/matching tests seed
 *   both sides… assert the matching rows appear and the non-matching rows are excluded."
 *
 * spec: spec/feature/FRONTEND_VALIDATION.md §List — covered (default checked) /
 *   uncovered (default unchecked) checkboxes mapping to the `coverage` query param.
 * spec: spec/API.md §Validation — GET /spoke/validation coverage=covered|uncovered|both.
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group; one concern per test.
 */

import { test, expect, IMAZON_URNS } from "../../fixtures/index";
import { apiBaseUrl } from "../../fixtures/env";

const VALIDATION_URL = "/validation";

// The dataset that gets a validation conf here → must read as `covered`.
// user_ratings is untouched by the UC2 arc (which uses daily_fulfillment_summary).
const COVERED_URN = IMAZON_URNS.userRatings;
// A seeded dataset deliberately left without a conf → must read as `uncovered`.
const UNCOVERED_URN = IMAZON_URNS.editions;

const CONF_DESCRIPTION = "ground coverage-filter seed";
let confCreated = false;

// ── Registry sync preflight + covered-side seed ─────────────────────────────────
// dataset_registry starts EMPTY after the reset; POST /internal/activities/
// ingestion/sync is its sole writer (reconciles from DataHub). The uncovered set is
// the registry minus confs, so it is empty until sync runs. DataHub ES indexing lags
// ~2-3 min after reset-seed, so re-trigger sync each iteration until BOTH probe
// datasets appear in coverage=uncovered (180s budget). Then create the one conf that
// moves COVERED_URN to the covered side.
// spec: TESTING.md §E2E §Execution discipline — "Gate data-dependent UI assertions on
// confirmed backend state… it absorbs eventual consistency — DataHub's search index lags
// writes by minutes"; the loop declares its deadline and asserts after it.
// spec: tests/e2e/use-case/uc1-01-datahub-managed.spec.ts:270-304 — sync-poll pattern.
test.beforeAll(async ({ adminApi }) => {
  test.setTimeout(240_000);
  const base = apiBaseUrl();
  const token = process.env["DATASPOKE_DEV_INTERNAL_TOKEN"] ?? "";
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
      const urns = new Set(body.validations.map((v) => v.dataset_urn));
      // Both are seeded datasets with no validation conf yet → both uncovered.
      if (urns.has(UNCOVERED_URN) && urns.has(COVERED_URN)) {
        ready = true;
        break;
      }
    }
    await new Promise((r) => setTimeout(r, 5_000));
  }
  expect(
    ready,
    `Expected ${UNCOVERED_URN} and ${COVERED_URN} to appear in GET /spoke/validation` +
      "?coverage=uncovered within 180s. The uncovered set is dataset_registry minus " +
      "confs; the registry is populated by ingestion sync and ES indexing may lag " +
      "~2-3 min. spec: feature/BACKEND.md §Ingestion Service — Sync sweep.",
  ).toBe(true);

  // -- Seed the covered side: one validation conf on COVERED_URN --
  // spec: API.md §Validation — PUT /spoke/common/data/{urn}/attr/validation/conf
  const putResp = await adminApi.put(
    `/api/v1/spoke/common/data/${encodeURIComponent(COVERED_URN)}/attr/validation/conf`,
    {
      data: {
        description: CONF_DESCRIPTION,
        variables: [{ name: "row_cnt", description: "row count" }],
      },
    },
  );
  expect(putResp.ok(), `validation conf PUT failed: ${await putResp.text()}`).toBeTruthy();
  confCreated = true;
});

// ── Cleanup: hard-delete the seeded conf and assert it is gone ──────────────────
// spec: API.md §Validation — DELETE …/attr/validation/conf returns 204; afterwards
//   the dataset reads as never-created.
test.afterAll(async ({ adminApi }) => {
  if (!confCreated) return;
  const enc = encodeURIComponent(COVERED_URN);
  const delResp = await adminApi.delete(
    `/api/v1/spoke/common/data/${enc}/attr/validation/conf`,
  );
  expect([204, 404]).toContain(delResp.status());
  const readBack = await adminApi.get(
    `/api/v1/spoke/common/data/${enc}/attr/validation/conf`,
  );
  expect(
    readBack.status(),
    "the seeded validation conf must be gone so later specs see the baseline",
  ).toBe(404);
  confCreated = false;
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

// ── Test 3 — the coverage filter separates covered from uncovered ───────────────
// spec: FRONTEND_VALIDATION.md §List — the covered / uncovered checkboxes map to the
//   `coverage` query param; checking uncovered lists registered datasets with no
//   validation conf.
// spec: API.md §Validation — GET /spoke/validation coverage=covered|uncovered|both.

test("the coverage filter lists the conf'd dataset under covered and excludes it from uncovered", async ({
  page,
  adminApi,
}) => {
  // -- Backend dual-confirmation (covered): the seeded conf's dataset is in the
  //    covered set with its description, and the no-conf dataset is NOT. --
  const coveredResp = await adminApi.get(
    "/api/v1/spoke/validation?coverage=covered&limit=500",
  );
  expect(coveredResp.ok(), `covered probe failed: ${await coveredResp.text()}`).toBeTruthy();
  const coveredBody = (await coveredResp.json()) as {
    validations: { dataset_urn: string; description: string | null }[];
  };
  const coveredRow = coveredBody.validations.find((v) => v.dataset_urn === COVERED_URN);
  expect(coveredRow, "the seeded conf's dataset must be in coverage=covered").toBeTruthy();
  expect(coveredRow!.description).toBe(CONF_DESCRIPTION);
  expect(coveredBody.validations.map((v) => v.dataset_urn)).not.toContain(UNCOVERED_URN);

  // -- Backend dual-confirmation (uncovered): the mirror image. Uncovered rows carry
  //    a null conf description. --
  const uncoveredResp = await adminApi.get(
    "/api/v1/spoke/validation?coverage=uncovered&limit=500",
  );
  expect(uncoveredResp.ok(), `uncovered probe failed: ${await uncoveredResp.text()}`).toBeTruthy();
  const uncoveredBody = (await uncoveredResp.json()) as {
    validations: { dataset_urn: string; description: string | null }[];
  };
  const uncoveredRow = uncoveredBody.validations.find((v) => v.dataset_urn === UNCOVERED_URN);
  expect(uncoveredRow, "a registered dataset with no conf must be in coverage=uncovered").toBeTruthy();
  expect(uncoveredRow!.description).toBeNull();
  expect(
    uncoveredBody.validations.map((v) => v.dataset_urn),
    "a dataset that HAS a conf must never appear in coverage=uncovered",
  ).not.toContain(COVERED_URN);

  await page.goto(VALIDATION_URL);
  await expect(page).not.toHaveURL(/\/login/);

  const covered = page.getByRole("checkbox", { name: "covered", exact: true });
  const uncovered = page.getByRole("checkbox", { name: "uncovered", exact: true });
  await expect(covered).toBeVisible({ timeout: 15_000 });

  // -- UI (default: covered only) — the conf'd dataset is listed, the no-conf one is not --
  await expect(covered).toHaveAttribute("aria-checked", "true");
  await expect(uncovered).toHaveAttribute("aria-checked", "false");
  const coveredLink = page.getByRole("link", { name: COVERED_URN });
  const uncoveredLink = page.getByRole("link", { name: UNCOVERED_URN });
  const renderedUrns = page.getByRole("main").getByRole("link", { name: /^urn:li:dataset:/ });
  await expect(coveredLink).toBeVisible({ timeout: 15_000 });
  await expect(coveredLink).toHaveAttribute(
    "href",
    `/data/${encodeURIComponent(COVERED_URN)}`,
  );
  // Completeness first: the page renders the WHOLE covered set, so the absence below
  // means "filtered out", not "paginated onto page 2".
  await expect
    .poll(() => renderedUrns.count(), {
      timeout: 15_000,
      message: "the covered view must render the complete covered set on one page",
    })
    .toBe(coveredBody.validations.length);
  await expect(uncoveredLink).toHaveCount(0);

  // -- UI gesture: switch to uncovered only → the two rows swap --
  await uncovered.click();
  await covered.click();
  await expect(uncovered).toHaveAttribute("aria-checked", "true");
  await expect(covered).toHaveAttribute("aria-checked", "false");
  await expect(uncoveredLink).toBeVisible({ timeout: 15_000 });
  await expect(uncoveredLink).toHaveAttribute(
    "href",
    `/data/${encodeURIComponent(UNCOVERED_URN)}`,
  );
  // Same completeness backstop for the mirrored view.
  await expect
    .poll(() => renderedUrns.count(), {
      timeout: 15_000,
      message: "the uncovered view must render the complete uncovered set on one page",
    })
    .toBe(uncoveredBody.validations.length);
  await expect(coveredLink).toHaveCount(0);
});
