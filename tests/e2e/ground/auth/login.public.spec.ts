/**
 * Ground spec — /login page (unauthenticated).
 *
 * One concern per test; each proves a single observable UI behaviour
 * against the real stack with the minimum gesture sequence.
 *
 * Concerns covered:
 *   1. Form renders: email field, password field, Sign-in button,
 *      "Sign in with Google" button, Register link, Forgot-password link.
 *   2. Bad credentials → error message shown; user stays on /login.
 *   3. Register link navigates to /register.
 *   4. Forgot-password link navigates to /forgot-password.
 *
 * Selector notes (informed by page.tsx + global-setup.ts):
 *   - Password input collides with "Show password" toggle → `input#password`.
 *   - Submit "/sign in/i" also matches "Sign in with Google" → exact: true.
 *   - Toasts render text twice (visual div + aria-live span) → .first().
 *
 * spec: spec/feature/FRONTEND_BASIC.md §Authentication — /login form contract.
 * spec: spec/feature/FRONTEND_BASIC.md §Routing — public route list.
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group, selector guidance.
 *
 * Rate-limit note: only 1 bad-login call across the whole module; well within
 * the 10/min /auth/token limit.
 */

import { test, expect } from "../../fixtures/index";

// ── Test 1 — Form renders all expected elements ────────────────────────────────
// spec: FRONTEND_BASIC.md §Authentication — login wireframe:
//   Email, Password fields; Sign in button; Sign in with Google; Register link;
//   Forgot password link.

test("login page renders email, password, sign-in button, Google button, and nav links", async ({
  page,
}) => {
  await page.goto("/login");

  // Page should not redirect to /governance/dashboard (unauthenticated context).
  await expect(page).toHaveURL(/\/login/);

  // -- UI assertion: Email input (by label) --
  await expect(page.getByLabel("Email")).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: Password input (by id — avoids matching "Show password" toggle) --
  // spec: FRONTEND_BASIC.md — PasswordInput component with toggle; target input#password.
  await expect(page.locator("input#password")).toBeVisible();

  // -- UI assertion: Submit button (exact — avoids matching "Sign in with Google") --
  await expect(page.getByRole("button", { name: "Sign in", exact: true })).toBeVisible();

  // -- UI assertion: Google sign-in button --
  // spec: FRONTEND_BASIC.md §Authentication — "Sign in with Google" (Google OAuth flow)
  await expect(page.getByRole("button", { name: "Sign in with Google" })).toBeVisible();

  // -- UI assertion: Register link --
  // spec: FRONTEND_BASIC.md §Routing — /login links to /register ("Register")
  await expect(page.getByRole("link", { name: "Register" })).toBeVisible();

  // -- UI assertion: Forgot password link --
  // spec: FRONTEND_BASIC.md §Routing — /login links to /forgot-password
  await expect(page.getByRole("link", { name: "Forgot password?" })).toBeVisible();
});

// ── Test 2 — Bad credentials → error toast; stays on /login ───────────────────
// spec: FRONTEND_BASIC.md §Authentication — POST /auth/token with bad credentials
//   → API returns 401; UI shows destructive toast "Sign in failed"; stays on /login.

test("bad credentials show error message and stay on /login", async ({ page }) => {
  await page.goto("/login");
  await expect(page.getByLabel("Email")).toBeVisible({ timeout: 15_000 });

  // Fill with clearly invalid credentials (no real account for this email).
  await page.getByLabel("Email").fill("no-such-user@test.dataspoke.example.com");
  await page.locator("input#password").fill("wrongpassword");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();

  // -- UI assertion: error toast appears --
  // spec: FRONTEND_BASIC.md §Authentication — onSubmit catch: toast({ variant: "destructive",
  //   title: "Sign in failed", ... }). Toasts render twice (visual + aria-live) → .first().
  await expect(page.getByText("Sign in failed").first()).toBeVisible({ timeout: 20_000 });

  // -- UI assertion: still on /login (no redirect occurred) --
  await expect(page).toHaveURL(/\/login/);
});

// ── Test 3 — Register link navigates to /register ─────────────────────────────
// spec: FRONTEND_BASIC.md §Routing — "Need an account? Register →" link → /register.

test("Register link navigates to /register", async ({ page }) => {
  await page.goto("/login");
  await expect(page.getByRole("link", { name: "Register" })).toBeVisible({ timeout: 15_000 });

  await page.getByRole("link", { name: "Register" }).click();

  await page.waitForURL(/\/register/, { timeout: 15_000 });
  await expect(page).toHaveURL(/\/register/);
});

// ── Test 4 — Forgot-password link navigates to /forgot-password ───────────────
// spec: FRONTEND_BASIC.md §Routing — "Forgot password?" link → /forgot-password.

test("Forgot password link navigates to /forgot-password", async ({ page }) => {
  await page.goto("/login");
  await expect(page.getByRole("link", { name: "Forgot password?" })).toBeVisible({ timeout: 15_000 });

  await page.getByRole("link", { name: "Forgot password?" }).click();

  await page.waitForURL(/\/forgot-password/, { timeout: 15_000 });
  await expect(page).toHaveURL(/\/forgot-password/);
});
