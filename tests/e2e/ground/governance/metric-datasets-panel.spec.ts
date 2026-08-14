/**
 * Ground spec: the Datasets panel on /governance/metrics/[id].
 *
 * Narrow per-page UI flows — the spot analogue for the covered-dataset table.
 * The use-case spec (`use-case/uc5-01-governance.spec.ts`) walks the panel as
 * part of the UC5 story after a real run; this file isolates the three panel
 * behaviours a story cannot pin cleanly:
 *
 *   1. Columns + the never-evaluated row. A metric that has NEVER run has a
 *      scope but no verdicts, so every row reads `unknown` with an em-dash
 *      last-check time — the one state the run-then-read story cannot produce.
 *   2. The three-way verdict toggle carries its visible `criterion met:` label and
 *      drives the repeatable `met` query param (observed on the wire), and zero
 *      toggles issue NO request at all.
 *   3. The scope-freshness line states the envelope's `attrs_synced_at`.
 *
 * Independent: seeds one disabled, on-demand metric via REST so the detail route
 * resolves, never runs it, and deletes it in afterAll. `dataset_filter` is the
 * empty clause — every registered dataset — so the panel has a scope as soon as
 * the ingestion sweep has populated `dataset_registry`.
 *
 * spec: spec/feature/FRONTEND_GOVERNANCE.md §Metrics — "The **Datasets** panel
 *   (`MetricDatasetTable`, modelled on the Ingestion `SourceDatasetTable`) sits
 *   between the `Result` and `Event` panels … columns `dataset_urn` (linked to
 *   `/data/[urn]`), `datahub` …, a `met` badge (`true` / `false` / `unknown`), and
 *   `last check time` … em dash when the row is `unknown`. A three-way toggle
 *   group — true / false / unknown, all on by default — drives the repeatable
 *   `met` query param, resetting `offset` on change. With **zero** toggles
 *   selected the client renders the empty state and issues **no request** …
 *   Beneath the table a muted line states the envelope's `attrs_synced_at` as the
 *   scope's freshness".
 * spec: spec/API.md §Metric — GET /spoke/governance/metric/{metric_id}/dataset;
 *   "`met` is `\"unknown\"` exactly when the dataset is in the filter's scope but
 *   carries no verdict — the metric has never run"; `attrs_synced_at` is
 *   scope-relative and "unaffected by `met` filtering or paging".
 * spec: spec/API.md §`dataset_filter` grammar — the empty string matches every
 *   registered dataset.
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group; one concern per test.
 */

import { test, expect } from "../../fixtures/index";
import { apiBaseUrl } from "../../fixtures/env";

// A stable natural key, NOT a per-run suffix: `retries` restarts the worker and
// re-evaluates module scope, so a generated id would differ between the original
// run and its retry. Fixed id + pre-delete is what makes the seed idempotent.
const METRIC_API = "/api/v1/spoke/governance/metric";
const METRIC_ID = "ground-datasets-panel";
const DETAIL_URL = `/governance/metrics/${METRIC_ID}`;
const DATASET_PATH = `${METRIC_API}/${METRIC_ID}/dataset`;

// Admin-only — filename convention (`*.spec.ts` → admin project). Do not override
// storageState.

