/**
 * UC5 — Governance: browser UI flow.
 *
 * Mirrors tests/integration/api_wired/test_uc5_01_governance.py step-for-step,
 * with dual confirmation at each mutating step:
 *   - UI assertion (headings, badges, table rows, chart sections, event log)
 *   - Backend probe via adminApi (same REST read-back the api-wired step asserts)
 *
 * Steps (verbatim from USE_CASE_en.md §UC5 §Imazon Example):
 *   1a. Navigate to /governance/metrics/new; fill the form; Save.
 *       Assert redirect to /governance/metrics/[id]; backend: POST → 201, body shape.
 *       Repeat for all three built-in active metric types (ingestion-freshness,
 *       validation-score, doc-health).
 *   1b. Re-POST same metric_id via adminApi → 409 METRIC_EXISTS (collision rejection).
 *   1c. PUT replace-only: existing id → 200 + change reflected on GET;
 *       absent id (PUT via adminApi) → 404 METRIC_NOT_FOUND.
 *   2.  Trigger immediate first run for each metric via the Run button + ConfirmDialog.
 *       Backend: POST .../method/run → 200 with run_id.
 *   3.  Poll adminApi until ≥1 result row is present for each metric, THEN:
 *       - Dashboard (/governance/dashboard): one combined card per enabled metric
 *         (title + description + metric_type badge + Details link + latest values +
 *         inline trend chart).
 *       - Metric list (/governance/metrics): all three rows listed with title + type badge.
 *       - Per-metric detail (/governance/metrics/[id]): Config, Result chart,
 *         Datasets panel, Event log sections; Edit/Run/Delete buttons for admin.
 *   4.  Cleanup: delete the three metrics via ConfirmDialog; backend: GET → 404.
 *
 * A second scenario mirrors api-wired
 * `test_uc5_dataset_filter_worked_examples_and_dataset_view`: the documented clause
 * forms are authored in the browser's SQL editor (including Auto-indent), a clause
 * outside the grammar renders its 422 inline, and the Datasets panel is read back
 * after a real run.
 *
 * Data setup: global-setup runs --reset-seed (seeded Imazon baseline).
 * Metric result rows are written by the triggered run (POST .../method/run),
 * which stamps results at run time (= now) — always within the UI's 30-day window.
 * Cleanup: afterAll deletes all three metrics idempotently.
 *
 * spec: USE_CASE_en.md §UC5 §Imazon Example
 * spec: spec/feature/FRONTEND_GOVERNANCE.md §Dashboard, §Metrics
 * spec: spec/API.md §`dataset_filter` grammar; §Metric — GET .../{metric_id}/dataset
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — dual confirmation, selector guidance
 */

import { test, expect } from "../fixtures/index";

// ── Constants (verbatim from api-wired test) ───────────────────────────────────

// Three built-in active metric types — DEV-scoped, daily, enabled.
// spec: USE_CASE_en.md §UC5 §Built-in active metric types
//
// `metrics` is a list of series descriptors `{name, color, idx}` — the chart draws
// one line per descriptor, in `idx` order, stroked with `color`.
// spec: API.md §Metric — Definition body.
//
// `dataset_filter` is a SQL WHERE clause over the dataset registry; `origin = 'DEV'`
// is the DEV-scoped filter the UC5 narrative uses.
// spec: API.md §`dataset_filter` grammar.

const DEV_FILTER = "origin = 'DEV'";

const METRIC_INGESTION = {
  metric_id: "ingestion-freshness-dev",
  metric_type: "ingestion-freshness",
  title: "Ingestion Freshness (DEV)",
  description: "Daily count of datasets ingested within the configured time window across DEV",
  metrics: [
    { name: "total", color: "#64748B", idx: 1 },
    { name: "ingested_in_time", color: "#22C55E", idx: 2 },
  ],
  metric_conf: { time_window_sec: 172800 },
} as const;

const METRIC_VALIDATION = {
  metric_id: "validation-score-dev",
  metric_type: "validation-score",
  title: "Validation Score (DEV)",
  // Counts, not a score sum: `valid_confd` is how many of the scoped datasets carry a
  // validation config, `valid_in_time` how many of those pass their cadence-anchored
  // window test. Mirrors tests/integration/api_wired/test_uc5_01_governance.py.
  // spec: feature/BACKEND.md §Metrics Service — "`validation-score` counts and the
  //   unconfigured set" — "the measurer emits two counts — `valid_confd` … and
  //   `valid_in_time` …. Both are counts; neither is a score sum".
  description: "Daily count of DEV datasets validated inside their own cadence-anchored window",
  metrics: [
    { name: "valid_confd", color: "#3B82F6", idx: 1 },
    { name: "valid_in_time", color: "#22C55E", idx: 2 },
  ],
  metric_conf: { time_window_sec: 172800 },
} as const;

const METRIC_DOC = {
  metric_id: "doc-health-dev",
  metric_type: "doc-health",
  title: "Doc Health (DEV)",
  description: "Daily documentation-completeness check across DEV datasets",
  metrics: [
    { name: "total", color: "#64748B", idx: 1 },
    { name: "doc_health", color: "#A855F7", idx: 2 },
  ],
  metric_conf: {},
} as const;

const ALL_METRICS = [METRIC_INGESTION, METRIC_VALIDATION, METRIC_DOC] as const;
type MetricCfg = (typeof ALL_METRICS)[number];

// Throwaway id used to verify PUT on absent id → 404.
const THROWAWAY_ID = "uc5-put-absent-test";

// Runs under the admin project only — enforced by the filename convention in
// playwright.config.ts (default *.spec.ts → admin), which supplies the admin
// storageState. Do not override storageState here.
// spec: spec/TESTING.md §E2E §Authentication — "Playwright projects are keyed on role
// (admin / editor / reader); role-gated tests select the matching project."

// ── Module-level state shared across serial step tests ─────────────────────────

// Track which metric ids were created so afterAll can clean up.
const createdIds = new Set<string>();

// ── Cleanup: delete all three metrics (and throwaway) after all steps ──────────

test.afterAll(async ({ adminApi }) => {
  for (const id of [...Array.from(createdIds), THROWAWAY_ID]) {
    await adminApi.delete(`/api/v1/spoke/governance/metric/${id}/attr/conf`);
    // 204 = deleted; 404 = never created / already gone — both acceptable.
  }
});

// Serial mode: the steps below form one ordered, stateful scenario (each step
// depends on metrics + module state established by the prior step). In serial
// mode the file's tests run as one group; if a step fails, the WHOLE group is
// retried together — re-running every step in order. The metric-create step is
// idempotent across a group-retry: createMetricViaUI pre-deletes the metric_id
// before the UI create, so the re-create lands cleanly with a 201. The negative
// paths (1b 409 METRIC_EXISTS, 1c PUT-absent 404) remain exact.
// spec: spec/TESTING.md §E2E §Execution discipline — "Ordered scenarios run serial…
// Playwright retries a failed serial group from the first step, so a file either makes
// every step re-runnable or sets `retries: 0`".
test.describe.configure({ mode: "serial" });

// ─────────────────────────────────────────────────────────────────────────────
// Step 1a — Create three DEV-scoped daily metrics via /governance/metrics/new
// spec: USE_CASE_en.md §UC5 §Imazon Example — "The CDO adds the doc-health metric
//   with a DEV-scoped daily run, supplying the metric_id in the create body."
// spec: FRONTEND_GOVERNANCE.md §Metrics — /governance/metrics/new form fields +
//   redirect to /governance/metrics/[id] on success.
// spec: API.md §Metric — POST /spoke/governance/metric creates; 409 METRIC_EXISTS on collision.
// ─────────────────────────────────────────────────────────────────────────────

