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
 *   5. The FIRST HTML response already carries an absolute "Sign in with Google"
 *      href pointing at the configured API host (regression cover for issue #129).
 *
 * Run-mode precondition (test 5 only): the app under test must be the CLUSTER
 * frontend (`install.sh --frontend cluster`, which is what `baseURL` =
 * `http://app.<INGRESS_DOMAIN>` serves). That image bakes no NEXT_PUBLIC_API_BASE_URL
 * (.dockerignore excludes .env*), so the server-side DATASPOKE_API_BASE_URL read is
 * the ONLY thing that can make the href absolute. Under host `pnpm dev` +
 * PLAYWRIGHT_BASE_URL=http://localhost:3000, .env.local supplies
 * NEXT_PUBLIC_API_BASE_URL and even the pre-fix build renders an absolute href, so
 * test 5 would pass vacuously there — it proves nothing about issue #129 in that mode.
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
 * the 10/min /auth/token limit. Test 5 adds exactly one `/auth/google/login`
 * request, against that route's own 10/min budget.
 */

import { test, expect } from "../../fixtures/index";
import { appBaseUrl, apiBaseUrl } from "../../fixtures/env";

/**
 * Extracts the `href` of the anchor that wraps `label`, out of RAW HTML.
 *
 * Operates on the response text rather than on a parsed live DOM on purpose — the
 * subject of test 5 is what the server sent, before any client bundle ran.
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

// ── Test 5 — The server-rendered Google href targets the API host (issue #129) ─
//
// spec: src/frontend/README.md §Production / runtime configuration — "Server (SSR and
//   Server Components, where that window global does not exist yet) — from `process.env`
//   directly. Server-rendered markup therefore carries the deployed URLs, so absolute
//   links such as the Google sign-in href are correct in the first HTML response rather
//   than depending on hydration to repair them."
// spec: FRONTEND_BASIC.md §Routing — "/login | Login page (email+password and Google
//   sign-in) | POST /auth/token, GET /auth/google/login" — the button's destination is
//   that API route, resolved against the runtime-configured API base URL. On this dev
//   deployment that base URL is a separate `api.<domain>` host; FRONTEND_BASIC.md §Stack
//   also permits an empty base URL (same-origin), which is why the assertion below is
//   equality with the CONFIGURED host rather than "not the app host".
// spec: API.md §OAuth browser-redirect contract — "/auth/google/login … the handler
//   answers 302 on every outcome, never a JSON body".
//
// Deliberately asserts on the INITIAL SERVER HTML, fetched over plain HTTP, not on the
// post-hydration DOM. Hydration is precisely what does NOT repair this: React leaves an
// already-rendered attribute alone and GoogleButton never re-renders, so a `page.goto` +
// `getAttribute("href")` check passes even against the defect. In the defective build the
// server branch resolved apiBaseUrl to "" (NEXT_PUBLIC_* are never set in the container),
// and the delivered anchor carried the relative "/api/v1/auth/google/login", which the
// frontend origin answers with a 404.

test("the first HTML response carries an absolute Google sign-in href on the configured API host", async ({
  request,
}) => {
  const appOrigin = new URL(appBaseUrl()).origin;
  // Only the cluster frontend image proves anything here: it bakes no NEXT_PUBLIC_*, so an
  // absolute href can only have come from the server-side DATASPOKE_* read. Host `pnpm dev`
  // reads .env.local, whose NEXT_PUBLIC_API_BASE_URL is inlined at build time and derives
  // from the same ingress domain as apiBaseUrl() — so both assertions below would pass
  // against the defective build. Skip rather than assert vacuously.
  test.skip(
    new URL(appBaseUrl()).hostname === "localhost" || Boolean(process.env.PLAYWRIGHT_BASE_URL),
    "the #129 guard is load-bearing only against the cluster frontend image — " +
      "run ./helm-charts/bin/install.sh --profile dev --components frontend and retry"
  );

  // The value the chart was configured with: install.sh sets
  // `frontend.config.apiBaseUrl=<scheme>://api.<INGRESS_DOMAIN>`, and fixtures/env.ts
  // derives apiBaseUrl() from the same DATASPOKE_KUBE_INGRESS_DOMAIN.
  const expectedApiHost = new URL(apiBaseUrl()).host;

  // maxRedirects: 0 — APIRequestContext.get follows redirects by default, so a route that
  // 302'd /login elsewhere would still report 200 and the anchor lookup would then fail
  // with a misdiagnosing message.
  const resp = await request.get(`${appOrigin}/login`, { maxRedirects: 0 });
  expect(resp.status(), "the /login document itself must render").toBe(200);
  const html = await resp.text();

  const href = anchorHrefWrapping(html, "Sign in with Google");

  // -- SSR assertion: the href is absolute, not a bare path --
  expect(
    href,
    `server-rendered Google sign-in href was "${href}"; a relative href means the SSR ` +
      `runtime-config read resolved apiBaseUrl to "" (issue #129)`
  ).toMatch(/^https?:\/\//);

  const target = new URL(href);

  // -- SSR assertion: it points at the API the deployment was configured with --
  // Host, not origin: the ingress scheme follows DATASPOKE_KUBE_INGRESS_SCHEME, so an
  // https deployment must not fail this. Equality (rather than "not the app origin") is
  // what pins the href to apiBaseUrl: a swapped-field regression building the href from
  // airflowUrl is still absolute and still off the app origin, and only this assertion
  // catches it. It also keeps the test honest about FRONTEND_BASIC.md §Stack — "empty
  // falls back to same-origin" is an allowed configuration, so "off the app origin" is
  // not itself the contract; "the configured API host" is.
  expect(
    target.host,
    `Google sign-in points at ${target.host}; the deployment's API is ${expectedApiHost}`
  ).toBe(expectedApiHost);

  // -- SSR assertion: it is the OAuth entry point, not some other API path --
  expect(target.pathname).toBe("/api/v1/auth/google/login");

  // -- Backend confirmation: that absolute URL is a live route, independently probed --
  // A 302 on every outcome is the route's contract, so this holds whether or not Google
  // OAuth is configured on the deployment — and a 404 (the #129 symptom) fails it.
  const probe = await request.get(href, { maxRedirects: 0 });
  expect(
    probe.status(),
    `GET ${href} answered ${probe.status()}; per API.md §OAuth browser-redirect contract ` +
      `this route answers 302 on every outcome`
  ).toBe(302);
});
