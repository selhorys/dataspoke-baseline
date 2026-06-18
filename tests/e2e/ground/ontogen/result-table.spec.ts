/**
 * Ground spec: /ontogen/result — the redesigned uniform result table surface.
 *
 * Narrow, single-concern real-browser flows that complement the UC3 use-case
 * spec (which focuses on the Run → review arc). This spec pins the table
 * redesign's structural + wiring invariants that UC3 does not assert:
 *   - the Created-At SortControl writes ?sort=created_at_asc|_desc onto the
 *     GET /spoke/ontogen/result/node request (server-side sort);
 *   - the shared standard Pagination control is present (Rows-per-page selector
 *     defaulting to 20, Prev/Next), replacing the old inline Prev/Next;
 *   - when rows exist, the Nodes table renders the uniform 6 compact columns
 *     (Title, Description, Status, Confidence, Actions, Created At) and the
 *     Confidence cell carries an Evidence button that opens a modal.
 *
 * Data-conditional parts (6 columns, Evidence modal) act on whatever rows the
 * current result set holds — under the stub default the set is empty (stub runs
 * persist zero rows), so those steps self-skip, mirroring UC3 step 4/4b.
 *
 * Runs under the admin (writer) storageState (`*.spec.ts` → admin project) so the
 * Actions column and review controls render.
 *
 * spec: spec/feature/FRONTEND_ONTOGEN.md §Result table — uniform 6-column compact
 *   layout; evidence-as-modal; Created-At sort control; standard Pagination.
 * spec: spec/API.md §UC3 result rows — ?sort=created_at_asc|created_at_desc
 *   (default created_at_desc); offset/limit pagination envelope.
 * spec: spec/feature/FRONTEND_BASIC.md §Pagination — shared control (size selector
 *   20/50/100 default 20, Prev/Next, numbered pages).
 * spec: spec/TESTING.md §E2E — ground group, real-session role, single concern.
 */

import { test, expect } from "../../fixtures/index";

const NODE_RESULT_API = "/api/v1/spoke/ontogen/result/node";

// ─────────────────────────────────────────────────────────────────────────────
// Sort control writes ?sort= onto the node result request (server-side sort).
// ─────────────────────────────────────────────────────────────────────────────
test("/ontogen/result — Created-At sort control drives ?sort= on the node fetch", async ({
  page,
}) => {
  // Capture the sort param of every GET to the node result endpoint.
  const sortParams: Array<string | null> = [];
  page.on("request", (req) => {
    if (req.method() === "GET" && req.url().includes(NODE_RESULT_API)) {
      sortParams.push(new URL(req.url()).searchParams.get("sort"));
    }
  });

  await page.goto("/ontogen/result");
  await expect(page).not.toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: "Ontology Generation", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // Nodes tab is the default; its panel carries the SortControl (aria-label "Sort order").
  // spec: FRONTEND_ONTOGEN.md §Result table — Created-At sort control beside the status filter.
  const sortControl = page.getByLabel("Sort order").first();
  await expect(sortControl).toBeVisible({ timeout: 10_000 });

  // -- Default: the initial fetch sorts created_at_desc (newest first) --
  // spec: API.md §UC3 result rows — default ordering created_at_desc.
  await expect
    .poll(() => sortParams.length, { timeout: 10_000 })
    .toBeGreaterThan(0);
  expect(sortParams.some((s) => s === "created_at_desc")).toBe(true);

  // -- UI gesture: switch the sort to "Created (oldest)" → created_at_asc --
  // sort-control.tsx — Select options "Created (newest)" / "Created (oldest)".
  await sortControl.click();
  await page.getByRole("option", { name: "Created (oldest)", exact: true }).click();

  // -- Core assertion: a node fetch now carries ?sort=created_at_asc --
  // spec: API.md §UC3 — ?sort=created_at_asc reverses the default order (server-side).
  await expect
    .poll(() => sortParams.includes("created_at_asc"), {
      timeout: 10_000,
      message: "switching sort to oldest must issue a node fetch with ?sort=created_at_asc",
    })
    .toBe(true);
});

