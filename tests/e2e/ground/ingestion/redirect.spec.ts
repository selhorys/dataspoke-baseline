/**
 * Ground spec: `/ingestion` rests on `/ingestion/conf`.
 *
 * Concern: the bare feature root is a redirect shell — navigating to `/ingestion`
 * as an authenticated user comes to rest on the source-list route, which then
 * renders. Nothing else in tests/e2e/ navigates to bare `/ingestion`, so without
 * this spec a broken (or removed) redirect target would surface only as a 404 in
 * a user's browser.
 *
 * The redirect is asserted mechanism-agnostically (server 302 or client push):
 * only the resting URL and the page that renders there are contractual.
 *
 * spec: spec/feature/FRONTEND_INGESTION.md §Routes — `| /ingestion | 302 to
 *   /ingestion/conf | — |`
 * spec: spec/feature/FRONTEND_INGESTION.md §Routes — `| /ingestion/conf |
 *   Source list (filter by mode) | GET /spoke/ingestion/sources |`
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group; one concern per test.
 */

import { test, expect } from "../../fixtures/index";

test("/ingestion — authenticated navigation rests on /ingestion/conf", async ({
  page,
  adminApi,
}) => {
  // Navigate to the bare feature root.
  await page.goto("/ingestion");

  // -- UI assertion: the resting URL is exactly /ingestion/conf --
  // spec: FRONTEND_INGESTION.md §Routes — /ingestion 302s to /ingestion/conf.
  // Anchored with $ so a deeper sub-path (or a bounce back to /ingestion) fails.
  await page.waitForURL(/\/ingestion\/conf$/, { timeout: 15_000 });
  expect(page.url()).toMatch(/\/ingestion\/conf$/);
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: the source-list page actually rendered at the target --
  // A redirect that lands on a route which then errors out is not "covered".
  // conf/page.tsx renders <PageHeader title="Ingestion" /> → an <h1>.
  await expect(
    page.getByRole("heading", { name: "Ingestion", exact: true }),
  ).toBeVisible({ timeout: 15_000 });

  // -- Backend dual-confirmation: the list the target route renders is the real
  //    GET /spoke/ingestion/sources set, so the landing page is the live source
  //    list rather than a stale shell. --
  // spec: FRONTEND_INGESTION.md §Routes — /ingestion/conf ⟵ GET /spoke/ingestion/sources
  const sourcesResp = await adminApi.get("/api/v1/spoke/ingestion/sources?limit=100");
  expect(
    sourcesResp.ok(),
    `GET /spoke/ingestion/sources failed: ${await sourcesResp.text()}`,
  ).toBeTruthy();
  const sourcesBody = (await sourcesResp.json()) as {
    sources: Array<{ id: string; name: string }>;
    total_count: number;
  };
  const main = page.getByRole("main");
  if (sourcesBody.sources.length > 0) {
    // Every source the API reports must be findable on the landing page.
    await expect(
      main.getByText(sourcesBody.sources[0]!.name, { exact: false }).first(),
    ).toBeVisible({ timeout: 15_000 });
  } else {
    // No sources registered — the list still renders, showing its empty row.
    // src/frontend/components/ingestion/ingestion-source-list.tsx —
    // "No ingestion sources found." when sources.length === 0.
    await expect(main.getByText(/no ingestion sources/i)).toBeVisible({
      timeout: 15_000,
    });
  }
});
