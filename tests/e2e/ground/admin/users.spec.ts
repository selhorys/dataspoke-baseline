/**
 * Ground spec: /admin/users page — narrow UI-flow tests.
 *
 * Concern: the admin user list renders, shows the bootstrap admin and the two
 * provisioned E2E users, and supports a complete CRUD round-trip on a throwaway
 * user: create via adminApi, change role via the inline Radix Select on the page,
 * confirm the change both in the row and via adminApi GET, hard-delete the user
 * via the ⋯ menu → ConfirmDialog, and confirm it is gone.
 *
 * One CRUD concern per test; tests are sequentially ordered but kept idempotent
 * via thorough afterAll cleanup.
 *
 * spec: spec/feature/FRONTEND_BASIC.md §Admin user list (/admin/users)
 * spec: spec/feature/FRONTEND_BASIC.md §Routing — /admin/users: GET /admin/users,
 *   PATCH /admin/users/{id}/role, DELETE /admin/users/{id}
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — dual confirmation, ConfirmDialog,
 *   Radix Select, selector guidance
 */

import { test, expect } from "../../fixtures/index";

// ── Constants ─────────────────────────────────────────────────────────────────

/** Bootstrap admin email — provisioned by reset-seed. */
const ADMIN_EMAIL = "dataspoke@dataspoke.local";

/** E2E provisioned users (from global-setup). */
const EDITOR_EMAIL = "e2e-editor@test.dataspoke.example.com";
const READER_EMAIL = "e2e-reader@test.dataspoke.example.com";

/** Throwaway user created via adminApi; deleted in afterAll. */
const THROWAWAY_EMAIL = "e2e-ground-users-throwaway@test.dataspoke.example.com";
const THROWAWAY_NAME = "Ground Users Throwaway";
const THROWAWAY_PASSWORD = "throwaway-ground-pass";

// ── Module state ──────────────────────────────────────────────────────────────

let throwawayId: string | null = null;

// ── Cleanup ───────────────────────────────────────────────────────────────────

