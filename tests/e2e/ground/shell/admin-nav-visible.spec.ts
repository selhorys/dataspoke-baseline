/**
 * Ground spec — app-shell Admin section visibility for Admin role.
 *
 * One concern per test; each proves a single observable shell behaviour
 * against the real stack using the admin storageState (Playwright "admin" project).
 *
 * Concerns covered:
 *   1. The sidebar Admin section label is visible for an Admin user.
 *   2. The "Users" link (href=/admin/users) is visible in the Admin section.
 *   3. The "Configurations" link (href=/admin/conf) is visible in the Admin section.
 *   4. The Admin section appears ABOVE the Account section in DOM order.
 *   5. The Account section (Profile, API Tokens, Settings) is also visible.
 *   6. The main feature nav (Dashboard, Ingestion, etc.) is visible.
 *
 * Source references:
 *   - src/frontend/components/app-shell.tsx: adminNav + accountNav + mainNav arrays;
 *     {isAdmin && <div className="mb-3">…adminNav…</div>} before accountNav.
 *   - src/frontend/lib/auth/use-me.ts: isAdmin = role === "Admin"
 *   - src/frontend/components/app-shell.test.tsx: Vitest unit counterpart (mocked useMe).
 *
 * Selector notes:
 *   - The sidebar section labels ("Admin", "Account") are <p> tags with uppercase
 *     CSS text-transform, but the text content in HTML is "Admin" / "Account".
 *   - "Users" could match other text; guard with href check for /admin/users.
 *   - "Configurations" is unique in the nav — exact match is safe.
 *   - Wait for "Dashboard" link to confirm shell is mounted before absence checks.
 *
 * spec: spec/feature/FRONTEND_BASIC.md §Shell — Admin section (Users + Configurations)
 *       renders ONLY when role === "Admin"; placed above Account section; Account always
 *       renders; mainNav always renders.
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group, real-session role gates.
 */

import { test, expect } from "../../fixtures/index";
import type { Page } from "@playwright/test";

// ── Wait helper ────────────────────────────────────────────────────────────────

/**
 * Navigate to the app shell and wait for it to fully mount.
 * Uses the "Dashboard" link as the discriminating anchor: it is always present
 * for any authenticated user in the mainNav, so its visibility proves the
 * shell is rendered and the role-dependent sections are stable.
 */
async function gotoShell(page: Page) {
  await page.goto("/governance/dashboard");
  // Confirm shell mounted — also proves auth is valid (no /login redirect).
  await expect(page.getByRole("link", { name: "Dashboard", exact: true })).toBeVisible({
    timeout: 20_000,
  });
}

// ── Test 1 — Admin section label is visible ────────────────────────────────────
// spec: FRONTEND_BASIC.md §Shell — "Admin" section label renders only when isAdmin.
// Real-session proof: the storageState was created by global-setup logging in as
// dataspoke@dataspoke.local (bootstrap Admin), so isAdmin is derived from the real
// GET /auth/me response — not a mock.

test("admin: sidebar shows 'Admin' section label", async ({ page }) => {
  await gotoShell(page);

  // The sidebar <p> element with text "Admin" (case-insensitive, exact word).
  // spec: app-shell.tsx line 187 — uppercase tracking-wider label "Admin"
  const adminLabel = page.getByText("Admin", { exact: true });
  await expect(adminLabel).toBeVisible({ timeout: 10_000 });
});

// ── Test 2 — Users link is visible in the Admin section ───────────────────────
// spec: FRONTEND_BASIC.md §Shell — adminNav[0]: {label:"Users", href:"/admin/users"}

test("admin: sidebar shows 'Users' link pointing to /admin/users", async ({ page }) => {
  await gotoShell(page);

  // The sidebar link for adminNav "Users". getByRole name is case-insensitive
  // substring — use exact:true to avoid false matches on other text.
  // Guard with href to distinguish from any other "Users" occurrence.
  const usersLink = page.getByRole("link", { name: "Users", exact: true });
  await expect(usersLink).toBeVisible({ timeout: 10_000 });
  await expect(usersLink).toHaveAttribute("href", "/admin/users");
});

// ── Test 3 — Configurations link is visible in the Admin section ───────────────
// spec: FRONTEND_BASIC.md §Shell — adminNav[1]: {label:"Configurations", href:"/admin/conf"}

