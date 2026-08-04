/**
 * Ground spec — unified per-dataset hub page /data/[urn].
 *
 * The spot-tier analogue of the use-case flows: narrow, single-concern UI checks
 * of the per-dataset hub that the use-case group reaches only incidentally.
 *
 * One concern per test:
 *   1. Header renders the dataset URN; the three summary cards
 *      (Ingestion / Validation / MetaGen) render.
 *   2. The three foldable CollapsiblePanels (Validation, MetaGen, Events) render,
 *      default open, and each folds/unfolds via its header. The ingestion
 *      reverse-lookup is a summary card, not a panel — there is NO Ingestion panel.
 *   3. The consolidated Ingestion summary card renders, and the per-dataset header
 *      exposes a DataHub deep-link (the dev cluster seeds a DataHub frontend_url
 *      into peripheral_config, the sole source of that URL).
 *   4. The Events panel's major-type filter exposes a checkbox per major type
 *      (INGESTION / VALIDATION / METAGEN), all checked by default; unchecking one
 *      narrows the timeline to the still-checked majors.
 *
 * Data setup: global-setup runs --reset-seed (seeded Imazon baseline — every
 * dataset present in DataHub). Tests 1-3 are read-only. Test 4 seeds one INGESTION
 * and one VALIDATION event on the dataset over REST (its own describe-scoped
 * beforeAll) so the filter has rows on both sides, and removes both in afterAll.
 *
 * spec: spec/feature/FRONTEND_BASIC.md §Per-dataset page (three summary cards +
 *   three foldable panels + Events major-type filter; the Ingestion reverse-lookup
 *   folds into the summary card, the standalone Ingestion panel is removed; shared
 *   DataHub dataset deep-link in the header).
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group, selector guidance.
 */

import { test, expect, IMAZON_URNS } from "../../fixtures/index";
import { apiBaseUrl } from "../../fixtures/env";

const DATASET_URN = IMAZON_URNS.titleMaster;
const DATA_URL = `/data/${encodeURIComponent(DATASET_URN)}`;

const PANEL_TITLES = ["Validation", "MetaGen", "Events"] as const;
const MAJOR_TYPES = ["INGESTION", "VALIDATION", "METAGEN"] as const;

// ── Test 1 — header URN + three summary cards ──────────────────────────────────
// spec: FRONTEND_BASIC.md §Per-dataset page — header (URN) + Ingestion/Validation/
//   MetaGen summary cards.

test("data hub renders the dataset URN header and the three summary cards", async ({
  page,
}) => {
  await page.goto(DATA_URL);
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: header shows the dataset URN --
  // src/frontend/app/(app)/data/[urn]/page.tsx — <h1>{datasetUrn}</h1>
  await expect(
    page.getByRole("heading", { name: DATASET_URN, exact: true }),
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: the three summary-card titles render --
  // The labels Ingestion/Validation/MetaGen each appear at least twice (summary
  // card title + panel header); assert each is present at least once.
  for (const label of ["Ingestion", "Validation", "MetaGen"]) {
    await expect(page.getByText(label, { exact: true }).first()).toBeVisible({
      timeout: 10_000,
    });
  }
});

// ── Test 2 — three foldable panels fold/unfold; no Ingestion panel ─────────────
// spec: FRONTEND_BASIC.md §Per-dataset page — three CollapsiblePanels (Validation,
//   MetaGen, Events), default open, header toggles the body; the Ingestion
//   reverse-lookup is a summary card, not a panel.

test("data hub renders three foldable panels that each fold and unfold", async ({
  page,
}) => {
  await page.goto(DATA_URL);
  await expect(page).not.toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: DATASET_URN, exact: true }),
  ).toBeVisible({ timeout: 15_000 });

  for (const title of PANEL_TITLES) {
    // The sidebar nav renders collapsible group toggle buttons for "Ingestion"
    // and "MetaGen" (each with its own aria-expanded), preceding <main> in DOM
    // order — so an unscoped button-role locator would grab the nav button, not
    // the panel header. ("Validation" is a flat nav link, not a button, but we
    // scope uniformly regardless.) Scope to <main> and to the CollapsiblePanel
    // header, the only toggle button carrying aria-controls (the nav group
    // button does not). The summary card title with the same label is a
    // non-button CardTitle, so this selects the panel header unambiguously.
    const header = page
      .getByRole("main")
      .getByRole("button", { name: new RegExp(`^${title}$`, "i") })
      .and(page.locator("[aria-controls]"))
      .first();

    await expect(header).toBeVisible({ timeout: 10_000 });
    // Default open.
    await expect(header).toHaveAttribute("aria-expanded", "true");

    // Fold → aria-expanded false.
    await header.click();
    await expect(header).toHaveAttribute("aria-expanded", "false");

    // Unfold → back to open.
    await header.click();
    await expect(header).toHaveAttribute("aria-expanded", "true");
  }

  // No standalone Ingestion panel: there is no CollapsiblePanel toggle button
  // (the only buttons carrying aria-controls) named "Ingestion" in <main>.
  const ingestionPanel = page
    .getByRole("main")
    .getByRole("button", { name: /^ingestion$/i })
    .and(page.locator("[aria-controls]"));
  await expect(ingestionPanel).toHaveCount(0);
});

