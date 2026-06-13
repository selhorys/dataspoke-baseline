/**
 * Playwright global setup.
 *
 * Steps (mirrors tests/integration/conftest.py acquire_lock + dummy_data_reset):
 *   1. Load helm-charts/.env
 *   2. Acquire dev-env lock (honours DATASPOKE_DEV_ENV_LOCK_PREACQUIRED)
 *   3. Reset + seed dummy data via Python utilities
 *   4. Provision editor + reader users via admin API
 *   5. Log in once per role, save storageState JSON to tests/e2e/.auth/
 *
 * Lock/reset reuse the existing Python utilities — no TS reimplementation.
 *
 * storageState rationale: the refresh token is an HttpOnly cookie. Playwright
 * persists cookies in storageState. On page load, providers.tsx SilentRefresh
 * calls POST /auth/token/refresh with credentials:"include", restoring the
 * in-memory access token from the saved cookie. storageState is therefore
 * sufficient for authenticated page loads.
 *
 * Source: src/frontend/app/providers.tsx SilentRefresh + lib/api/client.ts
 * ensureFreshToken() — confirmed the app refreshes on load from the cookie.
 */

import { execSync, execFileSync } from "child_process";
import * as fs from "fs";
import * as path from "path";
import { chromium, type FullConfig } from "@playwright/test";
import { loadDotenv, apiBaseUrl, lockUrl, lockOwner, appBaseUrl, ADMIN_EMAIL, ADMIN_PASSWORD } from "./fixtures/env";

const AUTH_DIR = path.join(__dirname, ".auth");

/** E2E test users provisioned during setup. Passwords are deterministic so
 *  global-teardown/next-run can rely on them. */
const TEST_USERS = [
  // .local is rejected by the API's EmailStr validator (special-use domain); use the
  // same example.com subdomain the spot auth tests register under.
  { email: "e2e-editor@test.dataspoke.example.com", name: "E2E Editor", password: "e2e-editor-password", role: "Editor" },
  { email: "e2e-reader@test.dataspoke.example.com", name: "E2E Reader", password: "e2e-reader-password", role: "Reader" },
] as const;

/** storageState file per role — keyed to match playwright.config.ts projects */
const STORAGE_STATE_FILES: Record<string, string> = {
  admin: path.join(AUTH_DIR, "admin.json"),
  editor: path.join(AUTH_DIR, "editor.json"),
  reader: path.join(AUTH_DIR, "reader.json"),
};

/** Long-lived admin API token for backend probes — minted once here, read by the
 *  worker-scoped adminApi fixture. Keeps /auth/token calls to a handful per run
 *  (well under the 10/min limit) and avoids 15-min access-token expiry mid-run. */
const ADMIN_API_TOKEN_FILE = path.join(AUTH_DIR, "admin-api-token.txt");

async function acquireLock(): Promise<void> {
  if (process.env["DATASPOKE_DEV_ENV_LOCK_PREACQUIRED"]) {
    console.log("[e2e setup] Lock pre-acquired; skipping acquire.");
    return;
  }
  const url = lockUrl();
  const owner = lockOwner();
  console.log(`[e2e setup] Acquiring dev-env lock at ${url} (owner: ${owner})...`);
  // Retry transient connect failures (cluster blips), but never retry a 409 —
  // that is a real "held by another tester" signal.
  const maxAttempts = 3;
  let resp: Response | undefined;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      resp = await fetch(`${url}/lock/acquire`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ owner, message: "playwright e2e test suite" }),
      });
      break;
    } catch (err) {
      if (attempt === maxAttempts) throw err;
      console.warn(
        `[e2e setup] lock acquire attempt ${attempt}/${maxAttempts} failed to connect (transient); retrying...`
      );
    }
  }
  if (resp!.status === 409) {
    throw new Error(
      `Dev-env lock is held by another process. ` +
        `Release it first: DELETE ${url}/lock\n` +
        `Or set DATASPOKE_DEV_ENV_LOCK_PREACQUIRED=1 if you hold it externally.`
    );
  }
  if (!resp!.ok) {
    throw new Error(`Lock acquire failed: ${resp!.status} ${await resp!.text()}`);
  }
  console.log("[e2e setup] Lock acquired.");
}

