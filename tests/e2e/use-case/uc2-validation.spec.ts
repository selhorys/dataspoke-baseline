/**
 * UC2 — Validation: browser UI flow.
 *
 * Mirrors tests/integration/api_wired/test_uc2_validation.py step-for-step,
 * with dual confirmation at each mutating step:
 *   - UI assertion (table rows, badges, chart panels, event log, section headings)
 *   - Backend probe via adminApi (same REST read-back the api-wired step asserts)
 *
 * Steps (verbatim from USE_CASE_en.md §UC2):
 *   1. PUT validation conf for postgres + kafka datasets (via adminApi — no UI surface
 *      for "register slot"; the form is on the per-dataset detail page, reached by
 *      navigating to it with no existing conf). Creates both configs.
 *   2. POST validation results: 3 days for postgres, 2 days for kafka [API-fired, no UI].
 *      Poll via adminApi until results are visible, then assert UI renders the charts.
 *   3. Cross-dataset list (/validation) shows BOTH datasets with description, variable
 *      count, latest_data_time, and Quality Score badge.
 *   4. Per-dataset detail (/validation/data/[urn]) for postgres shows:
 *      - conf section (description + variable badges)
 *      - score chart panel rendered ("Quality Score (attr/validation/result)")
 *      - variables chart panel rendered ("Variables (attr/validation/result)")
 *      - event log section rendered ("event/validation (latest 5)")
 *   5. DELETE postgres conf via the Delete button + ConfirmDialog:
 *      - redirected to /validation list
 *      - postgres row absent from active list
 *      - kafka row still present
 *      Backend: GET conf → 404; ?removed=true includes postgres.
 *   6. Navigate to /validation/data/[postgres urn] (no conf now):
 *      - no Edit/Delete buttons; create form shown instead.
 *   7. PUT-after-DELETE: submit the create form to resurrect postgres conf.
 *      Backend: GET conf → 200 with new description.
 *
 * Data setup: global-setup runs --reset-seed (seeded Imazon baseline — postgres and
 * kafka datasets exist in DataHub). Results are POST-ed via adminApi directly.
 * Cleanup: afterAll deletes both confs.
 *
 * spec: USE_CASE_en.md §UC2
 * spec: spec/feature/FRONTEND_VALIDATION.md §Navigation, §Page contracts
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — dual confirmation, selector guidance
 */

import { test, expect, IMAZON_URNS } from "../fixtures/index";

// ── Constants (verbatim from api-wired test) ───────────────────────────────────

// Postgres dataset — daily_fulfillment_summary (primary subject of UC2 narrative)
const PG_URN = IMAZON_URNS.dailyFulfillment;

// Kafka dataset — imazon.orders.events (second dataset; exercises cross-dataset list)
// spec: TESTING.md §Imazon Dummy-Data Reference — Kafka topics use example_kafka instance
const KAFKA_URN =
  "urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.orders.events,DEV)";

// API URL helpers: encode URN for path segment (not query string).
// The API accepts either percent-encoded or raw URN in path segments; use encodeURIComponent.
const PG_URN_ENC = encodeURIComponent(PG_URN);
const KAFKA_URN_ENC = encodeURIComponent(KAFKA_URN);

// API routes (mirroring api-wired constants)
const PG_CONF_API = `/api/v1/spoke/common/data/${PG_URN_ENC}/attr/validation/conf`;
const PG_RESULT_API = `/api/v1/spoke/common/data/${PG_URN_ENC}/attr/validation/result`;
const KAFKA_CONF_API = `/api/v1/spoke/common/data/${KAFKA_URN_ENC}/attr/validation/conf`;
const KAFKA_RESULT_API = `/api/v1/spoke/common/data/${KAFKA_URN_ENC}/attr/validation/result`;

// Frontend routes
const PG_DETAIL_URL = `/validation/data/${PG_URN_ENC}`;
const KAFKA_DETAIL_URL = `/validation/data/${KAFKA_URN_ENC}`;

