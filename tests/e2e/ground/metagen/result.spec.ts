/**
 * Ground spec: /metagen/result — global review queue + cross-conf events.
 *
 * Narrow per-page flow: the page renders the cross-dataset/cross-conf item queue
 * (GET /spoke/metagen/item) and the cross-conf event feed (GET /spoke/metagen/event),
 * and the queue's conf_id filter select is populated from the conf list. Picking a
 * conf in the filter narrows the queue request to that conf_id.
 *
 * Independent: seeds one conf via REST so the conf_id filter has an entry; deletes
 * it in afterAll. Does not require items to exist (an empty queue is a valid render).
 *
 * spec: spec/feature/FRONTEND_METAGEN.md §Result queue — cross-dataset/cross-conf
 *   queue filterable by dataset_urn / kind / status / conf_id; second section is
 *   the cross-conf event feed
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

test("/metagen/result — renders the queue + events; conf_id filter narrows the item request", async ({
  page,
}) => {
  // Track GET /spoke/metagen/item requests so we can prove the conf_id filter is wired.
  const itemRequests: string[] = [];
  page.on("request", (req) => {
    const url = req.url();
    if (req.method() === "GET" && /\/spoke\/metagen\/item(\?|$)/.test(url)) {
      itemRequests.push(url);
    }
  });

  await page.goto("/metagen/result");
  await expect(page).not.toHaveURL(/\/login/);

  // -- Heading + the two sections --
  // result/page.tsx: <h1>Review queue</h1>; queue + Run events sections
  await expect(page.getByRole("heading", { name: "Review queue", exact: true })).toBeVisible({
    timeout: 15_000,
  });
  await expect(
    page.getByText("item queue (cross-dataset, cross-conf)", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Run events (cross-conf)", { exact: true })).toBeVisible();

  // The initial unfiltered queue request must have fired.
  await expect.poll(() => itemRequests.length, { timeout: 15_000 }).toBeGreaterThan(0);

  // -- conf_id filter select is populated from the conf list; pick the seeded conf --
  // queue-table.tsx: Select aria-label "Filter by conf" with one option per conf
  const confFilter = page.getByRole("combobox", { name: "Filter by conf" });
  await expect(confFilter).toBeVisible({ timeout: 10_000 });
  await confFilter.click();
  await page.getByRole("option", { name: CONF_NAME, exact: true }).click();

  // -- The selection narrows the item request to conf_id={seeded conf id} --
  // queue-table.tsx: useMetagenQueue({ conf_id }) → GET /spoke/metagen/item?conf_id=...
  await expect
    .poll(() => itemRequests.some((u) => u.includes(`conf_id=${confId}`)), { timeout: 15_000 })
    .toBe(true);
});
