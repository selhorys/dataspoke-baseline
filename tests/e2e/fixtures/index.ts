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

import * as path from "path";
import { test as base, type APIRequestContext, expect } from "@playwright/test";
import { apiBaseUrl, ADMIN_EMAIL, ADMIN_PASSWORD, type StubField, IMAZON_URNS } from "./env";

export { IMAZON_URNS };

const AUTH_DIR = path.join(__dirname, "..", ".auth");

/** storageState files keyed by role (matching playwright.config.ts project names) */
export const STORAGE_STATE: Record<string, string> = {
  admin: path.join(AUTH_DIR, "admin.json"),
  editor: path.join(AUTH_DIR, "editor.json"),
  reader: path.join(AUTH_DIR, "reader.json"),
};

// ── Extended fixture type ─────────────────────────────────────────────────────

type E2EFixtures = {
  /** APIRequestContext carrying a fresh admin bearer token. */
  adminApi: APIRequestContext;
  /** Toggle a stub_* field via PATCH /api/v1/admin/conf; read it back via GET. */
  toggleStub: (field: StubField, value: boolean) => Promise<Record<string, unknown>>;
};

// ── Fixture implementations ───────────────────────────────────────────────────

export const test = base.extend<E2EFixtures>({
  /**
   * Fresh admin bearer token obtained at fixture setup time.
   * Each test gets its own APIRequestContext so tokens don't bleed across tests.
   *
   * Source: tests/integration/api_wired/conftest.py admin_token fixture.
   */
  adminApi: async ({ playwright }, use) => {
    const base = apiBaseUrl();

    // Obtain admin token
    const tokenResp = await playwright.request.newContext({ baseURL: base });
    const tokenRes = await tokenResp.post("/api/v1/auth/token", {
      data: { email: ADMIN_EMAIL, password: ADMIN_PASSWORD },
    });
    expect(tokenRes.ok(), `Admin token request failed: ${await tokenRes.text()}`).toBeTruthy();
    const { access_token: adminToken } = (await tokenRes.json()) as { access_token: string };
    await tokenResp.dispose();

    const ctx = await playwright.request.newContext({
      baseURL: base,
      extraHTTPHeaders: {
        Authorization: `Bearer ${adminToken}`,
        "Content-Type": "application/json",
        Accept: "application/json",
      },
    });
    await use(ctx);
    await ctx.dispose();
  },

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
