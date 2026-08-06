/**
 * Ground spec: the metric view controls on /governance/dashboard.
 *
 * Narrow per-page UI flows — the spot analogue for the dashboard's three
 * client-side view controls: the metric-type multi-select, the title search,
 * and the title sort. One concern per test:
 *   1. Defaults — every type checked, the search blank, the sort ascending by
 *      title (the seeded cards render in ascending title order).
 *   2. Deselecting one type removes that type's cards and keeps the others.
 *   3. The title search is a case-insensitive substring over `title`, and a
 *      token only a `description` carries matches nothing.
 *   4. Deselecting every type yields the filtered-empty state — the one pointing
 *      at the view controls, NOT the "enable a metric on the Metrics page" one.
 *   5. The choices survive a page reload (localStorage, per the RangePicker /
 *      ChartGrainPicker rule).
 *   6. Dual confirmation, negative form: none of the three controls changes the
 *      metric-list request. Every `GET /spoke/governance/metric` the page issues
 *      carries exactly `is_enabled=true` + `limit=100`, before and after the
 *      gestures.
 *
 * Independent: seeds three ENABLED, on-demand metrics via REST — one per metric
 * type — and deletes them in afterAll. No metric results are required: the
 * controls sit above the grid and a result-less card is a valid render for this
 * concern. Assertions are scoped to the seeded ids, so other enabled metrics left
 * on the dev env do not perturb them; the one whole-grid assertion (test 4) is by
 * construction independent of what else is enabled.
 *
 * THE FIXTURE IS BUILT TO SEPARATE `title` FROM `description`, because the spec
 * names `title` for both the sort and the search:
 *   - the two orderings DISAGREE — titles ascend Alpha < bravado < Charlie while
 *     descriptions ascend Xray < Yankee < Zulu, i.e. [bravo, alpha, charlie].
 *     That is a third-order permutation, not the reverse, so it matches neither
 *     ascending nor descending title order and an impl that sorted by
 *     `description` fails test 1 whichever way its direction flag is mapped;
 *   - the middle title starts LOWER-CASE and sorts between two upper-case ones
 *     for a reader, but last under a raw code-unit comparator, so test 1 also
 *     pins human collation;
 *   - every search needle sits MID-`title` and appears in NO description, and
 *     each description opens with a token found in no title, so an impl that
 *     searched `description`, or matched a prefix instead of a substring, fails
 *     tests 3, 5 and 6.
 *
 * spec: spec/feature/FRONTEND_GOVERNANCE.md §Dashboard (Metric view controls) —
 *   "A row of three controls beneath the header narrows and orders the
 *   already-fetched enabled set entirely client-side …: a **metric-type filter**
 *   (checkbox-group multi-select over the built-in `metric_type` values listed in
 *   [USE_CASE §UC5](../USE_CASE_en.md#uc5-governance), each box labelled by its
 *   raw `metric_type` value, all selected by default; deselecting every type
 *   yields an empty set rather than falling back to all), a **title search**
 *   (case-insensitive substring over each metric's `title`, inactive while
 *   blank), and a **title sort** (`Title A→Z` / `Title Z→A`, ascending by
 *   default) — the title is the metric's human-facing identifier and what the
 *   reader scans the grid by, so both controls key off it. Each selection
 *   persists across visits in browser `localStorage` under a stable key"; and the
 *   controls run over "the same `GET /spoke/governance/metric` (filter
 *   `is_enabled=true`) read that backs the cards — **no request parameter**".
 * spec: spec/USE_CASE_en.md §UC5 §Built-in active metric types — the three values
 *   the type filter offers: `ingestion-freshness`, `validation-score`,
 *   `doc-health`.
 * spec: spec/feature/FRONTEND_GOVERNANCE.md §Dashboard — "The grid carries two
 *   distinct empty states. With no enabled metrics at all it points at the
 *   Metrics page …. With enabled metrics present but none surviving the type
 *   filter and title search it points at the view controls instead."
 * spec: spec/API.md §Metric — POST /spoke/governance/metric;
 *   GET /spoke/governance/metric (filterable by is_enabled).
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group; one concern per test.
 */

import type { Page } from "@playwright/test";
import { test, expect } from "../../fixtures/index";

