/**
 * Ground spec: app-shell peripheral links resolve from the `peripheral_config`
 * DB plane, with the chart-injected env value taking precedence.
 *
 * Concern: after `PATCH /admin/peripherals/datahub {frontend_url}` — a pure
 * DB-plane operation, no `helm upgrade` and no pod restart — the header DataHub
 * icon and the per-dataset DataHub deep-link both resolve to the new host. This
 * is the regression the feature was filed on: DataHub was fully wired through
 * the admin API and the UI still showed no link, because the browser-facing URL
 * lived only in chart values and could not be derived from `gms_url`.
 *
 * Arrangement — why an init script:
 * The frontend resolves these links **env-first** (`getRuntimeConfig()` over the
 * API), and the cluster frontend is deployed with `DATASPOKE_DATAHUB_URL` set by
 * chart values, so on a stock install the env value always wins and the API path
 * is never taken. The DB-fallback test therefore installs an init script that
 * intercepts the root layout's `window.__DATASPOKE_RUNTIME_CONFIG__` assignment
 * and blanks `datahubUrl`, reproducing an install that never set the env var —
 * without mutating the cluster. The precedence test runs unmodified and asserts
 * the opposite direction, so both branches of the merge are covered.
 *
 * Group placement: ground, not use-case. `USE_CASE_en.md` carries no
 * peripheral-wiring narrative, and this is a narrow single-page (app-shell)
 * behavior — the spot analogue.
 *
 * spec: spec/API.md §Data Resource — `GET /spoke/common/peripheral-links`:
 *   `datahub_url` ⟵ `datahub.frontend_url` (the browser-facing UI URL, **never**
 *   `gms_url`); an unconfigured peripheral yields `""`, read as "render no link".
 * spec: spec/feature/FRONTEND_BASIC.md §Shell — peripheral-sourced links resolve
 *   env-first then API; the link renders only when the URL is set.
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — dual confirmation
 *   (UI assertion + independent REST read-back), semantic-first selectors.
 */

import type { Page } from "@playwright/test";
import { test, expect, IMAZON_URNS } from "../../fixtures/index";

// ── Constants ─────────────────────────────────────────────────────────────────

/**
 * Deliberately unlike any dev-cluster value, and unlike `gms_url` in host, port,
 * AND scheme — mirroring the reported deployment where GMS is an internal
 * plain-HTTP ELB and the UI a public TLS hostname. A URL derived from `gms_url`
 * can therefore never coincidentally equal this.
 */
const SENTINEL_FRONTEND_URL = "https://datahub-ui.imazon-e2e.example.com:8443";

const ADMIN_DATAHUB = "/api/v1/admin/peripherals/datahub";
const PERIPHERAL_LINKS = "/api/v1/spoke/common/peripheral-links";

interface DatahubPeripheral {
  gms_url: string;
  frontend_url: string;
  kafka_brokers: string;
  token: string;
  service_corpuser_urn: string;
  default_env: string;
  is_configured: boolean;
  updated_at: string | null;
}

interface PeripheralLinks {
  datahub_url: string;
  langfuse_url: string;
  langfuse_project_id: string;
}

/**
 * Read the `datahubUrl` the root layout INJECTED into `window.__DATASPOKE_RUNTIME_CONFIG__`.
 *
 * This is not the fully-resolved env value: `getRuntimeConfig()` resolves
 * `w.datahubUrl || process.env.NEXT_PUBLIC_DATAHUB_URL || ""`
 * (`src/frontend/lib/runtime-config.ts`), and the build-time fallback is inlined
 * into the client bundle where it cannot be read back. Used only as a setup
 * backstop confirming the init script below intercepted the injection; the
 * load-bearing check in each test is the rendered `href`, which is fail-safe — a
 * `NEXT_PUBLIC_DATAHUB_URL` in the bundle would surface as a failed href
 * assertion, not as a false pass. Test 3 deliberately does not use this helper.
 *
 * `globalThis` is `window` in the browser; the E2E tsconfig has no DOM lib, so
 * the callback avoids naming `window` directly.
 */
async function readEnvDatahubUrl(page: Page): Promise<string> {
  return page.evaluate(() => {
    const cfg = (globalThis as unknown as Record<string, unknown>)[
      "__DATASPOKE_RUNTIME_CONFIG__"
    ] as Record<string, unknown> | undefined;
    return (cfg?.datahubUrl as string | undefined) ?? "";
  });
}

// ── Module state ──────────────────────────────────────────────────────────────

/** Original DataHub frontend_url; restored in afterEach and afterAll. */
let originalFrontendUrl: string | null = null;

/**
 * Restore the peripheral baseline. Asserted, not assumed — a silent failed
 * restore would leave every later spec running against a corrupted config.
 *
 * spec: spec/TESTING.md §Integration Lifecycle & Isolation — "Snapshot → mutate
 *   → verified restore ... The restore is **asserted**, not assumed".
 */
