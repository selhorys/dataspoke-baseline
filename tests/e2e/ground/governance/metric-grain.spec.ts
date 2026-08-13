/**
 * Ground spec: the ChartGrainPicker on /governance/metrics/[id] (Result panel).
 *
 * Narrow per-page UI flow — the spot analogue for the display-grain control:
 * the Result panel header carries a grain picker beside its RangePicker, the
 * three grains are selectable, switching one changes NO request the page makes,
 * and the choice survives a reload and carries to another metric's Result panel
 * (one stored grain per panel type, shared across entities of that type).
 *
 * Independent: seeds two disabled, on-demand metrics via REST so the detail
 * route resolves, and deletes them in afterAll. No results are required — the
 * grain control lives in the panel header, above the chart, so an empty result
 * set is a valid render for this concern.
 *
 * spec: spec/feature/FRONTEND_BASIC.md §Shared Component Notes → ChartGrainPicker —
 *   "the **display-grain** control for every chart surface, placed in the heading
 *   row of the section whose charts it governs: … the governance metric detail
 *   `Result` panel header (beside that panel's RangePicker) …. It selects one of
 *   three grains — hourly, daily (default), weekly"; "the grain is a client-side
 *   display concern and **adds no request parameter**: it never alters the
 *   `from` / `to` / `until` / `limit` a call site sends"; "The selection
 *   **persists across visits** in browser `localStorage` under a stable key per
 *   logical panel … each panel keeps its own grain, shared across all entities of
 *   that panel type."
 * spec: spec/feature/FRONTEND_GOVERNANCE.md §Metrics (detail wireframe) —
 *   "Result   [Last 2 weeks ▾] [Daily ▾]".
 * spec: spec/API.md §Metric — POST /spoke/governance/metric;
 *   GET /spoke/governance/metric/{id}/attr/result.
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group; one concern per test.
 */

import { test, expect } from "../../fixtures/index";

// Stable natural keys, NOT a per-run suffix: `retries` restarts the worker and
// re-evaluates module scope, so a generated id would differ between the original
// run and its retry — leaving the first run's metrics behind and making the
// pre-delete below a no-op against the wrong id. Fixed ids + pre-delete are what
// make the seed idempotent across retries.
const METRIC_API = "/api/v1/spoke/governance/metric";
const METRIC_A = "ground-grain-a";
const METRIC_B = "ground-grain-b";

function createBody(metricId: string, title: string) {
  return {
    metric_id: metricId,
    mode: "active",
    is_enabled: false,
    metric_type: "doc-health",
    title,
    description: "Ground spec fixture for the chart grain picker",
    metrics: [
      { name: "total", color: "#64748B", idx: 1 },
      { name: "doc_health", color: "#A855F7", idx: 2 },
    ],
    metric_conf: {},
    schedule_tier: null,
    // Empty clause = every registered dataset (API.md §`dataset_filter` grammar).
    dataset_filter: "",
  };
}

test.beforeAll(async ({ adminApi }) => {
  for (const [id, title] of [
    [METRIC_A, "Grain Ground A"],
    [METRIC_B, "Grain Ground B"],
  ]) {
    // Pre-delete so a retried run (or a previous run that died before afterAll)
    // re-creates cleanly — POST on an existing id is 409 METRIC_EXISTS.
    await adminApi.delete(`${METRIC_API}/${id}/attr/conf`).catch(() => null);
    const resp = await adminApi.post(METRIC_API, { data: createBody(id, title) });
    expect(
      [200, 201],
      `seeding metric ${id} failed: ${resp.status()} ${await resp.text()}`,
    ).toContain(resp.status());
  }
});

test.afterAll(async ({ adminApi }) => {
  for (const id of [METRIC_A, METRIC_B]) {
    await adminApi.delete(`${METRIC_API}/${id}/attr/conf`).catch(() => null);
  }
});

// ── Test 1 — the control is present, defaults to Daily, offers the three grains ──
// spec: FRONTEND_BASIC.md §Shared Component Notes → ChartGrainPicker — placed in
//   the Result panel heading row beside that panel's RangePicker; hourly / daily
//   (default) / weekly.

test("the Result panel offers hourly / daily / weekly, defaulting to Daily", async ({
  page,
}) => {
  await page.goto(`/governance/metrics/${METRIC_A}`);
  await expect(page).not.toHaveURL(/\/login/);

  await expect(page.getByRole("heading", { name: "Result", exact: true })).toBeVisible({
    timeout: 15_000,
  });

  const grain = page.getByRole("combobox", { name: "Chart grain" });
  await expect(grain).toBeVisible({ timeout: 10_000 });

  // -- Default selection is daily --
  await expect(grain).toHaveText("Daily");

  // -- Exactly the three grains are offered --
  await grain.click();
  const listbox = page.getByRole("listbox");
  await expect(listbox.getByRole("option")).toHaveCount(3);
  for (const label of ["Hourly", "Daily", "Weekly"]) {
    await expect(listbox.getByRole("option", { name: label, exact: true })).toBeVisible();
  }

  // -- Selecting one updates the trigger --
  await listbox.getByRole("option", { name: "Hourly", exact: true }).click();
  await expect(grain).toHaveText("Hourly");
});