// ── Test 3 — consolidated Ingestion card + header DataHub deep-link ─────────────
// spec: FRONTEND_BASIC.md §Per-dataset page — the ingestion reverse-lookup folds
//   into the Ingestion summary card; the header carries a shared DataHub deep-link
//   (the dev cluster seeds a DataHub frontend_url into peripheral_config).

test("data hub shows the consolidated Ingestion card and a header DataHub link", async ({
  page,
  adminApi,
}) => {
  await page.goto(DATA_URL);
  await expect(page).not.toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: DATASET_URN, exact: true }),
  ).toBeVisible({ timeout: 15_000 });

  // -- Backend dual-confirmation: read the reverse-lookup so the Ingestion-card
  //    assertion tracks real backend state rather than a bare title presence. --
  // spec: API.md §Data Resource — GET /spoke/common/data/{urn}/attr/ingestion
  const enc = encodeURIComponent(DATASET_URN);
  const probe = await adminApi.get(
    `/api/v1/spoke/common/data/${enc}/attr/ingestion`,
  );
  expect(probe.ok(), `reverse-lookup probe failed: ${await probe.text()}`).toBeTruthy();
  const lookup = (await probe.json()) as {
    source_id: string | null;
    name: string | null;
  };

  // -- UI assertion: the Ingestion summary card reflects the reverse-lookup --
  // The card is the only place on the hub that renders an owning-source link, so
  // its presence (covered) / the Unmanaged state (no source) proves the card mirrors
  // the backend — independently meaningful, not a bare label presence check.
  const main = page.getByRole("main");
  if (lookup.source_id !== null && lookup.name) {
    const sourceLink = main.getByRole("link", { name: lookup.name });
    await expect(sourceLink.first()).toBeVisible({ timeout: 10_000 });
    await expect(sourceLink.first()).toHaveAttribute(
      "href",
      `/ingestion/sources/${encodeURIComponent(lookup.source_id)}`,
    );
  } else {
    await expect(main.getByText(/unmanaged/i).first()).toBeVisible({ timeout: 10_000 });
  }

  // -- UI assertion: the header DataHub deep-link is present in <main> --
  // The app-shell header also carries a "DataHub" infra icon (aria-label
  // "Open DataHub") — scope to <main> to select the per-dataset deep-link, whose
  // visible text is "DataHub" and href points at the DataHub dataset page.
  const datahubLink = main.getByRole("link", { name: "DataHub" });
  await expect(datahubLink).toBeVisible({ timeout: 10_000 });
  await expect(datahubLink).toHaveAttribute("href", /\/dataset\//);
});

// ── Test 4 — Events major-type filter ──────────────────────────────────────────
// spec: FRONTEND_BASIC.md §Per-dataset page (Events filter) — checkbox-group over
//   INGESTION / VALIDATION / METAGEN, default all checked; toggling narrows the
//   timeline.
//
// The filter is a query predicate, so it is seeded on BOTH sides: this dataset's
// timeline carries at least one INGESTION row and at least one VALIDATION row
// before the gestures, and each gesture asserts that the deselected major's rows
// disappear while the still-selected major's rows remain.
// spec: spec/TESTING.md §Assertion Discipline — "Filter/query/matching tests seed
//   both sides… assert the matching rows appear and the non-matching rows are excluded."

