/**
 * Ground spec: /ingestion/conf — the status column reports the newest RUN OUTCOME,
 * not the newest event in the source's feed.
 *
 * Concern: the list view has no `latest_run` field on its payload, so it derives the
 * badge client-side from a page of `GET /spoke/ingestion/sources/{id}/event`. That feed
 * carries more than run outcomes — source-lifecycle events and per-dataset ingestion
 * observations share it — and either can be NEWER than the run the badge must report.
 * A derivation that took the head of the page renders a green "success" badge over a
 * source whose last run failed.
 *
 * The seeded shape is the exact inversion: a failed run, then a source edit. The edit
 * books `INGESTION.SOURCE_UPDATE` with `status="success"`, which sorts above the
 * `INGESTION.FAIL`. Both required predicates are exercised by it — the lifecycle event is
 * excluded only by the event-type whitelist (it carries no `detail.source` key, so the
 * producer blacklist keeps it), and the `FAIL` is admitted only because that blacklist
 * treats an absent `detail.source` as run-level.
 *
 * The failing run is produced by a recipe naming a `source.type` with no registered
 * extractor: create validates the recipe's *shape*, not the extractor registry, so the
 * run returns `status="error"` immediately and books `INGESTION.FAIL` — no credentials,
 * no network, no timeout.
 *
 * Independent: seeds its own source via REST and deletes it in `afterAll`.
 *
 * spec: spec/feature/FRONTEND_INGESTION.md §List View — "The newest **run outcome** is
 *   derived from that page by two predicates … Both predicates are required and neither
 *   is sufficient alone — the whitelist alone lets a per-dataset observation outrank an
 *   older failure, the blacklist alone lets a newer `SOURCE_UPDATE` (`status="success"`)
 *   do the same."
 * spec: spec/feature/FRONTEND_INGESTION.md §List View — "The first surviving row supplies
 *   the badge; a source whose newest page holds no run outcome shows none."
 * spec: spec/feature/BACKEND.md §Event Catalogue — "`detail.source` is absent, not null,
 *   on the inline record."
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group; one concern per test,
 *   setup fires through the API, dual confirmation against the backend.
 */

import { test, expect } from "../../fixtures/index";

const SOURCES_API = "/api/v1/spoke/ingestion/sources";
/** Natural-key prefix this spec owns. Stable across module loads — a retry re-imports
 *  the module, so a per-load timestamp would make the pre-delete below unable to match
 *  the previous attempt's leftovers, which is the only thing it exists for. */
const SOURCE_NAME_PREFIX = "ground-run-status";
const SOURCE_NAME = SOURCE_NAME_PREFIX;
const RENAMED = `${SOURCE_NAME_PREFIX}-edited`;

interface FeedEvent {
  event_type: string;
  status: string;
  detail: Record<string, unknown> | null;
  occurred_at: string;
}

let sourceId: string | null = null;

test.beforeAll(async ({ adminApi }) => {
  // Idempotent pre-delete by natural key: a retry replays this setup over the leftovers
  // of a failed attempt, and a worker that dies before afterAll leaves the source behind
  // under either name (the edit in step 3 renames it). The prefix sweep covers both.
  // An absent source is success — the sweep simply finds nothing.
  // spec: spec/TESTING.md §End-to-End (E2E) Testing §Execution discipline.
  const existing = await adminApi.get(`${SOURCES_API}?limit=1000`);
  if (existing.ok()) {
    const body = (await existing.json()) as {
      sources: Array<{ id: string; name: string }>;
    };
    for (const s of body.sources) {
      if (s.name.startsWith(SOURCE_NAME_PREFIX)) {
        await adminApi.delete(`${SOURCES_API}/${s.id}`).catch(() => null);
      }
    }
  }

  // 1. Create the source. `source.type` names no registered extractor — create validates
  //    the recipe's shape only, so this is accepted and its run fails deterministically.
  const created = await adminApi.post(SOURCES_API, {
    data: {
      mode: "ACTIVE_CUSTOM_MANAGED",
      name: SOURCE_NAME,
      schedule: "0 0 * * *",
      recipe: {
        source: { type: "e2e-no-such-extractor", config: { host_port: "pg.example:5432" } },
      },
    },
  });
  expect(
    [200, 201],
    `create source failed: ${created.status()} ${await created.text()}`,
  ).toContain(created.status());
  sourceId = ((await created.json()) as { id: string }).id;

  // 2. Run it → INGESTION.FAIL. The route answers 200 carrying status="error"; the run
  //    outcome is the event it books, which is what this spec is about.
  const run = await adminApi.post(`${SOURCES_API}/${sourceId}/method/run`);
  expect(run.ok(), `run failed to execute: ${run.status()} ${await run.text()}`).toBeTruthy();
  expect(
    ((await run.json()) as { status: string }).status,
    "the seeded run must fail — an unregistered source.type produces no emission",
  ).toBe("error");

  // 3. Edit the source → INGESTION.SOURCE_UPDATE, newer than the FAIL and
  //    status="success". This is the inversion the badge must not fall for.
  const patched = await adminApi.patch(`${SOURCES_API}/${sourceId}`, {
    data: { name: RENAMED },
  });
  expect(
    patched.ok(),
    `patch failed: ${patched.status()} ${await patched.text()}`,
  ).toBeTruthy();
});

