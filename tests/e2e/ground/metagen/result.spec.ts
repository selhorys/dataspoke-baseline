/**
 * Ground spec: /metagen/result — per-dataset result rollup + cross-conf events.
 *
 * Narrow per-page flow: the page renders the per-dataset rollup table
 * (GET /spoke/metagen/dataset) and the cross-conf event feed
 * (GET /spoke/metagen/event), and the rollup's conf_id filter select is populated
 * from the conf list. Picking a conf in the filter narrows the rollup request to
 * that conf_id.
 *
 * Independent: seeds one conf via REST so the conf_id filter has an entry; deletes
 * it in afterAll. Does not require datasets to exist (an empty rollup is a valid
 * render).
 *
 * spec: spec/feature/FRONTEND_METAGEN.md §Result rollup — per-dataset rollup
 *   (GET /spoke/metagen/dataset) filterable by dataset_urn text + conf_id select
 *   (no kind / status filters); second section is the cross-conf event feed
 * spec: spec/API.md §Metadata Generation — GET /spoke/metagen/dataset
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group, real-session role
 */

import { test, expect } from "../../fixtures/index";

const CONF_API = "/api/v1/spoke/metagen/conf";
const CONF_NAME = `ground-result-${Date.now().toString(36)}`;

let confId: string | null = null;

test.beforeAll(async ({ adminApi }) => {
  const resp = await adminApi.post(CONF_API, {
    data: {
      name: CONF_NAME,
      is_enabled: true,
      schedule_tier: null,
      dataset_filter: {},
      result_limit: 3,
      overwrite_pending: true,
    },
  });
  expect([200, 201]).toContain(resp.status());
  confId = ((await resp.json()) as { id: string }).id;
});

test.afterAll(async ({ adminApi }) => {
  if (confId) await adminApi.delete(`${CONF_API}/${confId}`).catch(() => null);
});

test("/metagen/result — renders the per-dataset rollup + events; conf_id filter narrows the rollup request", async ({
  page,
}) => {
  // Track GET /spoke/metagen/dataset requests so we can prove the conf_id filter is wired.
  const datasetRequests: string[] = [];
  page.on("request", (req) => {
    const url = req.url();
    if (req.method() === "GET" && /\/spoke\/metagen\/dataset(\?|$)/.test(url)) {
      datasetRequests.push(url);
    }
  });

  await page.goto("/metagen/result");
  await expect(page).not.toHaveURL(/\/login/);

  // -- Heading + the two sections --
  // result/page.tsx: <h1>Result rollup</h1>; per-dataset rollup + Run events sections
  await expect(page.getByRole("heading", { name: "Result rollup", exact: true })).toBeVisible({
    timeout: 15_000,
  });
  await expect(
    page.getByText("datasets (per-dataset rollup)", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Run events (cross-conf)", { exact: true })).toBeVisible();

  // The initial unfiltered rollup request must have fired.
  await expect.poll(() => datasetRequests.length, { timeout: 15_000 }).toBeGreaterThan(0);

  // -- conf_id filter select is populated from the conf list; pick the seeded conf --
  // dataset-table.tsx: Select aria-label "Filter by conf" with one option per conf
  const confFilter = page.getByRole("combobox", { name: "Filter by conf" });
  await expect(confFilter).toBeVisible({ timeout: 10_000 });
  await confFilter.click();
  await page.getByRole("option", { name: CONF_NAME, exact: true }).click();

  // -- The selection narrows the rollup request to conf_id={seeded conf id} --
  // dataset-table.tsx: useMetagenDatasets({ conf_id }) → GET /spoke/metagen/dataset?conf_id=...
  await expect
    .poll(() => datasetRequests.some((u) => u.includes(`conf_id=${confId}`)), {
      timeout: 15_000,
    })
    .toBe(true);
});
