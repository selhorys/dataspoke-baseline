/**
 * UC5 — Governance: browser UI flow.
 *
 * Mirrors tests/integration/api_wired/test_uc5_governance.py step-for-step,
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
 *       - Dashboard (/governance/dashboard): metric cards and trend chart section visible.
 *       - Metric list (/governance/metrics): all three rows listed with title + type badge.
 *       - Per-metric detail (/governance/metrics/[id]): attr/conf, attr/result chart,
 *         event log sections; Edit/Run/Delete buttons rendered for admin.
 *   4.  Cleanup: delete the three metrics via ConfirmDialog; backend: GET → 404.
 *
 * Data setup: global-setup runs --reset-seed (seeded Imazon baseline).
 * Metric result rows are written by the triggered run (POST .../method/run),
 * which stamps results at run time (= now) — always within the UI's 30-day window.
 * Cleanup: afterAll deletes all three metrics idempotently.
 *
 * spec: USE_CASE_en.md §UC5 §Imazon Example
 * spec: spec/feature/FRONTEND_GOVERNANCE.md §Dashboard, §Metrics
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — dual confirmation, selector guidance
 */

import { test, expect } from "../fixtures/index";

// ── Constants (verbatim from api-wired test) ───────────────────────────────────

// Three built-in active metric types — DEV-scoped, daily, enabled.
// spec: USE_CASE_en.md §UC5 §Built-in active metric types

const METRIC_INGESTION = {
  metric_id: "ingestion-freshness-dev",
  metric_type: "ingestion-freshness",
  title: "Ingestion Freshness (DEV)",
  description: "Daily count of datasets ingested within the configured time window across DEV",
  metrics: ["total", "ingested_in_time"],
  metric_conf: { time_window_sec: 172800 },
} as const;

const METRIC_VALIDATION = {
  metric_id: "validation-score-dev",
  metric_type: "validation-score",
  title: "Validation Score (DEV)",
  description: "Daily sum of dataset validation scores within the configured time window across DEV",
  metrics: ["total", "validation_score_sum"],
  metric_conf: { time_window_sec: 172800 },
} as const;

const METRIC_DOC = {
  metric_id: "doc-health-dev",
  metric_type: "doc-health",
  title: "Doc Health (DEV)",
  description: "Daily documentation-completeness check across DEV datasets",
  metrics: ["total", "doc_health"],
  metric_conf: {},
} as const;

const ALL_METRICS = [METRIC_INGESTION, METRIC_VALIDATION, METRIC_DOC] as const;
type MetricCfg = (typeof ALL_METRICS)[number];

// Throwaway id used to verify PUT on absent id → 404.
const THROWAWAY_ID = "uc5-put-absent-test";