// Conf payloads (verbatim from api-wired test)
const PG_DESCRIPTION =
  "Daily order fulfillment quality: row count, fill rate, and anomaly score";
const PG_CONF_PAYLOAD = {
  description: PG_DESCRIPTION,
  variables: ["row_cnt", "fill_rate", "anomaly_score"],
};
const KAFKA_CONF_PAYLOAD = {
  description: "Order events stream quality: message count and lag",
  variables: ["msg_cnt", "lag_seconds"],
};

// Resurrection payload (step 7)
const RESURRECT_DESCRIPTION = "Reinstated quality check with extended variables";
const RESURRECT_VARIABLES = ["row_cnt", "fill_rate", "anomaly_score", "null_rate"];

// Result data_time values are RECENT (relative to now), not the api-wired fixed
// May-2026 dates: the detail page queries results with from = 30 days ago, so
// stale dates would render no score/series in the UI. day_2 is most recent.
// (Playwright specs are normal Node — new Date() is allowed here.)
function daysAgoIso(n: number): string {
  const d = new Date();
  d.setUTCHours(0, 0, 0, 0);
  d.setUTCDate(d.getUTCDate() - n);
  return d.toISOString();
}
function dateOnly(n: number): string {
  return daysAgoIso(n).slice(0, 10);
}
const DAY_0 = daysAgoIso(3); // oldest
const DAY_1 = daysAgoIso(2);
const DAY_2 = daysAgoIso(1); // most recent

// Result payloads (scores/variables verbatim from api-wired; dates recent).
const PG_RESULTS = [
  { data_time: DAY_0, score: 1.0, variables: { row_cnt: 1250.0, fill_rate: 0.98, anomaly_score: 0.02 } },
  { data_time: DAY_1, score: 0.9, variables: { row_cnt: 1180.0, fill_rate: 0.92, anomaly_score: 0.08 } },
  { data_time: DAY_2, score: 1.0, variables: { row_cnt: 1305.0, fill_rate: 0.99, anomaly_score: 0.01 } },
];
const KAFKA_RESULTS = [
  { data_time: DAY_0, score: 1.0, variables: { msg_cnt: 48000.0, lag_seconds: 1.2 } },
  { data_time: DAY_1, score: 0.85, variables: { msg_cnt: 47220.0, lag_seconds: 5.4 } },
];

// Runs under the admin project only — enforced by the filename convention in
// playwright.config.ts (default *.spec.ts → admin), which supplies the admin
// storageState. Do not override storageState here.

// ── Module-level state shared across serial step tests ─────────────────────────

// Tracks whether confs were created (so afterAll can clean up idempotently).
let pgConfCreated = false;
let kafkaConfCreated = false;

// ── Cleanup: delete both confs after all steps ─────────────────────────────────