function resetSeed(): void {
  console.log("[e2e setup] Running --reset-seed via Python utilities...");
  // Reuses the existing Python util — no TS reimplementation of reset logic.
  // Runs from the repo root (two levels above tests/e2e/).
  const repoRoot = path.resolve(__dirname, "..", "..");
  // The dev cluster (GKE Autopilot) has intermittent connectivity drops that make
  // reset-seed fail mid-ingest (DataHub/PG timeouts). Retry a few times before
  // giving up — this is transient-blip resilience, not a timeout bump.
  const maxAttempts = 3;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      execSync("uv run python -m tests.integration.util --reset-seed", {
        cwd: repoRoot,
        stdio: "inherit",
        timeout: 300_000, // 5 min
      });
      console.log("[e2e setup] Reset-seed complete.");
      return;
    } catch (err) {
      if (attempt === maxAttempts) throw err;
      console.warn(
        `[e2e setup] reset-seed attempt ${attempt}/${maxAttempts} failed (likely a transient cluster blip); retrying...`
      );
    }
  }
}

async function getAdminToken(): Promise<string> {
  const base = apiBaseUrl();

  // Best-effort bootstrap so the admin account exists after a --reset-all.
  const internalToken = process.env["DATASPOKE_TEST_INTERNAL_TOKEN"];
  if (internalToken) {
    try {
      await fetch(`${base}/internal/admin/bootstrap`, {
        method: "POST",
        headers: { "X-Internal-Token": internalToken, "Content-Type": "application/json" },
        body: "{}",
      });
    } catch {
      // best-effort — token login below surfaces real failures
    }
  }

  const resp = await fetch(`${base}/api/v1/auth/token`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: ADMIN_EMAIL, password: ADMIN_PASSWORD }),
  });
  if (!resp.ok) {
    throw new Error(`Admin token fetch failed: ${resp.status} ${await resp.text()}`);
  }
  const data = (await resp.json()) as { access_token: string };
  return data.access_token;
}

/**
 * Flush slowapi rate-limit keys from Redis before hitting /auth/register and
 * /auth/token. The slowapi limiter wires its own Redis connection from
 * settings.redis_host/port/password (rate_limit.py) — independent of the
 * stub_redis_client toggle — so rate limits apply even when the app is in
 * stub mode. Mirrors the _flush_rate_limit_keys autouse fixture in
 * tests/integration/conftest.py. Uses execSync + Python to avoid adding a
 * redis npm dependency to the e2e project.
 */
function flushRateLimitKeys(): void {
  const host = process.env["DATASPOKE_TEST_REDIS_HOST"];
  const port = process.env["DATASPOKE_TEST_REDIS_PORT"] ?? "6379";
  const password = process.env["DATASPOKE_TEST_REDIS_PASSWORD"] ?? "";
  if (!host) {
    console.log("[e2e setup] DATASPOKE_TEST_REDIS_HOST not set; skipping rate-limit key flush.");
    return;
  }
  console.log("[e2e setup] Flushing slowapi rate-limit keys from Redis...");
  const repoRoot = path.resolve(__dirname, "..", "..");
  const script = [
    "import os, redis as r",
    "c = r.Redis(host=os.environ['E2E_REDIS_HOST'], port=int(os.environ['E2E_REDIS_PORT']), password=os.environ.get('E2E_REDIS_PASSWORD') or None)",
    "keys = list(c.scan_iter('LIMITS:LIMITER/*'))",
    "[c.delete(k) for k in keys]",
    "c.close()",
    "print(f'[e2e setup] Flushed {len(keys)} rate-limit key(s).')",
  ].join("; ");
  try {
    // execFileSync (no shell) + env-passed connection params avoids any quoting
    // collision between the shell wrapper and the interpolated values.
    execFileSync("uv", ["run", "python", "-c", script], {
      cwd: repoRoot,
      stdio: "inherit",
      timeout: 10_000,
      env: { ...process.env, E2E_REDIS_HOST: host, E2E_REDIS_PORT: port, E2E_REDIS_PASSWORD: password },
    });
  } catch {
    // Non-fatal — if Redis is unreachable the limiter falls back to in-memory
    // and a 429 during provisioning will surface as a clearer error than a flush failure.
    console.warn("[e2e setup] Rate-limit key flush failed (non-fatal).");
  }
}

