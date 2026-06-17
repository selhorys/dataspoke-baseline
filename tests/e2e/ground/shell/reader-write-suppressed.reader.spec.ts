/**
 * Ground spec — write controls are suppressed for Reader on feature pages.
 *
 * Runs in the Playwright "reader" project (storageState = .auth/reader.json,
 * role=Reader). Do NOT add test.use({ storageState }) — the project wires it.
 *
 * Concerns covered (all on /ingestion/conf, the most ergonomic page for this check):
 *   1. The "Create source" button (write control) is NOT rendered for a Reader.
 *   2. Read-only content IS rendered: the "Ingestion" page heading is visible.
 *   3. A non-write navigation control IS visible for a Reader: the sidebar
 *      Ingestion → "unmanaged" link.
 *
 * Why /ingestion/conf:
 *   The source-list view moved to /ingestion/conf (/ingestion redirects there). The
 *   page uses {canWrite && <Button>Create source</Button>}. canWrite = role ===
 *   "Admin" || role === "Editor"; for Reader canWrite=false so the button is not
 *   rendered. The page heading "Ingestion" is unconditional and confirms read-only
 *   content renders. The "unmanaged" navigation moved to the sidebar Ingestion
 *   NavGroup (always rendered, no canWrite guard).
 *
 * API-level gate note:
 *   The spec (FRONTEND_BASIC.md §Routing) states: "The API enforces the same gate via
 *   403 READ_ONLY_ROLE on write methods; the UI suppression is for UX hygiene, not
 *   security." The API gate itself is covered by spot integration tests. This E2E spec
 *   focuses on the UI suppression: a Reader should not even see the write button.
 *   Dual-confirming via adminApi is not applicable here because the UI suppression is
 *   purely a frontend concern (no backend endpoint to probe for "button absent").
 *
 * Selector notes:
 *   - "Create source" uses canWrite && <Button size="sm" asChild><Link href=...>
 *     <Plus .../> Create source</Link></Button>. The accessible name of the link
 *     includes "Create source" — assert the link/button is absent.
 *   - The sidebar "unmanaged" link is a NavGroup child (always rendered, no canWrite
 *     guard) with href="/ingestion/unmanaged".
 *   - Wait for the page heading "Ingestion" to confirm the page is loaded and shell
 *     is mounted before asserting absence of write controls.
 *
 * spec: spec/feature/FRONTEND_BASIC.md §Routing — "Inside each function page, write
 *       actions ... are rendered only when role ∈ {Editor, Admin} — Reader users see
 *       read-only views."
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group, real-session role
 *       gates; selector guidance (toHaveCount(0) for absent controls).
 */

import { test, expect } from "../../fixtures/index";

// ── Test 1 — "Create source" button is absent for Reader ──────────────────────
// spec: FRONTEND_BASIC.md §Routing — write actions rendered only when role ∈ {Editor, Admin}.
// Source: src/frontend/app/(app)/ingestion/conf/page.tsx — {canWrite && <Button>…Create source…</Button>}
//   canWrite = useMe().canWrite = role === "Admin" || role === "Editor"
//   For Reader: canWrite=false → the button/link is not mounted.

test("reader: /ingestion/conf page does NOT show the 'Create source' button", async ({ page }) => {
  await page.goto("/ingestion/conf");

  // Wait for the page to load: the "Ingestion" heading is always rendered regardless
  // of role (it is outside the canWrite guard). Its visibility proves the page content
  // loaded and the canWrite condition has been evaluated.
  await expect(page.getByRole("heading", { name: "Ingestion", exact: true })).toBeVisible({
    timeout: 20_000,
  });

  // -- UI assertion: "Create source" write control is absent --
  // The link/button text is "Create source". toHaveCount(0) asserts the element is not
  // in the DOM at all (not just hidden).
  await expect(page.getByRole("link", { name: "Create source", exact: true })).toHaveCount(0);
});

// ── Test 2 — Read-only content IS rendered: page heading visible ───────────────
// spec: FRONTEND_BASIC.md §Routing — Reader sees read-only views (content is still rendered).

test("reader: /ingestion/conf page heading 'Ingestion' is visible (read-only content renders)", async ({
  page,
}) => {
  await page.goto("/ingestion/conf");

  // The page heading is unconditional — proves the page loaded for Reader.
  await expect(page.getByRole("heading", { name: "Ingestion", exact: true })).toBeVisible({
    timeout: 20_000,
  });

  // Must NOT redirect to /login (Reader is authenticated).
  await expect(page).not.toHaveURL(/\/login/);
});

// ── Test 3 — sidebar "unmanaged" navigation link IS visible for Reader ─────────
// spec: FRONTEND_BASIC.md §Routing — Reader sees the read-only view; the sidebar
//   Ingestion → "unmanaged" link is a navigation control (not a write control)
//   rendered unconditionally.
// Source: app-shell.tsx mainNav Ingestion NavGroup child
//   { label: "unmanaged", href: "/ingestion/unmanaged" } — outside any canWrite guard.

test("reader: sidebar shows the Ingestion 'unmanaged' navigation link", async ({ page }) => {
  await page.goto("/ingestion/conf");

  await expect(page.getByRole("heading", { name: "Ingestion", exact: true })).toBeVisible({
    timeout: 20_000,
  });

  // -- UI assertion: sidebar "unmanaged" link (non-write nav control) is present --
  const unmanagedLink = page.getByRole("link", { name: "unmanaged", exact: true });
  await expect(unmanagedLink).toBeVisible({ timeout: 10_000 });
  await expect(unmanagedLink).toHaveAttribute("href", "/ingestion/unmanaged");
});
