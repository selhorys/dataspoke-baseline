/**
 * Ground spec: /metagen/conf/new — create a conf.
 *
 * Narrow per-page flow: fill the conf create form (name, is_enabled,
 * schedule_tier, dataset_filter, result_limit, overwrite_pending), Submit,
 * land on the new conf's detail page, and confirm the row persisted via
 * GET /spoke/metagen/conf (real-stack read-back, not a UI cache).
 *
 * Independent: creates its own conf; deletes it in afterAll.
 *
 * spec: spec/feature/FRONTEND_METAGEN.md §Page contracts — /metagen/conf/new
 *   POSTs /spoke/metagen/conf {name, is_enabled, schedule_tier, dataset_filter,
 *   result_limit, overwrite_pending}; redirect to /metagen/conf/[id]
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group, real-session role
 */

import { test, expect } from "../../fixtures/index";

const CONF_API = "/api/v1/spoke/metagen/conf";
/** Natural-key prefix this spec owns. Every conf it creates carries it, so the setup
 *  sweep can find leftovers from earlier runs whose exact name it cannot predict. */
const CONF_NAME_PREFIX = "ground-new-";
const CONF_NAME = `${CONF_NAME_PREFIX}${Date.now().toString(36)}`;
const TITLE_MASTER_URN =
  "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)";

let confId: string | null = null;

// ── Setup: pre-delete every conf holding this spec's natural key ──────────────
// `confId` is resolved from the read-back AFTER the create, so a run that dies between
// the POST and that read-back leaves its conf behind with nothing for afterAll to delete.
// The natural key swept here is the CONF_NAME_PREFIX this spec owns, not the exact
// CONF_NAME: the per-load timestamp suffix differs on every worker load (a retry
// re-imports the module), so only the prefix matches a leftover from an earlier attempt.
// An absent conf is success — the sweep simply finds nothing.
// spec: spec/TESTING.md §E2E §Execution discipline — "Setup is idempotent and lives in
//   hooks… each setup path pre-deletes by natural key and accepts the upsert/absent
//   status codes (200-or-201, 404-as-success)."
test.beforeAll(async ({ adminApi }) => {
  const listResp = await adminApi.get(`${CONF_API}?limit=100`);
  if (listResp.ok()) {
    const list = (await listResp.json()) as { confs?: Array<{ id: string; name: string }> };
    for (const c of (list.confs ?? []).filter((x) => x.name.startsWith(CONF_NAME_PREFIX))) {
      await adminApi.delete(`${CONF_API}/${c.id}`);
    }
  }
  confId = null;
});

test.afterAll(async ({ adminApi }) => {
  if (confId) await adminApi.delete(`${CONF_API}/${confId}`).catch(() => null);
});

test("/metagen/conf/new — create form posts a conf and redirects to its detail", async ({
  page,
  adminApi,
}) => {
  await page.goto("/metagen/conf/new");
  await expect(page).not.toHaveURL(/\/login/);

  // -- Heading --
  // new/page.tsx: <h1>Create conf</h1>
  await expect(page.getByRole("heading", { name: "Create conf", exact: true })).toBeVisible({
    timeout: 15_000,
  });

  // -- Fill the form --
  // conf-form.tsx field ids
  await page.locator("#metagen-conf-name").fill(CONF_NAME);

  const isEnabled = page.locator("#metagen-conf-is-enabled");
  if (!(await isEnabled.isChecked().catch(() => false))) await isEnabled.click();

  await page.locator("#metagen-conf-schedule-tier").click();
  await page.getByRole("option", { name: "weekly", exact: true }).click();

  // dataset_filter — the shared DatasetFilterEditor's SQL clause box
  // (dataset-filter-editor.tsx, aria-label "dataset_filter"). Scoping a conf to one
  // dataset is the `dataset_urn = '…'` scalar-equality predicate.
  // spec: API.md §`dataset_filter` grammar.
  await page
    .getByLabel("dataset_filter", { exact: true })
    .fill(`dataset_urn = '${TITLE_MASTER_URN}'`);

  await page.locator("#metagen-conf-result-limit").fill("5");

  // -- Submit --
  // conf-form.tsx: <Button type="submit">Create conf</Button>
  await page.getByRole("button", { name: "Create conf", exact: true }).click();

  // -- Toast + redirect to /metagen/conf/[id] --
  // new/page.tsx onSuccess → toast({ title: "Conf created" }) + router.push
  await expect(page.getByText("Conf created", { exact: false }).first()).toBeVisible({
    timeout: 20_000,
  });
  await expect(page).toHaveURL(/\/metagen\/conf\/[^/]+$/, { timeout: 15_000 });

  // -- Backend read-back: the conf persisted with the submitted fields --
  // spec: FRONTEND_METAGEN.md §Page contracts — POST /spoke/metagen/conf
  const listResp = await adminApi.get(`${CONF_API}?limit=100`);
  expect(listResp.status()).toBe(200);
  const list = (await listResp.json()) as {
    confs: Array<{
      id: string;
      name: string;
      is_enabled: boolean;
      schedule_tier: string | null;
      result_limit: number;
      dataset_filter: string;
    }>;
  };
  const created = list.confs.find((c) => c.name === CONF_NAME);
  expect(created, `conf ${CONF_NAME} must exist after create`).toBeTruthy();
  confId = created!.id;
  expect(created!.is_enabled).toBe(true);
  expect(created!.schedule_tier).toBe("weekly");
  expect(created!.result_limit).toBe(5);
  // The clause is stored verbatim — the backend owns the grammar and no route
  // rewrites it. spec: API.md §`dataset_filter` grammar.
  expect(created!.dataset_filter).toBe(`dataset_urn = '${TITLE_MASTER_URN}'`);
});
