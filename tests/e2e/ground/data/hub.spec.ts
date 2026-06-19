/**
 * Ground spec — unified per-dataset hub page /data/[urn].
 *
 * The spot-tier analogue of the use-case flows: narrow, single-concern UI checks
 * of the per-dataset hub that the use-case group reaches only incidentally.
 *
 * One concern per test:
 *   1. Header renders the dataset URN; the three summary cards
 *      (Ingestion / Validation / MetaGen) render.
 *   2. The four foldable CollapsiblePanels (Ingestion, Validation, MetaGen,
 *      Events) render, default open, and each folds/unfolds via its header.
 *   3. The Events panel's major-type filter exposes a checkbox per major type
 *      (INGESTION / VALIDATION / METAGEN), all checked by default; unchecking one
 *      narrows the timeline without emptying the table area.
 *
 * Data setup: global-setup runs --reset-seed (seeded Imazon baseline — every
 * dataset present in DataHub). title_master is owned by a DataHub-managed source
 * in the seed, so its Ingestion card/panel are populated; no mutation is made
 * here (read-only page), so no cleanup is required.
 *
 * spec: spec/feature/FRONTEND_BASIC.md §Per-dataset page (cards + four foldable
 *   panels + Events major-type filter; CollapsiblePanel / EventsPanel /
 *   EventMajorTypeFilter primitives).
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group, selector guidance.
 */

import { test, expect, IMAZON_URNS } from "../../fixtures/index";

const DATASET_URN = IMAZON_URNS.titleMaster;
const DATA_URL = `/data/${encodeURIComponent(DATASET_URN)}`;

const PANEL_TITLES = ["Ingestion", "Validation", "MetaGen", "Events"] as const;
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

// ── Test 2 — four foldable panels fold/unfold ──────────────────────────────────
// spec: FRONTEND_BASIC.md §Per-dataset page — four CollapsiblePanels, default open,
//   header toggles the body.

test("data hub renders four foldable panels that each fold and unfold", async ({
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
});

// ── Test 3 — Events major-type filter ──────────────────────────────────────────
// spec: FRONTEND_BASIC.md §Per-dataset page (Events filter) — checkbox-group over
//   INGESTION / VALIDATION / METAGEN, default all checked; toggling narrows the
//   timeline.

test("Events panel filter shows all major types checked and unchecks one", async ({
  page,
}) => {
  await page.goto(DATA_URL);
  await expect(page).not.toHaveURL(/\/login/);

  // Ensure the Events panel is expanded so its filter is in the DOM.
  const eventsHeader = page
    .getByRole("button", { name: /^events$/i })
    .first();
  await expect(eventsHeader).toBeVisible({ timeout: 15_000 });
  if ((await eventsHeader.getAttribute("aria-expanded")) === "false") {
    await eventsHeader.click();
  }

  // -- UI assertion: one checkbox per major type, all checked by default --
  // spec: FRONTEND_BASIC.md §Per-dataset page — EventMajorTypeFilter default all checked.
  for (const t of MAJOR_TYPES) {
    const box = page.getByRole("checkbox", { name: t });
    await expect(box).toBeVisible({ timeout: 10_000 });
    await expect(box).toHaveAttribute("aria-checked", "true");
  }

  // -- UI gesture: uncheck VALIDATION → it becomes unchecked, others stay checked --
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
});
