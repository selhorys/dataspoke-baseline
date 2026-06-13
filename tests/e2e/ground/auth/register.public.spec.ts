/**
 * Ground spec — /register page (unauthenticated).
 *
 * One concern per test:
 *   1. Form renders: Email, Name, Password fields; Create account button;
 *      Sign up with Google button; Sign in link.
 *   2. Client-side validation — short password (< 10 chars) shows validation
 *      error without hitting the server.
 *   3. Real successful signup via the UI → redirects to /governance/dashboard.
 *      Cleanup: afterAll deletes the created user via adminApi.
 *
 * Email domain: @test.dataspoke.example.com (the API's EmailStr validator
 * rejects .local as a special-use domain; .example.com is always accepted).
 *
 * Rate-limit note: only 1 real registration call across the module (test 3).
 * The 5/min /auth/register limit is not approached.
 *
 * spec: spec/feature/FRONTEND_BASIC.md §Authentication — /register form contract.
 * spec: spec/feature/FRONTEND_BASIC.md §Routing — public routes; /register
 *   calls POST /auth/register, sets token, redirects to /governance/dashboard.
 * spec: src/frontend/app/(public)/register/register.schema.ts — validation rules:
 *   email valid, name non-empty (max 128), password min 10 chars (max 128).
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group; afterAll cleanup.
 */

import { test, expect } from "../../fixtures/index";

// Deterministic email — stable across reruns; suffix is fixed so afterAll
// cleanup is idempotent (delete-if-exists even if the test that created it failed).
const REGISTER_EMAIL = "ground-register-01@test.dataspoke.example.com";
const REGISTER_NAME = "Ground Register User";
const REGISTER_PASSWORD = "gr0und-r3g!ster"; // >= 10 chars, clearly unique

// Track whether the user was created in test 3 so afterAll cleans up correctly.
let createdUserId: string | null = null;

// ── Cleanup: delete the registered user (idempotent) ─────────────────────────

test.afterAll(async ({ adminApi }) => {
  // Look up the user by listing all users and matching by email.
  // Works whether the test ran or was skipped (delete-if-exists semantics).
  const listResp = await adminApi.get("/api/v1/admin/users?limit=100");
  if (!listResp.ok()) return; // best-effort

  const body = (await listResp.json()) as {
    users: Array<{ id: string; email: string }>;
  };
  const user = body.users.find((u) => u.email === REGISTER_EMAIL);
  if (!user) return; // already gone or was never created

  await adminApi.delete(`/api/v1/admin/users/${user.id}`);
  createdUserId = null;
});

// ── Test 1 — Form renders all expected elements ────────────────────────────────
// spec: FRONTEND_BASIC.md §Authentication — register wireframe:
//   Email, Name, Password fields; Create account button; Sign up with Google;
//   "Already have an account? Sign in" link.

test("register page renders email, name, password fields and sign-up controls", async ({
  page,
}) => {
  await page.goto("/register");
  await expect(page).toHaveURL(/\/register/);

  // -- UI assertion: page heading --
  // src/frontend/app/(public)/register/page.tsx — <h1>Create account</h1>
  await expect(
    page.getByRole("heading", { name: "Create account", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: Email field --
  await expect(page.getByLabel("Email")).toBeVisible();

  // -- UI assertion: Name field --
  await expect(page.getByLabel("Name")).toBeVisible();

  // -- UI assertion: Password field (via id — avoids matching Show toggle) --
  await expect(page.locator("input#password")).toBeVisible();

  // -- UI assertion: Create account submit button --
  await expect(page.getByRole("button", { name: "Create account", exact: true })).toBeVisible();

  // -- UI assertion: Sign up with Google button --
  // src/frontend/app/(public)/register/page.tsx — "Sign up with Google"
  await expect(page.getByRole("button", { name: "Sign up with Google" })).toBeVisible();

  // -- UI assertion: Sign in link --
  // spec: FRONTEND_BASIC.md §Routing — /register links back to /login
  await expect(page.getByRole("link", { name: "Sign in" })).toBeVisible();
});

// ── Test 2 — Short password triggers client-side validation error ──────────────
// spec: register.schema.ts — password min 10 chars; error message:
//   "Password must be at least 10 characters".
// This validates WITHOUT hitting POST /auth/register (zod schema fires first).

test("password shorter than 10 characters shows validation error without server call", async ({
  page,
}) => {
  await page.goto("/register");
  await expect(
    page.getByRole("heading", { name: "Create account", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // Fill a valid email and name so those fields don't block submission.
  await page.getByLabel("Email").fill("short-pw-test@test.dataspoke.example.com");
  await page.getByLabel("Name").fill("Short PW Test");

  // Fill a password that is too short (9 chars — one under the 10-char minimum).
  await page.locator("input#password").fill("tooshort!");

  // Attempt to submit.
  await page.getByRole("button", { name: "Create account", exact: true }).click();

  // -- UI assertion: inline validation error appears --
  // src/frontend/app/(public)/register/register.schema.ts:
  //   password: z.string().min(10, "Password must be at least 10 characters")
  await expect(
    page.getByText("Password must be at least 10 characters", { exact: false })
  ).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: no redirect; still on /register --
  await expect(page).toHaveURL(/\/register/);
});

// ── Test 3 — Real signup → redirects to /governance/dashboard ─────────────────
// spec: FRONTEND_BASIC.md §Authentication — POST /auth/register → access_token issued;
//   router.replace("/governance/dashboard").
// spec: FRONTEND_BASIC.md §Routing — /register → /governance/dashboard on success.
//
// Uses a new browser context (no stored state) to avoid cross-contamination
// with other tests' tokens.

test("successful registration redirects to /governance/dashboard", async ({ page }) => {
  await page.goto("/register");
  await expect(
    page.getByRole("heading", { name: "Create account", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // Fill all required fields with valid values.
  await page.getByLabel("Email").fill(REGISTER_EMAIL);
  await page.getByLabel("Name").fill(REGISTER_NAME);
  await page.locator("input#password").fill(REGISTER_PASSWORD);

  // Submit the form.
  await page.getByRole("button", { name: "Create account", exact: true }).click();

  // -- UI assertion: redirected to the post-login home --
  // spec: FRONTEND_BASIC.md §Routing — /register → router.replace("/governance/dashboard")
  await page.waitForURL(/\/governance\/dashboard/, { timeout: 30_000 });
  await expect(page).toHaveURL(/\/governance\/dashboard/);

  // Mark created so afterAll can clean up.
  createdUserId = REGISTER_EMAIL; // afterAll uses email lookup, not stored id
});
