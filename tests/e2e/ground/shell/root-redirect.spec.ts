/**
 * Ground spec: root redirect — `/` → `/governance/dashboard`.
 *
 * Concern: navigating to `/` as an authenticated user redirects immediately to
 * `/governance/dashboard` (the post-login home defined by the spec).
 *
 * spec: spec/feature/FRONTEND_BASIC.md §Routing — `/`: 302 to `/governance/dashboard`
 *   (post-login home); no API call.
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group; one concern per test.
 */

import { test, expect } from "../../fixtures/index";

// ─────────────────────────────────────────────────────────────────────────────
// Test 1 — Navigating to `/` redirects to `/governance/dashboard`
// spec: FRONTEND_BASIC.md §Routing — `/`: 302 to `/governance/dashboard`.
// spec: src/frontend/app/page.tsx — redirect("/governance/dashboard") (Next.js redirect).
// ─────────────────────────────────────────────────────────────────────────────

test("/ — authenticated navigation redirects to /governance/dashboard", async ({ page }) => {
  // Navigate to the root.
  await page.goto("/");

  // -- UI assertion: URL is /governance/dashboard --
  // spec: FRONTEND_BASIC.md §Routing — post-login home is /governance/dashboard.
  // Exclude a deeper sub-path (e.g. /governance/dashboard/...) by anchoring with $.
  await page.waitForURL(/\/governance\/dashboard$/, { timeout: 15_000 });
  expect(page.url()).toMatch(/\/governance\/dashboard$/);

  // -- UI assertion: /governance/dashboard page heading visible --
  // spec: FRONTEND_GOVERNANCE.md §Dashboard — h1 "Governance · Dashboard"
  // Confirm we actually landed on the dashboard, not just a redirect loop or login page.
  await expect(page).not.toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: "Governance · Dashboard", exact: true })
  ).toBeVisible({ timeout: 15_000 });
});
