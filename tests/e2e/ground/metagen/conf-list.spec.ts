/**
 * Ground spec: /metagen/conf — conf list page.
 *
 * Narrow per-page flow (spot analogue): the conf list renders its heading, the
 * writer-only "Create conf" link routes to /metagen/conf/new, and the list
 * surfaces confs read from GET /spoke/metagen/conf with a per-row Run action.
 *
 * Independent: seeds one conf via the admin REST API, asserts the UI, then
 * deletes it in afterAll. Does not depend on the UC4 use-case arc.
 *
 * spec: spec/feature/FRONTEND_METAGEN.md §Routes — /metagen/conf conf list
 * spec: spec/feature/FRONTEND_METAGEN.md §Conf list — name link, is_enabled badge,
 *   schedule_tier, dataset_filter summary, result_limit, per-row Run; Create conf button
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group, real-session role
 */

import { test, expect } from "../../fixtures/index";

const CONF_API = "/api/v1/spoke/metagen/conf";
const CONF_NAME = `ground-list-${Date.now().toString(36)}`;

let confId: string | null = null;

test.beforeAll(async ({ adminApi }) => {
  const resp = await adminApi.post(CONF_API, {
    data: {
      name: CONF_NAME,
      is_enabled: true,
      schedule_tier: "daily",
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

test("/metagen/conf — lists confs with a Create-conf link and per-row Run action", async ({
  page,
}) => {
  await page.goto("/metagen/conf");
  await expect(page).not.toHaveURL(/\/login/);

  // -- Heading (navigational landmark) --
  // conf-list.tsx: <h1>Metadata Generation</h1>
  await expect(
    page.getByRole("heading", { name: "Metadata Generation", exact: true }),
  ).toBeVisible({ timeout: 15_000 });

  // -- Writer-only Create-conf link routes to /metagen/conf/new --
  // conf-list.tsx: <Link href="/metagen/conf/new">Create conf</Link>
  const createLink = page.getByRole("link", { name: /create conf/i });
  await expect(createLink).toBeVisible();
  await expect(createLink).toHaveAttribute("href", "/metagen/conf/new");

  // -- The seeded conf is rendered as a row linking to its detail page --
  // conf-list.tsx: name cell links to /metagen/conf/{id}
  const confLink = page.getByRole("link", { name: CONF_NAME, exact: true });
  await expect(confLink).toBeVisible({ timeout: 15_000 });
  await expect(confLink).toHaveAttribute("href", `/metagen/conf/${confId}`);

  // -- is_enabled badge + per-row Run action present for the writer (admin) role --
  // conf-list.tsx: enabled badge; Button aria-label "Run conf {name}"
  await expect(page.getByText("enabled", { exact: true }).first()).toBeVisible();
  await expect(
    page.getByRole("button", { name: `Run conf ${CONF_NAME}`, exact: true }),
  ).toBeVisible();
});
