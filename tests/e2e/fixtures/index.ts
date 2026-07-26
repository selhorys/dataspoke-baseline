/**
 * Custom Playwright fixtures for DataSpoke E2E tests.
 *
 * Fixtures provided:
 *   - authedPage: Page pre-loaded with the role's storageState (admin / editor / reader).
 *     The base `page` from each Playwright project already carries the correct
 *     storageState; this re-exports it for clarity in test files that import
 *     from fixtures/index.ts.
 *   - adminApi: APIRequestContext with a fresh admin bearer token. Used for
 *     dual-confirmation backend probes in use-case specs (mirroring the api-wired
 *     httpx.AsyncClient pattern).
 *
 * No stub-toggle helper is provided, by design: the four `stub_*` fields are dev-env-wide
 * settings owned by the profile seed and the operator, and a test may read them to gate an
 * LLM variant but never sets them.
 * spec: spec/TESTING.md §End-to-End (E2E) Testing §Execution discipline — "Never flip the
 *   stub toggles… A test may read them to gate an LLM variant and must assert them
 *   unchanged after any `/admin/conf` write, but never sets them."
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — dual confirmation via an independent
 *   APIRequestContext probe; §Test Data Design — Imazon is the canonical company context.
 */

import * as fs from "fs";
import * as path from "path";
import { test as base, type APIRequestContext, expect } from "@playwright/test";
import { apiBaseUrl, IMAZON_URNS } from "./env";

export { IMAZON_URNS };

const AUTH_DIR = path.join(__dirname, "..", ".auth");

/** Long-lived admin API token written by global-setup; read by the adminApi fixture. */
const ADMIN_API_TOKEN_FILE = path.join(AUTH_DIR, "admin-api-token.txt");

/** storageState files keyed by role (matching playwright.config.ts project names) */
export const STORAGE_STATE: Record<string, string> = {
  admin: path.join(AUTH_DIR, "admin.json"),
  editor: path.join(AUTH_DIR, "editor.json"),
  reader: path.join(AUTH_DIR, "reader.json"),
};

// ── Extended fixture types ────────────────────────────────────────────────────

/** No test-scoped fixtures — everything this project adds is worker-scoped. */
type E2ETestFixtures = Record<never, never>;

type E2EWorkerFixtures = {
  /** APIRequestContext carrying a long-lived admin API token (worker-scoped). */
  adminApi: APIRequestContext;
};

// ── Fixture implementations ───────────────────────────────────────────────────

export const test = base.extend<E2ETestFixtures, E2EWorkerFixtures>({
  /**
   * Worker-scoped admin probe context for dual-confirmation backend checks.
   *
   * Reads the long-lived `dsk_` API token that global-setup minted and wrote to
   * `.auth/admin-api-token.txt`, and builds one reusable APIRequestContext bearing
   * it. No login or mint happens here, so the test run makes ZERO /auth/token calls
   * (only global-setup's handful) — this avoids both the /auth/token 10/min rate
   * limit (which a per-test login blew) and 15-min access-token expiry during long
   * ES-settle polls. workers:1 means one context per run.
   *
   * Source: tests/integration/api_wired/conftest.py admin_token fixture;
   * spec/feature/AUTH.md §API Tokens (dsk_ bearer, raw token returned once).
   */
  adminApi: [
    async ({ playwright }, use) => {
      const apiBase = apiBaseUrl();
      if (!fs.existsSync(ADMIN_API_TOKEN_FILE)) {
        throw new Error(
          `Admin API token file missing: ${ADMIN_API_TOKEN_FILE}. ` +
            `global-setup must mint it before tests run.`
        );
      }
      const apiToken = fs.readFileSync(ADMIN_API_TOKEN_FILE, "utf-8").trim();
      const ctx = await playwright.request.newContext({
        baseURL: apiBase,
        extraHTTPHeaders: {
          Authorization: `Bearer ${apiToken}`,
          "Content-Type": "application/json",
          Accept: "application/json",
        },
      });
      await use(ctx);
      await ctx.dispose();
    },
    { scope: "worker" },
  ],
});

export { expect } from "@playwright/test";
