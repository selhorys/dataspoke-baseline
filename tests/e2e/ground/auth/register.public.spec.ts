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
 *   4. The FIRST HTML response already carries an absolute "Sign up with Google"
 *      href pointing at the configured API host (regression cover for issue #129).
 *
 * Run-mode precondition (test 4 only): the app under test must be the CLUSTER
 * frontend (`install.sh --frontend cluster`, which is what `baseURL` =
 * `http://app.<INGRESS_DOMAIN>` serves). That image bakes no NEXT_PUBLIC_API_BASE_URL
 * (.dockerignore excludes .env*), so the server-side DATASPOKE_API_BASE_URL read is
 * the ONLY thing that can make the href absolute. Under host `pnpm dev` +
 * PLAYWRIGHT_BASE_URL=http://localhost:3000, .env.local supplies
 * NEXT_PUBLIC_API_BASE_URL and even the pre-fix build renders an absolute href, so
 * test 4 would pass vacuously there — it proves nothing about issue #129 in that mode.
 *
 * Email domain: @test.dataspoke.example.com (the API's EmailStr validator
 * rejects .local as a special-use domain; .example.com is always accepted).
 *
 * Rate-limit note: only 1 real registration call across the module (test 3).
 * The 5/min /auth/register limit is not approached. Test 4 adds exactly one
 * `/auth/google/login` request, against that route's own 10/min budget.
 *
 * spec: spec/feature/FRONTEND_BASIC.md §Authentication — /register form contract.
 * spec: spec/feature/FRONTEND_BASIC.md §Routing — public routes; /register
 *   calls POST /auth/register, sets token, redirects to /governance/dashboard.
 * spec: src/frontend/app/(public)/register/register.schema.ts — validation rules:
 *   email valid, name non-empty (max 128), password min 10 chars (max 128).
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group; afterAll cleanup.
 */

import { test, expect } from "../../fixtures/index";
import { appBaseUrl, apiBaseUrl } from "../../fixtures/env";

/**
 * Extracts the `href` of the anchor that wraps `label`, out of RAW HTML.
 *
 * Operates on the response text rather than on a parsed live DOM on purpose — the
 * subject of test 4 is what the server sent, before any client bundle ran.
 * `(?:(?!</a>)[\s\S])*?` stops the match at the first closing tag, so an anchor
 * appearing earlier in the document can never have its href attributed to this one.
 *
 * Assumption: `label` survives server rendering as one contiguous run of text. It does
 * today because the button label is a single static child. Were it ever split into
 * multiple JSX text/expression children, React would emit a `<!-- -->` separator between
 * them, the match would return null, and the failure message below would MISDIAGNOSE the
 * cause ("the page did not server-render the Google button at all") — so a label change
 * needs a matching change here, not a retry.
 */
function anchorHrefWrapping(html: string, label: string): string {
  const match = html.match(
    new RegExp(`<a\\b[^>]*\\bhref="([^"]*)"[^>]*>(?:(?!</a>)[\\s\\S])*?${label}`)
  );
  expect(
    match,
    `no anchor wrapping "${label}" found in the server HTML — the page did not ` +
      `server-render the Google button at all, so its href cannot be judged`
  ).not.toBeNull();
  return match![1];
}

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

// ── Test 4 — The server-rendered Google href targets the API host (issue #129) ─
//
// spec: src/frontend/README.md §Production / runtime configuration — "Server (SSR and
//   Server Components, where that window global does not exist yet) — from `process.env`
//   directly. Server-rendered markup therefore carries the deployed URLs, so absolute
//   links such as the Google sign-in href are correct in the first HTML response rather
//   than depending on hydration to repair them."
// spec: FRONTEND_BASIC.md §Routing — "/register | Self-service sign-up … and Google
//   sign-up | POST /auth/register, GET /auth/google/login" — /register carries the same
//   OAuth entry point as /login, from the same runtime-config read, so it regresses with
//   it and is covered here rather than assumed. FRONTEND_BASIC.md §Stack also permits an
//   empty API base URL (same-origin), so the assertion below is equality with the
//   CONFIGURED API host rather than "not the app host".
// spec: API.md §OAuth browser-redirect contract — "/auth/google/login … the handler
//   answers 302 on every outcome, never a JSON body".
//
// Asserts on the INITIAL SERVER HTML, not the post-hydration DOM: React does not repair
// an attribute it already rendered, so a `page.goto` + `getAttribute("href")` check would
// pass even against the defect this test exists to catch.

test("the first HTML response carries an absolute Google sign-up href on the configured API host", async ({
  request,
}) => {
  const appOrigin = new URL(appBaseUrl()).origin;
  // Only the cluster frontend image proves anything here — see the sibling assertion in
  // login.public.spec.ts for why host `pnpm dev` would pass this vacuously.
  test.skip(
    new URL(appBaseUrl()).hostname === "localhost" || Boolean(process.env.PLAYWRIGHT_BASE_URL),
    "the #129 guard is load-bearing only against the cluster frontend image — " +
      "run ./helm-charts/bin/install.sh --profile dev --components frontend and retry"
  );

  // The value the chart was configured with: install.sh sets
  // `frontend.config.apiBaseUrl=<scheme>://api.<INGRESS_DOMAIN>`, and fixtures/env.ts
  // derives apiBaseUrl() from the same DATASPOKE_KUBE_INGRESS_DOMAIN.
  const expectedApiHost = new URL(apiBaseUrl()).host;

  // maxRedirects: 0 — a followed 302 would still report 200 and misdiagnose downstream.
  const resp = await request.get(`${appOrigin}/register`, { maxRedirects: 0 });
  expect(resp.status(), "the /register document itself must render").toBe(200);
  const html = await resp.text();

  const href = anchorHrefWrapping(html, "Sign up with Google");

  // -- SSR assertion: the href is absolute, not a bare path --
  expect(
    href,
    `server-rendered Google sign-up href was "${href}"; a relative href means the SSR ` +
      `runtime-config read resolved apiBaseUrl to "" (issue #129)`
  ).toMatch(/^https?:\/\//);

  const target = new URL(href);

  // -- SSR assertion: it points at the API the deployment was configured with --
  // Host, not origin: the ingress scheme follows DATASPOKE_KUBE_INGRESS_SCHEME, so an
  // https deployment must not fail this. Equality (rather than "not the app origin") is
  // what pins the href to apiBaseUrl: a swapped-field regression building the href from
  // airflowUrl is still absolute and still off the app origin, and only this assertion
  // catches it.
  expect(
    target.host,
    `Google sign-up points at ${target.host}; the deployment's API is ${expectedApiHost}`
  ).toBe(expectedApiHost);

  // -- SSR assertion: it is the OAuth entry point, not some other API path --
  expect(target.pathname).toBe("/api/v1/auth/google/login");

  // -- Backend confirmation: that absolute URL is a live route, independently probed --
  const probe = await request.get(href, { maxRedirects: 0 });
  expect(
    probe.status(),
    `GET ${href} answered ${probe.status()}; per API.md §OAuth browser-redirect contract ` +
      `this route answers 302 on every outcome`
  ).toBe(302);
});
