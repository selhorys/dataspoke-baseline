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
 *   - toggleStub: helper to PATCH /api/v1/admin/conf stub fields and read them back.
 *
 * spec: spec/TESTING.md §E2E — dual confirmation, stub toggle, Imazon URNs.
 */

import * as fs from "fs";
import * as path from "path";
import { test as base, type APIRequestContext, expect } from "@playwright/test";
import { apiBaseUrl, type StubField, IMAZON_URNS } from "./env";

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

type E2EWorkerFixtures = {
  /** APIRequestContext carrying a long-lived admin API token (worker-scoped). */
  adminApi: APIRequestContext;
};

type E2EFixtures = {
  /** Toggle a stub_* field via PATCH /api/v1/admin/conf; read it back via GET. */
  toggleStub: (field: StubField, value: boolean) => Promise<Record<string, unknown>>;
};

// ── Fixture implementations ───────────────────────────────────────────────────

export const test = base.extend<E2EFixtures, E2EWorkerFixtures>({
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

  /**
   * Toggle a stub_* field via PATCH /api/v1/admin/conf and return the updated conf.
   * Tests that rely on stub_llm_client state use this to gate real-LLM branches
   * — mirrors the api-wired pattern (PATCH /admin/conf, then pytest.skip when stubbed).
   *
   * Source: spec/TESTING.md §Stub Toggles — PATCH /api/v1/admin/conf; ≤30s propagation.
   */
  toggleStub: async ({ adminApi }, use) => {
    const toggle = async (field: StubField, value: boolean): Promise<Record<string, unknown>> => {
      const patchResp = await adminApi.patch("/api/v1/admin/conf", {
        data: { [field]: value },
      });
      expect(
        patchResp.ok(),
        `PATCH /admin/conf {${field}: ${value}} failed: ${await patchResp.text()}`
      ).toBeTruthy();

      const getResp = await adminApi.get("/api/v1/admin/conf");
      expect(getResp.ok(), `GET /admin/conf failed: ${await getResp.text()}`).toBeTruthy();
      return (await getResp.json()) as Record<string, unknown>;
    };
    await use(toggle);
  },
});

export { expect } from "@playwright/test";