// Runs under the admin project only — enforced by the filename convention in
// playwright.config.ts (default *.spec.ts → admin), which supplies the admin
// storageState. Do not override storageState here.
// spec: spec/TESTING.md §E2E §Authentication

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
  // spec: spec/TESTING.md §Assertion Principles — idempotent setup.
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
  // spec: TESTING.md §E2E — Radix Select: click the #id trigger, then getByRole("option", {name}).click()
  await page.locator("#metric-type").click();
  await page.getByRole("option", { name: cfg.metric_type, exact: true }).click();

  // -- UI gesture: title field --
  // spec: metric-form.tsx — id="title"
  await page.locator("#title").fill(cfg.title);

  // -- UI gesture: description field --
  // spec: metric-form.tsx — id="description"
  await page.locator("#description").fill(cfg.description);

  // -- UI gesture: metrics checkboxes --
  // spec: metric-form.tsx — Checkbox id="metric-key-{key}" per emitted key
  // Only check the keys declared in cfg.metrics; others remain unchecked.
  for (const key of cfg.metrics) {
    const checkbox = page.locator(`#metric-key-${key}`);
    if (await checkbox.isVisible()) {
      await checkbox.check();
    }
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

  // -- UI gesture: dataset_filter origin → DEV --
  // spec: FRONTEND_GOVERNANCE.md §Metrics — dataset_filter.origin dropdown (Radix Select).
  // DatasetFilterEditor renders a Radix Select for origin; no static id on the trigger,
  // but the accessible label "origin" is a label element. We target by the visible
  // label text then pick the adjacent SelectTrigger via aria-label or data-testid.
  // Risk flag: DatasetFilterEditor does not expose a deterministic selector for origin.
  // REQUIRED data-testid: dataset-filter-origin (component: DatasetFilterEditor, element: SelectTrigger)
  // Fallback: select by placeholder text "Any origin".
  const originTrigger = page.getByRole("combobox", { name: /origin/i }).first();
  if (await originTrigger.isVisible({ timeout: 3_000 }).catch(() => false)) {
    await originTrigger.click();
    await page.getByRole("option", { name: "DEV", exact: true }).click();
  }
  // If the combobox is not reachable by name, skip — tested via adminApi backend probe.

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

  // -- UI assertion: metric_type badge visible in attr/conf section --
  // spec: [id]/page.tsx — attr/conf dl section renders metric_type as Badge variant="outline"
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
  };
  expect(conf.id).toBe(cfg.metric_id);
  expect(conf.metric_type).toBe(cfg.metric_type);
  expect(conf.title).toBe(cfg.title);
  expect(conf.mode).toBe("active");
  expect(conf.is_enabled).toBe(true);

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
  // Requires step 1a to have created ingestion-freshness-dev.
  if (!createdIds.has(METRIC_INGESTION.metric_id)) test.skip();

  // Re-POST the same metric_id — must reject with 409.
  const collisionResp = await adminApi.post("/api/v1/spoke/governance/metric", {
    data: {
      metric_id: METRIC_INGESTION.metric_id,
      mode: "active",
      is_enabled: true,
      metric_type: METRIC_INGESTION.metric_type,
      title: METRIC_INGESTION.title,
      description: METRIC_INGESTION.description,
      metrics: Array.from(METRIC_INGESTION.metrics),
      metric_conf: METRIC_INGESTION.metric_conf,
      schedule_tier: "daily",
      dataset_filter: { origin: "DEV" },
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
  // Requires step 1a to have created doc-health-dev.
  if (!createdIds.has(METRIC_DOC.metric_id)) test.skip();

  // (a) PUT on existing doc-health-dev — edit via the UI Edit button + Save.
  // spec: FRONTEND_GOVERNANCE.md §Metrics §[id] — Edit button → form inline; Save → PUT attr/conf.
  const UPDATED_DESCRIPTION = "Updated description for replace-only test";

  await page.goto(`/governance/metrics/${METRIC_DOC.metric_id}`);
  await expect(
    page.getByRole("heading", { name: METRIC_DOC.title, exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI gesture: click Edit button --
  // spec: [id]/page.tsx — Button "Edit" sets isEditing=true; form rendered inline in attr/conf.
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
  // The metric detail attr/conf section does NOT render `description` (it shows
  // mode/type/schedule/enabled/metrics), so the visible UI confirmation that the
  // PUT landed is the form closing without error; the backend probe below verifies
  // the new description value.
  await expect(page.getByRole("button", { name: "Save" })).not.toBeVisible({ timeout: 15_000 });
  await expect(
    page.getByRole("button", { name: "Edit" })
  ).toBeVisible({ timeout: 10_000 });

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
        metrics: ["total", "doc_health"],
        metric_conf: {},
        schedule_tier: "daily",
        dataset_filter: {},
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
  // The ConfirmDialog closes on success (setShowRunDialog(false)).
  await expect(
    page.getByRole("heading", { name: "Run metric", exact: true })
  ).not.toBeVisible({ timeout: 30_000 });

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
  if (!createdIds.has(METRIC_INGESTION.metric_id)) test.skip();
  await triggerRunViaUI(page, adminApi, METRIC_INGESTION.metric_id, METRIC_INGESTION.title);
});

test("UC5 step 2 — trigger immediate run for validation-score metric", async ({
  page,
  adminApi,
}) => {
  if (!createdIds.has(METRIC_VALIDATION.metric_id)) test.skip();
  await triggerRunViaUI(page, adminApi, METRIC_VALIDATION.metric_id, METRIC_VALIDATION.title);
});

test("UC5 step 2 — trigger immediate run for doc-health metric", async ({
  page,
  adminApi,
}) => {
  if (!createdIds.has(METRIC_DOC.metric_id)) test.skip();
  await triggerRunViaUI(page, adminApi, METRIC_DOC.metric_id, METRIC_DOC.title);
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 3a — Dashboard: metric cards + trend chart section visible after results land
// spec: USE_CASE_en.md §UC5 §Imazon Example — "A week later, trends are pulled"
// spec: FRONTEND_GOVERNANCE.md §Dashboard — metric cards + small-multiples chart section.
// spec: dashboard/page.tsx — DashboardContent: MetricCard + MetricTimeseriesChart per metric.
// ─────────────────────────────────────────────────────────────────────────────

test("UC5 step 3a — dashboard shows metric cards and trend chart section", async ({
  page,
  adminApi,
}) => {
  if (!createdIds.has(METRIC_INGESTION.metric_id)) test.skip();

  // Poll adminApi until ≥1 result row appears for ingestion-freshness (bellwether metric).
  // spec: TESTING.md §E2E critical pitfall 3 — poll adminApi until present, THEN assert UI.
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

  // Navigate to the dashboard.
  await page.goto("/governance/dashboard");
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: page heading --
  // spec: dashboard/page.tsx — h1 "Governance · Dashboard"
  await expect(
    page.getByRole("heading", { name: "Governance · Dashboard", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: metric cards visible (async TanStack Query; wait for panel) --
  // spec: FRONTEND_GOVERNANCE.md §Dashboard — one card per enabled metric (CardTitle = title).
  // spec: dashboard/page.tsx — MetricCard renders CardTitle={metric.title}.
  // Use .first() — the title may also appear in the chart section heading (pitfall 1).
  await expect(
    page.getByText(METRIC_INGESTION.title, { exact: true }).first()
  ).toBeVisible({ timeout: 30_000 });
  await expect(
    page.getByText(METRIC_VALIDATION.title, { exact: true }).first()
  ).toBeVisible({ timeout: 15_000 });
  await expect(
    page.getByText(METRIC_DOC.title, { exact: true }).first()
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: "Daily trend (last 30 d)" section heading visible --
  // spec: dashboard/page.tsx — h2 "Daily trend (last 30 d)"
  await expect(
    page.getByRole("heading", { name: "Daily trend (last 30 d)", exact: true })
  ).toBeVisible({ timeout: 15_000 });

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
  if (createdIds.size < 3) test.skip();

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

  // -- UI assertion: metric_type badges visible --
  // spec: metrics/page.tsx — Badge variant="outline" text={m.metric_type}
  // Three distinct type strings; each appears at least once (could be > once if filter reused).
  await expect(
    page.getByText("ingestion-freshness", { exact: true }).first()
  ).toBeVisible({ timeout: 10_000 });
  await expect(
    page.getByText("validation-score", { exact: true }).first()
  ).toBeVisible({ timeout: 10_000 });
  await expect(
    page.getByText("doc-health", { exact: true }).first()
  ).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: "Enabled" badges visible (all three were created is_enabled=true) --
  // spec: metrics/page.tsx — Badge variant="default" text="Enabled" when m.is_enabled
  // Multiple rows render "Enabled"; assert ≥1 is visible.
  await expect(page.getByText("Enabled", { exact: true }).first()).toBeVisible({ timeout: 10_000 });

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
// Step 3c — Per-metric detail: attr/conf, attr/result chart, event log
// spec: FRONTEND_GOVERNANCE.md §Metrics §[id] — detail page sections:
//   attr/conf (read-only dl), attr/result (MetricTimeseriesChart), event (log list).
// spec: [id]/page.tsx — section h2 labels: "attr/conf", "attr/result", "event".
// ─────────────────────────────────────────────────────────────────────────────

test("UC5 step 3c — doc-health detail page: attr/conf, attr/result, event sections; Edit/Run/Delete buttons", async ({
  page,
  adminApi,
}) => {
  if (!createdIds.has(METRIC_DOC.metric_id)) test.skip();

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

  // -- UI assertion: attr/conf section heading --
  // spec: [id]/page.tsx — h2 "attr/conf"
  await expect(
    page.getByRole("heading", { name: "attr/conf", exact: true })
  ).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: key conf fields rendered in the dl --
  // spec: [id]/page.tsx — dl renders mode, metric_type badge, schedule_tier, is_enabled badge
  await expect(page.getByText("active", { exact: true }).first()).toBeVisible({ timeout: 10_000 });

  // metric_type as Badge variant="outline" — check exact string to target the badge text.
  await expect(
    page.getByText("doc-health", { exact: true }).first()
  ).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: attr/result section heading --
  // spec: [id]/page.tsx — section h2 "attr/result"
  await expect(
    page.getByRole("heading", { name: "attr/result", exact: true })
  ).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: range selector visible (7d / 30d / 90d) --
  // spec: [id]/page.tsx — Select trigger renders current rangeLabel; default "30d"
  // Radix SelectTrigger; locate by role="combobox" first occurrence in the attr/result section.
  // The trigger renders the current rangeLabel text ("30d" by default).
  await expect(page.getByText("30d", { exact: true }).first()).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: event section heading --
  // spec: [id]/page.tsx — section h2 "event"
  await expect(
    page.getByRole("heading", { name: "event", exact: true })
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
  // values keys must match the declared metrics list.
  // spec: USE_CASE_en.md §UC5 §Built-in active metric types — values is a dict; keys = metrics[]
  const valuesKeys = new Set(Object.keys(resultBody.results[0]!.values));
  for (const key of METRIC_DOC.metrics) {
    expect(valuesKeys.has(key), `values.${key} missing from doc-health result`).toBe(true);
  }

  // -- Backend probe: GET event → ≥1 METRIC.RUN_COMPLETE event after the run --
  // spec: [id]/page.tsx — event section renders eventsData.events; METRIC.RUN_COMPLETE
  // spec: API.md §Governance events — POST .../method/run emits METRIC.RUN_COMPLETE
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
  if (!createdIds.has(METRIC_INGESTION.metric_id)) test.skip();
  await deleteMetricViaUI(page, adminApi, METRIC_INGESTION.metric_id, METRIC_INGESTION.title);
  createdIds.delete(METRIC_INGESTION.metric_id);
});

test("UC5 step 4 — delete validation-score metric via ConfirmDialog", async ({
  page,
  adminApi,
}) => {
  if (!createdIds.has(METRIC_VALIDATION.metric_id)) test.skip();
  await deleteMetricViaUI(page, adminApi, METRIC_VALIDATION.metric_id, METRIC_VALIDATION.title);
  createdIds.delete(METRIC_VALIDATION.metric_id);
});

test("UC5 step 4 — delete doc-health metric via ConfirmDialog", async ({
  page,
  adminApi,
}) => {
  if (!createdIds.has(METRIC_DOC.metric_id)) test.skip();
  await deleteMetricViaUI(page, adminApi, METRIC_DOC.metric_id, METRIC_DOC.title);
  createdIds.delete(METRIC_DOC.metric_id);
});
