/**
 * Ground spec: /metagen/uncovered — undocumented-datasets view + include_disallowed toggle.
 *
 * Narrow per-page flow: the page renders the uncovered table (read-only) from
 * GET /spoke/metagen/uncovered; toggling the include_disallowed checkbox flips the
 * query param off→on, widening the result set from no_conf_match to also include
 * boundary_blocked rows.
 *
 * The toggle is a query predicate, so it is seeded on BOTH sides: beforeAll creates
 * one enabled conf scoping a single dataset that has no writable boundary, which
 * makes that URN `boundary_blocked` — present in the widened (on) set and absent
 * from the default (off) set — while every other registered dataset stays
 * `no_conf_match` in both. Without such a row the widening could not be observed at
 * all and the toggle test would pass on an unchanged set.
 * spec: spec/TESTING.md §Assertion Discipline — "Filter/query/matching tests seed
 *   both sides… assert the matching rows appear and the non-matching rows are excluded."
 *
 * spec: spec/feature/FRONTEND_METAGEN.md §Uncovered — GET /spoke/metagen/uncovered
 *   with include_disallowed toggle: off shows no_conf_match only, on additionally
 *   shows boundary_blocked rows; read-only; each row links to its dataset page
 * spec: spec/API.md §Metadata Generation — uncovered rows carry a `reason`;
 *   include_disallowed=true adds datasets matched by a conf but blocked by their boundary
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group, real-session role
 */

import { test, expect, IMAZON_URNS } from "../../fixtures/index";
import { apiBaseUrl } from "../../fixtures/env";

const UNCOVERED_API = "/api/v1/spoke/metagen/uncovered";
const CONF_API = "/api/v1/spoke/metagen/conf";

// The dataset the seeded conf scopes. shipping.carrier_status is untouched by the
// UC4 arc (which scopes customers/orders) so the two never contend for it.
const BLOCKED_URN = IMAZON_URNS.carrierStatus;

const CONF_NAME = `ground-uncovered-${Date.now().toString(36)}`;
let confId: string | null = null;

test.beforeAll(async ({ adminApi }) => {
  // The sync sweep is polled against a real DataHub enumeration; allow more than
  // the default per-hook budget.
  test.setTimeout(180_000);

  // -- Precondition: BLOCKED_URN is registered and currently unmatched (no_conf_match).
  //    dataset_registry starts EMPTY after global-setup's --reset-seed and is filled by
  //    the ingestion sync sweep, whose DataHub enumeration lags the seed by ~2-3 min. --
  // spec: feature/BACKEND.md §DataHub Sync — the hourly `datahub-sync-hourly` sweep
  //   (`POST /internal/activities/ingestion/sync`) "enumerates DataHub once and
  //   reconciles `dataset_registry` — inserting newly-seen URNs and soft-flagging
  //   `datahub_registered` true/false".
  // spec: TESTING.md §E2E §Execution discipline — "A hand-rolled polling loop declares its
  //   deadline and asserts the awaited condition after the loop, so exhausting the budget
  //   fails rather than falling through."
  const internalToken = process.env["DATASPOKE_DEV_INTERNAL_TOKEN"] ?? "";
  const deadline = Date.now() + 150_000;
  let registered = false;
  while (Date.now() < deadline) {
    await fetch(`${apiBaseUrl()}/internal/activities/ingestion/sync`, {
      method: "POST",
      headers: { "X-Internal-Token": internalToken, "Content-Type": "application/json" },
    }).catch(() => {});
    const resp = await adminApi.get(`${UNCOVERED_API}?limit=500`);
    if (resp.ok()) {
      const body = (await resp.json()) as { datasets: Array<{ dataset_urn: string }> };
      if (body.datasets.some((d) => d.dataset_urn === BLOCKED_URN)) {
        registered = true;
        break;
      }
    }
    await new Promise((r) => setTimeout(r, 5_000));
  }
  expect(
    registered,
    `${BLOCKED_URN} must be registered and unmatched (no_conf_match) before the ` +
      "seeded conf can turn it into a boundary_blocked row. The registry is filled by " +
      "the ingestion sync sweep and DataHub indexing lags a fresh reset-seed.",
  ).toBe(true);

  // -- Seed: an ENABLED conf scoping exactly that one dataset. No boundary is created
  //    for it, so the conf matches the dataset but may not write it → boundary_blocked. --
  // spec: API.md §Metadata Generation — POST /spoke/metagen/conf.
  // spec: API.md §`dataset_filter` grammar — `dataset_urn = '…'` is the scalar-equality
  //   predicate that scopes a conf to exactly one dataset.
  // schedule_tier null so no scheduled DAG picks the conf up during the run.
  const createResp = await adminApi.post(CONF_API, {
    data: {
      name: CONF_NAME,
      is_enabled: true,
      schedule_tier: null,
      dataset_filter: `dataset_urn = '${BLOCKED_URN}'`,
      result_limit: 1,
      overwrite_pending: false,
    },
  });
  expect([200, 201], `conf create failed: ${await createResp.text()}`).toContain(
    createResp.status(),
  );
  confId = ((await createResp.json()) as { id: string }).id;

  // Backstop: the seed really produced a boundary_blocked row before any assertion runs.
  const seededResp = await adminApi.get(`${UNCOVERED_API}?include_disallowed=true&limit=500`);
  expect(seededResp.status()).toBe(200);
  const seeded = (await seededResp.json()) as {
    datasets: Array<{ dataset_urn: string; reason: string }>;
  };
  expect(
    seeded.datasets.find((d) => d.dataset_urn === BLOCKED_URN)?.reason,
    "seed precondition: the scoped dataset must read as boundary_blocked in the widened set",
  ).toBe("boundary_blocked");
});