const DASHBOARD_URL = "/governance/dashboard";
const METRIC_API = "/api/v1/spoke/governance/metric";

// Stable natural keys, NOT a per-run suffix: `retries` restarts the worker and
// re-evaluates module scope, so a generated id would differ between the original
// run and its retry. Fixed ids + pre-delete are what make the seed idempotent.
//
// Titles run Alpha / bravado / Charlie against descriptions Yankee / Xray / Zulu:
// ascending description order is [bravado, Alpha, Charlie] — neither ascending
// nor descending title order. No title carries a description token, and no
// description carries a search needle.
const ALPHA = {
  metric_id: "ground-view-alpha",
  metric_type: "ingestion-freshness",
  // "Quebec" is mid-title and in no description; no description token
  // ("Yankee" / "Xray" / "Zulu") appears in any title.
  title: "View Ground Alpha Quebec",
  description: "Yankee seed for the enabled-metric grid",
  metrics: ["total", "ingested_in_time"],
  metric_conf: { time_window_sec: 172800 },
};
const BRAVO = {
  metric_id: "ground-view-bravo",
  metric_type: "validation-score",
  // "ROMEO" is upper-case and mid-title, matched below by a lower-case needle:
  // case-insensitivity in one direction, substring (not prefix) in the other,
  // and it appears in no description. "Tango" is shared with CHARLIE alone.
  // The lower-case "bravado" is what makes the order a human one: it sorts
  // between Alpha and Charlie for a reader, but after both by code unit.
  title: "View Ground bravado Tango ROMEO",
  description: "Xray seed for the enabled-metric grid",
  metrics: ["total", "validation_score_sum"],
  metric_conf: { time_window_sec: 172800 },
};
const CHARLIE = {
  metric_id: "ground-view-charlie",
  metric_type: "doc-health",
  // "sierra" mirrors ROMEO in the other case direction.
  title: "View Ground Charlie Tango sierra",
  description: "Zulu seed for the enabled-metric grid",
  metrics: ["total", "doc_health"],
  metric_conf: {},
};
const SEEDED = [ALPHA, BRAVO, CHARLIE];
// Ascending by `title` as a reader reads it: Alpha < bravado < Charlie. Ascending
// by `description` would be [bravado, Alpha, Charlie] and a code-unit comparison
// [Alpha, Charlie, bravado] — both different, which is the point of the fixture.
const ASCENDING_IDS = [ALPHA.metric_id, BRAVO.metric_id, CHARLIE.metric_id];

test.beforeAll(async ({ adminApi }) => {
  for (const m of SEEDED) {
    // Pre-delete so a retried run re-creates cleanly — POST on an existing id is
    // 409 METRIC_EXISTS.
    await adminApi.delete(`${METRIC_API}/${m.metric_id}/attr/conf`).catch(() => null);
    const resp = await adminApi.post(METRIC_API, {
      data: {
        metric_id: m.metric_id,
        mode: "active",
        // Enabled: the dashboard reads is_enabled=true. On-demand
        // (schedule_tier null) so the seed never enters a scheduled DAG.
        is_enabled: true,
        metric_type: m.metric_type,
        title: m.title,
        description: m.description,
        metrics: m.metrics,
        metric_conf: m.metric_conf,
        schedule_tier: null,
        dataset_filter: {},
      },
    });
    expect(
      [200, 201],
      `seeding metric ${m.metric_id} failed: ${resp.status()} ${await resp.text()}`,
    ).toContain(resp.status());
  }

  // Backend precondition: all three are in the very read the dashboard makes.
  const listResp = await adminApi.get(`${METRIC_API}?is_enabled=true&limit=100`);
  expect(listResp.ok(), `enabled-metric probe failed: ${await listResp.text()}`).toBeTruthy();
  const enabled = new Set(
    ((await listResp.json()) as { metrics: { id: string }[] }).metrics.map((m) => m.id),
  );
  for (const m of SEEDED) {
    expect(
      enabled.has(m.metric_id),
      `${m.metric_id} must be in GET /spoke/governance/metric?is_enabled=true`,
    ).toBe(true);
  }
});

