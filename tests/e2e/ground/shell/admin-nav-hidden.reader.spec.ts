/**
 * Ground spec — app-shell Admin section is HIDDEN for Reader role.
 *
 * Runs in the Playwright "reader" project (storageState = .auth/reader.json,
 * provisioned by global-setup as e2e-reader@test.dataspoke.example.com with
 * role=Reader). Do NOT add test.use({ storageState }) — the project wires it.
 *
 * Concerns covered:
 *   1. The sidebar Admin section label ("Admin") is NOT rendered for a Reader.
 *   2. The "Users" sidebar link (href=/admin/users) is NOT rendered for a Reader.
 *   3. The "Configurations" sidebar link (href=/admin/conf) is NOT rendered for a Reader.
 *   4. The main feature nav IS visible for a Reader (gate is Admin-section only).
 *   5. The Account section IS visible for a Reader.
 *   6. Navigating directly to /admin/users shows the API-enforced gate: the page
 *      renders "You do not have permission to access this page." (NOT a redirect to
 *      /login). This is the client-side gate in AdminUsersPage — when !isAdmin the
 *      component renders the permission-denied message in place of the user table.
 *      The API also enforces the gate (403), but the UI renders the error message
 *      rather than letting the API error propagate to a toast in this flow.
 *
 * Source references:
 *   - src/frontend/components/app-shell.tsx: {isAdmin && <div>…adminNav…</div>}
 *   - src/frontend/lib/auth/use-me.ts: isAdmin = role === "Admin"
 *   - src/frontend/app/(app)/admin/users/page.tsx: if (!isAdmin) return <permission-denied-msg>
 *   - src/frontend/components/app-shell.test.tsx lines 182-200 (Reader unit test counterpart).
 *
 * Selector notes:
 *   - Wait for shell to mount (Dashboard link visible) before asserting absence.
 *   - Use toHaveCount(0) for "control not rendered" — preferred over not.toBeVisible()
 *     because toHaveCount(0) does not depend on CSS visibility; it checks DOM presence.
 *   - "Admin" as an exact text match avoids collisions with "Admin — Users" heading
 *     that appears on the /admin/users page itself.
 *   - For direct /admin/users navigation, the page renders the shell + the content
 *     area — the shell sidebar still has no Admin section links (Reader role), but the
 *     content area shows the permission-denied message.
 *
 * spec: spec/feature/FRONTEND_BASIC.md §Shell — Admin section renders ONLY when isAdmin.
 * spec: spec/feature/FRONTEND_BASIC.md §Routing — the UI hides the admin-menu entry
 *       when role !== "Admin"; Account section renders for everyone.
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group, real-session role gates.
 */

import { test, expect } from "../../fixtures/index";
import type { Page } from "@playwright/test";

// ── Wait helper ────────────────────────────────────────────────────────────────

async function gotoShell(page: Page) {
  await page.goto("/governance/dashboard");
  // Shell mounted — Dashboard is always in mainNav for every role.
  await expect(page.getByRole("link", { name: "Dashboard", exact: true })).toBeVisible({
    timeout: 20_000,
  });
}

// ── Test 1 — Admin section label is absent ────────────────────────────────────
// spec: FRONTEND_BASIC.md §Shell — "Admin" section label renders ONLY when isAdmin;
//   for Reader (isAdmin=false) the {isAdmin && …} block is not mounted.

test("reader: sidebar does NOT show the 'Admin' section label", async ({ page }) => {
  await gotoShell(page);

  // The section label "Admin" inside the <aside> sidebar must not exist in the DOM.
  // Use exact:true to avoid matching "Admin — Users" page heading or other substrings.
  await expect(page.getByText("Admin", { exact: true })).toHaveCount(0);
});

// ── Test 2 — Users link is absent ─────────────────────────────────────────────
// spec: FRONTEND_BASIC.md §Shell — adminNav[0]: {label:"Users", href:"/admin/users"}
//   must not render for Reader.