// Cleanup is asserted, not assumed: a leftover enabled conf would silently reshape
// the uncovered set for every later spec.
test.afterAll(async ({ adminApi }) => {
  if (!confId) return;
  const delResp = await adminApi.delete(`${CONF_API}/${confId}`);
  expect(
    [204, 200, 404],
    `conf cleanup failed: ${delResp.status()}`,
  ).toContain(delResp.status());
  const readBack = await adminApi.get(`${CONF_API}/${confId}`);
  expect(readBack.status(), "the seeded metagen conf must be gone").toBe(404);
  confId = null;
});

test("/metagen/uncovered — include_disallowed toggle reveals the boundary_blocked row", async ({
  page,
  adminApi,
}) => {
  // Capture GET /spoke/metagen/uncovered requests to prove the toggle is wired.
  const uncoveredRequests: string[] = [];
  page.on("request", (req) => {
    const url = req.url();
    if (req.method() === "GET" && url.includes("/spoke/metagen/uncovered")) {
      uncoveredRequests.push(url);
    }
  });

  await page.goto("/metagen/uncovered");
  await expect(page).not.toHaveURL(/\/login/);

  // -- Heading + toggle present --
  // uncovered/page.tsx: <h1>Uncovered datasets</h1>; Checkbox id="uncovered-include-disallowed"
  await expect(
    page.getByRole("heading", { name: "Uncovered datasets", exact: true }),
  ).toBeVisible({ timeout: 15_000 });
  const toggle = page.locator("#uncovered-include-disallowed");
  await expect(toggle).toBeVisible({ timeout: 10_000 });
  await expect(toggle).not.toBeChecked();

  // The initial (off) request must NOT carry include_disallowed=true.
  await expect.poll(() => uncoveredRequests.length, { timeout: 15_000 }).toBeGreaterThan(0);
  const offRequests = [...uncoveredRequests];
  expect(offRequests.every((u) => !u.includes("include_disallowed=true"))).toBe(true);

  // -- Backend (off): non-empty, every row no_conf_match, and the seeded conf's
  //    dataset is EXCLUDED (it is matched by a conf, so it is not a no_conf_match row) --
  // spec: FRONTEND_METAGEN.md §Uncovered — off shows no_conf_match only
  const offResp = await adminApi.get(`${UNCOVERED_API}?limit=500&offset=0`);
  expect(offResp.status()).toBe(200);
  const offBody = (await offResp.json()) as {
    datasets: Array<{ dataset_urn: string; reason: string }>;
    total_count: number;
  };
  // Backstop before the reason loop: an empty page would make it vacuous.
  expect(
    offBody.datasets.length,
    "the reset-seed estate registers datasets outside every conf scope, so the " +
      "default uncovered set must be non-empty",
  ).toBeGreaterThan(0);
  for (const row of offBody.datasets) {
    expect(row.reason).toBe("no_conf_match");
  }
  expect(offBody.datasets.map((d) => d.dataset_urn)).not.toContain(BLOCKED_URN);

  // -- UI (off): the boundary-blocked URN is not rendered --
  const blockedRow = page.getByRole("row").filter({ hasText: BLOCKED_URN });
  const renderedUrns = page.getByRole("main").getByRole("link", { name: /^urn:li:dataset:/ });
  await expect(page.getByRole("link", { name: offBody.datasets[0]!.dataset_urn })).toBeVisible({
    timeout: 15_000,
  });
  // Completeness first: the table renders the WHOLE off set, so the absence below means
  // "excluded by the predicate", not "paginated onto page 2".
  await expect
    .poll(() => renderedUrns.count(), {
      timeout: 15_000,
      message: "the default (off) view must render the complete no_conf_match set on one page",
    })
    .toBe(offBody.datasets.length);
  await expect(blockedRow).toHaveCount(0);

  // -- UI gesture: enable include_disallowed --
  await toggle.click();
  await expect(toggle).toBeChecked();

  // -- The toggle fires a new request carrying include_disallowed=true --
  // uncovered/page.tsx: useMetagenUncovered(includeDisallowed) → ?include_disallowed=true
  await expect
    .poll(() => uncoveredRequests.some((u) => u.includes("include_disallowed=true")), {
      timeout: 15_000,
    })
    .toBe(true);

  // -- Backend (on): the same first page now carries the seeded dataset as
  //    boundary_blocked, and every reason is in the documented classification set --
  // spec: FRONTEND_METAGEN.md §Uncovered — on additionally shows boundary_blocked rows
  const onResp = await adminApi.get(
    `${UNCOVERED_API}?include_disallowed=true&limit=500&offset=0`,
  );
  expect(onResp.status()).toBe(200);
  const onBody = (await onResp.json()) as {
    datasets: Array<{ dataset_urn: string; reason: string }>;
    total_count: number;
  };
  for (const row of onBody.datasets) {
    expect(["no_conf_match", "boundary_blocked"]).toContain(row.reason);
  }
  const blocked = onBody.datasets.find((d) => d.dataset_urn === BLOCKED_URN);
  expect(
    blocked,
    "the widened set's first page must carry the seeded dataset (the estate is small " +
      "enough to fit one page, which is also what the UI renders)",
  ).toBeTruthy();
  expect(blocked!.reason).toBe("boundary_blocked");
  // The seed adds EXACTLY one boundary_blocked dataset, so the widening is exactly +1 —
  // a stricter claim than "≥", and one an over-broad predicate would break.
  expect(
    onBody.total_count,
    "include_disallowed=true must add exactly the one seeded boundary_blocked dataset",
  ).toBe(offBody.total_count + 1);

  // -- UI (on): the row appears, carrying its boundary_blocked reason badge --
  // uncovered-table.tsx: dataset_urn link + <Badge>{row.reason}</Badge>
  await expect
    .poll(() => renderedUrns.count(), {
      timeout: 15_000,
      message: "the widened (on) view must render the complete set on one page",
    })
    .toBe(onBody.datasets.length);
  await expect(blockedRow).toHaveCount(1, { timeout: 15_000 });
  await expect(blockedRow.getByText("boundary_blocked", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: BLOCKED_URN })).toHaveAttribute(
    "href",
    `/data/${encodeURIComponent(BLOCKED_URN)}`,
  );
});
