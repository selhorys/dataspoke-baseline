/**
 * Ground spec — /oauth-error page (unauthenticated).
 *
 * The landing page for a failed Google sign-in. The API's two Google routes are
 * browser-navigation endpoints that 302 on every outcome, so a failure arrives here as
 * `?error=<code>` instead of on a JSON envelope. The page is public and makes no API
 * call: copy is a lookup into a fixed code→copy map, and the received parameter value is
 * never echoed into the rendered output.
 *
 * One concern per test:
 *   1. The route is public — reachable with no session, no redirect to /login.
 *   2. EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT renders the three-step recovery sequence.
 *   3. A sibling code (OAUTH_NOT_CONFIGURED) renders its own copy and no procedure.
 *   4. An absent code falls back to generic wording.
 *   5. A hostile ?error= value falls back to generic wording and is not echoed.
 *   6. "Back to sign in" navigates to /login — the only way onward from the page.
 *   7. Real-stack tie-in: the location `/auth/google/callback` redirects to actually
 *      renders this page with the copy matching the code the backend emitted. Runs on
 *      every deployment — the callback lands here whether or not OAuth is configured.
 *
 * No dual-confirmation backend probe on tests 1–6: the page makes no API call
 * (spec/feature/FRONTEND_BASIC.md §Routing — "`/oauth-error` … Pure presentation of the
 * query param | — (no API call)"), so there is no backend state for a probe to read.
 * Test 7 supplies the real-stack binding instead, by driving the producer of the
 * redirect rather than the page alone.
 *
 * Rate-limit note: exactly one `/auth/google/callback` call across the module (test 7),
 * against that route's fail-closed 10/min budget.
 *
 * spec: spec/feature/FRONTEND_BASIC.md §OAuth error page (`/oauth-error`) — the code→copy
 *   map, the never-echo rule, and "Every state carries a link back to `/login`, the only
 *   way onward from the page."
 * spec: spec/feature/FRONTEND_BASIC.md §Routing — `/oauth-error` is a public route.
 * spec: spec/feature/AUTH.md §Admin unbind — the three-step recovery sequence behind
 *   EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT.
 * spec: spec/API.md §OAuth browser-redirect contract — the five codes that reach the page.
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group, selector guidance.
 */

import { test, expect } from "../../fixtures/index";
import { apiBaseUrl, appBaseUrl } from "../../fixtures/env";

// ── Test 1 — The route is public ──────────────────────────────────────────────
// spec: FRONTEND_BASIC.md §Routing — "/login, /register, /forgot-password,
//   /reset-password, /oauth-error, and the OAuth callback URL are public; all other
//   routes redirect to /login?next=<path> when no access token is available."

test("oauth-error is reachable without a session and does not redirect to /login", async ({
  page,
}) => {
  await page.goto("/oauth-error?error=OAUTH_STATE_MISMATCH");

  // -- UI assertion: stayed on /oauth-error (no route-guard redirect) --
  await expect(page).toHaveURL(/\/oauth-error/);

  // -- UI assertion: the OAUTH_STATE_MISMATCH copy rendered --
  // spec: FRONTEND_BASIC.md §OAuth error page — "The sign-in attempt expired or was
  //   interrupted; start again from /login."
  await expect(page.getByRole("heading")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText(/expired or was interrupted/i)).toBeVisible();
});

// ── Test 2 — EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT → three-step recovery ──────
// spec: FRONTEND_BASIC.md §OAuth error page — "This address is already linked to a
//   different Google account, plus the three-step recovery sequence — request and
//   complete a password reset, ask an admin to unlink, then sign in with Google again".
// spec: AUTH.md §Admin unbind — the same three steps, in that order.