test.afterAll(async ({ adminApi }) => {
  // Hard-delete the throwaway user idempotently (404 = already gone — OK).
  if (throwawayId) {
    await adminApi.delete(`/api/v1/admin/users/${throwawayId}`);
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 1 — /admin/users renders with bootstrap admin + E2E users visible
// spec: FRONTEND_BASIC.md §Admin user list — table renders Email, Name, Role,
//   Created columns; all provisioned users show in the list.
// ─────────────────────────────────────────────────────────────────────────────

test("/admin/users — user list renders with bootstrap admin and E2E users", async ({
  page,
  adminApi,
}) => {
  // Backend probe pre-flight: ensure the list endpoint returns all expected users.
  // spec: TESTING.md §E2E — poll adminApi until present, THEN assert UI.
  const listResp = await adminApi.get("/api/v1/admin/users?limit=100");
  expect(listResp.status()).toBe(200);
  const listBody = (await listResp.json()) as {
    users: Array<{ email: string; role: string }>;
  };
  const emails = listBody.users.map((u) => u.email);
  expect(emails).toContain(ADMIN_EMAIL);
  expect(emails).toContain(EDITOR_EMAIL);
  expect(emails).toContain(READER_EMAIL);

  // Navigate to the page.
  await page.goto("/admin/users");
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: page heading --
  // spec: admin/users/page.tsx — h1 "Admin — Users"
  await expect(
    page.getByRole("heading", { name: "Admin — Users", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: Email column header visible (table rendered) --
  // spec: admin/users/page.tsx — TableHead "Email"
  await expect(page.getByRole("columnheader", { name: "Email" })).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: bootstrap admin email present in a cell --
  // spec: admin/users/page.tsx — TableCell className="font-medium" {user.email}
  await expect(page.getByText(ADMIN_EMAIL, { exact: true })).toBeVisible({ timeout: 20_000 });

  // -- UI assertion: E2E provisioned users visible --
  // spec: FRONTEND_BASIC.md §Admin user list — all provisioned rows render.
  await expect(page.getByText(EDITOR_EMAIL, { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText(READER_EMAIL, { exact: true })).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: Search input rendered --
  // spec: admin/users/page.tsx — Input placeholder="Search..."
  await expect(page.getByPlaceholder("Search...")).toBeVisible();
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 2 — Create throwaway via adminApi, change role via inline Radix Select,
//           confirm via UI row + adminApi GET, hard-delete via ⋯ → ConfirmDialog.
// spec: FRONTEND_BASIC.md §Admin user list — inline role dropdown writes
//   PATCH /admin/users/{id}/role; ⋯ menu → Delete user → ConfirmDialog →
//   DELETE /admin/users/{id}.
// spec: TESTING.md §E2E — Radix Select: click the trigger, then getByRole("option").
// ─────────────────────────────────────────────────────────────────────────────

test("/admin/users — role change via inline select + delete via ConfirmDialog", async ({
  page,
  adminApi,
}) => {
  // Pre-flight: delete the throwaway if it exists from a previous failed run.
  const listPreResp = await adminApi.get("/api/v1/admin/users?limit=100");
  if (listPreResp.ok()) {
    const preBody = (await listPreResp.json()) as { users: Array<{ id: string; email: string }> };
    const existing = preBody.users.find((u) => u.email === THROWAWAY_EMAIL);
    if (existing) {
      await adminApi.delete(`/api/v1/admin/users/${existing.id}`);
    }
  }

  // Create the throwaway user via POST /auth/register (public) then look up via adminApi.
  // spec: FRONTEND_BASIC.md §Authentication — POST /auth/register {email, name, password}
  // The page has no "create user" button; the only create path is register (public) or
  // adminApi POST register; use the same register endpoint that global-setup uses.
  const regResp = await adminApi.post("/api/v1/auth/register", {
    data: { email: THROWAWAY_EMAIL, name: THROWAWAY_NAME, password: THROWAWAY_PASSWORD },
  });
  // 201 = created; 409 = already exists (cleaned above but race — also OK).
  expect([201, 409]).toContain(regResp.status());

  // Look up the newly created user's id.
  const listResp = await adminApi.get("/api/v1/admin/users?limit=100");
  expect(listResp.status()).toBe(200);
  const listBody = (await listResp.json()) as {
    users: Array<{ id: string; email: string; role: string }>;
  };
  const throwaway = listBody.users.find((u) => u.email === THROWAWAY_EMAIL);
  expect(throwaway, `Throwaway user ${THROWAWAY_EMAIL} not found after registration`).toBeTruthy();
  throwawayId = throwaway!.id;

  // The user registers as Reader by default; change to Editor via PATCH.
  // First change to Editor so we can then change it to Admin in the UI for
  // a visible delta — or simply change Reader → Editor in the UI.
  // At this point the user has role "Reader"; we will change to "Editor" via the UI.

  // -- Navigate to /admin/users --
  await page.goto("/admin/users");
  await expect(page).not.toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: "Admin — Users", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // Wait for the throwaway user's email to appear in the table (TanStack Query).
  // spec: TESTING.md §E2E critical pitfall — async panels → await expect toBeVisible.
  await expect(page.getByText(THROWAWAY_EMAIL, { exact: true })).toBeVisible({ timeout: 20_000 });

  // -- UI gesture: click the role Radix SelectTrigger in the throwaway user's row --
  // spec: admin/users/page.tsx — RoleSelect: Select value={user.role}; SelectTrigger h-8 w-28
  // spec: TESTING.md §E2E — Radix Select: click the trigger, then getByRole("option", {name}).
  // The inline role selects have no unique accessible name (no aria-label on SelectTrigger
  // for each row). Strategy: locate the throwaway row's cell, then find the combobox within it.
  // RISK FLAG: if multiple rows render a trigger with the same value text, a rowscoped
  // locator is needed. We use the email cell's row parent strategy.
  // Implementation: get the table row containing the throwaway email, then find the
  // role combobox (SelectTrigger renders as role="combobox") within that row.
  const throwawayRow = page.locator("tr").filter({ hasText: THROWAWAY_EMAIL });
  const roleTrigger = throwawayRow.getByRole("combobox").first();
  await expect(roleTrigger).toBeVisible({ timeout: 10_000 });
  await roleTrigger.click();

  // -- UI gesture: pick "Editor" from the Radix options --
  // spec: admin/users/page.tsx — SelectItem values: "Admin", "Editor", "Reader"
  await page.getByRole("option", { name: "Editor", exact: true }).click();

  // -- UI assertion: toast "Role updated to Editor." --
  // spec: admin/users/page.tsx — toast({ title: `Role updated to ${role}.` })
  // Toasts render twice (visual + aria-live span) → .first() on the toast text.
  await expect(page.getByText("Role updated to Editor.", { exact: true }).first()).toBeVisible({ timeout: 15_000 });

  // -- Backend probe (dual confirmation): GET /admin/users → throwaway role = Editor --
  // spec: FRONTEND_BASIC.md — PATCH /admin/users/{id}/role persists the new role.
  const probeResp = await adminApi.get("/api/v1/admin/users?limit=100");
  expect(probeResp.status()).toBe(200);
  const probeBody = (await probeResp.json()) as {
    users: Array<{ id: string; email: string; role: string }>;
  };
  const updated = probeBody.users.find((u) => u.email === THROWAWAY_EMAIL);
  expect(updated, "Throwaway user not found after role change").toBeTruthy();
  expect(updated!.role).toBe("Editor");

  // -- UI gesture: open the ⋯ dropdown and click "Delete user" --
  // spec: admin/users/page.tsx — DropdownMenuTrigger aria-label="More actions";
  //   DropdownMenuItem "Delete user" → setDialog({ kind: "delete", user })
  const moreActionsBtn = throwawayRow.getByRole("button", { name: "More actions" });
  await expect(moreActionsBtn).toBeVisible({ timeout: 10_000 });
  await moreActionsBtn.click();

  // "Delete user" menu item.
  await page.getByRole("menuitem", { name: "Delete user", exact: true }).click();

  // -- UI assertion: ConfirmDialog opens with title "Delete user" --
  // spec: admin/users/page.tsx — ConfirmDialog title="Delete user"
  await expect(
    page.getByRole("heading", { name: "Delete user", exact: true })
  ).toBeVisible({ timeout: 10_000 });

  // -- UI gesture: confirm deletion --
  // spec: admin/users/page.tsx — ConfirmDialog confirmLabel="Delete"
  // Use last() to avoid matching any "Delete user" heading still in the DOM.
  await page.getByRole("button", { name: "Delete", exact: true }).last().click();

  // -- UI assertion: toast "User ... deleted." --
  // spec: admin/users/page.tsx — toast({ title: `User ${user.email} deleted.` })
  await expect(
    page.getByText(/User .+ deleted\./i).first()
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: throwaway email no longer visible in the table --
  // The TanStack Query invalidates on success; the row should disappear.
  await expect(page.getByText(THROWAWAY_EMAIL, { exact: true })).not.toBeVisible({ timeout: 15_000 });

  // -- Backend probe (dual confirmation): GET /admin/users → throwaway gone --
  // spec: FRONTEND_BASIC.md — DELETE /admin/users/{id} removes the user permanently.
  const afterDeleteResp = await adminApi.get("/api/v1/admin/users?limit=100");
  expect(afterDeleteResp.status()).toBe(200);
  const afterBody = (await afterDeleteResp.json()) as { users: Array<{ email: string }> };
  const stillPresent = afterBody.users.some((u) => u.email === THROWAWAY_EMAIL);
  expect(stillPresent, "Throwaway user must be absent after deletion").toBe(false);

  // Mark as cleaned up so afterAll does not double-delete.
  throwawayId = null;
});