test.describe("Events panel major-type filter", () => {
  // Both event majors are seeded over REST on THIS dataset:
  //
  //   INGESTION — an ACTIVE_CUSTOM_MANAGED source whose recipe matches the catalog
  //     schema. Its create books INGESTION.SOURCE_CREATE and a run against an
  //     unreachable host_port books INGESTION.FAIL (both on the source); the sync
  //     sweep then maps this dataset to the source (derivation='matched'), which is
  //     what makes the source's runs surface on the dataset timeline.
  //     spec: API.md §Data Resource — GET …/event "unions the covering source's
  //     ingestion runs (resolved by reverse-lookup, incl. its internal-wrapper runs)".
  //   VALIDATION — a validation conf (VALIDATION.CONFIG_CREATE) plus one posted
  //     result (VALIDATION.RESULT_RECORDED), both dataset-level events.
  //
  // Both are torn down in afterAll: DELETE conf is a documented cascade over the
  // dataset's validation events, and deleting the source removes its runs/mapping.
  const SOURCE_NAME = `ground-hub-events-${Date.now().toString(36)}`;
  let sourceId: string | null = null;
  let confCreated = false;

  test.beforeAll(async ({ adminApi }) => {
    // The sync sweep below is polled against a real DataHub enumeration, so allow
    // more than the default per-hook budget.
    test.setTimeout(180_000);

    // -- Seed INGESTION: create a source scoped to the catalog schema --
    // spec: API.md §Ingestion — POST /spoke/ingestion/sources
    const createResp = await adminApi.post("/api/v1/spoke/ingestion/sources", {
      data: {
        mode: "ACTIVE_CUSTOM_MANAGED",
        name: SOURCE_NAME,
        schedule: null,
        recipe: {
          source: {
            type: "postgres",
            config: {
              // Deliberately unreachable: the run must fail immediately (connection
              // refused) so the seed costs no ingestion work. The recipe's
              // schema_pattern — not the connection — is what the sync matcher reads.
              host_port: "127.0.0.1:1",
              database: "example_db",
              username: "postgres",
              password: "unused-the-run-is-expected-to-fail",
              env: "DEV",
              schema_pattern: { allow: ["^catalog$"] },
            },
          },
        },
      },
    });
    expect(
      [200, 201],
      `source create failed: ${await createResp.text()}`,
    ).toContain(createResp.status());
    sourceId = ((await createResp.json()) as { id: string }).id;

    // -- Run it: the failure books INGESTION.FAIL on the source --
    // spec: API.md §Ingestion — POST …/method/run books INGESTION.COMPLETE/INGESTION.FAIL
    const runResp = await adminApi.post(
      `/api/v1/spoke/ingestion/sources/${sourceId}/method/run`,
    );
    expect(runResp.status()).toBe(200);
    const runBody = (await runResp.json()) as { status: string };
    expect(
      runBody.status,
      "the seed run must fail (unreachable host) so an INGESTION.FAIL row exists",
    ).toBe("error");

    // -- Sync sweep maps catalog datasets to the source, so its runs reach this
    //    dataset's timeline. Poll: the sweep enumerates DataHub, which lags a fresh
    //    reset-seed by ~2-3 min. --
    // spec: feature/BACKEND.md §Sync + mapping sweep step 2 (Mapping) — "rebuild
    //   `ingestion_source_dataset` by evaluating each source's **filter-matcher**" …
    //   "`derivation = matched` (authority `medium`)".
    // spec: TESTING.md §E2E §Execution discipline — "A hand-rolled polling loop declares
    //   its deadline and asserts the awaited condition after the loop, so exhausting the
    //   budget fails rather than falling through."
    const enc = encodeURIComponent(DATASET_URN);
    const internalToken = process.env["DATASPOKE_DEV_INTERNAL_TOKEN"] ?? "";
    const deadline = Date.now() + 150_000;
    let mapped = false;
    while (Date.now() < deadline) {
      await fetch(`${apiBaseUrl()}/internal/activities/ingestion/sync`, {
        method: "POST",
        headers: { "X-Internal-Token": internalToken, "Content-Type": "application/json" },
      }).catch(() => {});
      const lookup = await adminApi.get(
        `/api/v1/spoke/common/data/${enc}/attr/ingestion`,
      );
      if (lookup.ok()) {
        const body = (await lookup.json()) as { source_id: string | null };
        if (body.source_id === sourceId) {
          mapped = true;
          break;
        }
      }
      await new Promise((r) => setTimeout(r, 5_000));
    }
    expect(
      mapped,
      "the seeded source must cover this dataset (reverse-lookup) before its " +
        "INGESTION events can appear on the dataset timeline",
    ).toBe(true);

    // -- Seed VALIDATION: conf + one result --
    // spec: API.md §Validation — PUT …/attr/validation/conf, POST …/attr/validation/result
    const confResp = await adminApi.put(
      `/api/v1/spoke/common/data/${enc}/attr/validation/conf`,
      {
        data: {
          description: "ground hub events seed",
          variables: [{ name: "row_cnt", description: "row count" }],
        },
      },
    );
    expect(confResp.ok(), `validation conf PUT failed: ${await confResp.text()}`).toBeTruthy();
    confCreated = true;

    const resultResp = await adminApi.post(
      `/api/v1/spoke/common/data/${enc}/attr/validation/result`,
      { data: { data_time: new Date().toISOString(), score: 0.9, variables: { row_cnt: 30 } } },
    );
    expect(
      resultResp.ok(),
      `validation result POST failed: ${await resultResp.text()}`,
    ).toBeTruthy();

    // -- Backstop: the timeline really holds both majors before the UI gestures run --
    const timeline = await adminApi.get(
      `/api/v1/spoke/common/data/${enc}/event?limit=50`,
    );
    expect(timeline.ok()).toBeTruthy();
    const events = ((await timeline.json()) as {
      events: Array<{ event_type: string }>;
    }).events;
    expect(
      events.filter((e) => e.event_type.startsWith("INGESTION.")).length,
      "seed precondition: ≥1 INGESTION event on this dataset",
    ).toBeGreaterThan(0);
    expect(
      events.filter((e) => e.event_type.startsWith("VALIDATION.")).length,
      "seed precondition: ≥1 VALIDATION event on this dataset",
    ).toBeGreaterThan(0);
  });

  // Cleanup is asserted, not assumed: a silent failure here would leave a stray
  // ingestion source and validation conf on a shared dataset for every later spec.
  test.afterAll(async ({ adminApi }) => {
    const enc = encodeURIComponent(DATASET_URN);
    if (confCreated) {
      // Hard-delete cascades the dataset's validation results and events.
      // spec: API.md §Validation — DELETE …/attr/validation/conf cascades to delete
      //   the dataset's validation results and validation events; afterwards GET → 404.
      const delConf = await adminApi.delete(
        `/api/v1/spoke/common/data/${enc}/attr/validation/conf`,
      );
      expect([204, 404]).toContain(delConf.status());
      confCreated = false;
    }
    if (sourceId) {
      const delSource = await adminApi.delete(`/api/v1/spoke/ingestion/sources/${sourceId}`);
      expect([204, 404]).toContain(delSource.status());
      const readBack = await adminApi.get(`/api/v1/spoke/ingestion/sources/${sourceId}`);
      expect(readBack.status(), "the seeded ingestion source must be gone").toBe(404);
      sourceId = null;
    }
    // The two event types THIS spec seeded are gone. Asserting the whole timeline is
    // empty would claim something about the dataset rather than about this seed — another
    // spec may legitimately have written to it.
    const timeline = await adminApi.get(`/api/v1/spoke/common/data/${enc}/event?limit=500`);
    if (timeline.ok()) {
      const types = ((await timeline.json()) as { events: Array<{ event_type: string }> }).events.map(
        (e) => e.event_type,
      );
      expect(types, "the seeded INGESTION.FAIL must not outlive this spec").not.toContain(
        "INGESTION.FAIL",
      );
      expect(
        types,
        "the seeded VALIDATION.RESULT_RECORDED must not outlive this spec",
      ).not.toContain("VALIDATION.RESULT_RECORDED");
    }
  });

  test("Events panel filter narrows the timeline to the checked major types", async ({
    page,
  }) => {
    // The event_type cell is the row's identity in the Events table; only that table
    // renders event types on this page.
    // src/frontend/components/events-table.tsx — <TableCell>{e.event_type}</TableCell>
    const ingestionRow = page.getByRole("cell", { name: "INGESTION.FAIL", exact: true });
    const validationRow = page.getByRole("cell", {
      name: "VALIDATION.RESULT_RECORDED",
      exact: true,
    });
    const eventsHeader = page.getByRole("button", { name: /^events$/i }).first();

    // -- Baseline: with every major checked BOTH seeded rows are in the timeline --
    //
    // One navigation, then wait in place. The panel's preset window is open above —
    // it sends `from` only and omits `to` — so a row the API stamped with the
    // CLUSTER's clock can never fall outside the window, whatever the browser's
    // clock says. What is left is the poll interval: the panel refetches every 15 s
    // (usePoll), so the wait budget must span at least one tick with margin.
    // spec: FRONTEND_BASIC.md §shared-component-notes (RangePicker) — "A preset
    //   resolves to an open-ended window — the lower bound only, with `to`/`until`
    //   omitted — so the read always reaches the present, which is what lets a 15 s-
    //   polled panel … surface records written after page load."
    // spec: TESTING.md §E2E §Execution discipline — "Never sleep for a fixed duration":
    //   wait with a bounded construct — "expect(locator).toBeVisible({ timeout })".
    await page.goto(DATA_URL);
    await expect(page).not.toHaveURL(/\/login/);

    // Ensure the Events panel is expanded so its filter is in the DOM.
    await expect(eventsHeader).toBeVisible({ timeout: 15_000 });
    if ((await eventsHeader.getAttribute("aria-expanded")) === "false") {
      await eventsHeader.click();
    }

    await expect(ingestionRow).toBeVisible({ timeout: 30_000 });
    await expect(validationRow).toBeVisible({ timeout: 30_000 });

    // -- UI assertion: one checkbox per major type, all checked by default --
    // spec: FRONTEND_BASIC.md §Per-dataset page — EventMajorTypeFilter default all checked.
    for (const t of MAJOR_TYPES) {
      const box = page.getByRole("checkbox", { name: t });
      await expect(box).toBeVisible({ timeout: 10_000 });
      await expect(box).toHaveAttribute("aria-checked", "true");
    }

    // -- Gesture 1: uncheck VALIDATION → its rows leave, INGESTION rows remain --
    // spec: API.md §Data Resource — repeatable event_major_type filter narrows the feed.
    await page.getByRole("checkbox", { name: "VALIDATION" }).click();
    await expect(page.getByRole("checkbox", { name: "VALIDATION" })).toHaveAttribute(
      "aria-checked",
      "false",
    );
    await expect(page.getByRole("checkbox", { name: "INGESTION" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    await expect(page.getByRole("checkbox", { name: "METAGEN" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    await expect(validationRow).toHaveCount(0);
    await expect(ingestionRow).toBeVisible({ timeout: 15_000 });

    // -- Gesture 2: the mirror image — re-check VALIDATION, uncheck INGESTION and
    //    METAGEN, so the VALIDATION rows return and the INGESTION rows leave. --
    await page.getByRole("checkbox", { name: "VALIDATION" }).click();
    await page.getByRole("checkbox", { name: "INGESTION" }).click();
    await page.getByRole("checkbox", { name: "METAGEN" }).click();
    await expect(page.getByRole("checkbox", { name: "VALIDATION" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    await expect(page.getByRole("checkbox", { name: "INGESTION" })).toHaveAttribute(
      "aria-checked",
      "false",
    );
    await expect(validationRow).toBeVisible({ timeout: 15_000 });
    await expect(ingestionRow).toHaveCount(0);
  });
});
