/**
 * Playwright configuration for DataSpoke E2E tests.
 *
 * baseURL: PLAYWRIGHT_BASE_URL env override, else http://app.<INGRESS_DOMAIN>
 *   INGRESS_DOMAIN is read from DATASPOKE_KUBE_INGRESS_DOMAIN (in helm-charts/.env).
 *
 * Projects are keyed on role (admin / editor / reader), each pointing at its
 * per-role storageState. The refresh token (HttpOnly cookie) is captured in
 * storageState by global-setup.ts; SilentRefresh in providers.tsx restores the
 * in-memory access token on page load via POST /auth/token/refresh.
 *
 * spec: spec/TESTING.md §End-to-End (E2E) Testing
 */

import * as path from "path";
import { defineConfig, devices } from "@playwright/test";
import { loadDotenv, appBaseUrl } from "./fixtures/env";

// Load helm-charts/.env so DATASPOKE_KUBE_INGRESS_DOMAIN is available at config time.
loadDotenv();

const AUTH_DIR = path.join(__dirname, ".auth");

export default defineConfig({
  testDir: ".",
  // Role assignment by filename convention: `*.editor.spec.ts` / `*.reader.spec.ts`
  // run under the editor / reader project; every other `*.spec.ts` runs under admin
  // only (the full-write role). See per-project testMatch/testIgnore below.
  testMatch: ["use-case/**/*.spec.ts", "ground/**/*.spec.ts"],

  globalSetup: "./global-setup.ts",
  globalTeardown: "./global-teardown.ts",

  /* Reasonable defaults for a full-stack cluster environment */
  timeout: 60_000,
  expect: { timeout: 10_000 },
  retries: 1,
  workers: 1, // sequential — shares dev-env lock with integration tests

  reporter: [["html", { open: "never" }], ["list"]],

  use: {
    baseURL: appBaseUrl(),
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "off",
  },

  projects: [
    {
      name: "admin",
      // Everything except the role-specific ground specs.
      testIgnore: ["**/*.editor.spec.ts", "**/*.reader.spec.ts"],
      use: {
        ...devices["Desktop Chrome"],
        storageState: path.join(AUTH_DIR, "admin.json"),
      },
    },
    {
      name: "editor",
      testMatch: ["**/*.editor.spec.ts"],
      use: {
        ...devices["Desktop Chrome"],
        storageState: path.join(AUTH_DIR, "editor.json"),
      },
    },
    {
      name: "reader",
      testMatch: ["**/*.reader.spec.ts"],
      use: {
        ...devices["Desktop Chrome"],
        storageState: path.join(AUTH_DIR, "reader.json"),
      },
    },
  ],
});
