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
 *   - when rows exist, the Nodes table renders the uniform 7 compact columns
 *     (Title, Description, Status, Confidence, Actions, Created At, Evidence) and
 *     the Evidence cell carries a Langfuse session Link (or an em dash when the
 *     row has no run / tracing is unconfigured).
 *
 * Data-conditional parts (7 columns, Evidence Link) act on whatever rows the
 * current result set holds — under the stub default the set is empty (stub runs
 * persist zero rows), so those steps self-skip, mirroring UC3 step 4/4b.
 *
 * Runs under the admin (writer) storageState (`*.spec.ts` → admin project) so the
 * Actions column and review controls render.
 *
 * spec: spec/feature/FRONTEND_ONTOGEN.md §Page contracts — uniform 7-column compact
 *   layout; Evidence column links to the run's Langfuse session; Created-At sort
 *   control; standard Pagination.
 * spec: spec/API.md §Ontology Generation (/spoke/ontogen) — ?sort=created_at_asc|created_at_desc
 *   (default created_at_desc); offset/limit pagination envelope.
 * spec: spec/feature/FRONTEND_BASIC.md §Shared Component Notes — shared control (size selector
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
  // spec: FRONTEND_ONTOGEN.md §Page contracts — Created-At sort control beside the status filter.
  const sortControl = page.getByLabel("Sort order").first();
  await expect(sortControl).toBeVisible({ timeout: 10_000 });

  // -- Default: the initial fetch sorts created_at_desc (newest first) --
  // spec: API.md §Ontology Generation (/spoke/ontogen) — default ordering created_at_desc.
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
  // spec: FRONTEND_BASIC.md §Shared Component Notes — page-size selector (20/50/100), default 20.
  const sizeSelect = panel.getByLabel("Rows per page");
  await expect(sizeSelect).toBeVisible({ timeout: 10_000 });
  await expect(sizeSelect).toContainText("20");

  // -- Standard control: Prev/Next buttons (replacing the old inline Prev/Next) --
  // spec: FRONTEND_BASIC.md §Shared Component Notes — Prev/Next + numbered pages.
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
// When rows exist: uniform 7 compact columns + Evidence column → Langfuse Link.
// Data-conditional — self-skips under the stub default (zero persisted rows).
// ─────────────────────────────────────────────────────────────────────────────
test("/ontogen/result — node rows render 7 columns; Evidence cell links to the run's Langfuse session", async ({
  page,
  adminApi,
}) => {
  // Probe the backend for at least one node; skip the row-dependent assertions otherwise.
  const listResp = await adminApi.get(`${NODE_RESULT_API}?offset=0&limit=10`);
  expect(listResp.status()).toBe(200);
  const listBody = (await listResp.json()) as {
    nodes: Array<{ id: string; name: string; run_id: string | null }>;
  };
  if (listBody.nodes.length === 0) {
    test.skip(true, "no ontology nodes (stub run persists zero rows); nothing to render");
  }

  // Read the browser-reachable Langfuse host + project slug the page itself will
  // resolve, so the branch below is decided by backend state rather than by
  // whatever happens to be in the DOM at the moment of the check.
  // spec: FRONTEND_ONTOGEN.md §Page contracts (Evidence cell) — the URL is built as
  //   {langfuse_url}/project/{langfuse_project_id}/sessions/{run_id}; both the host
  //   and the project slug resolve from GET /spoke/common/peripheral-links.
  const linksResp = await adminApi.get("/api/v1/spoke/common/peripheral-links");
  expect(linksResp.status()).toBe(200);
  const peripheralLinks = (await linksResp.json()) as {
    langfuse_url: string;
    langfuse_project_id: string;
  };

  // Arm the settle signal before navigating: the page's own peripheral-links read
  // is what turns an Evidence cell from its link-free first paint into its final
  // state, so the em-dash branch below waits on this rather than asserting on the
  // pre-query DOM.
  // The rejection is folded into a null so the Link branch, which does not await
  // this promise, cannot trip an unhandled rejection; the em-dash branch asserts
  // on the settled value instead.
  const pageLinksSettled = page
    .waitForResponse(
      (resp) => resp.url().includes("/spoke/common/peripheral-links") && resp.status() === 200,
      { timeout: 20_000 }
    )
    .catch(() => null);

  await page.goto("/ontogen/result");
  await expect(page).not.toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: "Ontology Generation", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  const panel = page.getByRole("tabpanel");

  // -- UI assertion: the uniform 7-column header set (Evidence after Created At) --
  // spec: FRONTEND_ONTOGEN.md §Page contracts — Title, Description, Status, Confidence,
  //   Actions, Created At, Evidence (uniform across Node/Edge/Triple).
  for (const header of [
    "Title",
    "Description",
    "Status",
    "Confidence",
    "Actions",
    "Created At",
    "Evidence",
  ]) {
    await expect(panel.getByRole("columnheader", { name: header, exact: true })).toBeVisible({
      timeout: 10_000,
    });
  }
  await expect(panel.getByRole("columnheader")).toHaveCount(7);

  // -- UI assertion: the Confidence cell no longer hosts an Evidence button --
  // spec: FRONTEND_ONTOGEN.md §Page contracts — Confidence is score-only; the per-row
  //   debate transcript lives in Langfuse, reached via the Evidence column Link.
  const firstRow = panel.getByRole("row").nth(1); // row 0 is the header
  await expect(firstRow.getByRole("button", { name: /evidence/i })).toHaveCount(0);

  // -- UI assertion: the Evidence cell renders a "Link" (new tab) or an em dash --
  // spec: FRONTEND_ONTOGEN.md §Page contracts (Evidence cell) — the Link "renders only
  //   when all three values are present; otherwise the cell shows `—`". The three are
  //   the row's run_id plus the Langfuse host and project slug read above, so which
  //   branch applies is decided here from backend state — not from the DOM, which is
  //   momentarily link-free on a cold load while the peripheral-links query is in
  //   flight. evidence-link.tsx renders <a target="_blank" rel="noopener noreferrer">Link</a>.
  const targetNode = listBody.nodes[0];
  // Evidence is the seventh (last) column, so scope both branches to that cell —
  // a row-wide em-dash locator would also match an empty Description.
  const evidenceCell = firstRow.getByRole("cell").nth(6);
  const link = evidenceCell.getByRole("link", { name: "Link", exact: true });
  const evidenceExpected =
    !!targetNode.run_id &&
    !!peripheralLinks.langfuse_url &&
    !!peripheralLinks.langfuse_project_id;

  if (evidenceExpected) {
    // Every value is present, so the anchor must appear and point at the row's own
    // session. `toHaveAttribute` polls, covering the query round-trip.
    //
    // The spec states the shape `{langfuse_url}/project/{langfuse_project_id}/sessions/{run_id}`
    // and no normalisation rule for the configured host, so assert its two
    // spec-defined halves rather than re-deriving the whole string: the tail is the
    // exact path (pinning this row's own run_id — the strongest claim here) and the
    // head is the configured host. Both hold whether or not the host is stored with
    // a trailing slash.
    await expect(link.first()).toHaveAttribute(
      "href",
      new RegExp(
        `/project/${escapeRe(encodeURIComponent(peripheralLinks.langfuse_project_id))}` +
          `/sessions/${escapeRe(encodeURIComponent(targetNode.run_id!))}$`
      ),
      { timeout: 10_000 }
    );
    const href = await link.first().getAttribute("href");
    expect(href, "the Evidence href must be built on the configured Langfuse host").toContain(
      new URL(peripheralLinks.langfuse_url).origin
    );
    await expect(link.first()).toHaveAttribute("target", "_blank");
    await expect(link.first()).toHaveAttribute("rel", /noopener/);
  } else {
    // At least one of the three is absent, so the cell must settle on the em dash.
    //
    // Settle signal first: the page's GET /spoke/common/peripheral-links has
    // landed, so the absence check below runs on the resolved cell rather than on
    // the link-free first paint.
    expect(
      await pageLinksSettled,
      "the page must issue GET /spoke/common/peripheral-links before the Evidence cell can settle"
    ).not.toBeNull();
    await expect(evidenceCell.getByText("—", { exact: true })).toBeVisible({ timeout: 10_000 });
    await expect(link).toHaveCount(0);
  }
});

/** Escapes a value for literal use inside a RegExp. */
function escapeRe(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