// ─────────────────────────────────────────────────────────────────────────────
// The shared standard Pagination control is present (size selector + Prev/Next).
// ─────────────────────────────────────────────────────────────────────────────
test("/ontogen/result — Nodes panel renders the shared Pagination control", async ({ page }) => {
  await page.goto("/ontogen/result");
  await expect(page).not.toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: "Ontology Generation", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // The active Nodes tabpanel hosts the shared <Pagination>.
  const panel = page.getByRole("tabpanel");

  // -- Standard control: "Rows per page" size selector, defaulting to 20 --
  // spec: FRONTEND_BASIC.md §Pagination — page-size selector (20/50/100), default 20.
  const sizeSelect = panel.getByLabel("Rows per page");
  await expect(sizeSelect).toBeVisible({ timeout: 10_000 });
  await expect(sizeSelect).toContainText("20");

  // -- Standard control: Prev/Next buttons (replacing the old inline Prev/Next) --
  // spec: FRONTEND_BASIC.md §Pagination — Prev/Next + numbered pages.
  await expect(panel.getByRole("button", { name: /previous/i })).toBeVisible();
  await expect(panel.getByRole("button", { name: /next/i })).toBeVisible();

  // On the first page (or an empty set) Previous is disabled — the standard control's
  // end-of-range behavior. This holds whether or not rows are present.
  await expect(panel.getByRole("button", { name: /previous/i })).toBeDisabled();

  // -- Size selector offers 20 / 50 / 100 --
  await sizeSelect.click();
  for (const size of ["20", "50", "100"]) {
    await expect(page.getByRole("option", { name: size, exact: true })).toBeVisible();
  }
  // Close the menu without changing the size.
  await page.keyboard.press("Escape");
});

// ─────────────────────────────────────────────────────────────────────────────
// When rows exist: uniform 6 compact columns + Evidence button → modal.
// Data-conditional — self-skips under the stub default (zero persisted rows).
// ─────────────────────────────────────────────────────────────────────────────
test("/ontogen/result — node rows render 6 columns; Evidence opens a modal", async ({
  page,
  adminApi,
}) => {
  // Probe the backend for at least one node; skip the row-dependent assertions otherwise.
  const listResp = await adminApi.get(`${NODE_RESULT_API}?offset=0&limit=10`);
  expect(listResp.status()).toBe(200);
  const listBody = (await listResp.json()) as {
    nodes: Array<{ id: string; name: string }>;
  };
  if (listBody.nodes.length === 0) {
    test.skip(true, "no ontology nodes (stub run persists zero rows); nothing to render");
  }

  await page.goto("/ontogen/result");
  await expect(page).not.toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: "Ontology Generation", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  const panel = page.getByRole("tabpanel");

  // -- UI assertion: the uniform 6-column header set --
  // spec: FRONTEND_ONTOGEN.md §Result table — Title, Description, Status, Confidence,
  //   Actions, Created At (uniform across Node/Edge/Triple).
  for (const header of ["Title", "Description", "Status", "Confidence", "Actions", "Created At"]) {
    await expect(panel.getByRole("columnheader", { name: header, exact: true })).toBeVisible({
      timeout: 10_000,
    });
  }
  await expect(panel.getByRole("columnheader")).toHaveCount(6);

  // -- UI assertion: the first node row carries an Evidence button in the Confidence cell --
  // spec: FRONTEND_ONTOGEN.md §Result table — Confidence cell hosts an Evidence button
  //   (opens a modal rendering the evidence JSON); evidence-dialog.tsx.
  const firstRow = panel.getByRole("row").nth(1); // row 0 is the header
  const evidenceButton = firstRow.getByRole("button", { name: /evidence/i });
  await expect(evidenceButton).toBeVisible({ timeout: 10_000 });

  // -- UI gesture: click Evidence → the modal opens --
  await evidenceButton.click();

  // -- UI assertion: an "Evidence" dialog opens (modal, not inline disclosure) --
  // evidence-dialog.tsx — DialogTitle "Evidence"; lazily fetches the item attr on open.
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("heading", { name: "Evidence", exact: true })).toBeVisible({
    timeout: 10_000,
  });
});