test.afterAll(async ({ adminApi }) => {
  for (const m of SEEDED) {
    await adminApi.delete(`${METRIC_API}/${m.metric_id}/attr/conf`).catch(() => null);
    const readBack = await adminApi.get(`${METRIC_API}/${m.metric_id}/attr/conf`);
    expect(
      readBack.status(),
      `the seeded metric ${m.metric_id} must be gone so later specs see the baseline`,
    ).toBe(404);
  }
});

// ── Helpers ────────────────────────────────────────────────────────────────────

const typeBox = (page: Page, type: string) =>
  page.getByRole("checkbox", { name: type, exact: true });
const searchBox = (page: Page) => page.getByLabel("Search titles");
const sortSelect = (page: Page) => page.getByRole("combobox", { name: "Sort metrics" });
const card = (page: Page, metricId: string) => page.getByTestId(`metric-card-${metricId}`);

/** Ids of the SEEDED cards currently in the grid, in DOM (display) order. */
async function seededCardOrder(page: Page): Promise<string[]> {
  const ids = await page
    .locator('[data-testid^="metric-card-"]')
    .evaluateAll((els) =>
      els.map((el) => (el.getAttribute("data-testid") ?? "").replace("metric-card-", "")),
    );
  return ids.filter((id) => ASCENDING_IDS.includes(id));
}

/** Open the dashboard and wait until the seeded cards have rendered. */
async function openDashboard(page: Page): Promise<void> {
  await page.goto(DASHBOARD_URL);
  await expect(page).not.toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: "Governance · Dashboard", exact: true }),
  ).toBeVisible({ timeout: 15_000 });
  for (const m of SEEDED) {
    await expect(card(page, m.metric_id)).toBeVisible({ timeout: 20_000 });
  }
}

// ── Test 1 — defaults ───────────────────────────────────────────────────────────
// spec: FRONTEND_GOVERNANCE.md §Dashboard — "all selected by default"; the search
//   is "inactive while blank"; the sort is "ascending by default".

test("the view controls default to every type, a blank search, and ascending title order", async ({
  page,
}) => {
  await openDashboard(page);

  for (const type of ["ingestion-freshness", "validation-score", "doc-health"]) {
    await expect(typeBox(page, type)).toHaveAttribute("aria-checked", "true");
  }
  await expect(searchBox(page)).toHaveValue("");
  // The prompt the reader sees names the field searched, per the §Dashboard
  // sketch "[ Search titles…          ]". Anchored at the leading words; the
  // trailing ellipsis is the impl's typography.
  await expect(searchBox(page)).toHaveAttribute("placeholder", /^Search titles\b/i);
  // "Title A→Z": the ordering it names AND the key it names — a control still
  // reading "Description A→Z" over a title-ordered grid misdescribes the choice,
  // and "Description" contains no A or Z to break an ordering-only pattern.
  await expect(sortSelect(page)).toHaveText(/^Title\b.*A.*Z/);

  // The three seeded metrics render in ascending `title` order as a reader reads
  // it (Alpha < bravado < Charlie), regardless of the order the read returned
  // them in. Ascending `description` would be [bravado, Alpha, Charlie] and
  // descending [Charlie, Alpha, bravado], so neither direction of a description
  // sort passes; a raw code-unit comparator would put lower-case "bravado" last.
  expect(await seededCardOrder(page)).toEqual(ASCENDING_IDS);
});

// ── Test 2 — the type filter is a multi-select over the fetched set ─────────────
// spec: FRONTEND_GOVERNANCE.md §Dashboard — "a metric-type filter (checkbox-group
//   multi-select over ingestion-freshness / validation-score / doc-health …)".

test("deselecting one metric type removes that type's cards and keeps the rest", async ({
  page,
}) => {
  await openDashboard(page);

  await typeBox(page, "validation-score").click();
  await expect(typeBox(page, "validation-score")).toHaveAttribute("aria-checked", "false");

  // The validation-score card is gone; the other two types are untouched.
  await expect(card(page, BRAVO.metric_id)).toHaveCount(0);
  await expect(card(page, ALPHA.metric_id)).toBeVisible();
  await expect(card(page, CHARLIE.metric_id)).toBeVisible();

  // Re-checking brings it back — the control is a multi-select, not a one-way gate.
  await typeBox(page, "validation-score").click();
  await expect(typeBox(page, "validation-score")).toHaveAttribute("aria-checked", "true");
  await expect(card(page, BRAVO.metric_id)).toBeVisible();
});

