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
 * Also provided: `readStubLlmClient`, a READ-ONLY probe of the dev-env-wide
 * `stub_llm_client` toggle used to gate the UC3/UC4 real-LLM variants, plus
 * `resolveUnreadableStubLlmClient` / `describeStubLlmClient` for consuming that read at a
 * gate or reporting it in a diagnostic. The four `stub_*` fields are settings owned by the
 * profile seed and the operator; a test reads them to gate an LLM variant and never sets them.
 * spec: spec/TESTING.md §End-to-End (E2E) Testing §Execution discipline — "Never flip the
 *   stub toggles… A test may read them to gate an LLM variant and must assert them
 *   unchanged after any `/admin/conf` write, but never sets them."
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — dual confirmation via an independent
 *   APIRequestContext probe; §Test Data Design — Imazon is the canonical company context.
 */

import * as fs from "fs";
import * as path from "path";
import {
  test as base,
  type APIRequestContext,
  type APIResponse,
  expect,
} from "@playwright/test";
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

// ── stub_llm_client read (LLM-variant gate) ───────────────────────────────────

/** Path of the runtime-config resource carrying the four `stub_*` toggles. */
const ADMIN_CONF_PATH = "/api/v1/admin/conf";

/**
 * Outcome of reading `stub_llm_client` from `GET /api/v1/admin/conf`.
 *
 * A discriminated union, so a caller cannot conflate "the toggle is on" with "the toggle
 * could not be read". The two cases carry different obligations: a readable `stubbed: true`
 * gates an LLM variant, while `readable: false` is an absent precondition the caller skips
 * on, quoting `reason` verbatim.
 */
export type StubLlmClientRead =
  | { readable: true; stubbed: boolean }
  | { readable: false; kind: "unreachable"; reason: string }
  | { readable: false; kind: "defect"; reason: string };

/** An unreadable read, narrowed. */
export type UnreadableStubLlmClient = Extract<StubLlmClientRead, { readable: false }>;

/**
 * Builds an `unreachable` read: nothing answered at `/admin/conf`, so the LLM mode is an
 * absent precondition. The reason names the precondition, says how to supply it, and makes
 * no claim about the toggle's value.
 * spec: spec/TESTING.md §Assertion Discipline — "Skip only on an absent precondition…
 *   the skip reason names the precondition and how to supply it."
 */
function unreachableStubLlmClient(detail: string): UnreadableStubLlmClient {
  return {
    readable: false,
    kind: "unreachable",
    reason:
      `could not reach GET ${ADMIN_CONF_PATH} (${detail}), so the LLM mode this step is ` +
      "gated on is unknown — this says nothing about stub_llm_client's value. Supply the " +
      "precondition by making the DataSpoke API reachable: run " +
      "./helm-charts/bin/health-check.sh, redeploy with " +
      "./helm-charts/bin/install.sh --profile dev --components api if it fails, then re-run.",
  };
}

/**
 * Builds a `defect` read: the admin route ANSWERED, but with an error status or an
 * off-contract body. That is a product failure, not a missing precondition, so callers
 * fail on it instead of skipping — a 403 or a 500 from a live route must not be laundered
 * into a green-with-skips run.
 * spec: spec/TESTING.md §Assertion Discipline — "A test never skips on an outcome it
 *   exists to judge."
 * spec: spec/API.md §Admin — `GET /admin/conf` → "runtime config (behavioral tunables +
 *   `updated_at`)".
 */
function defectiveStubLlmClient(detail: string): UnreadableStubLlmClient {
  return {
    readable: false,
    kind: "defect",
    reason:
      `GET ${ADMIN_CONF_PATH} answered but did not honour its contract (${detail}). The ` +
      "route is live, so this is a product failure of the admin runtime-config surface, " +
      "not an absent precondition — it is reported as a failure rather than a skip.",
  };
}