test("reader: sidebar does NOT show the 'Users' link to /admin/users", async ({ page }) => {
  await gotoShell(page);

  // The link "Users" pointing to /admin/users must not be in the DOM.
  // getByRole("link", {name:"Users", exact:true}) could match other elements;
  // filter by href to be precise.
  const usersLinks = page.getByRole("link", { name: "Users", exact: true });
  // Expect zero links named "Users" with href=/admin/users
  // We check count=0 for any link named Users (in the nav context, exact match means
  // we won't catch admin/users page heading links — those are not <a> nav links).
  await expect(usersLinks).toHaveCount(0);
});

// ── Test 3 — Configurations link is absent ────────────────────────────────────
// spec: FRONTEND_BASIC.md §Shell — adminNav[1]: {label:"Configurations", href:"/admin/conf"}
//   must not render for Reader.

test("reader: sidebar does NOT show the 'Configurations' link to /admin/conf", async ({ page }) => {
  await gotoShell(page);

  await expect(page.getByRole("link", { name: "Configurations", exact: true })).toHaveCount(0);
});

// ── Test 4 — Main feature nav IS visible for Reader ───────────────────────────
// spec: FRONTEND_BASIC.md §Shell — mainNav renders for every role (the Admin-only
//   gate applies only to the Admin section, not the feature nav).

test("reader: sidebar shows all main feature nav links", async ({ page }) => {
  await gotoShell(page);

  await expect(page.getByRole("link", { name: "Dashboard", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Ingestion", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Validation", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "OntoGen", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "MetaGen", exact: true })).toBeVisible();
});

// ── Test 5 — Account section IS visible for Reader ────────────────────────────
// spec: FRONTEND_BASIC.md §Shell — Account section (Profile, API Tokens, Settings)
//   renders for everyone including Reader.

test("reader: sidebar shows 'Account' section with Profile, API Tokens, and Settings", async ({
  page,
}) => {
  await gotoShell(page);

  await expect(page.locator("aside").getByText("Account", { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole("link", { name: "Profile", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "API Tokens", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Settings", exact: true })).toBeVisible();
});

// ── Test 6 — Direct navigation to /admin/users shows permission-denied message ─
// spec: FRONTEND_BASIC.md §Routing — "/admin/* is server-side gated by the API's
//   role check (role = 'Admin')"; the UI hides the admin-menu entry; the page itself
//   also renders a permission-denied view when !isAdmin (AdminUsersPage, line 246-255).
//
// Actual behaviour from AdminUsersPage source (page.tsx lines 242-255):
//   if (!isAdmin) return (
//     <div>
//       <h1>Admin — Users</h1>
//       <p>You do not have permission to access this page.</p>
//     </div>
//   )
//
// The reader IS authenticated (valid refresh cookie), so the AuthGuard does NOT
// redirect to /login. Instead the component renders the permission message inline.
// The Admin sidebar section still does not appear (role is unchanged).
//
// Risk flag: if the route guard changes to a /login redirect (e.g. a Next.js
// middleware added for /admin/*), this test would fail expecting the message text.
// Monitoring: check that page does not redirect to /login.

test("reader: navigating to /admin/users shows permission-denied message, not a login redirect", async ({
  page,
}) => {
  await page.goto("/admin/users");

  // Must NOT redirect to /login — the reader is authenticated.
  await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });

  // The shell must still render (Dashboard link visible = shell mounted).
  await expect(page.getByRole("link", { name: "Dashboard", exact: true })).toBeVisible({
    timeout: 20_000,
  });

  // The content area renders the permission-denied message.
  // spec: AdminUsersPage — <p>You do not have permission to access this page.</p>
  await expect(
    page.getByText("You do not have permission to access this page.")
  ).toBeVisible({ timeout: 10_000 });

  // The Admin section label still must not appear in the sidebar.
  await expect(page.getByText("Admin", { exact: true })).toHaveCount(0);
});