// ── Test 3 — title search ───────────────────────────────────────────────────────
// spec: FRONTEND_GOVERNANCE.md §Dashboard — "a title search (case-insensitive
//   substring over each metric's title, inactive while blank)".

test("the title search matches a case-insensitive substring and is inactive while blank", async ({
  page,
}) => {
  await openDashboard(page);

  // Lower-case needle against BRAVO's upper-case "ROMEO". The token sits in the
  // MIDDLE of the `title` and in no `description`, so a prefix match, an exact
  // match, or a search over `description` all fail here.
  await searchBox(page).fill("romeo");
  await expect(card(page, BRAVO.metric_id)).toBeVisible();
  await expect(card(page, ALPHA.metric_id)).toHaveCount(0);
  await expect(card(page, CHARLIE.metric_id)).toHaveCount(0);

  // Upper-case needle against CHARLIE's lower-case "sierra" — the other direction,
  // likewise mid-title and absent from every description.
  await searchBox(page).fill("SIERRA");
  await expect(card(page, CHARLIE.metric_id)).toBeVisible();
  await expect(card(page, BRAVO.metric_id)).toHaveCount(0);

  // "Yankee" opens ALPHA's `description` and appears in no title: the search
  // keys off `title` only, so no seeded card may survive it.
  await searchBox(page).fill("Yankee");
  for (const m of SEEDED) {
    await expect(card(page, m.metric_id)).toHaveCount(0);
  }

  // Blank again → inactive → the whole enabled set is back.
  await searchBox(page).fill("");
  for (const m of SEEDED) {
    await expect(card(page, m.metric_id)).toBeVisible();
  }
});

// ── Test 4 — deselecting every type is empty, not an implicit "all" ─────────────
// spec: FRONTEND_GOVERNANCE.md §Dashboard — "deselecting every type yields an
//   empty set rather than falling back to all"; and with enabled metrics present
//   but none surviving, the grid "points at the view controls instead" of the
//   Metrics page.

test("deselecting every metric type empties the grid and shows the view-controls empty state", async ({
  page,
}) => {
  await openDashboard(page);

  for (const type of ["ingestion-freshness", "validation-score", "doc-health"]) {
    await typeBox(page, type).click();
    await expect(typeBox(page, type)).toHaveAttribute("aria-checked", "false");
  }

  // No card survives — an implicit fallback to "all" would leave the grid full.
  await expect(page.locator('[data-testid^="metric-card-"]')).toHaveCount(0);

  // The empty state is the one about the reader's own controls; the
  // enable-a-metric copy (which names the Metrics page) must NOT appear, since
  // enabled metrics do exist.
  await expect(page.getByRole("main").getByText(/controls/i)).toBeVisible();
  await expect(page.getByText(/Metrics page/i)).toHaveCount(0);
});

// ── Test 5 — the choices persist across visits ─────────────────────────────────
// spec: FRONTEND_GOVERNANCE.md §Dashboard — "Each selection persists across visits
//   in browser localStorage under a stable key, by the same rule as the shared
//   RangePicker and ChartGrainPicker selections."