test.afterEach(async ({ adminApi }) => {
  if (originalFrontendUrl === null) return;
  const resp = await adminApi.patch(ADMIN_DATAHUB, {
    data: { frontend_url: originalFrontendUrl },
  });
  expect(resp.status(), `restore PATCH failed: ${await resp.text()}`).toBe(200);
  const restored = (await resp.json()) as DatahubPeripheral;
  expect(restored.frontend_url, "frontend_url was not restored to its snapshot").toBe(
    originalFrontendUrl,
  );
  originalFrontendUrl = null;
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 1 — DB fallback: with no env value, the header link comes from the API
// The regression the feature was filed on.
// ─────────────────────────────────────────────────────────────────────────────

test("header DataHub icon resolves from peripheral_config when no env URL is set", async ({
  page,
  adminApi,
}) => {
  // -- Snapshot, then write the sentinel purely through the DB plane --
  const preResp = await adminApi.get(ADMIN_DATAHUB);
  expect(preResp.status()).toBe(200);
  originalFrontendUrl = ((await preResp.json()) as DatahubPeripheral).frontend_url;

  const patchResp = await adminApi.patch(ADMIN_DATAHUB, {
    data: { frontend_url: SENTINEL_FRONTEND_URL },
  });
  expect(patchResp.status(), `PATCH frontend_url failed: ${await patchResp.text()}`).toBe(200);

  // -- Backend probe (dual confirmation): the endpoint serves the new value --
  // spec: spec/API.md §Data Resource — datahub_url ⟵ datahub.frontend_url.
  const linksResp = await adminApi.get(PERIPHERAL_LINKS);
  expect(linksResp.status()).toBe(200);
  const links = (await linksResp.json()) as PeripheralLinks;
  expect(links.datahub_url).toBe(SENTINEL_FRONTEND_URL);

  // -- Simulate an install with DATASPOKE_DATAHUB_URL unset --
  // The root layout assigns window.__DATASPOKE_RUNTIME_CONFIG__ from a blocking
  // inline <script>. An init script runs before it, so rather than assigning the
  // object (which the layout would overwrite) we intercept the assignment and
  // strip the one field under test, leaving apiBaseUrl and the rest intact.
  await page.addInitScript(() => {
    // `globalThis` is `window` in the browser; using it keeps this callback
    // typeable under the E2E tsconfig, which has no DOM lib.
    let stored: Record<string, unknown> | undefined;
    Object.defineProperty(globalThis, "__DATASPOKE_RUNTIME_CONFIG__", {
      configurable: true,
      get: () => stored,
      set: (incoming: Record<string, unknown>) => {
        stored = { ...incoming, datahubUrl: "" };
      },
    });
  });

  await page.goto("/governance/dashboard");
  await expect(page).not.toHaveURL(/\/login/);

  // Backstop: prove the init script really blanked the env value, otherwise the
  // href assertion below could be satisfied by the env plane and would say
  // nothing about the API plane.
  const injectedDatahubUrl = await readEnvDatahubUrl(page);
  expect(
    injectedDatahubUrl,
    "the init script must have blanked the injected datahubUrl for this test",
  ).toBe("");

  // -- UI assertion: the header icon points at the DB-sourced host --
  // spec: app-shell.tsx — infraLinks DataHub entry href `${datahubUrl}/login`,
  //   rendered inside a Button asChild with aria-label `Open DataHub`.
  const datahubIcon = page.getByRole("link", { name: "Open DataHub" });
  await expect(datahubIcon).toBeVisible({ timeout: 15_000 });
  await expect(datahubIcon).toHaveAttribute("href", `${SENTINEL_FRONTEND_URL}/login`);
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 2 — the per-dataset DataHub deep-link resolves from the same source
// ─────────────────────────────────────────────────────────────────────────────

test("dataset DataHub deep-link resolves from peripheral_config when no env URL is set", async ({
  page,
  adminApi,
}) => {
  const preResp = await adminApi.get(ADMIN_DATAHUB);
  expect(preResp.status()).toBe(200);
  originalFrontendUrl = ((await preResp.json()) as DatahubPeripheral).frontend_url;

  const patchResp = await adminApi.patch(ADMIN_DATAHUB, {
    data: { frontend_url: SENTINEL_FRONTEND_URL },
  });
  expect(patchResp.status()).toBe(200);

  // Backend probe (dual confirmation).
  const linksResp = await adminApi.get(PERIPHERAL_LINKS);
  expect(linksResp.status()).toBe(200);
  expect(((await linksResp.json()) as PeripheralLinks).datahub_url).toBe(SENTINEL_FRONTEND_URL);

  await page.addInitScript(() => {
    // `globalThis` is `window` in the browser; using it keeps this callback
    // typeable under the E2E tsconfig, which has no DOM lib.
    let stored: Record<string, unknown> | undefined;
    Object.defineProperty(globalThis, "__DATASPOKE_RUNTIME_CONFIG__", {
      configurable: true,
      get: () => stored,
      set: (incoming: Record<string, unknown>) => {
        stored = { ...incoming, datahubUrl: "" };
      },
    });
  });

  const urn = IMAZON_URNS.titleMaster;
  await page.goto(`/data/${encodeURIComponent(urn)}`);
  await expect(page).not.toHaveURL(/\/login/);

  const injectedDatahubUrl = await readEnvDatahubUrl(page);
  expect(
    injectedDatahubUrl,
    "the init script must have blanked the injected datahubUrl for this test",
  ).toBe("");

  // -- UI assertion: the dataset deep-link targets the DB-sourced host --
  // spec: datahub-dataset-link.tsx — href `${datahubUrl}/dataset/{encoded urn}`.
  const expectedHref = `${SENTINEL_FRONTEND_URL}/dataset/${encodeURIComponent(urn)}`;
  const deepLink = page.locator(`a[href="${expectedHref}"]`);
  await expect(deepLink.first()).toBeVisible({ timeout: 20_000 });
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 3 — precedence: an explicit env value wins over a differing API value
// Guards the "existing chart installs are behaviourally unchanged" guarantee.
//
// Env is set BY CONSTRUCTION, using the same addInitScript interception tests 1-2
// rely on — there it blanks `datahubUrl`, here it substitutes a second sentinel.
// That removes the skip entirely. A conditional guard derived from the rendered
// href could not work here: `observedBase === SENTINEL_FRONTEND_URL` holds under
// BOTH "env unset" and "merge inverted", so it would report SKIP for the very
// regression this test exists to catch. Injecting the env value makes the
// assertion unconditional and falsifiable on every cluster.
// ─────────────────────────────────────────────────────────────────────────────

/**
 * Env-plane sentinel, distinct from both the API sentinel and any real cluster
 * value, so the assertion below can only be satisfied by the env plane winning.
 */
const SENTINEL_ENV_URL = "https://datahub-env.imazon-e2e.example.com:9443";

test("an explicit env DataHub URL wins over the peripheral_config value", async ({
  page,
  adminApi,
}) => {
  const preResp = await adminApi.get(ADMIN_DATAHUB);
  expect(preResp.status()).toBe(200);
  originalFrontendUrl = ((await preResp.json()) as DatahubPeripheral).frontend_url;

  // Seed the API plane with a value that differs from the env sentinel, so a
  // merge preferring the API value visibly changes the href.
  expect(SENTINEL_ENV_URL).not.toBe(SENTINEL_FRONTEND_URL);
  const patchResp = await adminApi.patch(ADMIN_DATAHUB, {
    data: { frontend_url: SENTINEL_FRONTEND_URL },
  });
  expect(patchResp.status()).toBe(200);

  // Backend probe (dual confirmation): the API really is serving the competing
  // value, so a passing UI assertion cannot be an artefact of an empty API plane.
  const linksResp = await adminApi.get(PERIPHERAL_LINKS);
  expect(linksResp.status()).toBe(200);
  expect(((await linksResp.json()) as PeripheralLinks).datahub_url).toBe(SENTINEL_FRONTEND_URL);

  // Inject the env plane. Mirrors the interception in tests 1-2, substituting a
  // known value instead of blanking, so both planes are populated by construction.
  await page.addInitScript((envUrl: string) => {
    let stored: Record<string, unknown> | undefined;
    Object.defineProperty(globalThis, "__DATASPOKE_RUNTIME_CONFIG__", {
      configurable: true,
      get: () => stored,
      set: (incoming: Record<string, unknown>) => {
        stored = { ...incoming, datahubUrl: envUrl };
      },
    });
  }, SENTINEL_ENV_URL);

  await page.goto("/governance/dashboard");
  await expect(page).not.toHaveURL(/\/login/);

  // Backstop: the injection landed, so the assertion below is about precedence
  // rather than about a failed init script.
  const injectedDatahubUrl = await readEnvDatahubUrl(page);
  expect(injectedDatahubUrl, "the init script must have injected the env sentinel").toBe(
    SENTINEL_ENV_URL,
  );

  // -- UI assertion: the env value won over the differing API value --
  // spec: spec/feature/FRONTEND_BASIC.md §Shell — "Peripheral links resolve
  //   env-first: an explicitly-set env value wins, and the API supplies the value
  //   when the env var is unset."
  // spec: app-shell.tsx — the DataHub infra link is `${datahubUrl}/login`.
  const datahubIcon = page.getByRole("link", { name: "Open DataHub" });
  await expect(datahubIcon).toBeVisible({ timeout: 15_000 });
  await expect(
    datahubIcon,
    "the env-plane URL must win over the peripheral_config value",
  ).toHaveAttribute("href", `${SENTINEL_ENV_URL}/login`);
});