async function provisionTestUsers(adminToken: string): Promise<void> {
  const base = apiBaseUrl();
  console.log("[e2e setup] Provisioning E2E test users...");

  for (const user of TEST_USERS) {
    // Register the user (idempotent: 409 means already exists — OK).
    const regResp = await fetch(`${base}/api/v1/auth/register`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: user.email, name: user.name, password: user.password }),
    });
    if (regResp.status !== 201 && regResp.status !== 409) {
      throw new Error(
        `Failed to register ${user.email}: ${regResp.status} ${await regResp.text()}`
      );
    }

    // Look up the user's ID so we can promote the role if needed.
    // limit=100 is the route's maximum (Query le=100 in src/api/routers/admin.py).
    const listResp = await fetch(
      `${base}/api/v1/admin/users?limit=100`,
      { headers: { Authorization: `Bearer ${adminToken}` } }
    );
    if (!listResp.ok) {
      throw new Error(`GET /admin/users failed: ${listResp.status}`);
    }
    const listBody = (await listResp.json()) as { users: Array<{ id: string; email: string; role: string }> };
    const found = listBody.users.find((u) => u.email === user.email);
    if (!found) throw new Error(`User ${user.email} not found after registration`);

    if (found.role !== user.role) {
      const roleResp = await fetch(`${base}/api/v1/admin/users/${found.id}/role`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", Authorization: `Bearer ${adminToken}` },
        body: JSON.stringify({ role: user.role }),
      });
      if (!roleResp.ok) {
        throw new Error(`Role promotion for ${user.email} failed: ${roleResp.status} ${await roleResp.text()}`);
      }
    }
    console.log(`[e2e setup] ${user.email} ready (role: ${user.role})`);
  }
}

async function loginAndSaveState(
  email: string,
  password: string,
  outputFile: string
): Promise<void> {
  const browser = await chromium.launch();
  const context = await browser.newContext({ baseURL: appBaseUrl() });
  const page = await context.newPage();

  // Navigate to the login page and complete the form.
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  // getByLabel("Password") also matches the "Show password" toggle button
  // (substring accessible-name match); target the unique input by id instead.
  await page.locator("input#password").fill(password);
  // Exact match — /sign in/i also matches "Sign in with Google".
  await page.getByRole("button", { name: "Sign in", exact: true }).click();

  // Wait for post-login redirect (to /governance/dashboard).
  await page.waitForURL("**/governance/dashboard", { timeout: 30_000 });

  // Persist cookies (includes HttpOnly refresh token) + localStorage.
  await context.storageState({ path: outputFile });
  console.log(`[e2e setup] storageState saved: ${outputFile}`);

  await browser.close();
}

/**
 * Mint a long-lived `dsk_` API token (POST /auth/api-tokens) using the admin
 * access token, and write the raw token to ADMIN_API_TOKEN_FILE. The worker-scoped
 * adminApi fixture reads this file, so per-test/per-worker logins are avoided —
 * the run makes only the handful of /auth/token calls in this setup. The
 * global-teardown --reset-seed wipes api_tokens, so tokens never accumulate.
 */
async function mintAdminApiToken(adminToken: string): Promise<void> {
  const base = apiBaseUrl();
  console.log("[e2e setup] Minting long-lived admin API token for probes...");
  const resp = await fetch(`${base}/api/v1/auth/api-tokens`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${adminToken}` },
    body: JSON.stringify({ name: "e2e-probe" }),
  });
  if (!resp.ok) {
    throw new Error(`API token mint failed: ${resp.status} ${await resp.text()}`);
  }
  const { token } = (await resp.json()) as { token: string };
  fs.writeFileSync(ADMIN_API_TOKEN_FILE, token, { encoding: "utf-8" });
  console.log(`[e2e setup] Admin API token saved: ${ADMIN_API_TOKEN_FILE}`);
}

export default async function globalSetup(_config: FullConfig): Promise<void> {
  // 1. Load env
  loadDotenv();

  // 2. Ensure .auth dir exists
  fs.mkdirSync(AUTH_DIR, { recursive: true });

  // 3. Acquire dev-env lock
  await acquireLock();

  // 4. Reset + seed dummy data (reuses Python util)
  resetSeed();

  // 5. Provision test users (flush rate-limit keys first to avoid 429 bleed)
  flushRateLimitKeys();
  const adminToken = await getAdminToken();
  await provisionTestUsers(adminToken);

  // 5b. Mint a long-lived admin API token for the worker-scoped adminApi fixture.
  await mintAdminApiToken(adminToken);

  // 6. Login per role and save storageState
  console.log("[e2e setup] Logging in per role and saving storageState...");
  await loginAndSaveState(ADMIN_EMAIL, ADMIN_PASSWORD, STORAGE_STATE_FILES["admin"]!);
  for (const user of TEST_USERS) {
    const role = user.role.toLowerCase();
    await loginAndSaveState(user.email, user.password, STORAGE_STATE_FILES[role]!);
  }

  console.log("[e2e setup] Global setup complete.");
}