test("the type / search / sort choices survive a reload", async ({ page }) => {
  await openDashboard(page);

  await typeBox(page, "ingestion-freshness").click();
  // "Tango" is mid-`title` in BRAVO and CHARLIE only and in no `description`,
  // so the persisted search has to be doing real work for the pair below.
  await searchBox(page).fill("Tango");
  await sortSelect(page).click();
  await page.getByRole("option", { name: /Z.*A/ }).click();
  await expect(sortSelect(page)).toHaveText(/Z.*A/);

  // Pre-reload state: Alpha out (both the type filter and the search exclude
  // it), bravado/Charlie kept by the search, in descending title order — Charlie
  // first, since a reader orders bravado before Charlie. A raw code-unit
  // comparator would descend to bravado first. (Whether the sort keys off
  // `title` at all is test 1's job; this pair's descriptions happen to agree.)
  await expect(card(page, ALPHA.metric_id)).toHaveCount(0);
  expect(await seededCardOrder(page)).toEqual([CHARLIE.metric_id, BRAVO.metric_id]);

  await page.reload();
  await expect(
    page.getByRole("heading", { name: "Governance · Dashboard", exact: true }),
  ).toBeVisible({ timeout: 15_000 });

  // -- Post-mount hydration restores all three controls --
  await expect(typeBox(page, "ingestion-freshness")).toHaveAttribute("aria-checked", "false");
  await expect(typeBox(page, "validation-score")).toHaveAttribute("aria-checked", "true");
  await expect(searchBox(page)).toHaveValue("Tango");
  await expect(sortSelect(page)).toHaveText(/Z.*A/);
  await expect(card(page, BRAVO.metric_id)).toBeVisible({ timeout: 20_000 });
  await expect(card(page, ALPHA.metric_id)).toHaveCount(0);
  expect(await seededCardOrder(page)).toEqual([CHARLIE.metric_id, BRAVO.metric_id]);
});

// ── Test 6 — dual confirmation: the controls add no request parameter ──────────
// spec: FRONTEND_GOVERNANCE.md §Dashboard — the view controls run over "the same
//   GET /spoke/governance/metric (filter is_enabled=true) read that backs the
//   cards — no request parameter", the way the ChartGrainPicker beside them is
//   display-only.

test("filtering, searching and sorting add no parameter to the metric-list read", async ({
  page,
}) => {
  const listQueries: string[][] = [];
  // Matches the LIST read only — `/metric/{id}/attr/result` cannot match, since
  // the path must end at `metric` or be followed by `?`.
  const listPattern = /\/spoke\/governance\/metric(\?|$)/;
  page.on("request", (req) => {
    if (req.method() === "GET" && listPattern.test(req.url())) {
      const params = [...new URL(req.url()).searchParams.entries()]
        .map(([k, v]) => `${k}=${v}`)
        .sort();
      listQueries.push(params);
    }
  });

  await openDashboard(page);

  // The initial list read must have fired, so the comparison below is not vacuous.
  await expect.poll(() => listQueries.length, { timeout: 20_000 }).toBeGreaterThan(0);

  // Wait for the request stream to go QUIET rather than sleeping a guessed
  // interval: the dashboard polls, and a poll tick must not be mistaken for a
  // control-driven refetch. Snapshot only once two consecutive samples agree.
  let stable = 0;
  let lastSeen = -1;
  await expect
    .poll(
      () => {
        const n = listQueries.length;
        stable = n === lastSeen ? stable + 1 : 0;
        lastSeen = n;
        return stable;
      },
      { timeout: 20_000, intervals: [500] },
    )
    .toBeGreaterThanOrEqual(2);
  const before = new Set(listQueries.map((p) => p.join("&")));

  // -- Exercise all three controls --
  await typeBox(page, "doc-health").click();
  await expect(card(page, CHARLIE.metric_id)).toHaveCount(0);
  await searchBox(page).fill("romeo");
  await expect(card(page, ALPHA.metric_id)).toHaveCount(0);
  await sortSelect(page).click();
  await page.getByRole("option", { name: /Z.*A/ }).click();
  await expect(sortSelect(page)).toHaveText(/Z.*A/);

  // Backstop: the controls really did reshape the grid, so the equality below
  // reads as "view changed, read didn't" — not "the controls were inert".
  await expect(card(page, BRAVO.metric_id)).toBeVisible();

  // Give any control-triggered refetch a chance to appear before asserting absence.
  await page.waitForTimeout(3_000);

  // -- Every list read the page ever issued carried exactly is_enabled + limit --
  for (const params of listQueries) {
    expect(
      params,
      "the dashboard's metric-list read must carry only is_enabled=true and limit=100",
    ).toEqual(["is_enabled=true", "limit=100"]);
  }
  // -- and the set of query strings is unchanged by the gestures (a poll tick
  //    repeats an identical query; a new/changed query would be a leak).
  const after = new Set(listQueries.map((p) => p.join("&")));
  expect(
    Array.from(after).sort(),
    "the view controls must not change the metric-list query string",
  ).toEqual(Array.from(before).sort());
});
