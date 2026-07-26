/**
 * Environment loader and typed accessors for E2E tests.
 *
 * Mirrors the Python _load_dotenv() pattern from
 * tests/integration/conftest.py: reads helm-charts/.env.dev into process.env
 * without overwriting existing values. Searches upward from the repo root
 * to handle git worktrees.
 *
 * Call loadDotenv() once (in global-setup.ts) before accessing any accessor.
 */

import * as fs from "fs";
import * as path from "path";

/**
 * Load helm-charts/.env.dev into process.env without overwriting existing vars.
 * Searches upward from this file's location to find the repo root, matching
 * the worktree-aware logic in tests/integration/conftest.py _load_dotenv().
 */
export function loadDotenv(): void {
  // Walk upward from tests/e2e/fixtures/ to find helm-charts/.env.dev
  let dir = path.resolve(__dirname);
  while (true) {
    const candidate = path.join(dir, "helm-charts", ".env.dev");
    if (fs.existsSync(candidate)) {
      const content = fs.readFileSync(candidate, "utf-8");
      for (const raw of content.split("\n")) {
        const line = raw.trim();
        if (!line || line.startsWith("#")) continue;
        const eqIdx = line.indexOf("=");
        if (eqIdx === -1) continue;
        const key = line.slice(0, eqIdx).trim();
        const value = line.slice(eqIdx + 1).trim();
        if (key && !(key in process.env)) {
          process.env[key] = value;
        }
      }
      return;
    }
    const parent = path.dirname(dir);
    if (parent === dir) break; // reached filesystem root
    dir = parent;
  }
  // Not finding .env is non-fatal in CI with vars already exported.
}

// ── Typed accessors (throw clearly when a required var is absent) ─────────────

export function required(key: string): string {
  const v = process.env[key];
  if (!v) throw new Error(`Required env var ${key} is not set. Source helm-charts/.env.dev first.`);
  return v;
}

function optional(key: string, fallback = ""): string {
  return process.env[key] ?? fallback;
}

/** e.g. "34.64.35.130.nip.io" */
export function ingressDomain(): string {
  return required("DATASPOKE_KUBE_INGRESS_DOMAIN");
}

/** e.g. "34.64.35.130" */
export function ingressIp(): string {
  return required("DATASPOKE_KUBE_INGRESS_IP");
}

/** Browser baseURL: PLAYWRIGHT_BASE_URL env override, else http://app.<domain> */
export function appBaseUrl(): string {
  return optional("PLAYWRIGHT_BASE_URL") || `http://app.${ingressDomain()}`;
}

/** API baseURL used by APIRequestContext and global-setup provisioning */
export function apiBaseUrl(): string {
  return `http://api.${ingressDomain()}`;
}

/** Bootstrap admin credentials (matches conftest.py runtime_conf fixture) */
export const ADMIN_EMAIL = "dataspoke@dataspoke.local";
export const ADMIN_PASSWORD = "dataspoke";

/** Internal token for /internal/* routes */
export function internalToken(): string {
  return required("DATASPOKE_TEST_INTERNAL_TOKEN");
}

/** Lock service URL: DATASPOKE_TEST_LOCK_URL override, else http://<ip>:9221 */
export function lockUrl(): string {
  return optional("DATASPOKE_TEST_LOCK_URL") || `http://${ingressIp()}:9221`;
}

/** Lock owner identifier: DATASPOKE_TEST_LOCK_OWNER override, else e2e-test-<USER> */
export function lockOwner(): string {
  return (
    optional("DATASPOKE_TEST_LOCK_OWNER") ||
    `e2e-test-${optional("USER", "unknown")}`
  );
}

/**
 * Canonical Imazon dataset URN used across E2E tests.
 * Mirrors the URN constants in tests/integration/api_wired/ files.
 * Platform: postgres, instance: example_db, env: DEV.
 */
export const IMAZON_URNS = {
  titleMaster: "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)",
  editions: "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)",
  euProfiles: "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.customers.eu_profiles,DEV)",
  userRatings: "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.reviews.user_ratings,DEV)",
  dailyFulfillment:
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.orders.daily_fulfillment_summary,DEV)",
  carrierStatus:
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.shipping.carrier_status,DEV)",
} as const;

// No `stub_*` field type lives here. The four toggles are dev-env-wide settings owned by
// the profile seed and the operator; a test reads them straight off GET /admin/conf to
// gate an LLM variant and never writes them, so no typed write-surface is offered.
// spec: spec/TESTING.md §End-to-End (E2E) Testing §Execution discipline — "Never flip the
//   stub toggles… but never sets them."
