/**
 * UC2 — Validation: browser UI flow.
 *
 * Mirrors tests/integration/api_wired/test_uc2_01_validation.py step-for-step,
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
 *   4. Per-dataset detail (/data/[urn], "Validation" panel) for postgres shows:
 *      - conf section (description + variable badges)
 *      - score chart panel rendered ("Quality Score (attr/validation/result)")
 *      - variables chart panel rendered ("Variables (attr/validation/result)")
 *      - validation events appear in the unified "Events" panel
 *   5. DELETE (freeze) postgres conf via the Delete button + ConfirmDialog:
 *      - redirected to /validation list
 *      - postgres row absent from active list
 *      - kafka row still present
 *      Backend: GET conf → 404 VALIDATION_CONF_REMOVED; ?removed=true includes postgres.
 *   6. Navigate to /data/[postgres urn] (Validation panel, frozen now):
 *      - by default (page-level "Show deleted" OFF) the panel reads as a
 *        never-created slot: a Create empty-state, no Undelete, no frozen note.
 *      - after checking the header "Show deleted" box the frozen-rule view
 *        appears: the deleted note + an "Undelete" button only (no Create/Edit/Delete).
 *   7. Restore (undelete): enable "Show deleted", click Undelete to reinstate the
 *      FROZEN conf unchanged.
 *      Backend: GET conf → 200 with the SAME frozen variables (no null_rate added);
 *      the preserved result history is still queryable. Then edit the now-active rule.
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

// Frontend routes — the per-dataset detail surface is the unified hub
// /data/[urn]; the validation body now lives under its "Validation"
// CollapsiblePanel (open by default) and the validation events fold into the
// unified "Events" panel.
// spec: FRONTEND_BASIC.md §Per-dataset page; FRONTEND_VALIDATION.md §Detail (moved to /data/[urn])
const PG_DETAIL_URL = `/data/${PG_URN_ENC}`;
const KAFKA_DETAIL_URL = `/data/${KAFKA_URN_ENC}`;

// Conf variable objects: {name, description} (verbatim shape from api-wired test).
// spec: VALIDATION.md §Rule Configuration — each variable is a {name, description}.
const PG_DESCRIPTION =
  "Daily order fulfillment quality: row count, fill rate, and anomaly score";
const PG_VARIABLES = [
  { name: "row_cnt", description: "Daily fulfillment row count" },
  { name: "fill_rate", description: "Fraction of orders fully shipped" },
  { name: "anomaly_score", description: "Detector score for the day" },
];
const PG_CONF_PAYLOAD = {
  description: PG_DESCRIPTION,
  variables: PG_VARIABLES,
};
const KAFKA_VARIABLES = [
  { name: "msg_cnt", description: "Messages produced in the window" },
  { name: "lag_seconds", description: "Consumer lag in seconds" },
];
const KAFKA_CONF_PAYLOAD = {
  description: "Order events stream quality: message count and lag",
  variables: KAFKA_VARIABLES,
};

// Edit-after-restore payload (step 7) — after the frozen rule is restored as-is,
// the now-active rule is edited via the normal Edit flow to add a new variable.
// names only; descriptions default to empty. The order matches the form gestures.
const EDIT_DESCRIPTION = "Reinstated quality check with extended variables";
const EDIT_VARIABLES = ["row_cnt", "fill_rate", "anomaly_score", "null_rate"];

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

// Serial mode: the steps below form one ordered, stateful scenario (each step
// depends on backend resources + module state established by the prior step).
// In serial mode the file's tests run as one group; if a step fails, the WHOLE
// group is retried together — re-running every step in order and re-establishing
// both module state and backend state. The create/mutate steps tolerate
// "already exists" on a group-retry (PUT-conf and POST-result upsert → 200).
// spec: spec/TESTING.md §E2E — dependent sequential steps use describe.serial.
test.describe.configure({ mode: "serial" });

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
  // 201 on first create; 200 if a group-retry re-PUTs an existing conf (upsert).
  expect([200, 201]).toContain(pgResp.status());
  // spec: VALIDATION.md §Rule Configuration — variables round-trip as {name, description}.
  const pgBody = (await pgResp.json()) as {
    variables: Array<{ name: string; description: string }>;
    description: string;
  };
  expect(pgBody.variables).toEqual(PG_VARIABLES);
  expect(pgBody.description).toBe(PG_DESCRIPTION);
  pgConfCreated = true;

  // PUT kafka conf.
  const kafkaResp = await adminApi.put(KAFKA_CONF_API, { data: KAFKA_CONF_PAYLOAD });
  // 201 on first create; 200 if a group-retry re-PUTs an existing conf (upsert).
  expect([200, 201]).toContain(kafkaResp.status());
  const kafkaBody = (await kafkaResp.json()) as {
    variables: Array<{ name: string; description: string }>;
  };
  expect(kafkaBody.variables).toEqual(KAFKA_VARIABLES);
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
  // POST 3 daily results for postgres.
  for (const payload of PG_RESULTS) {
    const resp = await adminApi.post(PG_RESULT_API, { data: payload });
    // 201 on first insert; 200 if a group-retry re-POSTs the same data_time (upsert).
    expect([200, 201]).toContain(resp.status());
  }

  // POST 2 daily results for kafka.
  for (const payload of KAFKA_RESULTS) {
    const resp = await adminApi.post(KAFKA_RESULT_API, { data: payload });
    // 201 on first insert; 200 if a group-retry re-POSTs the same data_time (upsert).
    expect([200, 201]).toContain(resp.status());
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

  // Navigate to the postgres detail page and assert its chart panels render.
  // Wrap the goto + UI visibility checks in a retry block to absorb residual
  // client-side render lag after the backend results are confirmed present —
  // without it a render flake fails the test and triggers a serial group-retry.
  // spec: TESTING.md §Assertion Principles — retry bounded deadline instead of fixed sleep.
  await expect(async () => {
    await page.goto(PG_DETAIL_URL);
    await expect(page).not.toHaveURL(/\/login/);

    // -- UI assertion: score chart panel rendered --
    // spec: FRONTEND_VALIDATION.md §Page contracts — score chart section heading
    // The section heading for the score chart is "Quality Score (attr/validation/result)".
    // Use exact: true to avoid matching the "Quality Score" column header on the list page.
    await expect(
      page.getByRole("heading", { name: "Quality Score (attr/validation/result)", exact: true })
    ).toBeVisible({ timeout: 10_000 });

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
    await expect(page.getByText(/Latest score/i).first()).toBeVisible({ timeout: 10_000 });
  }).toPass({ timeout: 120_000, intervals: [2_000, 3_000, 5_000, 10_000] });

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
  // Readiness poll: the cross-dataset list is sourced via DataHub ES, which lags
  // conf/result writes by ~2-3 min. Poll until BOTH dataset URNs are present in
  // the aggregated list before navigating + asserting the UI table.
  // spec: TESTING.md §Assertion Principles — poll bounded deadline instead of fixed sleep.
  const deadline = Date.now() + 180_000;
  let listReady = false;
  while (Date.now() < deadline) {
    const r = await adminApi.get("/api/v1/spoke/validation?limit=100");
    if (r.ok()) {
      const body = (await r.json()) as {
        validations: Array<{ dataset_urn: string }>;
      };
      const urns = new Set(body.validations.map((v) => v.dataset_urn));
      if (urns.has(PG_URN) && urns.has(KAFKA_URN)) {
        listReady = true;
        break;
      }
    }
    await new Promise((res) => setTimeout(res, 3_000));
  }
  expect(listReady, "both dataset URNs not present in /validation list within deadline").toBe(true);

  // Navigate + assert the data-dependent UI, retrying the whole block to absorb
  // residual client-side render lag after the backend is ready.
  await expect(async () => {
    await page.goto("/validation");
    await expect(page).not.toHaveURL(/\/login/);

    // -- UI assertion: page heading --
    // spec: FRONTEND_VALIDATION.md §Navigation — list page title "Validation"
    await expect(page.getByRole("heading", { name: "Validation", exact: true })).toBeVisible({
      timeout: 10_000,
    });

    // -- UI assertion: postgres URN link visible in the table --
    // spec: FRONTEND_VALIDATION.md §Page contracts — each row has a URN link.
    // The table renders multiple URN strings; .first() avoids strict-mode violations
    // when the URN appears in both a link and a raw-JSON-like cell.
    await expect(page.getByText(PG_URN, { exact: false }).first()).toBeVisible({ timeout: 10_000 });

    // -- UI assertion: kafka URN link visible --
    await expect(page.getByText(KAFKA_URN, { exact: false }).first()).toBeVisible({ timeout: 10_000 });

    // -- UI assertion: postgres description rendered (truncated, within 240px cell) --
    // spec: FRONTEND_VALIDATION.md §Page contracts — description column (max-w-[240px] truncate)
    // We check a prefix of the description that is short enough to appear un-truncated.
    await expect(page.getByText("Daily order fulfillment quality", { exact: false }).first()).toBeVisible({ timeout: 10_000 });

    // -- UI assertion: Quality Score badge for the postgres dataset's row --
    // spec: FRONTEND_VALIDATION.md §Page contracts — Quality Score column: badge or "—"
    // The badge for score=1.0 renders scoreLabel(1.0) = "1.0000".
    // Scoped to the PG_URN row (the validation table uses <TableRow key={v.dataset_urn}>
    // with PG_URN as the first cell text) so stale rows with a different dataset's score
    // cannot satisfy this assertion. The URN is unique enough to anchor the row.
    const pgRow = page.getByRole("row").filter({ hasText: PG_URN });
    await expect(pgRow.getByText("1.0000", { exact: false })).toBeVisible({ timeout: 10_000 });
  }).toPass({ timeout: 120_000, intervals: [2_000, 3_000, 5_000, 10_000] });

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
// Step 4 — Per-dataset detail: Validation panel conf + charts + unified Events
// spec: USE_CASE_en.md §UC2 — per-dataset view shows conf, historical timeseries,
//   and the dataset's events.
// spec: FRONTEND_BASIC.md §Per-dataset page — the /data/[urn] hub renders the
//   validation conf + score/variables charts inside the "Validation"
//   CollapsiblePanel; the per-dataset validation events fold into the unified
//   "Events" panel (one timeline with a major-type filter, default all checked).
// ─────────────────────────────────────────────────────────────────────────────
test("UC2 step 4 — postgres detail page renders conf, charts, and validation events", async ({
  page,
  adminApi,
}) => {
  await page.goto(PG_DETAIL_URL);
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: the dataset hub loaded (URN header rendered) --
  // spec: FRONTEND_BASIC.md §Per-dataset page — header shows the dataset URN.
  await expect(page.getByRole("heading", { name: PG_URN, exact: true })).toBeVisible({
    timeout: 15_000,
  });

  // -- UI assertion: description text rendered in the ConfReadOnly view --
  await expect(page.getByText(PG_DESCRIPTION, { exact: false })).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: variable badges rendered (ConfReadOnly renders each variable as Badge) --
  // spec: FRONTEND_VALIDATION.md §Page contracts — variables list with inline badges
  // The badge for "row_cnt" has variant="outline" and font-mono text; locate by text.
  // Use .first() because the variable name also appears in the chart legend.
  await expect(page.getByText("row_cnt", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("fill_rate", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("anomaly_score", { exact: true }).first()).toBeVisible();

  // -- UI assertion: per-variable descriptions render in the ConfReadOnly view --
  // spec: FRONTEND_VALIDATION.md §Page contracts — each variable shows its description.
  // ConfReadOnly renders {v.description} next to the variable name badge.
  await expect(
    page.getByText("Daily fulfillment row count", { exact: false }).first()
  ).toBeVisible({ timeout: 10_000 });
  await expect(
    page.getByText("Fraction of orders fully shipped", { exact: false }).first()
  ).toBeVisible();

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

  // -- UI assertion: validation events surface in the unified "Events" panel --
  // The former per-feature "event/validation" section is folded into the unified
  // Events panel (a single timeline with a major-type filter; default all checked).
  // Toggle the "Events" CollapsiblePanel open if collapsed, then assert a
  // VALIDATION.RESULT_RECORDED row appears (mirrors UC4 step 9's pattern).
  // spec: FRONTEND_BASIC.md §Per-dataset page (Events panel).
  const eventsPanel = page.getByRole("button", { name: /events/i }).first();
  await expect(eventsPanel).toBeVisible({ timeout: 10_000 });
  if ((await eventsPanel.getAttribute("aria-expanded")) === "false") {
    await eventsPanel.click();
  }
  await expect(
    page.getByText("VALIDATION.RESULT_RECORDED", { exact: false }).first()
  ).toBeVisible({ timeout: 20_000 });

  // -- UI assertion: Edit and Delete buttons visible (admin can write) --
  // spec: FRONTEND_VALIDATION.md §Page contracts — write actions rendered for Editor/Admin
  // Scope to the Validation panel <section>: on the merged hub the MetaGen panel
  // also renders Edit/Delete (for its boundary), so an unscoped locator can be
  // ambiguous in strict mode.
  const validationPanel = page
    .locator("section")
    .filter({ has: page.getByRole("button", { name: "Validation", exact: true }) });
  await expect(validationPanel.getByRole("button", { name: "Edit", exact: true })).toBeVisible({
    timeout: 10_000,
  });
  await expect(validationPanel.getByRole("button", { name: "Delete", exact: true })).toBeVisible({
    timeout: 10_000,
  });

  // -- Backend probe: GET conf → 200, description + variables match --
  const confResp = await adminApi.get(PG_CONF_API);
  expect(confResp.status()).toBe(200);
  const conf = (await confResp.json()) as {
    description: string;
    variables: Array<{ name: string; description: string }>;
  };
  expect(conf.description).toBe(PG_DESCRIPTION);
  expect(conf.variables).toEqual(PG_VARIABLES);

  // -- Backend probe: unified timeline filtered to VALIDATION → RESULT_RECORDED --
  // spec: VALIDATION.md §Validation Result — each accepted POST emits one event.
  // spec: FRONTEND_BASIC.md §Per-dataset page — the per-dataset timeline is the
  //   unified GET …/event with the repeatable event_major_type filter; VALIDATION
  //   narrows to the dataset's validation events (VALIDATION.RESULT_RECORDED rows).
  const eventResp = await adminApi.get(
    `/api/v1/spoke/common/data/${PG_URN_ENC}/event?event_major_type=VALIDATION&limit=50`
  );
  expect(eventResp.status()).toBe(200);
  const eventBody = (await eventResp.json()) as {
    events: Array<{ event_type: string }>;
    total_count: number;
  };
  expect(Array.isArray(eventBody.events)).toBe(true);
  expect(eventBody.events.length).toBeGreaterThanOrEqual(1);
  // The VALIDATION filter must only return VALIDATION.* rows.
  for (const e of eventBody.events) {
    expect(e.event_type.startsWith("VALIDATION.")).toBe(true);
  }
  expect(
    eventBody.events.some((e) => e.event_type === "VALIDATION.RESULT_RECORDED"),
    "expected a VALIDATION.RESULT_RECORDED row in the unified timeline"
  ).toBe(true);
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 5 — DELETE (freeze) postgres conf via UI; verify freeze semantics
// spec: USE_CASE_en.md §UC2 — "The DE retires the rule for the fulfillment table."
// spec: VALIDATION.md §Rule Configuration — DELETE freezes the rule; GET → 404
//   VALIDATION_CONF_REMOVED; result history preserved.
// spec: FRONTEND_VALIDATION.md §Page contracts — Delete button → ConfirmDialog →
//   redirect to /validation list.
// ─────────────────────────────────────────────────────────────────────────────
test("UC2 step 5 — Delete (freeze) postgres conf via ConfirmDialog; freeze semantics", async ({
  page,
  adminApi,
}) => {
  await page.goto(PG_DETAIL_URL);
  await expect(page).not.toHaveURL(/\/login/);
  // The Validation CollapsiblePanel is open by default; wait for its Delete
  // action to render (confirms the conf body loaded under the hub page).
  // spec: FRONTEND_BASIC.md §Per-dataset page — Validation panel hosts the conf.

  // -- UI gesture: click Delete button --
  // spec: FRONTEND_VALIDATION.md §Page contracts — Delete behind ConfirmDialog
  // Scope to the Validation panel <section>: the merged hub's MetaGen panel can
  // also render a Delete button (for its boundary), so an unscoped locator may
  // be ambiguous in strict mode.
  const validationPanel = page
    .locator("section")
    .filter({ has: page.getByRole("button", { name: "Validation", exact: true }) });
  await expect(validationPanel.getByRole("button", { name: "Delete", exact: true })).toBeVisible({
    timeout: 15_000,
  });
  await validationPanel.getByRole("button", { name: "Delete", exact: true }).click();

  // -- UI gesture: confirm in the ConfirmDialog --
  // spec: FRONTEND_BASIC.md §ConfirmDialog — confirm button label matches confirmLabel="Delete"
  // The ConfirmDialog confirm button (also labelled "Delete") renders in a portal
  // outside the panel section; click the last "Delete" button on the page to hit
  // the dialog confirm rather than the panel trigger still in the DOM.
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

  // -- Backend probe: GET postgres conf → 404 VALIDATION_CONF_REMOVED --
  // spec: VALIDATION.md §Rule Configuration — DELETE freezes the rule; GET on the
  // frozen slot returns 404 with error_code VALIDATION_CONF_REMOVED (a *restorable*
  // tombstone, distinct from CONFIG_NOT_FOUND for a never-created slot).
  const confResp = await adminApi.get(PG_CONF_API);
  expect(confResp.status()).toBe(404);
  const confErrBody = (await confResp.json()) as { error_code?: string };
  expect(confErrBody.error_code).toBe("VALIDATION_CONF_REMOVED");

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
// Step 6 — Detail page after freeze: gated by the page-level "Show deleted" toggle.
// spec: USE_CASE_en.md §UC2 — after retiring, the rule is frozen; the only way back
//   is an explicit Undelete (restore). No redefining on restore.
// spec: FRONTEND_BASIC.md §Per-dataset page (ShowDeletedToggle) +
//       FRONTEND_VALIDATION.md §Detail — the soft-deleted slot
//   (VALIDATION_CONF_REMOVED) is HIDDEN BY DEFAULT: while the page-level
//   "Show deleted" checkbox is OFF the Validation panel reads as a never-created
//   slot (Create empty-state, no Undelete). Flipping the header checkbox ON
//   reveals the frozen-rule view: the deleted note + an "Undelete" button only
//   (no Create form, no Edit/Delete).
// ─────────────────────────────────────────────────────────────────────────────
test("UC2 step 6 — postgres detail after freeze: Create empty-state by default, frozen Undelete only after 'Show deleted'", async ({
  page,
}) => {
  // The conf is frozen (soft-deleted in step 5). Navigate directly by URL; the
  // page reads 404 VALIDATION_CONF_REMOVED.
  await page.goto(PG_DETAIL_URL);
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: the dataset hub loaded (URN header rendered) --
  // spec: FRONTEND_BASIC.md §Per-dataset page — header shows the dataset URN.
  await expect(page.getByRole("heading", { name: PG_URN, exact: true })).toBeVisible({
    timeout: 15_000,
  });

  // Scope conf affordances to the Validation panel <section>: the merged hub's
  // MetaGen panel may render its own Edit/Delete (for its boundary), which an
  // unscoped negative assertion would wrongly trip on.
  const validationPanel = page
    .locator("section")
    .filter({ has: page.getByRole("button", { name: "Validation", exact: true }) });

  // -- UI assertion: default (toggle OFF) → Create empty-state, NO Undelete, no
  //    leaked frozen note --
  // spec: FRONTEND_VALIDATION.md §Detail — removed slot hidden by default reads as
  //   never-created (Create empty-state, identical to CONFIG_NOT_FOUND).
  await expect(validationPanel.getByRole("button", { name: "Create" })).toBeVisible({
    timeout: 10_000,
  });
  await expect(validationPanel.getByRole("button", { name: "Undelete" })).not.toBeVisible();
  await expect(
    page.getByText("This validation config is deleted.", { exact: false })
  ).not.toBeVisible();

  // -- UI gesture: enable the page-level "Show deleted" checkbox in the header --
  // spec: FRONTEND_BASIC.md §Per-dataset page — header carries a "Show deleted"
  //   checkbox (default OFF) that unhides the frozen validation slot.
  await page.getByRole("checkbox", { name: "Show deleted" }).check();

  // -- UI assertion: frozen-rule view appears — deleted note + Undelete only --
  // spec: FRONTEND_VALIDATION.md §Detail — VALIDATION_CONF_REMOVED + Show deleted ON:
  //   "This validation config is deleted. Undelete it to restore the frozen rule…"
  await expect(
    page.getByText("This validation config is deleted.", { exact: false })
  ).toBeVisible({ timeout: 10_000 });
  await expect(validationPanel.getByRole("button", { name: "Undelete" })).toBeVisible();
  await expect(validationPanel.getByRole("button", { name: "Create" })).not.toBeVisible();
  await expect(validationPanel.getByRole("button", { name: "Edit", exact: true })).not.toBeVisible();
  // exact:true so this does NOT substring-match the "Undelete" button (which contains "Delete").
  await expect(validationPanel.getByRole("button", { name: "Delete", exact: true })).not.toBeVisible();
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 7 — Restore (undelete) reinstates the FROZEN rule unchanged, then edit
// spec: USE_CASE_en.md §UC2 — "The DE restores the retired rule; it comes back
//   exactly as it was, with its result history intact, and is edited afterward."
// spec: VALIDATION.md §Rule Configuration — restore reinstates the frozen
//   description/variables exactly (no redefinition); editing uses the active PUT/PATCH.
// spec: FRONTEND_VALIDATION.md §Page contracts — after restore the page returns to
//   the normal read/Edit/Delete state.
// ─────────────────────────────────────────────────────────────────────────────
test("UC2 step 7 — Undelete restores the frozen conf unchanged; result history intact; then edit", async ({
  page,
  adminApi,
}) => {
  await page.goto(PG_DETAIL_URL);
  await expect(page).not.toHaveURL(/\/login/);

  // The merged /data/[urn] hub puts the Validation and MetaGen panels on one
  // page, so unscoped action labels collide (e.g. the MetaGen panel's "Save
  // boundary" substring-matches a bare "Save", and a global "Save"/"Edit"
  // would be ambiguous in strict mode). Scope every validation conf action to
  // the Validation CollapsiblePanel <section> — the section that contains the
  // "Validation" panel-header toggle. (The sidebar "Validation" entry is a
  // role=link, not a button, so a button-name filter can't match it anyway;
  // this resolves to the panel section only.)
  // spec: FRONTEND_BASIC.md §Per-dataset page (one CollapsiblePanel per feature).
  const validationPanel = page
    .locator("section")
    .filter({ has: page.getByRole("button", { name: "Validation", exact: true }) });

  // -- UI gesture: enable the page-level "Show deleted" toggle to reveal the
  //    frozen slot (default OFF presents the removed slot as never-created) --
  // spec: FRONTEND_BASIC.md §Per-dataset page (ShowDeletedToggle) — the frozen
  //   Undelete affordance is gated by the header "Show deleted" checkbox.
  await page.getByRole("checkbox", { name: "Show deleted" }).check();

  // -- UI gesture: click Undelete to restore the frozen rule --
  await expect(validationPanel.getByRole("button", { name: "Undelete" })).toBeVisible({
    timeout: 15_000,
  });
  await validationPanel.getByRole("button", { name: "Undelete" }).click();

  // -- UI assertion: read-only conf view returns with the ORIGINAL frozen values --
  // Restore reinstates the frozen description/variables as-is — NOT a new set.
  await expect(page.getByText(PG_DESCRIPTION, { exact: false })).toBeVisible({ timeout: 20_000 });
  // The original variables come back; null_rate (added only on a later edit) is absent.
  await expect(page.getByText("row_cnt", { exact: true }).first()).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText("null_rate", { exact: true })).not.toBeVisible();

  // -- UI assertion: Edit + Delete return; Undelete gone (slot active again) --
  await expect(validationPanel.getByRole("button", { name: "Edit", exact: true })).toBeVisible({
    timeout: 10_000,
  });
  await expect(validationPanel.getByRole("button", { name: "Delete", exact: true })).toBeVisible();
  await expect(validationPanel.getByRole("button", { name: "Undelete" })).not.toBeVisible();

  // Conf is active again.
  pgConfCreated = true;

  // -- Backend probe: GET conf → 200 with the SAME frozen variables (no redefinition) --
  // spec: VALIDATION.md §Rule Configuration — restore reinstates frozen variables exactly.
  const restoredResp = await adminApi.get(PG_CONF_API);
  expect(restoredResp.status()).toBe(200);
  const restored = (await restoredResp.json()) as {
    description: string;
    variables: Array<{ name: string; description: string }>;
  };
  expect(restored.description).toBe(PG_DESCRIPTION);
  expect(restored.variables).toEqual(PG_VARIABLES);
  expect(restored.variables.map((v) => v.name)).not.toContain("null_rate");

  // -- Backend probe: the preserved result history is still queryable, unchanged --
  // spec: VALIDATION.md §Rule Configuration — validation_results survive freeze/restore.
  const from = daysAgoIso(5);
  const until = daysAgoIso(-1);
  const resultsResp = await adminApi.get(
    `${PG_RESULT_API}?from=${encodeURIComponent(from)}&until=${encodeURIComponent(until)}&limit=10`
  );
  expect(resultsResp.status()).toBe(200);
  const resultsBody = (await resultsResp.json()) as {
    total_count: number;
    results: Array<{ data_time: string }>;
  };
  expect(resultsBody.total_count).toBe(3);
  const dates = resultsBody.results.map((r) => r.data_time.slice(0, 10));
  expect(dates).toEqual([dateOnly(1), dateOnly(2), dateOnly(3)]);

  // ── Edit the now-active rule (restore then edit) ─────────────────────────────
  // spec: VALIDATION.md §Rule Configuration — "To redefine a rule after restoring,
  // edit the now-active slot with the normal PUT/PATCH." Click Edit, add null_rate.
  await validationPanel.getByRole("button", { name: "Edit", exact: true }).click();

  // -- UI gesture: update description --
  // spec: FRONTEND_VALIDATION.md §Page contracts — description textarea id="validation-description"
  await page.locator("#validation-description").fill(EDIT_DESCRIPTION);

  // The edit form is pre-filled with the 3 frozen variables; add a 4th (null_rate).
  // spec: validation-conf-form.tsx — "Add" button appends a new variable row.
  await validationPanel.getByRole("button", { name: "Add" }).click();
  await page.getByLabel(`Variable name ${EDIT_VARIABLES.length}`).fill("null_rate");

  // -- UI gesture: save the edit (validation panel header "Save" action) --
  // exact:true + panel scope so this never matches the MetaGen panel's
  // "Save boundary" button (which substring-matches a bare "Save").
  await validationPanel.getByRole("button", { name: "Save", exact: true }).click();

  // -- UI assertion: read-only view shows the edited description --
  await expect(page.getByText(EDIT_DESCRIPTION, { exact: false })).toBeVisible({ timeout: 20_000 });

  // -- Backend probe: GET conf → 200 with the edited description + null_rate added --
  // spec: VALIDATION.md §Rule Configuration — an active-slot PUT replaces the rule.
  const editedResp = await adminApi.get(PG_CONF_API);
  expect(editedResp.status()).toBe(200);
  const edited = (await editedResp.json()) as {
    description: string;
    variables: Array<{ name: string; description: string }>;
  };
  expect(edited.description).toBe(EDIT_DESCRIPTION);
  const editedNames = edited.variables.map((v) => v.name);
  expect(editedNames).toContain("null_rate");
  for (const v of EDIT_VARIABLES) {
    expect(editedNames).toContain(v);
  }
});
