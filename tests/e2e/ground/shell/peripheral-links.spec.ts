/**
 * Ground spec: the app-shell DataHub links resolve from the `peripheral_config`
 * DB plane.
 *
 * Concern: after `PATCH /admin/peripherals/datahub {frontend_url}` — a pure
 * DB-plane operation, no `helm upgrade` and no pod restart — the header DataHub
 * icon and the per-dataset DataHub deep-link both resolve to the new host. This
 * is the regression the feature was filed on: DataHub was fully wired through
 * the admin API and the UI still showed no link, because the browser-facing URL
 * had to come from chart values and could not be derived from `gms_url`.
 *
 * Arrangement: `GET /spoke/common/peripheral-links` is the sole source the client
 * carries for `datahub_url`, so on any install the rendered `href` can only have
 * come from the value PATCHed below — the sentinel is proof of provenance by
 * itself and the test needs no interception of the page. The links arrive with
 * that query rather than in the server-rendered HTML, so every assertion here is
 * a polled Playwright expectation.
 *
 * Group placement: ground, not use-case. `USE_CASE_en.md` carries no
 * peripheral-wiring narrative, and this is a narrow single-page (app-shell)
 * behavior — the spot analogue.
 *
 * spec: spec/API.md §Data Resource — `GET /spoke/common/peripheral-links`:
 *   `datahub_url` ⟵ `datahub.frontend_url` (the browser-facing UI URL, **never**
 *   `gms_url`); an unconfigured peripheral yields `""`, read as "render no link".
 * spec: spec/feature/FRONTEND_BASIC.md §Shell — "`GET /spoke/common/peripheral-links`
 *   serves the `peripheral_config` DB plane, the **sole** source of `datahub_url`,
 *   `langfuse_url`, and `langfuse_project_id` … Peripheral wiring done in that
 *   plane (`PATCH /admin/peripherals/{datahub,langfuse}`) therefore reaches the UI
 *   with no chart operation and no pod restart."
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — dual confirmation
 *   (UI assertion + independent REST read-back), semantic-first selectors.
 */

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

// ── Module state ──────────────────────────────────────────────────────────────

/** Original DataHub frontend_url; restored in afterEach. */
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
// Test 1 — the header icon follows a DB-plane-only PATCH
// The regression the feature was filed on.
// ─────────────────────────────────────────────────────────────────────────────

test("header DataHub icon resolves from the peripheral_config frontend_url", async ({
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

  await page.goto("/governance/dashboard");
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: the header icon points at the DB-sourced host --
  // No chart operation and no pod restart happened between the PATCH above and
  // this load, so the sentinel host in the href can only have come from the DB.
  // spec: app-shell.tsx — infraLinks DataHub entry href `${datahubUrl}/login`,
  //   rendered inside a Button asChild with aria-label `Open DataHub`.
  // The icon appears once the peripheral-links query resolves, so both the
  // visibility and the href are polled expectations rather than instant reads.
  const datahubIcon = page.getByRole("link", { name: "Open DataHub" });
  await expect(datahubIcon).toBeVisible({ timeout: 15_000 });
  await expect(datahubIcon).toHaveAttribute("href", `${SENTINEL_FRONTEND_URL}/login`, {
    timeout: 15_000,
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 2 — the per-dataset DataHub deep-link resolves from the same source
// ─────────────────────────────────────────────────────────────────────────────

test("dataset DataHub deep-link resolves from the peripheral_config frontend_url", async ({
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

  const urn = IMAZON_URNS.titleMaster;
  await page.goto(`/data/${encodeURIComponent(urn)}`);
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: the dataset deep-link targets the DB-sourced host --
  // spec: spec/feature/FRONTEND_BASIC.md §Shared Component Notes → DatahubDatasetLink —
  //   `<datahub_url>/dataset/{urn}` (URN URL-encoded), a new-tab link rendered
  //   only when that URL is non-empty.
  // Located by its own href and scoped to <main> so the assertion cannot be
  // satisfied by the header infra icon; `toBeVisible` polls, so the
  // peripheral-links round-trip on a cold load is tolerated.
  const expectedHref = `${SENTINEL_FRONTEND_URL}/dataset/${encodeURIComponent(urn)}`;
  const deepLink = page.getByRole("main").locator(`a[href="${expectedHref}"]`);
  await expect(deepLink.first()).toBeVisible({ timeout: 20_000 });
  await expect(deepLink.first()).toHaveAttribute("target", "_blank");
  await expect(deepLink.first()).toHaveAttribute("rel", /noopener/);
});
