/**
 * Ground spec — app-shell Admin section is HIDDEN for Editor role.
 *
 * Runs in the Playwright "editor" project (storageState = .auth/editor.json,
 * provisioned by global-setup as e2e-editor@test.dataspoke.example.com with
 * role=Editor). Do NOT add test.use({ storageState }) — the project wires it.
 *
 * Concerns covered:
 *   1. The sidebar Admin section label ("Admin") is NOT rendered for an Editor.
 *   2. The "Users" sidebar link (href=/admin/users) is NOT rendered for an Editor.
 *   3. The "Configurations" sidebar link (href=/admin/conf) is NOT rendered for an Editor.
 *   4. The main feature nav IS visible for an Editor.
 *   5. The Account section IS visible for an Editor.
 *
 * The Editor role has write access to feature pages but is NOT an admin. The
 * Admin-section gate applies to both non-admin roles (Reader and Editor) identically.
 *
 * Source references:
 *   - src/frontend/components/app-shell.tsx: {isAdmin && <div>…adminNav…</div>}
 *   - src/frontend/lib/auth/use-me.ts: isAdmin = role === "Admin"; isEditor = role === "Editor"
 *   - src/frontend/components/app-shell.test.tsx lines 154-180 (Editor unit test counterpart).
 *
 * Selector notes:
 *   - Wait for shell to mount (Dashboard link visible) before asserting absence.
 *   - Use toHaveCount(0) for "control not rendered" assertions.
 *   - exact:true on all nav label matches to prevent substring collisions.
 *
 * spec: spec/feature/FRONTEND_BASIC.md §Shell — Admin section renders ONLY when isAdmin;
 *       Editor has isAdmin=false so Admin section is suppressed.
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
//   for Editor (isAdmin=false) the {isAdmin && …} block is not mounted.

test("editor: sidebar does NOT show the 'Admin' section label", async ({ page }) => {
  await gotoShell(page);

  // The section label "Admin" inside the sidebar must not exist in the DOM.
  await expect(page.getByText("Admin", { exact: true })).toHaveCount(0);
});

// ── Test 2 — Users link is absent ─────────────────────────────────────────────
// spec: FRONTEND_BASIC.md §Shell — adminNav[0]: {label:"Users", href:"/admin/users"}
//   must not render for Editor.

test("editor: sidebar does NOT show the 'Users' link to /admin/users", async ({ page }) => {
  await gotoShell(page);

  await expect(page.getByRole("link", { name: "Users", exact: true })).toHaveCount(0);
});

// ── Test 3 — Configurations link is absent ────────────────────────────────────
// spec: FRONTEND_BASIC.md §Shell — adminNav[1]: {label:"Configurations", href:"/admin/conf"}
//   must not render for Editor.

test("editor: sidebar does NOT show the 'Configurations' link to /admin/conf", async ({ page }) => {
  await gotoShell(page);

  await expect(page.getByRole("link", { name: "Configurations", exact: true })).toHaveCount(0);
});

// ── Test 4 — Main feature nav IS visible for Editor ───────────────────────────
// spec: FRONTEND_BASIC.md §Shell fixes mainNav entries, grouping, and order for every
//   role (Editor has full read+write access to feature pages): Governance ▾ (Dashboard,
//   Metrics), Ingestion ▾ (conf, unmanaged), Validation, OntoGen ▾ (conf, seed, result),
//   MetaGen.
// Implementation realization of the §Shell grouping (not a spec mandate): each ▾ group
//   renders as a disclosure <button>, a group auto-opens when its active route is open,
//   and a collapsed group prunes its child links from the DOM. On /governance/dashboard
//   the Governance group auto-opens (child links present) while the Ingestion and OntoGen
//   groups stay collapsed (toggle buttons present, children pruned).

test("editor: sidebar shows all main feature nav links", async ({ page }) => {
  await gotoShell(page);

  // Group toggles render as buttons (impl realization); Governance is auto-open here,
  // Ingestion and OntoGen are collapsed.
  await expect(page.getByRole("button", { name: "Governance", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Ingestion", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "OntoGen", exact: true })).toBeVisible();
  // Governance child link is visible because the group auto-opens on /governance/dashboard.
  await expect(page.getByRole("link", { name: "Dashboard", exact: true })).toBeVisible();
  // Flat feature links.
  await expect(page.getByRole("link", { name: "Validation", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "MetaGen", exact: true })).toBeVisible();
});

// ── Test 5 — Account section IS visible for Editor ────────────────────────────
// spec: FRONTEND_BASIC.md §Shell — Account section (Profile, API Tokens, Settings)
//   renders for everyone including Editor.

test("editor: sidebar shows 'Account' section with Profile, API Tokens, and Settings", async ({
  page,
}) => {
  await gotoShell(page);

  await expect(page.locator("aside").getByText("Account", { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole("link", { name: "Profile", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "API Tokens", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Settings", exact: true })).toBeVisible();
});