test.afterAll(async ({ adminApi }) => {
  if (sourceId) await adminApi.delete(`${SOURCES_API}/${sourceId}`).catch(() => null);
});

test("/ingestion/conf — a newer SOURCE_UPDATE does not mask an older failed run", async ({
  page,
  adminApi,
}) => {
  // -- Backend first: confirm the feed really is inverted, so the UI assertion below
  //    separates "the API never produced it" from "the UI did not render it". --
  // spec: spec/TESTING.md §End-to-End (E2E) Testing §Execution discipline — "Gate
  //   data-dependent UI assertions on confirmed backend state."
  const feedResp = await adminApi.get(
    `${SOURCES_API}/${sourceId}/event?offset=0&limit=1000&sort=occurred_at_desc`,
  );
  expect(feedResp.ok(), `feed read failed: ${await feedResp.text()}`).toBeTruthy();
  const feed = ((await feedResp.json()) as { events: FeedEvent[] }).events;

  expect(
    feed[0]?.event_type,
    `the newest event must be the lifecycle edit, or this spec is not testing the ` +
      `inversion; feed head was ${JSON.stringify(feed[0])}`,
  ).toBe("INGESTION.SOURCE_UPDATE");
  expect(
    feed[0]?.status,
    "the lifecycle event carries status='success' — that is what makes it a trap",
  ).toBe("success");
  expect(
    feed[0]?.detail == null || !("source" in feed[0].detail),
    "the lifecycle event carries no detail.source, so only the event-type whitelist can " +
      "exclude it",
  ).toBeTruthy();

  const failEvent = feed.find((e) => e.event_type === "INGESTION.FAIL");
  expect(
    failEvent,
    `the failed run must be on the feed; got ${JSON.stringify(feed.map((e) => e.event_type))}`,
  ).toBeTruthy();
  expect(
    failEvent!.detail == null || !("source" in failEvent!.detail),
    "the inline run record carries no detail.source key — the badge's producer filter " +
      "must treat that as run-level",
  ).toBeTruthy();

  // -- UI: the list view's status column --
  await page.goto("/ingestion/conf");
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page.getByRole("heading", { name: "Ingestion", exact: true })).toBeVisible({
    timeout: 15_000,
  });

  // Narrow to Active sources so the seeded row is on the first page regardless of how
  // many DataHub-managed rows the estate carries. The Select is a Radix composite: click
  // the trigger, then the option by role.
  // spec: spec/TESTING.md §End-to-End (E2E) Testing §Selectors.
  await page.getByLabel("Filter sources by mode").click();
  await page.getByRole("option", { name: "Active", exact: true }).click();

  const row = page.getByRole("row").filter({ hasText: RENAMED });
  await expect(row).toHaveCount(1, { timeout: 15_000 });

  // The status cell is the sixth column. Anchor the index by asserting the header, so a
  // reordered or added column fails here rather than silently reading the wrong cell.
  // spec: spec/feature/FRONTEND_INGESTION.md §List View — the column set.
  const headers = page.getByRole("columnheader");
  await expect(headers).toHaveCount(6);
  await expect(headers.nth(5)).toHaveText("status");

  const statusCell = row.getByRole("cell").nth(5);
  await expect(
    statusCell,
    "the badge must report the failed RUN, not the newer SOURCE_UPDATE that heads the feed",
  ).toHaveText("error", { timeout: 20_000 });
  await expect(statusCell).not.toHaveText("success");
});
