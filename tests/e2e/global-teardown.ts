/**
 * Playwright global teardown.
 *
 * Steps (mirrors tests/integration/conftest.py acquire_lock teardown):
 *   1. Reset-seed to restore baseline (per spec/TESTING.md §Workflow step 6)
 *   2. Release dev-env lock (unless DATASPOKE_DEV_LOCK_PREACQUIRED)
 *
 * Reuses the existing Python utilities — no TS reimplementation.
 *
 * Source: tests/integration/conftest.py acquire_lock fixture teardown path.
 */

import { execSync } from "child_process";
import * as path from "path";
import type { FullConfig } from "@playwright/test";
import { loadDotenv, lockUrl, lockOwner } from "./fixtures/env";

function resetSeed(): void {
  console.log("[e2e teardown] Running --reset-seed to restore baseline...");
  const repoRoot = path.resolve(__dirname, "..", "..");
  try {
    execSync("uv run python -m tests.integration.util --reset-seed", {
      cwd: repoRoot,
      stdio: "inherit",
      timeout: 300_000,
    });
    console.log("[e2e teardown] Reset-seed complete.");
  } catch (err) {
    // Log but don't rethrow — we still want to release the lock.
    console.error("[e2e teardown] WARNING: reset-seed failed:", err);
  }
}

async function releaseLock(): Promise<void> {
  if (process.env["DATASPOKE_DEV_LOCK_PREACQUIRED"]) {
    console.log("[e2e teardown] Lock pre-acquired; skipping release.");
    return;
  }
  const url = lockUrl();
  const owner = lockOwner();
  console.log(`[e2e teardown] Releasing dev-env lock at ${url} (owner: ${owner})...`);
  try {
    const resp = await fetch(`${url}/lock/release`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ owner }),
    });
    if (resp.ok) {
      console.log("[e2e teardown] Lock released.");
    } else {
      console.warn(`[e2e teardown] Lock release returned ${resp.status}: ${await resp.text()}`);
    }
  } catch (err) {
    // Non-fatal — the lock has a TTL; this is best-effort.
    console.warn("[e2e teardown] Lock release request failed (network error):", err);
  }
}

export default async function globalTeardown(_config: FullConfig): Promise<void> {
  loadDotenv();

  resetSeed();
  await releaseLock();

  console.log("[e2e teardown] Global teardown complete.");
}
