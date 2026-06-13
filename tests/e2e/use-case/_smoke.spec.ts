/**
 * Harness smoke test — verifies the E2E infrastructure works end-to-end.
 *
 * This is NOT a UC test. It proves:
 *   1. The admin storageState (refresh cookie) is valid: the app shell renders
 *      on the post-login page without being redirected to /login.
 *   2. The adminApi fixture returns a working APIRequestContext: GET /admin/conf
 *      returns 200 with the expected shape.
 *
 * If this test fails, the harness itself is broken (auth/lock/reset/storageState).
 * Fix the infrastructure before writing UC specs.
 *
 * Runs in the "admin" project only — skip in editor/reader to avoid running
 * 6 copies of a harness-only test (playwright.config.ts has three projects).
 *
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — harness self-test
 */

import { test, expect } from "../fixtures/index";

// Restricted to the admin project via testIgnore in playwright.config.ts —
// editor and reader projects exclude this file so --list enumerates 2 tests.

test("smoke: admin storageState renders app shell on governance dashboard", async ({ page }) => {
  // Navigate to the post-login home. The AuthGuard (auth-guard.tsx) redirects
  // unauthenticated requests to /login; a successful render here proves that
  // SilentRefresh restored the access token from the saved refresh cookie.
  await page.goto("/governance/dashboard");

  // The page must NOT redirect to /login.
  await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });

  // Assert an AppShell-only nav link is visible. The sidebar nav (inside <aside>
  // in app-shell.tsx) renders "Dashboard" as a link only when the user is
  // authenticated and the AppShell is mounted. The public /login layout does
  // not render this link — making it a discriminating authenticated-only locator.
  await expect(page.getByRole("link", { name: "Dashboard" })).toBeVisible({ timeout: 15_000 });
});

test("smoke: adminApi probe — GET /admin/conf returns 200 with expected shape", async ({ adminApi }) => {
  // Dual-confirmation backend probe: the adminApi fixture mints a fresh admin
  // bearer token and wraps it in an APIRequestContext. This probe matches the
  // runtime_conf preflight in tests/integration/api_wired/conftest.py.
  const resp = await adminApi.get("/api/v1/admin/conf");

  expect(resp.status()).toBe(200);

  const conf = (await resp.json()) as Record<string, unknown>;

  // The dev-env baseline requires the three infra stubs to be true
  // (spec/TESTING.md §Stub Toggles — mirrors the Python runtime_conf preflight).
  // stub_llm_client is intentionally left as a typeof check only: real-LLM
  // test variants need it false, so we must not pin its value here.
  expect(conf["stub_redis_client"]).toBe(true);
  expect(conf["stub_pgvector_manager"]).toBe(true);
  expect(conf["stub_notification_service"]).toBe(true);
  expect(typeof conf["stub_llm_client"]).toBe("boolean");
});