test.beforeAll(async ({ adminApi }) => {
  // Budget: the registry sync poll below can take minutes on a fresh reset-seed,
  // and a hook carries its own timeout (the 60s project ceiling by default).
  test.setTimeout(240_000);

  await adminApi.delete(`${METRIC_API}/${METRIC_ID}/attr/conf`).catch(() => null);
  const resp = await adminApi.post(METRIC_API, {
    data: {
      metric_id: METRIC_ID,
      mode: "active",
      // Disabled + on-demand: this metric must never be picked up by a scheduled
      // DAG, because "has never run" is the precondition test 1 asserts on.
      is_enabled: false,
      metric_type: "doc-health",
      title: "Datasets Panel Ground Fixture",
      description: "Ground spec fixture for the metric Datasets panel",
      metrics: [
        { name: "total", color: "#64748B", idx: 1 },
        { name: "doc_health", color: "#A855F7", idx: 2 },
      ],
      metric_conf: {},
      schedule_tier: null,
      // Empty clause = every registered dataset (API.md §`dataset_filter` grammar).
      dataset_filter: "",
    },
  });
  expect(
    [200, 201],
    `seeding metric ${METRIC_ID} failed: ${resp.status()} ${await resp.text()}`,
  ).toContain(resp.status());

  // dataset_registry starts EMPTY after --reset-seed and is populated only by the
  // ingestion sync sweep; DataHub ES indexing lags ~2-3 min after a fresh seed, so
  // re-trigger the sweep until the metric's scope is non-empty. Without this the
  // row assertions below would be vacuous.
  // spec: project_es_indexing_lag_after_reset_seed; spec/feature/BACKEND.md
  //   §Ingestion Service — Sync + mapping sweep.
  const base = apiBaseUrl();
  const internalToken = process.env["DATASPOKE_DEV_INTERNAL_TOKEN"] ?? "";
  const deadline = Date.now() + 180_000;
  let scopeSize = 0;
  while (Date.now() < deadline) {
    await fetch(`${base}/internal/activities/ingestion/sync`, {
      method: "POST",
      headers: { "X-Internal-Token": internalToken, "Content-Type": "application/json" },
    }).catch(() => {});

    const probe = await adminApi.get(`${DATASET_PATH}?limit=1`);
    if (probe.ok()) {
      scopeSize = ((await probe.json()) as { total_count: number }).total_count;
      if (scopeSize > 0) break;
    }
    await new Promise((r) => setTimeout(r, 5_000));
  }
  expect(
    scopeSize,
    "the empty clause must cover at least one registered dataset within 180s, or " +
      "every row assertion in this file is vacuous. dataset_registry is populated " +
      "by the ingestion sync sweep. spec: spec/TESTING.md §Prerequisites.",
  ).toBeGreaterThan(0);
});

test.afterAll(async ({ adminApi }) => {
  await adminApi.delete(`${METRIC_API}/${METRIC_ID}/attr/conf`).catch(() => null);
});

/** The Datasets panel <section> on the metric detail page. */
function datasetsPanel(page: import("@playwright/test").Page) {
  return page
    .locator("section")
    .filter({ has: page.getByRole("heading", { name: "Datasets", exact: true }) });
}

async function openDetail(page: import("@playwright/test").Page) {
  await page.goto(DETAIL_URL);
  await expect(page).not.toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: "Datasets", exact: true }),
  ).toBeVisible({ timeout: 20_000 });
}

// ── Test 1 — columns, and the never-evaluated row ──────────────────────────────
// spec: FRONTEND_GOVERNANCE.md §Metrics — the four columns and the em-dash last
//   check time for an `unknown` row.
// spec: API.md §Metric — `met` is "unknown" exactly when the dataset is in scope
//   but carries no verdict (the metric has never run).

test("lists the covered datasets as unknown until the metric has run", async ({
  page,
  adminApi,
}) => {
  // -- Backend probe first: the scope, and that it carries no verdict yet --
  const probe = await adminApi.get(`${DATASET_PATH}?limit=200`);
  expect(probe.status(), await probe.text()).toBe(200);
  const body = (await probe.json()) as {
    total_count: number;
    attrs_synced_at: string | null;
    datasets: Array<{ dataset_urn: string; met: string; last_check_at: string | null }>;
  };
  expect(body.total_count).toBeGreaterThan(0);
  for (const row of body.datasets) {
    expect(
      row.met,
      `${row.dataset_urn} must read "unknown" — this metric has never run`,
    ).toBe("unknown");
    expect(row.last_check_at, "an unevaluated dataset has no check time").toBeNull();
  }

  await openDetail(page);
  const panel = datasetsPanel(page);

  // -- UI assertion: the four spec'd column headers --
  for (const col of ["dataset_urn", "datahub", "met criterion", "last check time"]) {
    await expect(
      panel.getByRole("columnheader", { name: col, exact: true }),
    ).toBeVisible({ timeout: 15_000 });
  }

  // -- UI assertion: the first served row renders, linked to its hub page --
  const urn = body.datasets[0]!.dataset_urn;
  const urnLink = panel.getByRole("link", { name: urn });
  await expect(urnLink).toBeVisible({ timeout: 15_000 });
  await expect(urnLink).toHaveAttribute("href", `/data/${encodeURIComponent(urn)}`);

  // -- UI assertion: that row is `unknown`, with an em-dash check time --
  const row = panel.getByRole("row").filter({ hasText: urn });
  await expect(row.getByText("unknown", { exact: true })).toBeVisible();
  await expect(row.getByText("—", { exact: true })).toBeVisible();

  // -- UI assertion: the row's datahub cell carries the shared deep-link --
  await expect(row.getByRole("link", { name: /datahub/i })).toHaveAttribute(
    "href",
    /\/dataset\//,
  );
});