test.afterAll(async ({ adminApi }) => {
  // Best-effort cleanup regardless of which steps ran.
  if (pgConfCreated) {
    await adminApi.delete(PG_CONF_API);
    pgConfCreated = false;
  }
  if (kafkaConfCreated) {
    await adminApi.delete(KAFKA_CONF_API);
    kafkaConfCreated = false;
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 1 — Create validation confs for both datasets [API-fired setup]
// spec: USE_CASE_en.md §UC2 — "The caller registers validation slots for the
//   fulfillment table and the upstream order-events topic."
// spec: VALIDATION.md §Rule Configuration — description + variables required.
// spec: TESTING.md §E2E — "[API-fired, no UI surface]" steps are probed via backend.
// ─────────────────────────────────────────────────────────────────────────────
test("UC2 step 1 — PUT validation confs for postgres + kafka datasets", async ({
  adminApi,
}) => {
  // PUT postgres conf (idempotent: if a prior test run left state, overwrite).
  const pgResp = await adminApi.put(PG_CONF_API, { data: PG_CONF_PAYLOAD });
  expect(pgResp.status()).toBe(201);
  const pgBody = (await pgResp.json()) as { variables: string[]; description: string };
  expect(pgBody.variables).toEqual(["row_cnt", "fill_rate", "anomaly_score"]);
  expect(pgBody.description).toBe(PG_DESCRIPTION);
  pgConfCreated = true;

  // PUT kafka conf.
  const kafkaResp = await adminApi.put(KAFKA_CONF_API, { data: KAFKA_CONF_PAYLOAD });
  expect(kafkaResp.status()).toBe(201);
  const kafkaBody = (await kafkaResp.json()) as { variables: string[] };
  expect(kafkaBody.variables).toEqual(["msg_cnt", "lag_seconds"]);
  kafkaConfCreated = true;
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 2 — POST results then verify charts render [API-fired + UI assertion]
// spec: USE_CASE_en.md §UC2 — "Each night, the validation task runs after the
//   partition write and POSTs the day's metrics to DataSpoke."
// spec: VALIDATION.md §Validation Result — data_time is partition timestamp.
// spec: FRONTEND_VALIDATION.md §Page contracts — historical timeseries panel plots
//   score and per-variable chart over data_time.
// ─────────────────────────────────────────────────────────────────────────────
test("UC2 step 2 — POST results; detail page renders score + variables charts", async ({
  page,
  adminApi,
}) => {
  if (!pgConfCreated || !kafkaConfCreated) test.skip();

  // POST 3 daily results for postgres.
  for (const payload of PG_RESULTS) {
    const resp = await adminApi.post(PG_RESULT_API, { data: payload });
    expect(resp.status()).toBe(201);
  }

  // POST 2 daily results for kafka.
  for (const payload of KAFKA_RESULTS) {
    const resp = await adminApi.post(KAFKA_RESULT_API, { data: payload });
    expect(resp.status()).toBe(201);
  }

  // Poll via adminApi until all 3 postgres results are present.
  // spec: TESTING.md §Assertion Principles — poll bounded deadline instead of fixed sleep.
  const from = daysAgoIso(5);
  const until = daysAgoIso(-1); // tomorrow — brackets the recent result dates
  const deadline = Date.now() + 30_000;
  let resultCount = 0;
  while (Date.now() < deadline) {
    const r = await adminApi.get(
      `${PG_RESULT_API}?from=${encodeURIComponent(from)}&until=${encodeURIComponent(until)}&limit=10`
    );
    if (r.ok()) {
      const body = (await r.json()) as { total_count: number };
      resultCount = body.total_count;
      if (resultCount >= 3) break;
    }
    await new Promise((res) => setTimeout(res, 2_000));
  }
  expect(resultCount).toBeGreaterThanOrEqual(3);

  // Navigate to the postgres detail page.
  await page.goto(PG_DETAIL_URL);
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: score chart panel rendered --
  // spec: FRONTEND_VALIDATION.md §Page contracts — score chart section heading
  // The section heading for the score chart is "Quality Score (attr/validation/result)".
  // Use exact: true to avoid matching the "Quality Score" column header on the list page.
  await expect(
    page.getByRole("heading", { name: "Quality Score (attr/validation/result)", exact: true })
  ).toBeVisible({ timeout: 20_000 });

  // -- UI assertion: variables chart panel rendered --
  // spec: FRONTEND_VALIDATION.md §Page contracts — variables chart section heading
  await expect(
    page.getByRole("heading", { name: "Variables (attr/validation/result)", exact: true })
  ).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: variable name badges in variables chart legend --
  // The ValidationVariablesChart renders each variable name as a toggle button with font-mono.
  // Use getByText to find at least one declared variable name in the legend.
  await expect(page.getByText("row_cnt", { exact: true }).first()).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: latest score badge in the header --
  // spec: FRONTEND_VALIDATION.md §Page contracts — detail header: "Latest score {scoreLabel}"
  // Results are confirmed present via adminApi above and fall inside the page's
  // default 30-day window, so the badge renders on the initial query.
  await expect(page.getByText(/Latest score/i).first()).toBeVisible({ timeout: 15_000 });

  // -- Backend probe: GET result range → 3 rows, descending order --
  // spec: USE_CASE_en.md §UC2 step 3 — GET prior series as baseline without re-scanning.
  const getResp = await adminApi.get(
    `${PG_RESULT_API}?from=${encodeURIComponent(from)}&until=${encodeURIComponent(until)}&limit=10`
  );
  expect(getResp.status()).toBe(200);
  const resultBody = (await getResp.json()) as {
    total_count: number;
    results: Array<{ data_time: string; score: number }>;
  };
  expect(resultBody.total_count).toBe(3);
  const returnedDates = resultBody.results.map((r) => r.data_time.slice(0, 10));
  // spec: VALIDATION.md §GET result — descending data_time order (newest first).
  expect(returnedDates).toEqual([dateOnly(1), dateOnly(2), dateOnly(3)]);
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 3 — Cross-dataset list shows BOTH datasets
// spec: USE_CASE_en.md §UC2 — "The caller checks the cross-dataset validation
//   list to see which datasets have recent quality signals."
// spec: FRONTEND_VALIDATION.md §Page contracts — list page: one row per dataset;
//   columns: dataset_urn, description, variables, latest data_time, Quality Score.
// ─────────────────────────────────────────────────────────────────────────────
test("UC2 step 3 — /validation list shows both datasets with score badges", async ({
  page,
  adminApi,
}) => {
  if (!pgConfCreated || !kafkaConfCreated) test.skip();

  // Navigate to the validation list page.
  await page.goto("/validation");
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: page heading --
  // spec: FRONTEND_VALIDATION.md §Navigation — list page title "Validation"
  await expect(page.getByRole("heading", { name: "Validation", exact: true })).toBeVisible({
    timeout: 15_000,
  });

  // -- UI assertion: postgres URN link visible in the table --
  // spec: FRONTEND_VALIDATION.md §Page contracts — each row has a URN link.
  // The table renders multiple URN strings; .first() avoids strict-mode violations
  // when the URN appears in both a link and a raw-JSON-like cell.
  await expect(page.getByText(PG_URN, { exact: false }).first()).toBeVisible({ timeout: 20_000 });

  // -- UI assertion: kafka URN link visible --
  await expect(page.getByText(KAFKA_URN, { exact: false }).first()).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: postgres description rendered (truncated, within 240px cell) --
  // spec: FRONTEND_VALIDATION.md §Page contracts — description column (max-w-[240px] truncate)
  // We check a prefix of the description that is short enough to appear un-truncated.
  await expect(page.getByText("Daily order fulfillment quality", { exact: false }).first()).toBeVisible();

  // -- UI assertion: at least one Quality Score badge rendered --
  // spec: FRONTEND_VALIDATION.md §Page contracts — Quality Score column: badge or "—"
  // The badge for score=1.0 renders scoreLabel(1.0) = "1.0000".
  // The kafka dataset's latest score (day_1: 0.85) renders "0.8500".
  // Locate whichever appears first. Avoid exact match to tolerate badge wrapping.
  await expect(page.getByText("1.0000", { exact: false }).first()).toBeVisible({ timeout: 10_000 });

  // -- Backend probe: GET /spoke/validation → BOTH URNs in validations --
  // spec: VALIDATION.md §API Surface — aggregates conf + latest result per dataset.
  const listResp = await adminApi.get("/api/v1/spoke/validation?limit=100");
  expect(listResp.status()).toBe(200);
  const listBody = (await listResp.json()) as {
    validations: Array<{
      dataset_urn: string;
      description: string;
      variable_count: number;
      latest_data_time: string | null;
      latest_score: number | null;
      is_removed: boolean;
    }>;
  };
  const byUrn = new Map(listBody.validations.map((v) => [v.dataset_urn, v]));

  expect(byUrn.has(PG_URN), `Postgres URN missing from /validation list`).toBe(true);
  expect(byUrn.has(KAFKA_URN), `Kafka URN missing from /validation list`).toBe(true);

  const pgItem = byUrn.get(PG_URN)!;
  expect(pgItem.description).toBe(PG_DESCRIPTION);
  expect(pgItem.variable_count).toBe(3);
  expect(pgItem.latest_data_time).not.toBeNull();
  expect(pgItem.latest_score).not.toBeNull();
  expect(pgItem.is_removed).toBe(false);

  const kafkaItem = byUrn.get(KAFKA_URN)!;
  expect(kafkaItem.variable_count).toBe(2);
  expect(kafkaItem.is_removed).toBe(false);
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 4 — Per-dataset detail: conf + charts + event log
// spec: USE_CASE_en.md §UC2 — per-dataset view shows conf, historical timeseries,
//   and event log.
// spec: FRONTEND_VALIDATION.md §Page contracts — detail page sections:
//   attr/validation/conf, Quality Score chart, Variables chart, event/validation (latest 5)
// ─────────────────────────────────────────────────────────────────────────────
test("UC2 step 4 — postgres detail page renders conf, charts, and event log", async ({
  page,
  adminApi,
}) => {
  if (!pgConfCreated) test.skip();

  await page.goto(PG_DETAIL_URL);
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: conf section heading --
  // spec: FRONTEND_VALIDATION.md §Page contracts — section heading "attr/validation/conf"
  await expect(
    page.getByRole("heading", { name: "attr/validation/conf", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: description text rendered in the ConfReadOnly view --
  await expect(page.getByText(PG_DESCRIPTION, { exact: false })).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: variable badges rendered (ConfReadOnly renders each variable as Badge) --
  // spec: FRONTEND_VALIDATION.md §Page contracts — variables list with inline badges
  // The badge for "row_cnt" has variant="outline" and font-mono text; locate by text.
  // Use .first() because the variable name also appears in the chart legend.
  await expect(page.getByText("row_cnt", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("fill_rate", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("anomaly_score", { exact: true }).first()).toBeVisible();

  // -- UI assertion: score chart section heading --
  // spec: FRONTEND_VALIDATION.md §Page contracts — "Quality Score (attr/validation/result)"
  await expect(
    page.getByRole("heading", { name: "Quality Score (attr/validation/result)", exact: true })
  ).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: variables chart section heading --
  // spec: FRONTEND_VALIDATION.md §Page contracts — "Variables (attr/validation/result)"
  await expect(
    page.getByRole("heading", { name: "Variables (attr/validation/result)", exact: true })
  ).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: event log section heading --
  // spec: FRONTEND_VALIDATION.md §Page contracts — "event/validation (latest 5)"
  // Use exact: true — avoids matching a longer heading that contains "validation".
  await expect(
    page.getByRole("heading", { name: "event/validation (latest 5)", exact: true })
  ).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: Edit and Delete buttons visible (admin can write) --
  // spec: FRONTEND_VALIDATION.md §Page contracts — write actions rendered for Editor/Admin
  await expect(page.getByRole("button", { name: "Edit" })).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole("button", { name: "Delete" })).toBeVisible({ timeout: 10_000 });

  // -- Backend probe: GET conf → 200, description + variables match --
  const confResp = await adminApi.get(PG_CONF_API);
  expect(confResp.status()).toBe(200);
  const conf = (await confResp.json()) as { description: string; variables: string[] };
  expect(conf.description).toBe(PG_DESCRIPTION);
  expect(conf.variables).toEqual(["row_cnt", "fill_rate", "anomaly_score"]);

  // -- Backend probe: GET event → 200, at least one event logged --
  // spec: VALIDATION.md §Validation Result — each accepted POST emits one event.
  // The event route for validation is /event with domain=validation.
  const eventResp = await adminApi.get(
    `/api/v1/spoke/common/data/${PG_URN_ENC}/event/validation`
  );
  expect(eventResp.status()).toBe(200);
  const eventBody = (await eventResp.json()) as { events: unknown[] };
  expect(Array.isArray(eventBody.events)).toBe(true);
  expect(eventBody.events.length).toBeGreaterThanOrEqual(1);
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 5 — DELETE postgres conf via UI; verify soft-delete semantics
// spec: USE_CASE_en.md §UC2 — "The DE retires the rule for the fulfillment table."
// spec: VALIDATION.md §Rule Configuration — DELETE performs soft delete; GET → 404.
// spec: FRONTEND_VALIDATION.md §Page contracts — Delete button → ConfirmDialog →
//   redirect to /validation list.
// ─────────────────────────────────────────────────────────────────────────────
test("UC2 step 5 — Delete postgres conf via ConfirmDialog; soft-delete semantics", async ({
  page,
  adminApi,
}) => {
  if (!pgConfCreated) test.skip();

  await page.goto(PG_DETAIL_URL);
  await expect(page).not.toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: "attr/validation/conf", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI gesture: click Delete button --
  // spec: FRONTEND_VALIDATION.md §Page contracts — Delete behind ConfirmDialog
  await page.getByRole("button", { name: "Delete" }).click();

  // -- UI gesture: confirm in the ConfirmDialog --
  // spec: FRONTEND_BASIC.md §ConfirmDialog — confirm button label matches confirmLabel="Delete"
  // The ConfirmDialog uses confirmLabel="Delete"; click the last "Delete" button to
  // avoid matching the initial Delete button still in the DOM.
  await page.getByRole("button", { name: "Delete", exact: true }).last().click();

  // -- UI assertion: redirected to /validation list --
  // spec: FRONTEND_VALIDATION.md §Page contracts — on delete, router.push("/validation")
  await page.waitForURL(/\/validation$/, { timeout: 30_000 });

  // Mark as deleted so afterAll does not attempt a double-delete.
  pgConfCreated = false;

  // -- UI assertion: postgres URN link no longer visible in the list --
  // The list page renders active confs only (no `removed` param → active by default).
  // Wait briefly for the list to re-render after the redirect.
  await expect(page.getByText(PG_URN, { exact: false }).first()).not.toBeVisible({
    timeout: 10_000,
  });

  // -- UI assertion: kafka URN still visible (not deleted) --
  await expect(page.getByText(KAFKA_URN, { exact: false }).first()).toBeVisible({ timeout: 10_000 });

  // -- Backend probe: GET postgres conf → 404 --
  // spec: VALIDATION.md §Rule Configuration — soft-delete makes GET return 404.
  const confResp = await adminApi.get(PG_CONF_API);
  expect(confResp.status()).toBe(404);

  // -- Backend probe: ?removed=true includes postgres --
  // spec: VALIDATION.md §Rule Configuration — ?removed=true lists tombstoned slots.
  const removedResp = await adminApi.get("/api/v1/spoke/validation?removed=true&limit=100");
  expect(removedResp.status()).toBe(200);
  const removedBody = (await removedResp.json()) as { validations: Array<{ dataset_urn: string }> };
  const removedUrns = removedBody.validations.map((v) => v.dataset_urn);
  expect(removedUrns).toContain(PG_URN);

  // -- Backend probe: ?removed=false → kafka present, postgres absent --
  const activeResp = await adminApi.get("/api/v1/spoke/validation?removed=false&limit=100");
  expect(activeResp.status()).toBe(200);
  const activeBody = (await activeResp.json()) as { validations: Array<{ dataset_urn: string }> };
  const activeUrns = activeBody.validations.map((v) => v.dataset_urn);
  expect(activeUrns).not.toContain(PG_URN);
  expect(activeUrns).toContain(KAFKA_URN);
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 6 — Detail page after soft-delete: create form shown, no Edit/Delete
// spec: USE_CASE_en.md §UC2 — after soft-delete, the slot is gone; the page
//   shows the "No validation config" empty state for Admin/Editor.
// spec: FRONTEND_VALIDATION.md §Page contracts — is404 branch: canWrite → create form.
// ─────────────────────────────────────────────────────────────────────────────
test("UC2 step 6 — postgres detail after delete shows create form, no Edit/Delete", async ({
  page,
}) => {
  // pgConfCreated is false at this point (deleted in step 5).
  // Navigate directly by URL; the page should 404 → empty state.
  await page.goto(PG_DETAIL_URL);
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: "attr/validation/conf" section still renders --
  await expect(
    page.getByRole("heading", { name: "attr/validation/conf", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: empty-state message shown --
  // spec: FRONTEND_VALIDATION.md §Page contracts — is404 + canWrite:
  //   "No validation config exists for this dataset. Create one below."
  await expect(
    page.getByText("No validation config exists for this dataset. Create one below.", {
      exact: false,
    })
  ).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: the create form's Save button is rendered (not Edit/Delete) --
  // The create form has a Save button (type=submit). There is no Edit button when
  // no conf exists, and no Delete button.
  await expect(page.getByRole("button", { name: "Save" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Edit" })).not.toBeVisible();
  await expect(page.getByRole("button", { name: "Delete" })).not.toBeVisible();
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 7 — PUT-after-DELETE (resurrection) via the create form
// spec: USE_CASE_en.md §UC2 — "The DE reinstates the rule with updated variable names."
// spec: VALIDATION.md §Rule Configuration — subsequent PUT resurrects; same URN reused.
// spec: FRONTEND_VALIDATION.md §Page contracts — create form submits PUT; on success
//   conf renders in read-only view.
// ─────────────────────────────────────────────────────────────────────────────
test("UC2 step 7 — resurrect postgres conf via create form; detail shows new description", async ({
  page,
  adminApi,
}) => {
  await page.goto(PG_DETAIL_URL);
  await expect(page).not.toHaveURL(/\/login/);

  // Wait for the create form to be present.
  await expect(page.getByRole("button", { name: "Save" })).toBeVisible({ timeout: 15_000 });

  // -- UI gesture: fill description field --
  // spec: FRONTEND_VALIDATION.md §Page contracts — description textarea id="validation-description"
  await page.locator("#validation-description").fill(RESURRECT_DESCRIPTION);

  // -- UI gesture: fill the first variable field (pre-filled with empty input) --
  // The form starts with one empty variable row.
  // spec: validation-conf-form.tsx — aria-label="Variable name 1" for the first input.
  await page.getByLabel("Variable name 1").fill(RESURRECT_VARIABLES[0]!);

  // -- UI gesture: add additional variable fields via "+ Add" button --
  // spec: validation-conf-form.tsx — "Add" button appends a new variable row.
  for (let i = 1; i < RESURRECT_VARIABLES.length; i++) {
    await page.getByRole("button", { name: "Add" }).click();
    await page.getByLabel(`Variable name ${i + 1}`).fill(RESURRECT_VARIABLES[i]!);
  }

  // -- UI gesture: submit the form --
  await page.getByRole("button", { name: "Save" }).click();

  // -- UI assertion: form closes; ConfReadOnly renders with new description --
  // On success, isEditing flips to false; the conf read-only view appears.
  // The new description should be visible within the section.
  await expect(page.getByText(RESURRECT_DESCRIPTION, { exact: false })).toBeVisible({
    timeout: 20_000,
  });

  // -- UI assertion: no more Save button (form is closed) --
  await expect(page.getByRole("button", { name: "Save" })).not.toBeVisible({ timeout: 5_000 });

  // -- UI assertion: Edit + Delete buttons re-appear (conf now exists) --
  await expect(page.getByRole("button", { name: "Edit" })).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole("button", { name: "Delete" })).toBeVisible();

  // Mark as created so afterAll cleans up.
  pgConfCreated = true;

  // -- Backend probe: GET conf → 200 with new description and extended variables --
  // spec: USE_CASE_en.md §UC2 step 6 — PUT-after-DELETE 201; GET conf 200 with new values.
  const confResp = await adminApi.get(PG_CONF_API);
  expect(confResp.status()).toBe(200);
  const conf = (await confResp.json()) as { description: string; variables: string[] };
  expect(conf.description).toBe(RESURRECT_DESCRIPTION);
  // spec: VALIDATION.md §Rule Configuration — subsequent PUT resurrects with new variables.
  expect(conf.variables).toContain("null_rate");
  for (const v of RESURRECT_VARIABLES) {
    expect(conf.variables).toContain(v);
  }
});