test("admin: sidebar shows 'Configurations' link pointing to /admin/conf", async ({ page }) => {
  await gotoShell(page);

  const configurationsLink = page.getByRole("link", { name: "Configurations", exact: true });
  await expect(configurationsLink).toBeVisible({ timeout: 10_000 });
  await expect(configurationsLink).toHaveAttribute("href", "/admin/conf");
});

// ── Test 4 — Admin section is ABOVE Account section in DOM order ───────────────
// spec: FRONTEND_BASIC.md §Shell — Admin section placed above Account section;
//   diagram shows ADMIN pinned above ACCOUNT in the sidebar bottom cluster.

test("admin: 'Admin' section appears above 'Account' section in the sidebar", async ({ page }) => {
  await gotoShell(page);

  const adminLabel = page.getByText("Admin", { exact: true });
  const accountLabel = page.locator("aside").getByText("Account", { exact: true });

  await expect(adminLabel).toBeVisible({ timeout: 10_000 });
  await expect(accountLabel).toBeVisible({ timeout: 10_000 });

  // compareDocumentPosition DOCUMENT_POSITION_FOLLOWING (0x04): if accountLabel
  // comes AFTER adminLabel in the DOM, then (adminLabel.compareDocumentPosition(accountLabel) & 0x04)
  // is non-zero — meaning Admin section is rendered above Account section.
  const adminEl = await adminLabel.elementHandle();
  const accountEl = await accountLabel.elementHandle();
  const position = await page.evaluate(
    ([admin, account]) => admin!.compareDocumentPosition(account!),
    [adminEl, accountEl],
  );
  // 0x04 = Node.DOCUMENT_POSITION_FOLLOWING
  expect(position & 0x04).toBeTruthy();
});

// ── Test 5 — Account section is always visible for Admin ──────────────────────
// spec: FRONTEND_BASIC.md §Shell — Account section (Profile, API Tokens, Settings)
//   renders for everyone including Admin.

test("admin: sidebar shows 'Account' section with Profile, API Tokens, and Settings links", async ({
  page,
}) => {
  await gotoShell(page);

  // Account section label
  await expect(page.locator("aside").getByText("Account", { exact: true })).toBeVisible({ timeout: 10_000 });

  // accountNav items: Profile → /profile, API Tokens → /profile/tokens, Settings → /settings
  await expect(page.getByRole("link", { name: "Profile", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "API Tokens", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Settings", exact: true })).toBeVisible();
});

// ── Test 6 — Main feature nav is visible ─────────────────────────────────────
// spec: FRONTEND_BASIC.md §Shell fixes mainNav entries, grouping, and order for every
//   role: Governance ▾ (Dashboard, Metrics), Ingestion ▾ (conf, unmanaged), Validation,
//   OntoGen ▾ (conf, seed, result), MetaGen ▾ (conf, result, uncovered).
// Implementation realization of the §Shell grouping (not a spec mandate): each ▾ group
//   renders as a disclosure <button>, a group auto-opens when its active route is open,
//   and a collapsed group prunes its child links from the DOM. On /governance/dashboard
//   the Governance group auto-opens (Dashboard + Metrics child links present) while the
//   Ingestion and OntoGen groups stay collapsed (toggle buttons present, children pruned).

test("admin: sidebar shows all main feature nav links", async ({ page }) => {
  await gotoShell(page);

  // Group toggles render as buttons (impl realization); Governance is auto-open here,
  // Ingestion and OntoGen are collapsed.
  await expect(page.getByRole("button", { name: "Governance", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Ingestion", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "OntoGen", exact: true })).toBeVisible();
  // The Ingestion and OntoGen groups are collapsed on this route, so their children
  // (Ingestion: conf/unmanaged; OntoGen: conf/seed/result) are not in the DOM — proving
  // the disclosure model the other assertions rely on. (`conf` is a child of both groups,
  // both collapsed here, so the count-0 assertion holds for either.)
  await expect(page.getByRole("link", { name: "conf", exact: true })).toHaveCount(0);
  // Governance children are present because the group auto-opens on /governance/dashboard.
  await expect(page.getByRole("link", { name: "Dashboard", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "Metrics", exact: true })).toBeVisible();
  // Flat feature link.
  await expect(page.getByRole("link", { name: "Validation", exact: true })).toBeVisible();
  // MetaGen is a disclosure group (conf/result/uncovered), collapsed here, so it renders
  // as a toggle button with its children pruned — same model as Ingestion/OntoGen.
  await expect(page.getByRole("button", { name: "MetaGen", exact: true })).toBeVisible();
});