test("bound-elsewhere code renders the three-step admin-unbind recovery sequence", async ({
  page,
}) => {
  await page.goto("/oauth-error?error=EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT");

  // -- UI assertion: the code's own heading, not the generic fallback --
  await expect(
    page.getByRole("heading", { name: /linked to a different Google account/i })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: exactly three ordered steps, in the spec'd order --
  // AUTH.md §Admin unbind: 1. password reset  2. admin unlinks  3. sign in with Google.
  //
  // Scoped to the ordered list rather than counting page-wide listitems: the spec'd
  // subject is "the three-step recovery sequence", and a page-wide count would silently
  // change meaning the day the public shell grows a nav or footer list.
  const steps = page.locator("ol").getByRole("listitem");
  await expect(steps).toHaveCount(3);
  await expect(steps.nth(0)).toContainText(/password reset/i);
  await expect(steps.nth(1)).toContainText(/unlink/i);
  await expect(steps.nth(2)).toContainText(/sign in with Google/i);
});

// ── Test 3 — Sibling code renders its own copy, without a procedure ───────────
// spec: FRONTEND_BASIC.md §OAuth error page — OAUTH_NOT_CONFIGURED: "Google sign-in is
//   not configured on this deployment; use email + password and contact an
//   administrator." EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT is "the only code whose copy
//   is a procedure rather than a sentence".

test("not-configured code renders its own sentence and no recovery sequence", async ({
  page,
}) => {
  await page.goto("/oauth-error?error=OAUTH_NOT_CONFIGURED");

  await expect(page.getByText(/not configured on this deployment/i)).toBeVisible({
    timeout: 15_000,
  });
  await expect(page.getByText(/administrator/i)).toBeVisible();

  // -- UI assertion: no ordered recovery steps on this code --
  await expect(page.locator("ol").getByRole("listitem")).toHaveCount(0);
});

// ── Test 4 — Absent code → generic wording ────────────────────────────────────
// spec: FRONTEND_BASIC.md §OAuth error page — "| absent or unrecognised | Generic
//   'Google sign-in could not be completed' wording. |"

test("navigating without an error code falls back to the generic wording", async ({ page }) => {
  await page.goto("/oauth-error");

  await expect(page.getByText(/Google sign-in could not be completed/i)).toBeVisible({
    timeout: 15_000,
  });
  await expect(page.locator("ol").getByRole("listitem")).toHaveCount(0);
});

// ── Test 5 — Hostile ?error= value → generic wording, never echoed ────────────
// spec: FRONTEND_BASIC.md §OAuth error page — "Selection is a lookup into the fixed map
//   below — the received parameter value is never echoed into the rendered output, since
//   the page is directly navigable with any value."
//
// The value below is injected by this test, so the absence assertions that follow are
// checking for something that was actually supplied.

test("a hostile error value renders generic wording and is not echoed into the page", async ({
  page,
}) => {
  const hostile = '<img src=x onerror="alert(1)">';
  await page.goto(`/oauth-error?error=${encodeURIComponent(hostile)}`);

  // -- UI assertion: the unrecognised value selected the generic copy --
  await expect(page.getByText(/Google sign-in could not be completed/i)).toBeVisible({
    timeout: 15_000,
  });

  // -- UI assertion: the value appears nowhere in the rendered text --
  const bodyText = (await page.locator("body").innerText()) ?? "";
  expect(bodyText).not.toContain("onerror");
  expect(bodyText).not.toContain("img src=x");

  // -- UI assertion: and was not interpreted as markup either --
  await expect(page.locator("body img")).toHaveCount(0);

  // -- Backstop: the query param really was delivered to the page --
  expect(page.url()).toContain("onerror");
});

// ── Test 6 — "Back to sign in" is the way onward ──────────────────────────────
// spec: FRONTEND_BASIC.md §OAuth error page — "Every state carries a link back to
//   /login, the only way onward from the page."

test("Back to sign in navigates to /login", async ({ page }) => {
  await page.goto("/oauth-error?error=GOOGLE_ACCOUNT_LINKED_ELSEWHERE");

  // Located by destination, not by label: the spec fixes where the link goes ("a link
  // back to /login"), not the words on it, so a copy change must not break this test.
  const back = page.locator('a[href="/login"]');
  await expect(back).toBeVisible({ timeout: 15_000 });
  await back.click();

  await page.waitForURL(/\/login/, { timeout: 15_000 });
  await expect(page).toHaveURL(/\/login/);
});

// ── Test 7 — The API's redirect target actually renders this page ─────────────
// spec: API.md §OAuth browser-redirect contract — "/auth/google/callback | Catalogued
//   failure | 302 to <ui>/oauth-error?error=<code>", where <ui> is "the origin of the
//   configured post-login redirect target … plus the absolute path /oauth-error".
//
// This is the one test that binds the page to its producer against the real stack: a
// deployment whose post-login redirect origin is wrong would still pass tests 1–6 and
// strand every failed sign-in on a dead URL.
//
// The callback is driven rather than /auth/google/login because it lands on the error page
// under BOTH OAuth configurations, so this check needs no precondition and never skips:
// with OAuth unconfigured the route raises OAUTH_NOT_CONFIGURED, and with it configured
// this credential-free request carries no session state for authlib to match, so it raises
// OAUTH_STATE_MISMATCH. /auth/google/login, by contrast, 302s to Google's consent screen
// on a configured deployment and would take the origin check with it.

// spec: FRONTEND_BASIC.md §OAuth error page — the copy each catalogued code selects.
// Inlined here (rather than imported from the page) so the expected wording is stated by
// the test, not read back out of the component under test.
const COPY_FOR_CODE: Record<string, RegExp> = {
  OAUTH_NOT_CONFIGURED: /not configured on this deployment/i,
  OAUTH_STATE_MISMATCH: /expired or was interrupted/i,
  OAUTH_EMAIL_NOT_VERIFIED: /has not verified/i,
  GOOGLE_ACCOUNT_LINKED_ELSEWHERE: /already linked to another DataSpoke user/i,
  EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT: /linked to a different Google account/i,
};

test("the location /auth/google/callback redirects to renders this page", async ({
  page,
  request,
}) => {
  const resp = await request.get(
    `${apiBaseUrl()}/api/v1/auth/google/callback?code=e2e-not-a-real-code&state=e2e-not-a-real-state`,
    { maxRedirects: 0 }
  );

  // -- Backend assertion: every handler outcome on this route is a 302, never an envelope --
  expect(
    resp.status(),
    "the callback answers 302 on every outcome per API.md §OAuth browser-redirect contract"
  ).toBe(302);

  const location = resp.headers()["location"] ?? "";
  // Relative locations (the bare "/" post-login default) resolve against the app origin,
  // exactly as the browser resolves them.
  const target = new URL(location, appBaseUrl());

  // -- Backend assertion: the location is the /oauth-error page, and the code is one of
  //    the five the contract says reach it --
  expect(target.pathname, `callback redirected to ${location}`).toBe("/oauth-error");
  const code = target.searchParams.get("error") ?? "";
  expect(
    Object.keys(COPY_FOR_CODE),
    `callback emitted ?error=${code}, which is not one of the five catalogued codes`
  ).toContain(code);

  // Follow the redirect exactly as a browser would.
  await page.goto(location);

  // -- UI assertion: the redirect landed on the rendered error page, not a 404, and the
  //    copy matches the code the backend actually emitted --
  await expect(page).toHaveURL(/\/oauth-error/);
  // `.first()` because two of the five codes carry the same phrase in the heading and the
  // description; the assertion is that the copy rendered, not how many nodes carry it.
  await expect(page.getByText(COPY_FOR_CODE[code]).first()).toBeVisible({ timeout: 15_000 });
});
