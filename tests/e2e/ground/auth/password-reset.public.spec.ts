/**
 * Ground spec — /forgot-password and /reset-password pages (unauthenticated).
 *
 * One concern per test:
 *
 * /forgot-password:
 *   1. Form renders: heading, email field, "Send reset link" button, Back to sign in link.
 *   2. Submitting an email (real or unknown) → success confirmation state rendered.
 *      spec: POST /auth/password/reset/request returns success even for unknown emails by
 *      design (prevents user enumeration). The UI transitions to the "Check your email"
 *      confirmation view regardless of whether the email is registered.
 *
 * /reset-password (no token in URL):
 *   3. Without a ?token= query param the page renders the "Invalid link" error state
 *      (token is empty string → page.tsx guards it).
 *
 * /reset-password (with dummy token):
 *   4. With ?token=dummy-token the new-password form renders (heading "Set new password",
 *      New password field, "Update password" button).
 *   5. Short password client-side validation: "New password" < 10 chars → inline error,
 *      no server call (reset-password.schema.ts min(10) fires before submission).
 *
 * Note: A real successful reset is NOT asserted — the API verifies the token against
 * its DB, and no mechanism exists in tests to mint a real reset token externally.
 * Asserting the form renders + client validation is the achievable scope.
 *
 * spec: spec/feature/FRONTEND_BASIC.md §Authentication — /forgot-password and
 *   /reset-password form contracts; POST /auth/password/reset/request and
 *   POST /auth/password/reset/confirm.
 * spec: spec/feature/FRONTEND_BASIC.md §Routing — public routes.
 * spec: src/frontend/app/(public)/forgot-password/forgot-password.schema.ts — email validator.
 * spec: src/frontend/app/(public)/reset-password/reset-password.schema.ts — min 10 chars.
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group, selector guidance.
 */

import { test, expect } from "../../fixtures/index";

// ─────────────────────────────────────────────────────────────────────────────
// /forgot-password tests
// ─────────────────────────────────────────────────────────────────────────────

// ── Test 1 — Form renders all expected elements ────────────────────────────────
// spec: FRONTEND_BASIC.md §Authentication — forgot-password page:
//   heading, email field, "Send reset link" submit, "Back to sign in" link.
// src/frontend/app/(public)/forgot-password/page.tsx