// ── Test 2 — the verdict toggle drives the repeatable `met` param ──────────────
// spec: FRONTEND_GOVERNANCE.md §Metrics — "A three-way toggle group — true /
//   false / unknown, all on by default — drives the repeatable `met` query param";
//   "With **zero** toggles selected the client renders the empty state and issues
//   **no request**: an omitted repeatable param and an empty one are the same HTTP
//   request, which the API reads as 'all three', so the no-selection case cannot be
//   expressed on the wire and is resolved client-side instead."
// spec: FRONTEND_GOVERNANCE.md §Metrics — "The group carries a visible `criterion
//   met:` label immediately before the three checkboxes, so the three bare words are
//   readable without relying on the group's accessible name; the table's own column
//   header is `met criterion`."

test("the verdict toggles drive the met param, and zero toggles issue no request", async ({
  page,
}) => {
  // Record every dataset read the page makes, with its query params. Observing
  // the wire (rather than routing/intercepting) leaves the app's own traffic
  // untouched, matching ground/governance/metric-grain.spec.ts.
  const datasetQueries: URLSearchParams[] = [];
  const datasetPattern = new RegExp(
    `/spoke/governance/metric/${METRIC_ID}/dataset(\\?|$)`,
  );
  page.on("request", (req) => {
    if (req.method() === "GET" && datasetPattern.test(req.url())) {
      datasetQueries.push(new URL(req.url()).searchParams);
    }
  });
  const metParams = {
    at: (i: number) => datasetQueries.at(i)?.getAll("met"),
    get length() {
      return datasetQueries.length;
    },
    slice: (from: number) => datasetQueries.slice(from).map((q) => q.getAll("met")),
  };

  await openDetail(page);
  const panel = datasetsPanel(page);
  await expect(
    panel.getByRole("columnheader", { name: "dataset_urn", exact: true }),
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: the group's visible label names what the three words qualify --
  const verdictGroup = panel.getByRole("group", {
    name: "Filter datasets by criterion verdict",
  });
  await expect(verdictGroup.getByText("criterion met:", { exact: true })).toBeVisible({
    timeout: 15_000,
  });

  // -- UI assertion: all three toggles start checked, still named by their verdict --
  for (const verdict of ["true", "false", "unknown"]) {
    await expect(
      panel.getByRole("checkbox", { name: verdict, exact: true }),
    ).toBeChecked();
  }

  // -- Wire assertion: the default read asks for all three verdicts --
  await expect.poll(() => metParams.length, { timeout: 15_000 }).toBeGreaterThan(0);
  expect(metParams.at(-1)).toEqual(["true", "false", "unknown"]);
  // …with the documented sort.
  // spec: FRONTEND_GOVERNANCE.md §Metrics — "The shared Pagination drives
  //   `offset`/`limit` with `sort=dataset_urn`."
  expect(datasetQueries.at(-1)!.get("sort")).toBe("dataset_urn");

  // -- Behaviour assertion: the caption is a caption, not a control label --
  // The exact-name queries above cannot tell the two structures apart: the checkbox
  // is a Radix `<button role="checkbox" aria-label={verdict}>`, and `aria-label`
  // outranks a wrapping `<label>` in the accname computation, so a caption nested
  // inside the first label would keep every name query resolving while silently
  // making it a click target for the `true` verdict. Clicking it is what separates
  // them.
  const readsBeforeCaptionClick = metParams.length;
  await verdictGroup.getByText("criterion met:", { exact: true }).click();
  for (const verdict of ["true", "false", "unknown"]) {
    await expect(
      panel.getByRole("checkbox", { name: verdict, exact: true }),
    ).toBeChecked();
  }
  // Bounded settle window before an absence assertion — the gestures below prove a
  // toggle in this group does fire a read, so a quiet window here carries signal.
  await page.waitForTimeout(500);
  expect(
    metParams.length,
    "clicking the group caption must toggle nothing and fire no read; saw " +
      `${JSON.stringify(metParams.slice(readsBeforeCaptionClick))}`,
  ).toBe(readsBeforeCaptionClick);

  // -- UI gesture: drop `false` --
  await panel.getByRole("checkbox", { name: "false", exact: true }).uncheck();
  await expect
    .poll(() => metParams.at(-1), { timeout: 15_000 })
    .toEqual(["true", "unknown"]);

  // -- UI gesture: drop the remaining two — a client-side empty state --
  const readsBeforeEmpty = metParams.length;
  await panel.getByRole("checkbox", { name: "true", exact: true }).uncheck();
  await panel.getByRole("checkbox", { name: "unknown", exact: true }).uncheck();

  await expect(panel.getByText(/no verdict selected/i)).toBeVisible({ timeout: 10_000 });
  await expect(panel.getByRole("table")).toHaveCount(0);
  // The empty selection was resolved client-side: the "drop true" step may fire one
  // more read (for ["unknown"]), but nothing is requested for the empty selection.
  expect(
    metParams.slice(readsBeforeEmpty).every((met) => met.length > 0),
    `no request may carry an empty met set; saw ${JSON.stringify(metParams.slice(readsBeforeEmpty))}`,
  ).toBe(true);

  // -- Backstop: re-checking one verdict brings the table back --
  await panel.getByRole("checkbox", { name: "unknown", exact: true }).check();
  await expect(panel.getByRole("table")).toBeVisible({ timeout: 15_000 });
  await expect.poll(() => metParams.at(-1), { timeout: 15_000 }).toEqual(["unknown"]);
});

// ── Test 3 — the scope-freshness line ──────────────────────────────────────────
// spec: FRONTEND_GOVERNANCE.md §Metrics — "Beneath the table a muted line states
//   the envelope's `attrs_synced_at` as the scope's freshness, so an empty or
//   unexpectedly small table is readable as a pending sync rather than as a filter
//   that matches nothing."
// spec: API.md §Metric — `attrs_synced_at` is the maximum
//   `dataset_registry.attrs_synced_at` over the datasets in scope.

test("states the scope's attribute-sync freshness beneath the table", async ({
  page,
  adminApi,
}) => {
  const probe = await adminApi.get(`${DATASET_PATH}?limit=1`);
  expect(probe.status()).toBe(200);
  const { attrs_synced_at: syncedAt } = (await probe.json()) as {
    attrs_synced_at: string | null;
  };
  expect(
    syncedAt,
    "the sweep in beforeAll must have stamped attrs_synced_at, or the freshness " +
      "line under test has nothing to state. spec: API.md §Metric.",
  ).not.toBeNull();

  await openDetail(page);
  const panel = datasetsPanel(page);

  // The line names the sync date; the exact formatting is the shared tz helper's,
  // so assert on the date part rather than pinning a format the spec does not fix.
  //
  // Derive the day in the *display* timezone, not UTC. The tz preference defaults
  // to "local" (lib/preferences/timezone.ts) and Playwright sets no timezoneId, so
  // the page renders in the system zone while `attrs_synced_at` is UTC. Slicing the
  // ISO string instead would disagree with the rendering whenever the two fall on
  // different calendar days — e.g. a 15:40Z sweep is already the next day in KST,
  // which makes the assertion fail every evening rather than deterministically.
  const day = new Date(syncedAt!).toLocaleDateString("en-CA");
  await expect(panel.getByText(new RegExp(`Scope synced.*${day}`))).toBeVisible({
    timeout: 15_000,
  });
});
