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
 *   5. DELETE (hard delete + cascade) postgres conf via the Delete button + ConfirmDialog:
 *      - redirected to /validation list
 *      - postgres row absent from the list, kafka row still present
 *      Backend: GET conf → 404 CONFIG_NOT_FOUND (never-created); the result series and
 *      the dataset's validation events are gone (cascade); dataset absent from /validation.
 *   6. Navigate to /data/[postgres urn] (Validation panel): the panel reads as a
 *      never-created slot — a plain Create empty-state, no Undelete, no frozen note,
 *      no "Show deleted" toggle anywhere.
 *   7. Recreate via the Create form: fill description + variables, Save → a brand-new
 *      conf (no resurrection).
 *      Backend: GET conf → 200 with the freshly-supplied variables; the result series
 *      starts empty (the prior cascade is not undone).
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

// Recreate payload (step 7) — after the hard delete, the dataset reads as
// never-created and a fresh conf is created via the Create form. Variable names
// only; descriptions default to empty. The order matches the form gestures.
const RECREATE_DESCRIPTION = "Freshly re-registered fulfillment quality check";
const RECREATE_VARIABLES = ["row_cnt", "fill_rate"];

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
  // spec: API.md §GET /spoke/validation — the aggregated row carries no is_removed
  // field (soft-delete is gone).
  expect("is_removed" in pgItem).toBe(false);

  const kafkaItem = byUrn.get(KAFKA_URN)!;
  expect(kafkaItem.variable_count).toBe(2);
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
// Step 5 — DELETE (hard delete + cascade) postgres conf via UI
// spec: USE_CASE_en.md §UC2 — "The DE deletes the rule for the fulfillment table."
// spec: API.md §DELETE attr/validation/conf — hard delete: conf row removed, results
//   + validation events cascaded, DataHub assertion hard-deleted; afterwards GET → 404
//   CONFIG_NOT_FOUND and the dataset is absent from /spoke/validation.
// spec: FRONTEND_VALIDATION.md §Page contracts — Delete button → ConfirmDialog →
//   redirect to /validation list.
// ─────────────────────────────────────────────────────────────────────────────
test("UC2 step 5 — Delete (hard delete + cascade) postgres conf via ConfirmDialog", async ({
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
  // The list has no `removed` filter — a hard-deleted dataset is simply gone.
  await expect(page.getByText(PG_URN, { exact: false }).first()).not.toBeVisible({
    timeout: 10_000,
  });

  // -- UI assertion: kafka URN still visible (not deleted) --
  await expect(page.getByText(KAFKA_URN, { exact: false }).first()).toBeVisible({ timeout: 10_000 });

  // -- Backend probe: GET postgres conf → 404 CONFIG_NOT_FOUND (never-created) --
  // spec: API.md §DELETE attr/validation/conf — after a hard delete the dataset reads
  // as never-created; GET conf returns 404 with error_code CONFIG_NOT_FOUND.
  const confResp = await adminApi.get(PG_CONF_API);
  expect(confResp.status()).toBe(404);
  const confErrBody = (await confResp.json()) as { error_code?: string };
  expect(confErrBody.error_code).toBe("CONFIG_NOT_FOUND");

  // -- Backend probe: the result series is gone (cascade) --
  // spec: API.md §DELETE attr/validation/conf — cascades validation results.
  const from = daysAgoIso(5);
  const until = daysAgoIso(-1);
  const resultsResp = await adminApi.get(
    `${PG_RESULT_API}?from=${encodeURIComponent(from)}&until=${encodeURIComponent(until)}&limit=10`
  );
  expect(resultsResp.status()).toBe(200);
  expect(((await resultsResp.json()) as { total_count: number }).total_count).toBe(0);

  // -- Backend probe: the dataset's validation events are gone (cascade) --
  // spec: API.md §DELETE attr/validation/conf — cascades validation events.
  const eventsResp = await adminApi.get(
    `/api/v1/spoke/common/data/${PG_URN_ENC}/event?event_major_type=VALIDATION&limit=50`
  );
  expect(eventsResp.status()).toBe(200);
  expect(((await eventsResp.json()) as { total_count: number }).total_count).toBe(0);

  // -- Backend probe: dataset absent from /spoke/validation; kafka untouched --
  const listResp = await adminApi.get("/api/v1/spoke/validation?limit=100");
  expect(listResp.status()).toBe(200);
  const listBody = (await listResp.json()) as { validations: Array<{ dataset_urn: string }> };
  const urns = listBody.validations.map((v) => v.dataset_urn);
  expect(urns).not.toContain(PG_URN);
  expect(urns).toContain(KAFKA_URN);
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 6 — Detail page after hard delete: reads as a never-created slot.
// spec: USE_CASE_en.md §UC2 — after deletion the dataset reads as never-created;
//   there is no restore.
// spec: FRONTEND_VALIDATION.md §Detail — a deleted slot (404 CONFIG_NOT_FOUND) renders
//   the plain Create empty-state. There is no Undelete, no frozen note, and no
//   "Show deleted" toggle anywhere on the page (soft-delete is gone).
// ─────────────────────────────────────────────────────────────────────────────
test("UC2 step 6 — postgres detail after delete reads as a never-created Create empty-state", async ({
  page,
}) => {
  // The conf was hard-deleted in step 5. Navigate directly by URL; the page
  // reads 404 CONFIG_NOT_FOUND and renders the Create empty-state.
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

  // -- UI assertion: plain Create empty-state — no Undelete, no frozen note --
  // spec: FRONTEND_VALIDATION.md §Detail — a CONFIG_NOT_FOUND slot renders the Create
  //   form. The spec'd contract is the Create affordance + the absence of any
  //   Undelete/frozen-rule affordance; the empty-state body copy is incidental DOM and
  //   deliberately not pinned here (a copy tweak with no behavior change must not break E2E).
  await expect(validationPanel.getByRole("button", { name: "Create" })).toBeVisible({
    timeout: 10_000,
  });
  await expect(validationPanel.getByRole("button", { name: "Undelete" })).not.toBeVisible();
  await expect(
    page.getByText("This validation config is deleted.", { exact: false })
  ).not.toBeVisible();

  // -- UI assertion: no "Show deleted" toggle anywhere (soft-delete is gone) --
  // spec: FRONTEND_BASIC.md §Per-dataset page — the ShowDeletedToggle is removed.
  await expect(page.getByRole("checkbox", { name: "Show deleted" })).toHaveCount(0);
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 7 — Recreate via the Create form: a fresh conf (no resurrection)
// spec: USE_CASE_en.md §UC2 — the DE re-registers a rule for the fulfillment table;
//   it is a fresh slot, not a resurrected one.
// spec: API.md §DELETE attr/validation/conf — a fresh PUT creates a new conf (201);
//   the result series starts empty (the prior cascade is not undone).
// spec: FRONTEND_VALIDATION.md §Page contracts — the Create form submits a PUT.
// ─────────────────────────────────────────────────────────────────────────────
test("UC2 step 7 — recreate postgres conf via the Create form; fresh conf, empty result series", async ({
  page,
  adminApi,
}) => {
  await page.goto(PG_DETAIL_URL);
  await expect(page).not.toHaveURL(/\/login/);

  // Scope every validation conf action to the Validation CollapsiblePanel <section>
  // — the merged hub also renders MetaGen actions that would otherwise collide.
  // spec: FRONTEND_BASIC.md §Per-dataset page (one CollapsiblePanel per feature).
  const validationPanel = page
    .locator("section")
    .filter({ has: page.getByRole("button", { name: "Validation", exact: true }) });

  // The Create form is rendered inline for the never-created slot.
  await expect(validationPanel.getByRole("button", { name: "Create" })).toBeVisible({
    timeout: 15_000,
  });

  // -- UI gesture: fill description --
  // spec: FRONTEND_VALIDATION.md §Page contracts — description textarea id="validation-description"
  await page.locator("#validation-description").fill(RECREATE_DESCRIPTION);

  // -- UI gesture: fill the first variable row, then Add a second --
  // The Create form starts with one empty variable row ("Variable name 1").
  // spec: validation-conf-form.tsx — "Add" appends a new variable row.
  await page.getByLabel("Variable name 1").fill(RECREATE_VARIABLES[0]);
  await validationPanel.getByRole("button", { name: "Add" }).click();
  await page.getByLabel("Variable name 2").fill(RECREATE_VARIABLES[1]);

  // -- UI gesture: submit the Create form (header "Create" action) --
  await validationPanel.getByRole("button", { name: "Create" }).click();

  // Conf is created again (fresh).
  pgConfCreated = true;

  // -- UI assertion: read-only view shows the new description --
  await expect(page.getByText(RECREATE_DESCRIPTION, { exact: false })).toBeVisible({
    timeout: 20_000,
  });

  // -- Backend probe: GET conf → 200 with the freshly-supplied variables --
  // spec: API.md §DELETE attr/validation/conf — a fresh PUT creates a new conf.
  const confResp = await adminApi.get(PG_CONF_API);
  expect(confResp.status()).toBe(200);
  const conf = (await confResp.json()) as {
    description: string;
    variables: Array<{ name: string }>;
  };
  expect(conf.description).toBe(RECREATE_DESCRIPTION);
  expect(conf.variables.map((v) => v.name)).toEqual(RECREATE_VARIABLES);

  // -- Backend probe: the fresh conf's result series starts empty --
  // spec: API.md §DELETE attr/validation/conf — the prior cascade is not undone.
  const from = daysAgoIso(5);
  const until = daysAgoIso(-1);
  const resultsResp = await adminApi.get(
    `${PG_RESULT_API}?from=${encodeURIComponent(from)}&until=${encodeURIComponent(until)}&limit=10`
  );
  expect(resultsResp.status()).toBe(200);
  expect(((await resultsResp.json()) as { total_count: number }).total_count).toBe(0);
});
