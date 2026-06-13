/**
 * Ground spec: /profile page — narrow UI-flow tests.
 *
 * Concern: the profile page renders the admin's own profile (email shown as a
 * locked read-only field), allows changing the display name, confirms the change
 * both in the UI and via GET /auth/me, then reverts the name afterward.
 *
 * spec: spec/feature/FRONTEND_BASIC.md §Authentication — /profile: GET /auth/me,
 *   PATCH /auth/me {name?, password?}; email is locked (read-only disabled input).
 * spec: spec/feature/FRONTEND_BASIC.md §Routing — /profile: own profile + change
 *   display name + change password.
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — dual confirmation, selector guidance
 */

import { test, expect } from "../../fixtures/index";

// ── Constants ─────────────────────────────────────────────────────────────────

const ADMIN_EMAIL = "dataspoke@dataspoke.local";

// ── Module state ──────────────────────────────────────────────────────────────

/** Original admin display name; used in afterAll revert. */
let originalName: string | null = null;

// ── Cleanup ───────────────────────────────────────────────────────────────────

test.afterAll(async ({ adminApi }) => {
  if (originalName !== null) {
    await adminApi.patch("/api/v1/auth/me", { data: { name: originalName } });
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 1 — /profile renders own profile with email locked
// spec: FRONTEND_BASIC.md §Authentication — Profile page: Email locked (disabled
//   input), Role disabled, Name editable, "Change password" section visible.
// ─────────────────────────────────────────────────────────────────────────────

test("/profile — renders own email (locked) and Save changes button", async ({
  page,
  adminApi,
}) => {
  // Backend probe: GET /auth/me to know the current profile.
  const meResp = await adminApi.get("/api/v1/auth/me");
  expect(meResp.status()).toBe(200);
  const me = (await meResp.json()) as { id: string; email: string; name: string; role: string };
  expect(me.email).toBe(ADMIN_EMAIL);

  // Navigate to the page.
  await page.goto("/profile");
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: page heading --
  // spec: profile/page.tsx — h1 "Profile"
  await expect(
    page.getByRole("heading", { name: "Profile", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: email field shows admin email and is disabled (locked) --
  // spec: profile/page.tsx — <Field label="Email"><Input value={me.email} disabled />.
  // The "Email" Field label has no htmlFor association (display-only disabled field),
  // so locate the input by its display value instead of getByLabel.
  // spec: FRONTEND_BASIC.md §Authentication — email is locked (disabled)
  await expect(page.getByText("Email", { exact: true })).toBeVisible({ timeout: 10_000 });
  // Email is the first input on the profile form (before Role / Google / Name).
  const emailInput = page.getByRole("textbox").first();
  await expect(emailInput).toBeVisible({ timeout: 10_000 });
  await expect(emailInput).toHaveValue(ADMIN_EMAIL);
  await expect(emailInput).toBeDisabled();

  // -- UI assertion: Role field is disabled (2nd textbox; Field label "Role" has no htmlFor) --
  // spec: profile/page.tsx — Input value={me.role} disabled
  const roleInput = page.getByRole("textbox").nth(1);
  await expect(roleInput).toBeVisible();
  await expect(roleInput).toBeDisabled();

  // -- UI assertion: Name field is editable --
  // spec: profile/page.tsx — Input id="name" type="text" (via Field label "Name")
  const nameInput = page.locator("#name");
  await expect(nameInput).toBeVisible({ timeout: 10_000 });
  await expect(nameInput).not.toBeDisabled();

  // -- UI assertion: "Change password" section header visible --
  // spec: profile/page.tsx — p.text-sm.font-medium "Change password"
  await expect(page.getByText("Change password", { exact: true })).toBeVisible();

  // -- UI assertion: Save changes button --
  // spec: profile/page.tsx — Button type="submit" "Save changes"
  await expect(page.getByRole("button", { name: "Save changes" })).toBeVisible();

  // Backend probe: admin email confirmed.
  expect(me.email).toBe(ADMIN_EMAIL);
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 2 — Change display name → Save → confirm via UI toast + adminApi GET
//           → revert afterward
// spec: FRONTEND_BASIC.md §Authentication — PATCH /auth/me {name} → profile updated.
// spec: profile/page.tsx — toast({ title: "Profile updated." }) on success.
// ─────────────────────────────────────────────────────────────────────────────

test("/profile — change name → Save → confirmed via GET /auth/me → reverted", async ({
  page,
  adminApi,
}) => {
  // Pre-flight: read current name.
  const preResp = await adminApi.get("/api/v1/auth/me");
  expect(preResp.status()).toBe(200);
  const pre = (await preResp.json()) as { name: string; email: string };
  originalName = pre.name;

  const NEW_NAME = `${pre.name} (ground-test)`;

  // Navigate to the page.
  await page.goto("/profile");
  await expect(page).not.toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: "Profile", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // Wait for the name field to be populated via useEffect (useMe hook settles).
  const nameInput = page.locator("#name");
  await expect(nameInput).toBeVisible({ timeout: 10_000 });
  await expect(nameInput).toHaveValue(pre.name, { timeout: 10_000 });

  // -- UI gesture: clear and fill the new name --
  // spec: profile/page.tsx — Input id="name" {register("name")}
  await nameInput.fill(NEW_NAME);

  // -- UI gesture: click "Save changes" --
  // spec: profile/page.tsx — Button type="submit" "Save changes"
  await page.getByRole("button", { name: "Save changes" }).click();

  // -- UI assertion: toast "Profile updated." --
  // spec: profile/page.tsx — toast({ title: "Profile updated." })
  // Toasts render twice (visual + aria-live span) → .first() on the toast text.
  await expect(
    page.getByText("Profile updated.", { exact: true }).first()
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: name field retains the new value (form reset to new name) --
  // spec: profile/page.tsx — reset({ name: values.name, password: "" }) on success
  await expect(nameInput).toHaveValue(NEW_NAME, { timeout: 5_000 });

  // -- Backend probe (dual confirmation): GET /auth/me → name persisted --
  // spec: FRONTEND_BASIC.md §Authentication — PATCH /auth/me writes name to DB.
  const afterResp = await adminApi.get("/api/v1/auth/me");
  expect(afterResp.status()).toBe(200);
  const after = (await afterResp.json()) as { name: string; email: string };
  expect(after.name).toBe(NEW_NAME);
  expect(after.email).toBe(ADMIN_EMAIL);

  // Revert via adminApi.
  const revertResp = await adminApi.patch("/api/v1/auth/me", { data: { name: originalName } });
  expect(revertResp.status()).toBe(200);
  const reverted = (await revertResp.json()) as { name: string };
  expect(reverted.name).toBe(originalName);
  // Clear afterAll guard.
  originalName = null;
});