test("forgot-password page renders heading, email field, and action controls", async ({
  page,
}) => {
  await page.goto("/forgot-password");
  await expect(page).toHaveURL(/\/forgot-password/);

  // -- UI assertion: page heading --
  // src/frontend/app/(public)/forgot-password/page.tsx — <h1>Forgot password</h1>
  await expect(
    page.getByRole("heading", { name: "Forgot password", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: Email field --
  await expect(page.getByLabel("Email")).toBeVisible();

  // -- UI assertion: Submit button --
  // src/frontend/app/(public)/forgot-password/page.tsx — "Send reset link"
  await expect(
    page.getByRole("button", { name: "Send reset link", exact: true })
  ).toBeVisible();

  // -- UI assertion: Back to sign in link --
  // src/frontend/app/(public)/forgot-password/page.tsx — Link href="/login"
  await expect(page.getByRole("link", { name: "Back to sign in" })).toBeVisible();
});

// ── Test 2 — Submit email → success state (even for unregistered email) ────────
// spec: FRONTEND_BASIC.md §Authentication — POST /auth/password/reset/request:
//   returns success even for unknown emails (by design: prevents user enumeration).
//   The UI transitions to the "Check your email" confirmation view.
// src/frontend/app/(public)/forgot-password/page.tsx — if (submitted) branch.

test("submitting any email shows the request-sent confirmation state", async ({ page }) => {
  await page.goto("/forgot-password");
  await expect(
    page.getByRole("button", { name: "Send reset link", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // Use an email that is very unlikely to be registered — the API returns 200
  // regardless of registration status (enumeration protection by design).
  await page.getByLabel("Email").fill("unknown-user-ground@test.dataspoke.example.com");
  await page.getByRole("button", { name: "Send reset link", exact: true }).click();

  // -- UI assertion: confirmation heading rendered --
  // src/frontend/app/(public)/forgot-password/page.tsx — submitted state:
  //   <h1>Check your email</h1>
  await expect(
    page.getByRole("heading", { name: "Check your email", exact: true })
  ).toBeVisible({ timeout: 20_000 });

  // -- UI assertion: helper text rendered --
  // src/frontend/app/(public)/forgot-password/page.tsx — submitted state body copy.
  await expect(
    page.getByText("password reset link has been sent", { exact: false })
  ).toBeVisible();

  // -- UI assertion: "Back to sign in" link present in confirmation state --
  await expect(page.getByRole("link", { name: "Back to sign in" })).toBeVisible();
});

// ─────────────────────────────────────────────────────────────────────────────
// /reset-password tests
// ─────────────────────────────────────────────────────────────────────────────

// ── Test 3 — No token → "Invalid link" state ──────────────────────────────────
// spec: FRONTEND_BASIC.md §Authentication — /reset-password without ?token renders
//   "Invalid link" error state.
// src/frontend/app/(public)/reset-password/page.tsx — !token branch.

test("reset-password without token query param shows the Invalid link error state", async ({
  page,
}) => {
  // Navigate without ?token=... parameter.
  await page.goto("/reset-password");

  // -- UI assertion: "Invalid link" heading --
  // src/frontend/app/(public)/reset-password/page.tsx — !token branch:
  //   <h1>Invalid link</h1>
  await expect(
    page.getByRole("heading", { name: "Invalid link", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: error description text --
  await expect(
    page.getByText("invalid or has already been used", { exact: false })
  ).toBeVisible();

  // -- UI assertion: "Request a new reset link" link rendered --
  // src/frontend/app/(public)/reset-password/page.tsx — !token branch link to /forgot-password
  await expect(
    page.getByRole("link", { name: "Request a new reset link" })
  ).toBeVisible();
});

// ── Test 4 — Dummy token → new-password form renders ─────────────────────────
// spec: FRONTEND_BASIC.md §Authentication — /reset-password?token=... renders the
//   new-password form (heading "Set new password", New password field, Update button).
// The form renders for any non-empty token string; actual validation happens only
// when the form is submitted (server rejects an invalid token at that point).
// src/frontend/app/(public)/reset-password/page.tsx — token present branch.

test("reset-password with token query param renders the new-password form", async ({
  page,
}) => {
  // A dummy token — real enough to pass the non-empty guard in the component.
  await page.goto("/reset-password?token=dummy-token-for-form-render-check");

  // -- UI assertion: form heading --
  // src/frontend/app/(public)/reset-password/page.tsx — token present: <h1>Set new password</h1>
  await expect(
    page.getByRole("heading", { name: "Set new password", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: New password input (by label via Field component) --
  // src/frontend/app/(public)/reset-password/page.tsx — Field label="New password"
  // PasswordInput renders with id="new_password"; use locator to avoid toggle collision.
  await expect(page.locator("input#new_password")).toBeVisible();

  // -- UI assertion: Update password submit button --
  // src/frontend/app/(public)/reset-password/page.tsx — <Button>"Update password"</Button>
  await expect(
    page.getByRole("button", { name: "Update password", exact: true })
  ).toBeVisible();
});

// ── Test 5 — Short password client-side validation on reset form ──────────────
// spec: reset-password.schema.ts — new_password: min 10 chars;
//   error message: "Password must be at least 10 characters".
// Validates without hitting POST /auth/password/reset/confirm (zod schema fires first).

test("new password shorter than 10 characters shows validation error on reset form", async ({
  page,
}) => {
  await page.goto("/reset-password?token=dummy-token-for-validation-check");
  await expect(
    page.getByRole("heading", { name: "Set new password", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // Fill a password that is too short (9 chars — one under the 10-char minimum).
  await page.locator("input#new_password").fill("tooshort!");

  // Attempt to submit.
  await page.getByRole("button", { name: "Update password", exact: true }).click();

  // -- UI assertion: inline validation error --
  // src/frontend/app/(public)/reset-password/reset-password.schema.ts:
  //   new_password: z.string().min(10, "Password must be at least 10 characters")
  await expect(
    page.getByText("Password must be at least 10 characters", { exact: false })
  ).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: still on /reset-password (no redirect) --
  await expect(page).toHaveURL(/\/reset-password/);
});