// ── Test 2 — grain is display-only: it changes no request ───────────────────────
// spec: FRONTEND_BASIC.md §Shared Component Notes → ChartGrainPicker — "the grain
//   is a client-side display concern and adds no request parameter: it never
//   alters the `from` / `to` / `until` / `limit` a call site sends".

test("switching grain adds no request parameter to the result read", async ({ page }) => {
  const resultQueries: string[] = [];
  const resultPattern = new RegExp(
    `/spoke/governance/metric/${METRIC_A}/attr/result(\\?|$)`,
  );
  page.on("request", (req) => {
    if (req.method() === "GET" && resultPattern.test(req.url())) {
      resultQueries.push(new URL(req.url()).search);
    }
  });

  await page.goto(`/governance/metrics/${METRIC_A}`);
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page.getByRole("heading", { name: "Result", exact: true })).toBeVisible({
    timeout: 15_000,
  });

  // The initial result read must have fired, so the comparison below is not vacuous.
  await expect.poll(() => resultQueries.length, { timeout: 15_000 }).toBeGreaterThan(0);

  // Wait for the request stream to go QUIET rather than sleeping a guessed
  // interval: post-mount hydration (the RangePicker's own stored selection) can
  // re-issue the read, and a hydration-driven query must not be mistaken later for
  // a grain leak. Snapshot only once two consecutive samples agree.
  let stable = 0;
  let lastSeen = -1;
  await expect
    .poll(
      () => {
        const n = resultQueries.length;
        stable = n === lastSeen ? stable + 1 : 0;
        lastSeen = n;
        return stable;
      },
      { timeout: 20_000, intervals: [500] },
    )
    .toBeGreaterThanOrEqual(2);
  const before = new Set(resultQueries);

  // -- Switch the grain twice --
  const grain = page.getByRole("combobox", { name: "Chart grain" });
  await grain.click();
  await page.getByRole("option", { name: "Weekly", exact: true }).click();
  await expect(grain).toHaveText("Weekly");
  await grain.click();
  await page.getByRole("option", { name: "Hourly", exact: true }).click();
  await expect(grain).toHaveText("Hourly");

  // Give any grain-triggered refetch a chance to appear before asserting absence.
  await page.waitForTimeout(3_000);

  // -- No result read ever carried a grain parameter, and the query strings the
  //    page sends are exactly the ones it sent before the grain changed (a poll
  //    tick repeats an identical query; a new/changed query would be a leak).
  const after = new Set(resultQueries);
  expect(
    resultQueries.filter((q) => /grain/i.test(q)),
    "grain must never enter the result query string",
  ).toEqual([]);
  expect(
    Array.from(after).sort(),
    "switching grain must not change the from/to/until/limit the page sends",
  ).toEqual(Array.from(before).sort());
});

// ── Test 3 — the selection persists per panel, across visits and entities ───────
// spec: FRONTEND_BASIC.md §Shared Component Notes → ChartGrainPicker — "The
//   selection persists across visits in browser localStorage under a stable key
//   per logical panel … each panel keeps its own grain, shared across all
//   entities of that panel type."

test("the chosen grain survives a reload and applies to another metric's Result panel", async ({
  page,
}) => {
  await page.goto(`/governance/metrics/${METRIC_A}`);
  await expect(page).not.toHaveURL(/\/login/);

  const grain = page.getByRole("combobox", { name: "Chart grain" });
  await expect(grain).toBeVisible({ timeout: 15_000 });
  await expect(grain).toHaveText("Daily");

  await grain.click();
  await page.getByRole("option", { name: "Weekly", exact: true }).click();
  await expect(grain).toHaveText("Weekly");

  // -- Persists across a reload of the same metric (post-mount hydration) --
  await page.reload();
  const afterReload = page.getByRole("combobox", { name: "Chart grain" });
  await expect(afterReload).toBeVisible({ timeout: 15_000 });
  await expect(afterReload).toHaveText("Weekly");

  // -- Shared across all entities of the same panel type --
  await page.goto(`/governance/metrics/${METRIC_B}`);
  const onMetricB = page.getByRole("combobox", { name: "Chart grain" });
  await expect(onMetricB).toBeVisible({ timeout: 15_000 });
  await expect(onMetricB).toHaveText("Weekly");
});