// Helper: create one metric through the UI and assert the result.
// Returns the captured metric_id from the redirected URL.
async function createMetricViaUI(
  page: import("@playwright/test").Page,
  adminApi: import("@playwright/test").APIRequestContext,
  cfg: MetricCfg
): Promise<string> {
  // Pre-flight: idempotent delete so a leftover from a prior run does not collide.
  // spec: spec/TESTING.md §E2E §Execution discipline — "Setup is idempotent and lives
  //   in hooks": "each setup path pre-deletes by natural key and accepts the
  //   upsert/absent status codes (200-or-201, 404-as-success)".
  await adminApi.delete(`/api/v1/spoke/governance/metric/${cfg.metric_id}/attr/conf`);

  // Navigate to the create page.
  await page.goto("/governance/metrics/new");
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: page heading --
  // spec: FRONTEND_GOVERNANCE.md §Metrics — create page heading "New metric"
  await expect(
    page.getByRole("heading", { name: "New metric", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI gesture: metric_id field --
  // spec: metric-form.tsx — id="metric-id" (create-only leading field)
  await page.locator("#metric-id").fill(cfg.metric_id);

  // -- UI gesture: metric_type selector --
  // spec: metric-form.tsx — SelectTrigger id="metric-type"; Radix Select pattern
  // (click trigger, then pick option by role).
  // spec: TESTING.md §E2E §Selectors — "Drive a Select by clicking its trigger — by `id`
  //   when rows render several unnamed triggers — then clicking the option by role."
  await page.locator("#metric-type").click();
  await page.getByRole("option", { name: cfg.metric_type, exact: true }).click();

  // -- UI gesture: title field --
  // spec: metric-form.tsx — id="title"
  await page.locator("#title").fill(cfg.title);

  // -- UI gesture: description field --
  // spec: metric-form.tsx — id="description"
  await page.locator("#description").fill(cfg.description);

  // -- UI gesture: metrics series rows (checkbox + color + display order) --
  // spec: FRONTEND_GOVERNANCE.md §Metrics — "The form's `metrics` control is one
  //   row per emitted key of the selected `metric_type`: a checkbox selecting the
  //   key, a color control (native color swatch paired with a `#RRGGBB` text input,
  //   kept in sync) and an order number. Only checked rows are submitted, as
  //   `{name, color, idx}`."
  // spec: metric-form.tsx — Checkbox id="metric-key-{name}", the hex text input
  //   labelled "{name} color", the order input id="metric-idx-{name}".
  // Only the keys declared in cfg.metrics are checked; any other row stays off.
  for (const series of cfg.metrics) {
    const checkbox = page.locator(`#metric-key-${series.name}`);
    await expect(checkbox).toBeVisible({ timeout: 10_000 });
    await checkbox.check();
    // The color/order inputs are disabled until the row is checked, so these two
    // fills also confirm the checkbox gesture landed.
    await page.getByLabel(`${series.name} color`, { exact: true }).fill(series.color);
    await page.locator(`#metric-idx-${series.name}`).fill(String(series.idx));
  }

  // -- UI gesture: time_window_sec field (ingestion-freshness / validation-score only) --
  // spec: metric-form.tsx — id="time-window-sec"; rendered only when needsWindow=true.
  const twInput = page.locator("#time-window-sec");
  if (await twInput.isVisible()) {
    // cfg.metric_conf has time_window_sec when the type needs it.
    const twValue = (cfg.metric_conf as { time_window_sec?: number }).time_window_sec;
    if (twValue !== undefined) {
      await twInput.fill(String(twValue));
    }
  }

  // -- UI gesture: schedule_tier selector → "daily" --
  // spec: metric-form.tsx — SelectTrigger id="schedule-tier"; Radix Select pattern.
  // spec: USE_CASE_en.md §UC5 §Imazon Example — daily schedule
  await page.locator("#schedule-tier").click();
  await page.getByRole("option", { name: "daily", exact: true }).click();

  // -- UI gesture: is_enabled checkbox -- enable the metric --
  // spec: metric-form.tsx — Checkbox id="is-enabled"
  const isEnabledCheckbox = page.locator("#is-enabled");
  await expect(isEnabledCheckbox).toBeVisible();
  if (!(await isEnabledCheckbox.isChecked())) {
    await isEnabledCheckbox.check();
  }

  // -- UI gesture: dataset_filter → the DEV-scoped SQL clause --
  // spec: FRONTEND_BASIC.md §Shared component notes → DatasetFilterEditor — "One
  //   vertically resizable monospace textarea holding the clause verbatim"; the
  //   box carries aria-label "dataset_filter" (dataset-filter-editor.tsx).
  // spec: API.md §`dataset_filter` grammar — `origin = 'DEV'` is a scalar-equality
  //   predicate over the registry's `origin` column.
  const filterBox = page.getByLabel("dataset_filter", { exact: true });
  await expect(filterBox).toBeVisible({ timeout: 10_000 });
  await filterBox.fill(DEV_FILTER);

  // -- UI gesture: click Save --
  // spec: metric-form.tsx — Button type="submit" "Save"
  await page.getByRole("button", { name: "Save" }).click();

  // -- UI assertion: redirect to /governance/metrics/[id] --
  // spec: FRONTEND_GOVERNANCE.md §Metrics — on success, redirect to /governance/metrics/[id]
  // Exclude /metrics/new so a failed create (stays on /new) is caught here.
  await page.waitForURL(/\/governance\/metrics\/(?!new$)[^/]+$/, { timeout: 30_000 });

  // Capture the metric_id from the URL for subsequent steps.
  const url = page.url();
  const idMatch = /\/governance\/metrics\/([^/?#]+)$/.exec(url);
  expect(idMatch, "Expected metric id in URL after redirect").toBeTruthy();
  const capturedId = decodeURIComponent(idMatch![1]!);

  // -- UI assertion: detail page heading shows the metric title --
  // spec: FRONTEND_GOVERNANCE.md §Metrics — detail page h1 = conf.title
  // spec: [id]/page.tsx — h1 renders {conf.title}
  await expect(
    page.getByRole("heading", { name: cfg.title, exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: metric_type badge visible in Config section --
  // spec: [id]/page.tsx — Config section renders metric_type as Badge variant="outline"
  // getByText is substring-insensitive; exact:true targets the badge text specifically.
  // Multiple elements may contain the type string (table row etc.); use .first().
  await expect(
    page.getByText(cfg.metric_type, { exact: true }).first()
  ).toBeVisible({ timeout: 10_000 });

  // -- Backend probe (dual confirmation): GET /spoke/governance/metric/{id}/attr/conf --
  // spec: USE_CASE_en.md §UC5 §Imazon Example — POST → 201; conf readable immediately.
  const confResp = await adminApi.get(
    `/api/v1/spoke/governance/metric/${capturedId}/attr/conf`
  );
  expect(confResp.status()).toBe(200);
  const conf = (await confResp.json()) as {
    id: string;
    metric_type: string;
    title: string;
    mode: string;
    is_enabled: boolean;
    schedule_tier: string | null;
    metrics: Array<{ name: string; color: string; idx: number }>;
    dataset_filter: string;
  };
  expect(conf.id).toBe(cfg.metric_id);
  expect(conf.metric_type).toBe(cfg.metric_type);
  expect(conf.title).toBe(cfg.title);
  expect(conf.mode).toBe("active");
  expect(conf.is_enabled).toBe(true);

  // The series rows the browser filled arrive as `{name, color, idx}` descriptors.
  // spec: API.md §Metric — Definition body: `metrics` is a list of series descriptors.
  expect(
    [...conf.metrics].sort((a, b) => a.idx - b.idx),
    "the checked series rows must persist with their color and display order"
  ).toEqual(cfg.metrics.map((s) => ({ name: s.name, color: s.color, idx: s.idx })));

  // The clause is stored verbatim — the backend owns the grammar, so no route
  // rewrites or normalises it. spec: API.md §`dataset_filter` grammar.
  expect(conf.dataset_filter).toBe(DEV_FILTER);

  return capturedId;
}

test("UC5 step 1a — create ingestion-freshness metric via /governance/metrics/new", async ({
  page,
  adminApi,
}) => {
  const id = await createMetricViaUI(page, adminApi, METRIC_INGESTION);
  expect(id).toBe(METRIC_INGESTION.metric_id);
  createdIds.add(id);
});

test("UC5 step 1a — create validation-score metric via /governance/metrics/new", async ({
  page,
  adminApi,
}) => {
  const id = await createMetricViaUI(page, adminApi, METRIC_VALIDATION);
  expect(id).toBe(METRIC_VALIDATION.metric_id);
  createdIds.add(id);
});

test("UC5 step 1a — create doc-health metric via /governance/metrics/new", async ({
  page,
  adminApi,
}) => {
  const id = await createMetricViaUI(page, adminApi, METRIC_DOC);
  expect(id).toBe(METRIC_DOC.metric_id);
  createdIds.add(id);
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 1b — Collision rejection: re-POST same metric_id → 409 METRIC_EXISTS
// spec: USE_CASE_en.md §UC5 §Imazon Example
// spec: API.md §Metric — colliding id returns 409 METRIC_EXISTS.
// spec: API.md §Error Catalogue — error_code field in envelope.
// [API-fired, no UI surface for collision — tested via adminApi]
// ─────────────────────────────────────────────────────────────────────────────

test("UC5 step 1b — re-POST existing metric_id → 409 METRIC_EXISTS", async ({ adminApi }) => {
  // Re-POST the same metric_id — must reject with 409.
  const collisionResp = await adminApi.post("/api/v1/spoke/governance/metric", {
    data: {
      metric_id: METRIC_INGESTION.metric_id,
      mode: "active",
      is_enabled: true,
      metric_type: METRIC_INGESTION.metric_type,
      title: METRIC_INGESTION.title,
      description: METRIC_INGESTION.description,
      metrics: METRIC_INGESTION.metrics.map((s) => ({ ...s })),
      metric_conf: METRIC_INGESTION.metric_conf,
      schedule_tier: "daily",
      dataset_filter: DEV_FILTER,
    },
  });
  expect(collisionResp.status()).toBe(409);
  const body = (await collisionResp.json()) as { error_code: string };
  expect(body.error_code).toBe("METRIC_EXISTS");
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 1c — PUT replace-only semantics
// spec: USE_CASE_en.md §UC5 §Imazon Example
// spec: API.md §Metric — PUT .../attr/conf replaces existing, 200;
//   PUT absent id → 404 METRIC_NOT_FOUND.
// [API-fired — no distinct UI gesture for "replace vs create" distinction]
// ─────────────────────────────────────────────────────────────────────────────

test("UC5 step 1c — PUT existing metric conf → 200 + change reflected; absent id → 404", async ({
  page,
  adminApi,
}) => {
  // (a) PUT on existing doc-health-dev — edit via the UI Edit button + Save.
  // spec: FRONTEND_GOVERNANCE.md §Metrics §[id] — Edit button → form inline; Save → PUT attr/conf.
  const UPDATED_DESCRIPTION = "Updated description for replace-only test";

  await page.goto(`/governance/metrics/${METRIC_DOC.metric_id}`);
  await expect(
    page.getByRole("heading", { name: METRIC_DOC.title, exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI gesture: click Edit button --
  // spec: [id]/page.tsx — Button "Edit" sets isEditing=true; form rendered inline in the Config panel.
  await page.getByRole("button", { name: "Edit" }).click();

  // -- UI assertion: form appears (Save button visible) --
  await expect(page.getByRole("button", { name: "Save" })).toBeVisible({ timeout: 10_000 });

  // -- UI gesture: overwrite description field --
  // spec: metric-form.tsx — id="description" (Textarea)
  const descArea = page.locator("#description");
  await descArea.clear();
  await descArea.fill(UPDATED_DESCRIPTION);

  // -- UI gesture: click Save (PUT .../attr/conf) --
  await page.getByRole("button", { name: "Save" }).click();

  // -- UI assertion: form closes on PUT success (isEditing→false on onSuccess) --
  // spec: [id]/page.tsx — Save → PUT attr/conf → onSuccess sets isEditing=false,
  //   re-rendering the read-only Config view with the Edit button back.
  await expect(page.getByRole("button", { name: "Save" })).not.toBeVisible({ timeout: 15_000 });
  await expect(
    page.getByRole("button", { name: "Edit" })
  ).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: read-only detail view renders the updated `description` --
  // spec: FRONTEND_GOVERNANCE.md §Metrics — the detail read-only view renders
  //   `description` alongside mode/metric_type/schedule_tier/is_enabled/metrics/
  //   metric_conf/dataset_filter. The visible confirmation that the PUT landed is
  //   the new description text appearing on the page.
  await expect(page.getByText(UPDATED_DESCRIPTION)).toBeVisible({ timeout: 10_000 });

  // -- Backend probe: GET attr/conf → updated description --
  // spec: API.md §Metric — PUT replaces existing definition, returns 200.
  const confResp = await adminApi.get(
    `/api/v1/spoke/governance/metric/${METRIC_DOC.metric_id}/attr/conf`
  );
  expect(confResp.status()).toBe(200);
  const conf = (await confResp.json()) as { description: string };
  expect(conf.description).toBe(UPDATED_DESCRIPTION);

  // (b) PUT on absent id → 404 METRIC_NOT_FOUND [API-fired].
  // spec: API.md §Metric — PUT returns 404 METRIC_NOT_FOUND when id is absent.
  // Pre-flight: ensure throwaway does not exist.
  await adminApi.delete(`/api/v1/spoke/governance/metric/${THROWAWAY_ID}/attr/conf`);
  const absentPutResp = await adminApi.put(
    `/api/v1/spoke/governance/metric/${THROWAWAY_ID}/attr/conf`,
    {
      data: {
        mode: "active",
        is_enabled: false,
        metric_type: "doc-health",
        title: "Should Fail",
        description: "PUT on absent id must return 404",
        metrics: [
          { name: "total", color: "#64748B", idx: 1 },
          { name: "doc_health", color: "#A855F7", idx: 2 },
        ],
        metric_conf: {},
        schedule_tier: "daily",
        dataset_filter: "",
      },
    }
  );
  expect(absentPutResp.status()).toBe(404);
  const absentBody = (await absentPutResp.json()) as { error_code: string };
  expect(absentBody.error_code).toBe("METRIC_NOT_FOUND");
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 2 — Trigger immediate first run for each metric via Run button + ConfirmDialog
// spec: USE_CASE_en.md §UC5 §Imazon Example — "The CDO triggers an immediate first run
//   rather than waiting for the schedule."
// spec: FRONTEND_GOVERNANCE.md §Metrics §[id] — Run button → ConfirmDialog → POST .../method/run
// spec: API.md §Metric — POST .../method/run returns 200 with run_id.
// ─────────────────────────────────────────────────────────────────────────────

async function triggerRunViaUI(
  page: import("@playwright/test").Page,
  adminApi: import("@playwright/test").APIRequestContext,
  metricId: string,
  metricTitle: string
): Promise<void> {
  // Budget (applies to the calling test): a 15s header wait, a 10s dialog wait, then up
  // to 90s for the run to resolve and close the dialog, then a second run fired as the
  // backend probe — chained past the 60s project ceiling.
  test.setTimeout(180_000);

  await page.goto(`/governance/metrics/${metricId}`);
  await expect(
    page.getByRole("heading", { name: metricTitle, exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI gesture: click Run button --
  // spec: [id]/page.tsx — Button "Run" → setShowRunDialog(true) → ConfirmDialog opens.
  // The Run button carries a Play icon; its text label is "Run".
  await page.getByRole("button", { name: "Run" }).click();

  // -- UI assertion: ConfirmDialog opens --
  // spec: [id]/page.tsx — ConfirmDialog title="Run metric"
  await expect(
    page.getByRole("heading", { name: "Run metric", exact: true })
  ).toBeVisible({ timeout: 10_000 });

  // -- UI gesture: confirm with the "Run" button in the dialog --
  // spec: [id]/page.tsx — ConfirmDialog confirmLabel="Run"
  // The dialog's confirm button text is "Run"; use last() to avoid matching the
  // header button that triggered the dialog (still in the DOM, but behind the overlay).
  await page.getByRole("button", { name: "Run", exact: true }).last().click();

  // -- UI assertion: dialog closes (Run button in dialog disappears) --
  // The ConfirmDialog closes on success (setShowRunDialog(false)). A governance metric
  // run can take longer than 30s on the dev cluster (real computation over DataHub/
  // Postgres), keeping the dialog open until the run resolves — allow up to 90s.
  await expect(
    page.getByRole("heading", { name: "Run metric", exact: true })
  ).not.toBeVisible({ timeout: 90_000 });

  // -- Backend probe: POST .../method/run → 200 with run_id --
  // spec: USE_CASE_en.md §UC5 §Imazon Example — run triggers a measurement; returns run_id.
  // spec: API.md §Metric — POST .../method/run returns 200 with run_id.
  // Note: The UI already fired the run; this backend probe validates the API contract
  // independently (the matching step in api-wired tests).
  const runResp = await adminApi.post(
    `/api/v1/spoke/governance/metric/${metricId}/method/run`
  );
  expect(runResp.status()).toBe(200);
  const runBody = (await runResp.json()) as { run_id: string };
  expect(runBody.run_id).toBeTruthy();
}

test("UC5 step 2 — trigger immediate run for ingestion-freshness metric", async ({
  page,
  adminApi,
}) => {
  await triggerRunViaUI(page, adminApi, METRIC_INGESTION.metric_id, METRIC_INGESTION.title);
});

test("UC5 step 2 — trigger immediate run for validation-score metric", async ({
  page,
  adminApi,
}) => {
  await triggerRunViaUI(page, adminApi, METRIC_VALIDATION.metric_id, METRIC_VALIDATION.title);
});

test("UC5 step 2 — trigger immediate run for doc-health metric", async ({
  page,
  adminApi,
}) => {
  await triggerRunViaUI(page, adminApi, METRIC_DOC.metric_id, METRIC_DOC.title);
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 3a — Dashboard: combined metric cards visible after results land
// spec: USE_CASE_en.md §UC5 §Imazon Example — "A week later, trends are pulled"
// spec: FRONTEND_GOVERNANCE.md §Dashboard — a responsive grid of combined cards,
//   one per enabled metric, each stacking the title, a metric_type outline badge,
//   the latest values, and that metric's inline trend chart. No separate
//   "Daily trend" section.
// spec: dashboard/page.tsx — DashboardContent: one MetricCard per enabled metric
//   (title + metric_type badge + latest values + inline MetricTimeseriesChart).
// ─────────────────────────────────────────────────────────────────────────────

test("UC5 step 3a — dashboard cards: title, type badge, description, Details link, colored trend chart", async ({
  page,
  adminApi,
}) => {
  // Budget: a 60s result-readiness poll — the whole project ceiling on its own, so without
  // this an exhausted poll would be pre-empted before its assertion — chained with the
  // 120s toPass block.
  test.setTimeout(240_000);

  // Poll adminApi until ≥1 result row appears for ingestion-freshness (bellwether metric).
  // spec: TESTING.md §E2E §Execution discipline — "Gate data-dependent UI assertions on
  //   confirmed backend state": read (or poll) the same state through adminApi first,
  //   then assert the UI against it.
  // Runs were triggered in step 2; results should appear within ~30s of the run completing.
  const now = new Date();
  const from = new Date(now.getTime() - 8 * 24 * 60 * 60 * 1000).toISOString(); // 8 days ago
  const to = new Date(now.getTime() + 24 * 60 * 60 * 1000).toISOString();       // +1 day
  const deadline = Date.now() + 60_000;
  let resultCount = 0;
  while (Date.now() < deadline) {
    const r = await adminApi.get(
      `/api/v1/spoke/governance/metric/${METRIC_INGESTION.metric_id}/attr/result?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}&limit=10`
    );
    if (r.ok()) {
      const body = (await r.json()) as { results: unknown[] };
      resultCount = body.results.length;
      if (resultCount >= 1) break;
    }
    await new Promise((res) => setTimeout(res, 3_000));
  }
  expect(resultCount).toBeGreaterThanOrEqual(1);

  // Navigate to the dashboard and assert its heading plus the combined metric
  // cards render. Wrap the goto + UI visibility checks in a retry block to
  // absorb residual client-side render lag after the backend results are
  // confirmed present — without it a render flake fails the test and triggers a
  // serial group-retry.
  // spec: TESTING.md §E2E §Execution discipline — "Never sleep for a fixed duration":
  //   wait with a bounded construct such as `expect(async () => {…}).toPass({ timeout })`.
  await expect(async () => {
    await page.goto("/governance/dashboard");
    await expect(page).not.toHaveURL(/\/login/);

    // -- UI assertion: page heading --
    // spec: dashboard/page.tsx — h1 "Governance · Dashboard"
    await expect(
      page.getByRole("heading", { name: "Governance · Dashboard", exact: true })
    ).toBeVisible({ timeout: 10_000 });

    // -- UI assertion: one combined card per enabled metric (CardTitle = title) --
    // spec: FRONTEND_GOVERNANCE.md §Dashboard — one card per enabled metric; each
    //   card's first line is the metric `title`.
    // spec: dashboard/page.tsx — MetricCard renders CardTitle={metric.title}.
    // Use .first() — the title may also surface elsewhere (e.g. nav) (pitfall 1).
    await expect(
      page.getByText(METRIC_INGESTION.title, { exact: true }).first()
    ).toBeVisible({ timeout: 10_000 });
    await expect(
      page.getByText(METRIC_VALIDATION.title, { exact: true }).first()
    ).toBeVisible({ timeout: 10_000 });
    await expect(
      page.getByText(METRIC_DOC.title, { exact: true }).first()
    ).toBeVisible({ timeout: 10_000 });

    // -- UI assertion: the bellwether card is a *combined* card --
    // spec: FRONTEND_GOVERNANCE.md §Dashboard — each card stacks the title, a
    //   metric_type outline badge, the latest values, and that metric's inline
    //   trend chart. There is no separate "Daily trend" section any more.
    // Scope to the card via its stable test id (metric-card.tsx renders
    // data-testid={`metric-card-${metric.id}`}) — document-order-independent and
    // not self-referential, so the chart assertion below is load-bearing.
    const ingestionCard = page.getByTestId(`metric-card-${METRIC_INGESTION.metric_id}`);
    // (a) the metric_type outline badge text is present inside the card.
    await expect(
      ingestionCard.getByText(METRIC_INGESTION.metric_type, { exact: true }).first()
    ).toBeVisible({ timeout: 10_000 });
    // (b) a Recharts chart is mounted inside the card (decoupled from the scope
    //     predicate — the card was selected by test id, not by containing a chart).
    await expect(
      ingestionCard.locator(".recharts-wrapper, svg.recharts-surface").first()
    ).toBeVisible({ timeout: 10_000 });
    // (c) the card header's Details button links to this metric's detail route.
    // spec: FRONTEND_GOVERNANCE.md §Dashboard — "top-right at a smaller size, a
    //   `metric_type` outline badge beside a `Details` button linking to
    //   `/governance/metrics/{id}`".
    await expect(
      ingestionCard.getByRole("link", { name: "Details", exact: true })
    ).toHaveAttribute("href", `/governance/metrics/${METRIC_INGESTION.metric_id}`);
    // (d) `description` sits under the title — read from the same list response
    //     as the title, so the card needs no extra fetch for it.
    // spec: FRONTEND_GOVERNANCE.md §Dashboard — "Below the heading sits
    //   `description` in small muted text. … `description` and `metrics` both come
    //   from the list read, so the card needs no extra fetch."
    await expect(
      ingestionCard.getByText(METRIC_INGESTION.description, { exact: true })
    ).toBeVisible({ timeout: 10_000 });
    // (e) one line per series descriptor, in `idx` order, stroked with its color.
    // spec: FRONTEND_GOVERNANCE.md §Dashboard — "one line per entry of the metric's
    //   `metrics[]` series descriptors, drawn in `idx` order and stroked with each
    //   descriptor's `color`, one visible point per grain window".
    //
    // How Recharts 3.8 renders that (verified against this chart's real DOM):
    //   * each <Line> emits exactly one `g.recharts-layer.recharts-line`, always;
    //   * the connecting `path.recharts-line-curve` is emitted **only when the
    //     series has at least two plotted points** — a fresh UC5 run produces a
    //     single measurement per metric, so every series is one point and no
    //     curve path exists. Counting curves would therefore assert on how much
    //     history happens to exist, not on the spec property;
    //   * each <Line>'s dots are portalled into one shared z-index layer as a
    //     `g.recharts-line-dots` group per series, in child render order, and
    //     every `circle.recharts-line-dot` carries that series' `stroke`. This
    //     holds for a series of any length, including one point — which is the
    //     "one visible point per grain window" the spec sentence ends on;
    //   * the `Legend` sorts its own items (Recharts default `itemSorter: "value"`,
    //     i.e. by series name) independently of child render order, so legend
    //     order is *not* evidence of draw order and is asserted as an unordered
    //     name→color mapping only.
    const expectedByIdx = [...METRIC_INGESTION.metrics].sort((a, b) => a.idx - b.idx);

    // (e1) one line per descriptor.
    await expect(
      ingestionCard.locator("g.recharts-line"),
      "one chart line per `metrics[]` series descriptor"
    ).toHaveCount(expectedByIdx.length, { timeout: 10_000 });

    // (e2) draw order + color: the per-series dot groups sit in the shared dot
    //      layer in <Line> render order, each stroked with its descriptor color.
    const seriesStrokes = await ingestionCard
      .locator("g.recharts-line-dots")
      .evaluateAll((groups) =>
        groups.map((g) => g.querySelector("circle")?.getAttribute("stroke") ?? null)
      );
    expect(
      seriesStrokes,
      "chart lines must be drawn in idx order, stroked with the descriptors' colors"
    ).toEqual(expectedByIdx.map((s) => s.color));

    // (e3) each descriptor's color belongs to *its* series name (the dot groups
    //      above carry no key, so the name↔color binding is read off the legend).
    //      Compared as a map because Recharts sorts legend items by name.
    const legendPairs = await ingestionCard
      .locator("li.recharts-legend-item")
      .evaluateAll((items) =>
        items.map((li) => [
          li.querySelector(".recharts-legend-item-text")?.textContent ?? "",
          li.querySelector("path.recharts-legend-icon")?.getAttribute("stroke") ?? null,
        ])
      );
    expect(
      Object.fromEntries(legendPairs),
      "each series descriptor's color must be bound to that descriptor's name"
    ).toEqual(
      Object.fromEntries(expectedByIdx.map((s) => [s.name, s.color]))
    );

    // -- UI assertion: no separate "Daily trend" section --
    // spec: FRONTEND_GOVERNANCE.md §Dashboard — combined card only; guard against a
    //   regression reintroducing a standalone trend section.
    await expect(
      page.getByRole("heading", { name: "Daily trend" })
    ).toHaveCount(0);
  }).toPass({ timeout: 120_000, intervals: [2_000, 3_000, 5_000, 10_000] });

  // -- Backend probe: GET /spoke/governance/metric?is_enabled=true → all 3 present --
  // spec: FRONTEND_GOVERNANCE.md §Dashboard — GET metric list filtered is_enabled=true.
  const listResp = await adminApi.get(
    "/api/v1/spoke/governance/metric?is_enabled=true&limit=50"
  );
  expect(listResp.status()).toBe(200);
  const listBody = (await listResp.json()) as {
    metrics: Array<{ id: string; is_enabled: boolean }>;
  };
  const enabledIds = new Set(listBody.metrics.map((m) => m.id));
  expect(enabledIds.has(METRIC_INGESTION.metric_id)).toBe(true);
  expect(enabledIds.has(METRIC_VALIDATION.metric_id)).toBe(true);
  expect(enabledIds.has(METRIC_DOC.metric_id)).toBe(true);
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 3b — Metrics list (/governance/metrics) shows all three metrics
// spec: FRONTEND_GOVERNANCE.md §Metrics — list page: table with Title, metric_type,
//   mode, schedule_tier, Enabled badge, Updated columns.
// spec: metrics/page.tsx — TableRow per metric; Link to detail; Badge for metric_type.
// ─────────────────────────────────────────────────────────────────────────────

test("UC5 step 3b — /governance/metrics list shows all three metrics with type badges", async ({
  page,
  adminApi,
}) => {
  await page.goto("/governance/metrics");
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: page heading --
  // spec: metrics/page.tsx — h1 "Governance · Metrics"
  await expect(
    page.getByRole("heading", { name: "Governance · Metrics", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: "New metric" button visible (admin can write) --
  // spec: metrics/page.tsx — Button with Link to /governance/metrics/new; shown for canWrite
  await expect(page.getByRole("link", { name: "New metric" })).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: all three metric titles visible as links in the table --
  // spec: metrics/page.tsx — Link href="/governance/metrics/{m.id}" text={m.title}
  for (const cfg of ALL_METRICS) {
    await expect(
      page.getByRole("link", { name: cfg.title })
    ).toBeVisible({ timeout: 20_000 });
  }

  // -- UI assertion: metric_type badges visible, scoped to the registered metrics' rows --
  // spec: metrics/page.tsx — Badge variant="outline" text={m.metric_type}
  // Each row renders m.id as a <p> subtitle under the title link (metrics/page.tsx line ~169).
  // Scope badge assertions to each registered metric's row via its metric_id to prevent
  // stale/pre-existing rows with the same metric_type from satisfying the assertion.
  const ingestionRow = page.getByRole("row").filter({ hasText: METRIC_INGESTION.metric_id });
  await expect(ingestionRow.getByText("ingestion-freshness", { exact: true })).toBeVisible({ timeout: 10_000 });

  const validationRow = page.getByRole("row").filter({ hasText: METRIC_VALIDATION.metric_id });
  await expect(validationRow.getByText("validation-score", { exact: true })).toBeVisible({ timeout: 10_000 });

  const docRow = page.getByRole("row").filter({ hasText: METRIC_DOC.metric_id });
  await expect(docRow.getByText("doc-health", { exact: true })).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: "Enabled" badge in each registered metric's row --
  // spec: metrics/page.tsx — Badge variant="default" text="Enabled" when m.is_enabled
  // Scoped to each registered metric's row (by metric_id) so stale rows do not satisfy.
  await expect(ingestionRow.getByText("Enabled", { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect(validationRow.getByText("Enabled", { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect(docRow.getByText("Enabled", { exact: true })).toBeVisible({ timeout: 10_000 });

  // -- Backend probe: GET /spoke/governance/metric → all 3 ids in response --
  // spec: USE_CASE_en.md §UC5 — metric catalogue persists across requests.
  const resp = await adminApi.get("/api/v1/spoke/governance/metric?limit=50");
  expect(resp.status()).toBe(200);
  const body = (await resp.json()) as { metrics: Array<{ id: string }> };
  const ids = new Set(body.metrics.map((m) => m.id));
  for (const cfg of ALL_METRICS) {
    expect(ids.has(cfg.metric_id), `${cfg.metric_id} missing from list`).toBe(true);
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 3c — Per-metric detail: Config, Result chart, Event log
// spec: FRONTEND_GOVERNANCE.md §Metrics §[id] — detail page sections:
//   Config (read-only panels), Result (MetricTimeseriesChart), Event (log list).
// spec: [id]/page.tsx — section h2 labels: "Config", "Result", "Event".
// ─────────────────────────────────────────────────────────────────────────────

test("UC5 step 3c — doc-health detail page: Config (series + clause), Result, Datasets, Event; Edit/Run/Delete", async ({
  page,
  adminApi,
}) => {
  await page.goto(`/governance/metrics/${METRIC_DOC.metric_id}`);
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: page heading shows conf.title --
  // spec: [id]/page.tsx — h1 = conf.title
  await expect(
    page.getByRole("heading", { name: METRIC_DOC.title, exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: metric id shown as mono text next to the heading --
  // spec: [id]/page.tsx — span.font-mono {conf.id}
  await expect(
    page.getByText(METRIC_DOC.metric_id, { exact: true })
  ).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: Config section heading --
  // spec: [id]/page.tsx — h2 "Config"
  await expect(
    page.getByRole("heading", { name: "Config", exact: true })
  ).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: key conf fields rendered in the dl --
  // spec: [id]/page.tsx — dl renders mode, metric_type badge, schedule_tier, is_enabled badge
  await expect(page.getByText("active", { exact: true }).first()).toBeVisible({ timeout: 10_000 });

  // metric_type as Badge variant="outline" — check exact string to target the badge text.
  await expect(
    page.getByText("doc-health", { exact: true }).first()
  ).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: Result section heading --
  // spec: [id]/page.tsx — section h2 "Result"
  await expect(
    page.getByRole("heading", { name: "Result", exact: true })
  ).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: range-selector control present on the Result section --
  // spec: [id]/page.tsx — Result section renders a <RangePicker> next to the h2.
  // spec: components/range-picker.tsx — the trigger is a <Button> (PopoverTrigger) showing
  //   selectionLabel(value) (a preset like "Last 2 weeks", or a custom range). The default
  //   preset can vary by persisted state, so assert the control's presence rather than a
  //   specific preset label. The Result section's only button is the RangePicker trigger,
  //   so scope the button locator to that section.
  const resultSection = page
    .locator("section")
    .filter({ has: page.getByRole("heading", { name: "Result", exact: true }) });
  await expect(resultSection.getByRole("button").first()).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: the Config panel lists the series descriptors --
  // spec: FRONTEND_GOVERNANCE.md §Metrics — "`metrics` renders one line per series
  //   descriptor — a color swatch, the `name`, and its `idx` — in `idx` order."
  for (const series of METRIC_DOC.metrics) {
    await expect(
      page.getByText(`${series.name}`, { exact: true }).first()
    ).toBeVisible({ timeout: 10_000 });
    await expect(
      page.getByText(`(${series.idx})`, { exact: true }).first()
    ).toBeVisible({ timeout: 10_000 });
  }

  // -- UI assertion: dataset_filter renders as the stored SQL clause --
  // spec: FRONTEND_GOVERNANCE.md §Metrics — "`dataset_filter` is a SQL `WHERE`-clause
  //   string …, rendered through DatasetFilterView".
  await expect(
    page.getByText(DEV_FILTER, { exact: true }).first()
  ).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: Datasets section heading + its table --
  // spec: FRONTEND_GOVERNANCE.md §Metrics — "The **Datasets** panel
  //   (`MetricDatasetTable` …) sits between the `Result` and `Event` panels" with
  //   columns dataset_urn / datahub / met / last check time.
  await expect(
    page.getByRole("heading", { name: "Datasets", exact: true })
  ).toBeVisible({ timeout: 10_000 });
  const datasetsSection = page
    .locator("section")
    .filter({ has: page.getByRole("heading", { name: "Datasets", exact: true }) });
  for (const col of ["dataset_urn", "datahub", "met criterion", "last check time"]) {
    await expect(
      datasetsSection.getByRole("columnheader", { name: col, exact: true })
    ).toBeVisible({ timeout: 10_000 });
  }

  // -- Backend probe (dual confirmation): GET .../dataset covers the same scope --
  // spec: API.md §Metric — GET /spoke/governance/metric/{metric_id}/dataset returns
  //   `{dataset_urn, met, last_check_at, detail}` rows plus `attrs_synced_at`.
  const coveredResp = await adminApi.get(
    `/api/v1/spoke/governance/metric/${METRIC_DOC.metric_id}/dataset?limit=200`
  );
  expect(coveredResp.status()).toBe(200);
  const covered = (await coveredResp.json()) as {
    total_count: number;
    attrs_synced_at: string | null;
    datasets: Array<{ dataset_urn: string; met: string; last_check_at: string | null }>;
  };
  expect(covered).toHaveProperty("attrs_synced_at");
  // The run in step 2 measured every dataset in scope, so none may read "unknown".
  // spec: API.md §Metric — "'unknown' = in scope but never evaluated".
  for (const row of covered.datasets) {
    expect(["true", "false"], `${row.dataset_urn} carries no verdict after a run`).toContain(
      row.met
    );
  }
  // Each served row is rendered, so the panel shows the scope the API reports.
  if (covered.datasets.length > 0) {
    await expect(
      datasetsSection.getByRole("link", { name: covered.datasets[0]!.dataset_urn })
    ).toBeVisible({ timeout: 10_000 });
  }

  // -- UI assertion: Event section heading --
  // spec: [id]/page.tsx — section h2 "Event"
  await expect(
    page.getByRole("heading", { name: "Event", exact: true })
  ).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: Edit, Run, Delete buttons visible (admin can write) --
  // spec: [id]/page.tsx — canWrite && !isEditing → Edit, Run, Delete buttons
  // spec: FRONTEND_GOVERNANCE.md §Metrics §[id] — write actions for Editor/Admin
  await expect(page.getByRole("button", { name: "Edit" })).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole("button", { name: "Run" })).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole("button", { name: "Delete" })).toBeVisible({ timeout: 10_000 });

  // -- Backend probe: GET attr/result → ≥1 result row within a 7-day window --
  // spec: USE_CASE_en.md §UC5 §Imazon Example — "A week later, trends are pulled … ≥1 result row."
  const now = new Date();
  const from = new Date(now.getTime() - 7 * 24 * 60 * 60 * 1000).toISOString();
  const to = new Date(now.getTime() + 24 * 60 * 60 * 1000).toISOString();
  const resultResp = await adminApi.get(
    `/api/v1/spoke/governance/metric/${METRIC_DOC.metric_id}/attr/result?from=${encodeURIComponent(from)}&to=${encodeURIComponent(to)}`
  );
  expect(resultResp.status()).toBe(200);
  const resultBody = (await resultResp.json()) as {
    results: Array<{ values: Record<string, number>; measured_at: string }>;
  };
  expect(resultBody.results.length).toBeGreaterThanOrEqual(1);
  // values keys must match the declared metrics list — one key per series descriptor.
  // spec: USE_CASE_en.md §UC5 §Built-in active metric types — values is a dict;
  //   spec: API.md §Metric — `metrics[].name` is one of the type's emitted keys.
  const valuesKeys = new Set(Object.keys(resultBody.results[0]!.values));
  for (const series of METRIC_DOC.metrics) {
    expect(
      valuesKeys.has(series.name),
      `values.${series.name} missing from doc-health result`
    ).toBe(true);
  }

  // -- Backend probe: GET event → ≥1 METRIC.RUN_COMPLETE event after the run --
  // spec: [id]/page.tsx — event section renders eventsData.events; METRIC.RUN_COMPLETE
  // spec: API.md §Metric — POST .../method/run emits METRIC.RUN_COMPLETE (BACKEND.md §Event Catalogue)
  const eventResp = await adminApi.get(
    `/api/v1/spoke/governance/metric/${METRIC_DOC.metric_id}/event`
  );
  expect(eventResp.status()).toBe(200);
  const eventBody = (await eventResp.json()) as { events: Array<{ event_type: string }> };
  expect(Array.isArray(eventBody.events)).toBe(true);
  expect(eventBody.events.length).toBeGreaterThanOrEqual(1);
  const hasRunComplete = eventBody.events.some((e) => e.event_type === "METRIC.RUN_COMPLETE");
  expect(hasRunComplete, "METRIC.RUN_COMPLETE event must be present after run").toBe(true);
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 4 — Cleanup: delete all three metrics via ConfirmDialog
// spec: FRONTEND_GOVERNANCE.md §Metrics §[id] — Delete button → ConfirmDialog →
//   router.push("/governance/metrics") on success.
// spec: API.md §Metric — DELETE .../attr/conf returns 204.
// ─────────────────────────────────────────────────────────────────────────────

async function deleteMetricViaUI(
  page: import("@playwright/test").Page,
  adminApi: import("@playwright/test").APIRequestContext,
  metricId: string,
  metricTitle: string
): Promise<void> {
  await page.goto(`/governance/metrics/${metricId}`);
  await expect(
    page.getByRole("heading", { name: metricTitle, exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI gesture: click Delete button --
  // spec: [id]/page.tsx — Button variant="destructive" "Delete" → setShowDeleteDialog(true)
  await page.getByRole("button", { name: "Delete" }).click();

  // -- UI assertion: ConfirmDialog opens --
  // spec: [id]/page.tsx — ConfirmDialog title="Delete metric"
  await expect(
    page.getByRole("heading", { name: "Delete metric", exact: true })
  ).toBeVisible({ timeout: 10_000 });

  // -- UI gesture: confirm deletion --
  // spec: [id]/page.tsx — ConfirmDialog confirmLabel="Delete"
  // Use last() to avoid matching the header Delete button still in the DOM.
  await page.getByRole("button", { name: "Delete", exact: true }).last().click();

  // -- UI assertion: redirected to /governance/metrics list --
  // spec: [id]/page.tsx — on delete success, router.push("/governance/metrics")
  await page.waitForURL(/\/governance\/metrics$/, { timeout: 30_000 });

  // -- UI assertion: deleted metric title no longer visible in the list --
  // Allow a brief stabilisation period for TanStack Query to invalidate.
  await expect(
    page.getByRole("link", { name: metricTitle })
  ).not.toBeVisible({ timeout: 10_000 });

  // -- Backend probe: GET .../attr/conf → 404 (metric deleted) --
  // spec: API.md §Metric — DELETE .../attr/conf → 204; subsequent GET → 404 METRIC_NOT_FOUND.
  const confResp = await adminApi.get(
    `/api/v1/spoke/governance/metric/${metricId}/attr/conf`
  );
  expect(confResp.status()).toBe(404);
}

test("UC5 step 4 — delete ingestion-freshness metric via ConfirmDialog", async ({
  page,
  adminApi,
}) => {
  await deleteMetricViaUI(page, adminApi, METRIC_INGESTION.metric_id, METRIC_INGESTION.title);
  createdIds.delete(METRIC_INGESTION.metric_id);
});

test("UC5 step 4 — delete validation-score metric via ConfirmDialog", async ({
  page,
  adminApi,
}) => {
  await deleteMetricViaUI(page, adminApi, METRIC_VALIDATION.metric_id, METRIC_VALIDATION.title);
  createdIds.delete(METRIC_VALIDATION.metric_id);
});

test("UC5 step 4 — delete doc-health metric via ConfirmDialog", async ({
  page,
  adminApi,
}) => {
  await deleteMetricViaUI(page, adminApi, METRIC_DOC.metric_id, METRIC_DOC.title);
  createdIds.delete(METRIC_DOC.metric_id);
});

// ─────────────────────────────────────────────────────────────────────────────
// UC5 continued — the documented `dataset_filter` clause forms, authored in the
// browser's SQL editor, and the Datasets panel they scope.
//
// Mirrors tests/integration/api_wired/test_uc5_01_governance.py ::
//   test_uc5_dataset_filter_worked_examples_and_dataset_view
// step-for-step, with the browser doing the authoring the api-wired test does
// over REST, and the same REST read-back as the dual confirmation.
//
// spec: USE_CASE_en.md §UC5 §Imazon Example — a metric is scoped by a
//   `dataset_filter`, then run on demand.
// spec: API.md §`dataset_filter` grammar — the grammar, its printed worked
//   example, and the 422 INVALID_DATASET_FILTER carrying a character position.
// spec: API.md §Metric — GET /spoke/governance/metric/{metric_id}/dataset.
// spec: FRONTEND_BASIC.md §Shared component notes → DatasetFilterEditor —
//   resizable monospace box, Auto-indent, server-side validation rendered inline.
// spec: FRONTEND_GOVERNANCE.md §Metrics — the Datasets panel and its three-way
//   verdict toggle.
// ─────────────────────────────────────────────────────────────────────────────

test.describe("UC5 — dataset_filter worked examples and the Datasets panel", () => {
  // Detached from the file's serial group: this is a self-contained story that
  // creates and deletes its own metric, so a failure here must not retry (and
  // re-run) the metric runs of steps 1–4 above.
  test.describe.configure({ mode: "default" });

  const FILTER_METRIC_ID = "uc5-filter-worked-examples";
  const FILTER_METRIC_TITLE = "Catalog documentation health";
  const CONF_PATH = `/api/v1/spoke/governance/metric/${FILTER_METRIC_ID}/attr/conf`;
  const DATASET_PATH = `/api/v1/spoke/governance/metric/${FILTER_METRIC_ID}/dataset`;
  const DETAIL_URL = `/governance/metrics/${FILTER_METRIC_ID}`;

  // The clause UC3's Imazon example prints — the simplest documented form.
  const TAG_CLAUSE = "'urn:li:tag:area:catalog' IN tag_urns";

  // A composite clause in the shape API.md §`dataset_filter` grammar prints as its
  // worked example — an origin equality AND-ed with a parenthesised tag /
  // glossary-term OR, here scoped to the seeded DEV estate — typed as one line so
  // Auto-indent has something to lay out.
  const COMPOSITE_ONE_LINE =
    "origin = 'DEV' AND ('urn:li:tag:area:catalog' IN tag_urns" +
    " OR 'urn:li:glossaryTerm:pii.gdpr' IN glossary_term_urns)";

  // What Auto-indent must produce from it: newline before each top-level
  // AND / OR, the group's body indented one level, its `)` at the parent indent.
  // spec: FRONTEND_BASIC.md §Shared component notes → DatasetFilterEditor.
  const COMPOSITE_FORMATTED = [
    "origin = 'DEV'",
    "AND (",
    "    'urn:li:tag:area:catalog' IN tag_urns",
    "    OR 'urn:li:glossaryTerm:pii.gdpr' IN glossary_term_urns",
    ")",
  ].join("\n");

  test.afterAll(async ({ adminApi }) => {
    await adminApi.delete(CONF_PATH);
  });

  /** Enters edit mode on the detail page and returns the clause textarea. */
  async function openFilterEditor(page: import("@playwright/test").Page) {
    await page.getByRole("button", { name: "Edit" }).click();
    const box = page.getByLabel("dataset_filter", { exact: true });
    await expect(box).toBeVisible({ timeout: 10_000 });
    return box;
  }

  test("UC5 filter — author the documented clauses, see a bad one refused, read the Datasets panel", async ({
    page,
    adminApi,
  }) => {
    // Budget: a metric run against the dev cluster plus several edit round-trips,
    // chained past the 60s project ceiling.
    test.setTimeout(240_000);

    // ── Step 1: create with the tag-membership clause ───────────────────────
    // Created over REST — the UI create path is already covered by step 1a above;
    // this scenario is about editing the clause, not creating a metric.
    // spec: USE_CASE_en.md §UC3 §Imazon Example prints this exact clause;
    //   API.md §`dataset_filter` grammar states UC5 uses the same grammar.
    await adminApi.delete(CONF_PATH); // pre-flight: a leftover must not 409
    const createResp = await adminApi.post("/api/v1/spoke/governance/metric", {
      data: {
        metric_id: FILTER_METRIC_ID,
        mode: "active",
        is_enabled: true,
        metric_type: "doc-health",
        title: FILTER_METRIC_TITLE,
        description: "Documentation completeness across the catalog-tagged estate",
        metrics: [
          { name: "total", color: "#2563EB", idx: 1 },
          { name: "doc_health", color: "#16A34A", idx: 2 },
        ],
        metric_conf: {},
        schedule_tier: "daily",
        dataset_filter: TAG_CLAUSE,
      },
    });
    expect(createResp.status(), await createResp.text()).toBe(201);

    await page.goto(DETAIL_URL);
    await expect(page).not.toHaveURL(/\/login/);
    await expect(
      page.getByRole("heading", { name: FILTER_METRIC_TITLE, exact: true })
    ).toBeVisible({ timeout: 15_000 });

    // -- UI assertion: the read-only view shows the stored clause verbatim --
    // spec: FRONTEND_BASIC.md §Shared component notes → DatasetFilterView.
    await expect(page.getByText(TAG_CLAUSE, { exact: true }).first()).toBeVisible({
      timeout: 10_000,
    });

    // ── Step 2: replace with the composite clause, via Auto-indent ──────────
    const box = await openFilterEditor(page);
    await box.fill(COMPOSITE_ONE_LINE);

    // -- UI gesture: Auto-indent lays the clause out --
    // spec: FRONTEND_BASIC.md §Shared component notes → DatasetFilterEditor —
    //   "An **Auto-indent** button reformats the text in place: newline before
    //   each top-level `AND` / `OR`, indent inside parentheses."
    await page.getByRole("button", { name: "Auto-indent", exact: true }).click();
    await expect(box).toHaveValue(COMPOSITE_FORMATTED);

    await page.getByRole("button", { name: "Save", exact: true }).click();
    // Save closing the form is the UI's signal that the PUT succeeded.
    await expect(page.getByRole("button", { name: "Edit" })).toBeVisible({
      timeout: 20_000,
    });

    // -- Backend probe: the formatted clause is stored byte-for-byte --
    // spec: API.md §`dataset_filter` grammar — the backend owns the grammar and
    //   stores the clause as written; no route normalises it.
    const afterPut = await adminApi.get(CONF_PATH);
    expect(afterPut.status()).toBe(200);
    expect((await afterPut.json())["dataset_filter"]).toBe(COMPOSITE_FORMATTED);

    // ── Step 3: a clause outside the grammar is refused, inline, with position ──
    // spec: API.md §Error catalogue — INVALID_DATASET_FILTER, 422, "`detail`
    //   carries the character position of the error".
    // spec: FRONTEND_GOVERNANCE.md §Metrics — "A `422 INVALID_DATASET_FILTER`
    //   from Save renders inline against the field."
    const box2 = await openFilterEditor(page);
    await box2.fill("owner = 'catalog-team'"); // `owner` is not a filter column
    await page.getByRole("button", { name: "Save", exact: true }).click();

    // -- UI assertion: the error renders against the field, carrying a position --
    const filterAlert = page.getByRole("alert").filter({ hasText: /character/i });
    await expect(filterAlert).toBeVisible({ timeout: 20_000 });
    // The form stays open: a refused Save must not look like a successful one.
    await expect(page.getByRole("button", { name: "Save", exact: true })).toBeVisible();

    // -- Backend probe: the rejected write left the stored clause alone --
    const afterReject = await adminApi.get(CONF_PATH);
    expect((await afterReject.json())["dataset_filter"]).toBe(COMPOSITE_FORMATTED);

    // ── Step 4: scope to DEV, run, and open the Datasets panel ──────────────
    // `origin` is parsed from every dataset URN by the registry sweep, so this
    // clause resolves without waiting on the tag/glossary attribute mirror.
    // spec: API.md §`dataset_filter` grammar — `origin` is the URN's third segment.
    await box2.fill(DEV_FILTER);
    await page.getByRole("button", { name: "Save", exact: true }).click();
    await expect(page.getByRole("button", { name: "Edit" })).toBeVisible({
      timeout: 20_000,
    });

    // -- UI gesture: Run the metric so every covered dataset gets a verdict --
    await page.getByRole("button", { name: "Run" }).click();
    await expect(
      page.getByRole("heading", { name: "Run metric", exact: true })
    ).toBeVisible({ timeout: 10_000 });
    await page.getByRole("button", { name: "Run", exact: true }).last().click();
    await expect(
      page.getByRole("heading", { name: "Run metric", exact: true })
    ).not.toBeVisible({ timeout: 90_000 });

    // -- Backend probe: the panel's scope, as the API reports it --
    // spec: API.md §Metric — rows carry `{dataset_urn, met, last_check_at, detail}`;
    //   the envelope carries `attrs_synced_at`.
    const coveredResp = await adminApi.get(`${DATASET_PATH}?limit=200`);
    expect(coveredResp.status(), await coveredResp.text()).toBe(200);
    const covered = (await coveredResp.json()) as {
      total_count: number;
      attrs_synced_at: string | null;
      datasets: Array<{ dataset_urn: string; met: string; last_check_at: string | null }>;
    };
    expect(
      covered.total_count,
      "the DEV clause must cover at least one registered dataset, or every row " +
        "assertion below is vacuous. A zero here means the ingestion sync sweep has " +
        "not populated dataset_registry — see spec/TESTING.md §Prerequisites."
    ).toBeGreaterThan(0);
    for (const row of covered.datasets) {
      expect(
        ["true", "false"],
        `${row.dataset_urn} was in scope for the run just made, so it must carry a ` +
          "verdict rather than 'unknown' (API.md §Metric)"
      ).toContain(row.met);
    }
    expect(covered).toHaveProperty("attrs_synced_at");

    // -- UI assertion: the panel renders the served rows and the freshness line --
    // spec: FRONTEND_GOVERNANCE.md §Metrics — the Datasets panel's columns and its
    //   muted `attrs_synced_at` line ("so an empty or unexpectedly small table is
    //   readable as a pending sync rather than as a filter that matches nothing").
    const datasetsSection = page
      .locator("section")
      .filter({ has: page.getByRole("heading", { name: "Datasets", exact: true }) });
    const firstUrn = covered.datasets[0]!.dataset_urn;
    await expect(
      datasetsSection.getByRole("link", { name: firstUrn })
    ).toBeVisible({ timeout: 15_000 });
    await expect(datasetsSection.getByText(/scope (synced|never synced)/i)).toBeVisible({
      timeout: 10_000,
    });

    // ── Step 5: the verdict toggles narrow the panel ────────────────────────
    // spec: FRONTEND_GOVERNANCE.md §Metrics — "A three-way toggle group — true /
    //   false / unknown, all on by default — drives the repeatable `met` query
    //   param"; "With **zero** toggles selected the client renders the empty state
    //   and issues **no request**".
    for (const verdict of ["true", "false", "unknown"]) {
      await expect(
        datasetsSection.getByRole("checkbox", { name: verdict, exact: true })
      ).toBeChecked();
    }

    // Narrow to the verdict the run actually produced, so a row remains to see.
    const presentVerdict = covered.datasets[0]!.met;
    const absentVerdicts = ["true", "false", "unknown"].filter((v) => v !== presentVerdict);
    for (const verdict of absentVerdicts) {
      await datasetsSection
        .getByRole("checkbox", { name: verdict, exact: true })
        .uncheck();
    }
    await expect(
      datasetsSection.getByRole("link", { name: firstUrn })
    ).toBeVisible({ timeout: 15_000 });

    // -- Backend probe: the same narrowing over REST --
    // spec: API.md §Metric — "Repeatable `met` query param (default: all three)".
    const narrowed = await adminApi.get(`${DATASET_PATH}?met=${presentVerdict}&limit=200`);
    expect(narrowed.status()).toBe(200);
    const narrowedBody = (await narrowed.json()) as {
      total_count: number;
      attrs_synced_at: string | null;
      datasets: Array<{ met: string }>;
    };
    expect(new Set(narrowedBody.datasets.map((r) => r.met))).toEqual(
      new Set([presentVerdict])
    );
    expect(
      narrowedBody.attrs_synced_at,
      "attrs_synced_at is scope-relative and must not move with the met filter"
    ).toBe(covered.attrs_synced_at);

    // Unchecking the last verdict is a client-side empty state, not a request.
    await datasetsSection
      .getByRole("checkbox", { name: presentVerdict, exact: true })
      .uncheck();
    await expect(datasetsSection.getByText(/no verdict selected/i)).toBeVisible({
      timeout: 10_000,
    });
    await expect(datasetsSection.getByRole("table")).toHaveCount(0);
  });
});