/**
 * Resolves an unreadable read AT THE GATE THAT CONSUMES IT.
 *
 * `defect` throws (the route is live and misbehaving). `unreachable` skips the calling scope,
 * quoting the reason verbatim — the single test when called from a test body, the whole suite
 * when called from a `beforeAll` (Playwright replays a hook's skip annotation onto every test
 * in that suite). Call it from a hook only when the precondition gates every test in the suite
 * (see uc3-01's real-LLM arc); otherwise call it at the consuming step, so mode-agnostic steps
 * are not taken down with the one or two that actually consume the toggle.
 *
 * Returns `never` so TypeScript narrows the caller's read to `readable: true` afterwards.
 *
 * spec: spec/TESTING.md §Assertion Discipline — "A test skips when a precondition it cannot
 *   establish is missing… A test never skips on an outcome it exists to judge."
 */
export function resolveUnreadableStubLlmClient(
  read: UnreadableStubLlmClient,
  gate: string
): never {
  if (read.kind === "defect") {
    throw new Error(`[${gate}] ${read.reason}`);
  }
  test.skip(true, `${gate} is gated on the LLM mode: ${read.reason}`);
  // test.skip(true, …) aborts the test, so control never reaches here; the throw exists so
  // the signature can be `never` and callers get the narrowing.
  throw new Error(`[${gate}] ${read.reason}`);
}

/**
 * One-line label of a read, for failure diagnostics that REPORT the mode without gating on
 * it. An unread or unreadable toggle is labelled as such — never as a concrete value.
 */
export function describeStubLlmClient(read: StubLlmClientRead | null): string {
  if (read === null) return "stub_llm_client=unread (the arc's /admin/conf read did not run)";
  return read.readable
    ? `stub_llm_client=${read.stubbed}`
    : `stub_llm_client=unknown (${read.reason})`;
}

/**
 * Reads the dev-env-wide `stub_llm_client` toggle. Read-only — never writes `/admin/conf`.
 *
 * Every failure mode returns `readable: false`, split by whether the route answered:
 * a transport error or a 404 is `unreachable` (an absent precondition the caller skips on),
 * while any other error status, a non-JSON body, or a missing/non-boolean field is a
 * `defect` (a live route breaking its contract, which the caller fails on). There is no
 * fail-safe default: reporting "stub mode" when the truth is "could not ask" would gate a
 * real-LLM variant off a fabricated value.
 *
 * spec: spec/TESTING.md §Stub Toggles (RuntimeConfig) — `stub_llm_client` is a
 *   RuntimeConfig field exposed at `/api/v1/admin/conf`.
 * spec: spec/TESTING.md §E2E §Two groups — "UC3/UC4 each carry a stub-mode variant and a
 *   gated real-LLM variant — the real-LLM variant `test.skip`s unless `stub_llm_client` is
 *   false in `/admin/conf`."
 */
export async function readStubLlmClient(
  adminApi: APIRequestContext
): Promise<StubLlmClientRead> {
  let resp: APIResponse;
  try {
    resp = await adminApi.get(ADMIN_CONF_PATH);
  } catch (err) {
    return unreachableStubLlmClient(
      `the request itself failed: ${err instanceof Error ? err.message : String(err)}`
    );
  }
  if (!resp.ok()) {
    // 404 means no admin surface is deployed at this base URL — nothing answered the
    // question. Any other error status came FROM the route and is a defect.
    const detail = `HTTP ${resp.status()} ${resp.statusText()}`;
    return resp.status() === 404
      ? unreachableStubLlmClient(detail)
      : defectiveStubLlmClient(detail);
  }
  let body: unknown;
  try {
    body = await resp.json();
  } catch (err) {
    return defectiveStubLlmClient(
      `the response body was not JSON: ${err instanceof Error ? err.message : String(err)}`
    );
  }
  const value = (body as Record<string, unknown> | null)?.["stub_llm_client"];
  if (typeof value !== "boolean") {
    return defectiveStubLlmClient(
      `the response carried no boolean stub_llm_client field (got ${JSON.stringify(value)})`
    );
  }
  return { readable: true, stubbed: value };
}
