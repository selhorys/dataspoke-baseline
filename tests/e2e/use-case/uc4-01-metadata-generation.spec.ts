/**
 * UC4 — Metadata Generation: browser UI flow (multiple-confs collection model).
 *
 * Mirrors tests/integration/api_wired/test_uc4_01_metadata_generation.py
 * step-for-step, with dual confirmation at each mutating step:
 *   - UI assertion (heading, badge, card, toast, event row)
 *   - Backend probe via adminApi (same REST read-back the api-wired step asserts)
 *
 * MetaGen is a managed COLLECTION of named confs. This arc creates TWO confs over
 * DIFFERENT dataset groups (conf EU scoped to eu_profiles, conf OE scoped to
 * orders.events), opts each dataset in via its per-dataset boundary, runs each
 * conf, reviews candidates in the UI (approve / reject) with backend read-back,
 * inspects the per-dataset result rollup + cross-conf events, and the uncovered view.
 *
 * Arc (verbatim from USE_CASE_en.md §UC4 / api-wired steps):
 *   Setup  — --uc4-seed: fulfillment document + approved ontogen nodes, mask
 *             DataHub descriptions for both URNs (Python SDK; not reachable via REST).
 *   Step 1 — /metagen redirects to /metagen/conf (conf list).
 *   Step 2 — Create conf EU via /metagen/conf/new (name, is_enabled, schedule_tier,
 *             dataset_filter dataset_urns=[eu_profiles], result_limit, overwrite_pending).
 *             Create conf OE the same way scoped to orders.events.
 *             Backend: POST /spoke/metagen/conf → 201; round-trip fields + dataset_filter.
 *   Step 3 — Opt each dataset in via /data/[urn] (MetaGen panel) boundary form.
 *             Backend: PUT .../attr/metagen/boundary → 200/201; echo dataset_urn + allowed.
 *   Step 4 — Run conf OE then conf EU from their /metagen/conf/[id] detail pages
 *             via the Run dialog. Poll adminApi until each conf's RUN_COMPLETE present.
 *             Backend: POST /spoke/metagen/conf/{id}/method/run → 200; run_id carries conf_id.
 *   Step 5 — /metagen/conf/[id] per-conf events show this conf's RUN_COMPLETE only.
 *             Backend: GET /spoke/metagen/conf/{id}/event → EU run present, OE run absent.
 *   Step 6 — /metagen/result per-dataset rollup lists datasets (one row per dataset
 *             with candidate-level counts + boundary); cross-conf event feed shows
 *             both confs' RUN_COMPLETE.
 *             Backend: GET /spoke/metagen/dataset + GET /spoke/metagen/event union.
 *   Step 7 — /data/[eu] (MetaGen panel) approve a dataset.description candidate via the
 *             dataset.description ItemKindTable candidate row (conf_name + status badges)
 *             → per-row Approve → ConfirmDialog.
 *             Backend: candidate status=approved; per-candidate conf_name = conf EU.
 *   Step 8 — /data/[eu] (MetaGen panel) reject a column.description candidate via its
 *             column-grouped row in the column.description ItemKindTable.
 *             Backend: candidate status=rejected.
 *   Step 9 — Per-dataset events show CANDIDATE_APPROVE + CANDIDATE_REJECT.
 *             Backend: GET .../event/metagen → events with item_id, candidate_id, reason.
 *   Step 10 — /metagen/uncovered lists datasets reached by no conf; include_disallowed
 *             toggle widens to boundary_blocked rows.
 *             Backend: GET /spoke/metagen/uncovered?include_disallowed=<bool>.
 *
 * Cleanup: afterAll deletes confs + boundaries via REST, then --uc4-restore (Python SDK).
 *
 * LLM mode: stub-mode flow runs by default (stub_llm_client=true in dev). A real-LLM
 * add-on assertion (candidates_added ≥ 1) skips unless stub_llm_client is false.
 *
 * spec: USE_CASE_en.md §UC4 Metadata Generation
 * spec: spec/feature/FRONTEND_METAGEN.md §Routes, §Page contracts
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — dual confirmation, selector guidance
 */

import { execSync } from "child_process";
import * as path from "path";
import type { APIRequestContext, Page } from "@playwright/test";
import { test, expect, IMAZON_URNS } from "../fixtures/index";

// ── Serial, shared-state retry policy ─────────────────────────────────────────
//
// This file is one ordered arc: the steps are separate `test()` blocks that share
// module-level mutable state (conf ids, run ids, approved/rejected candidate ids).
// They only make sense executed strictly in order, each building on the prior.
//
// We keep the per-step `test()` structure (it reads as the executable form of the
// USE_CASE_en.md narrative, mirroring the api-wired file step-for-step) but pin the
// execution model:
//   - mode: "serial"  → Playwright runs the steps in declaration order and, when a
//     step fails, SKIPS the remaining steps instead of running them against now-
//     inconsistent module state.
//   - retries: 0      → overrides the project-level `retries: 1`. A mid-chain flake
//     must fail LOUDLY in place rather than triggering an isolated single-step
//     retry: a retry re-runs only the failed step (earlier steps do not re-run), so
//     the shared module state would be stale on the retried tail (e.g. step 8 retry
//     leaves step 7's approvedEuDescCandidateId set but step 9 then reads a chain
//     that never re-derived). Fix #1's backend-readiness polling removes the flake's
//     root cause (eventual-consistency render lag), so disabling retries here does
//     not re-introduce the observed flakiness — it just refuses to paper over a real
//     failure with a state-corrupting retry.
//
// spec: TESTING.md §End-to-End (E2E) Testing — use-case group mirrors api-wired
test.describe.configure({ mode: "serial", retries: 0 });

// ── URN constants (verbatim from api-wired) ───────────────────────────────────

const EU_PROFILES_URN = IMAZON_URNS.euProfiles;
const ORDERS_EVENTS_URN =
  "urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.orders.events,DEV)";

const EU_PROFILES_ENC = encodeURIComponent(EU_PROFILES_URN);
const ORDERS_EVENTS_ENC = encodeURIComponent(ORDERS_EVENTS_URN);

// REST routes (mirroring api-wired constants)
const CONF_API = "/api/v1/spoke/metagen/conf";
const EU_BOUNDARY_API = `/api/v1/spoke/common/data/${EU_PROFILES_ENC}/attr/metagen/boundary`;
const OE_BOUNDARY_API = `/api/v1/spoke/common/data/${ORDERS_EVENTS_ENC}/attr/metagen/boundary`;
const GLOBAL_EVENT_API = "/api/v1/spoke/metagen/event";
const GLOBAL_DATASET_API = "/api/v1/spoke/metagen/dataset";
const UNCOVERED_API = "/api/v1/spoke/metagen/uncovered";

// Frontend routes
const METAGEN_URL = "/metagen";
const CONF_LIST_URL = "/metagen/conf";
const CONF_NEW_URL = "/metagen/conf/new";
const RESULT_URL = "/metagen/result";
const UNCOVERED_URL = "/metagen/uncovered";
// Per-dataset detail is the unified hub /data/[urn]; the metagen boundary form
// and candidate items live under its "MetaGen" CollapsiblePanel (open by
// default), and metagen events fold into the unified "Events" panel.
// spec: FRONTEND_BASIC.md §Per-dataset page; FRONTEND_METAGEN.md §Per-dataset (/data/[urn] MetaGen panel)
const EU_DATASET_URL = `/data/${EU_PROFILES_ENC}`;
const OE_DATASET_URL = `/data/${ORDERS_EVENTS_ENC}`;

// Repo root for running Python utilities
const REPO_ROOT = path.resolve(__dirname, "..", "..", "..");

// Unique conf names per run so reruns/retries do not collide on 409 METAGEN_CONF_EXISTS.
const CONF_SUFFIX = Date.now().toString(36);
const CONF_EU_NAME = `uc4-eu-${CONF_SUFFIX}`;
const CONF_OE_NAME = `uc4-oe-${CONF_SUFFIX}`;
const CONF_RIVAL_NAME = `uc4-rival-${CONF_SUFFIX}`;

// ── Shared state across serial steps ────────────────────────────────────────

let confEuId: string | null = null;
let confOeId: string | null = null;
// RIVAL conf also scopes eu_profiles — used by the cross-conf demotion step (8b)
// to prove the global one-approved-per-item invariant across confs.
let confRivalId: string | null = null;
let euBoundaryCreated = false;
let oeBoundaryCreated = false;

let euRunId: string | null = null;
let oeRunId: string | null = null;

let approvedEuDescCandidateId: string | null = null;
let rejectedEuColCandidateId: string | null = null;

// Stub-mode flag, read once in beforeAll.
let stubLlmClient = true;

// ── UC4 context seed helpers (--uc4-seed / --uc4-restore) ────────────────────

/**
 * --uc4-seed: fulfillment document + approved ontogen nodes mapped to both
 * datasets, plus DataHub description masking for both URNs. The masking (wipe
 * DatasetProperties.description, blank SchemaMetadata field descriptions) needs
 * the DataHub Python SDK — no REST route exposes it.
 *
 * The masking step (seed_uc4_context) deliberately fails loud with a RuntimeError
 * ("cannot mask absent DataHub aspects") when run without a prior --reset-seed
 * baseline: an unmasked estate produces zero candidates, which would otherwise
 * yield a green-but-untested run. This is a hard precondition, so we re-throw it
 * to fail the whole suite rather than swallowing it. --uc4-seed has no benign
 * "already seeded" idempotent path (it always seeds a fresh document + ontogen
 * nodes with a unique suffix), so any non-zero exit is a genuine precondition
 * failure and must abort.
 *
 * spec: tests/integration/util/__main__.py --uc4-seed
 * spec: tests/integration/util/metagen.py seed_uc4_context — masking precondition
 */
function runUc4Seed(): void {
  try {
    execSync("uv run python -m tests.integration.util --uc4-seed", {
      cwd: REPO_ROOT,
      stdio: "inherit",
      timeout: 120_000,
    });
  } catch (err) {
    // Re-throw: a failing --uc4-seed means the masking precondition (a
    // reset-seed baseline with ingested customers/orders schemas) is unmet, so
    // the estate is unmasked and the run would vacuously pass with zero
    // candidates. Surface the real error instead of warning past it.
    throw new Error(
      "[uc4] --uc4-seed failed — the masking precondition is unmet (run " +
        "`--reset-seed` and let it finish before this suite). Aborting so the " +
        "suite does not pass vacuously against an unmasked estate. Original error: " +
        (err instanceof Error ? err.message : String(err)),
    );
  }
}

/**
 * --uc4-restore: restore DataHub aspects, delete fulfillment document, metagen
 * state, ontogen nodes. Idempotent when the state file is absent.
 *
 * spec: tests/integration/util/__main__.py --uc4-restore
 */
function runUc4Restore(): void {
  try {
    execSync("uv run python -m tests.integration.util --uc4-restore", {
      cwd: REPO_ROOT,
      stdio: "inherit",
      timeout: 120_000,
    });
  } catch (err) {
    console.warn("[uc4] --uc4-restore failed (non-fatal for cleanup):", err);
  }
}

// ── beforeAll: seed + read stub mode ─────────────────────────────────────────

test.beforeAll(async ({ adminApi }) => {
  runUc4Seed();

  const confResp = await adminApi.get("/api/v1/admin/conf");
  if (confResp.ok()) {
    const body = (await confResp.json()) as Record<string, unknown>;
    stubLlmClient = body["stub_llm_client"] === true;
  } else {
    stubLlmClient = true; // assume stub when we cannot check
  }
});

// ── afterAll: cleanup REST state + restore DataHub ───────────────────────────

test.afterAll(async ({ adminApi }) => {
  if (euBoundaryCreated) {
    await adminApi.delete(EU_BOUNDARY_API).catch(() => null);
    euBoundaryCreated = false;
  }
  if (oeBoundaryCreated) {
    await adminApi.delete(OE_BOUNDARY_API).catch(() => null);
    oeBoundaryCreated = false;
  }
  if (confEuId) {
    await adminApi.delete(`${CONF_API}/${confEuId}`).catch(() => null);
    confEuId = null;
  }
  if (confOeId) {
    await adminApi.delete(`${CONF_API}/${confOeId}`).catch(() => null);
    confOeId = null;
  }
  if (confRivalId) {
    await adminApi.delete(`${CONF_API}/${confRivalId}`).catch(() => null);
    confRivalId = null;
  }
  runUc4Restore();
});

// ── Shared local gestures ─────────────────────────────────────────────────────

/**
 * Fill the dataset_urns field of the DatasetFilterEditor with a single URN.
 * The editor renders each multi-value dimension as a newline/comma-separated
 * textarea; dataset_urns has the stable id "df-dataset-urns"
 * (dataset-filter-editor.tsx). The resulting dataset_filter shape is asserted
 * via the backend probe, not the DOM.
 */
async function fillDatasetUrnFilter(page: Page, urn: string): Promise<void> {
  const urnInput = page.locator("#df-dataset-urns");
  await expect(urnInput).toBeVisible({ timeout: 10_000 });
  await urnInput.fill(urn);
}

/**
 * Enter boundary edit mode if a boundary already exists (an "Edit" button renders);
 * when no boundary exists the create form is already shown. Either way, confirm
 * the editable form is actually present afterwards by asserting an edit-mode-only
 * control (the is_enabled checkbox) is visible — so a broken Edit gesture fails
 * here rather than silently leaving us on a read-only view.
 *
 * boundary-form.tsx: <Checkbox id="boundary-is-enabled"> renders only in the
 * editable form, never in the read-only allowed-badges view.
 */
async function enterBoundaryEditMode(page: Page): Promise<void> {
  const editButton = page.getByRole("button", { name: "Edit", exact: true }).first();
  if (await editButton.isVisible().catch(() => false)) {
    await editButton.click();
  }
  await expect(
    page.locator("#boundary-is-enabled"),
    "boundary edit form must be active after entering edit mode (edit-mode-only checkbox)",
  ).toBeVisible({ timeout: 10_000 });
}

/**
 * Trigger a run from a conf detail page via the Run dialog, then poll the
 * cross-conf event feed until a METAGEN.RUN_COMPLETE for this conf appears.
 * Returns the run_id observed in the event.
 */
async function runConfFromDetail(
  page: Page,
  adminApi: APIRequestContext,
  confId: string,
  confName: string,
): Promise<string> {
  await page.goto(`/metagen/conf/${confId}`);
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page.getByRole("heading", { name: confName, exact: true })).toBeVisible({
    timeout: 15_000,
  });

  // -- UI gesture: open the Run dialog --
  // conf/[id]/page.tsx header: <Button onClick={() => setRunDialogOpen(true)}>Run</Button>
  await page.getByRole("button", { name: "Run", exact: true }).click();

  // -- UI assertion: RunDialog opens --
  // run-dialog.tsx: <DialogTitle>Run MetaGen</DialogTitle>
  await expect(page.getByRole("heading", { name: "Run MetaGen", exact: true })).toBeVisible({
    timeout: 10_000,
  });

  // -- UI gesture: confirm run (dry_run unchecked) — dialog footer Run button --
  await page.getByRole("button", { name: "Run", exact: true }).last().click();

  // -- UI assertion: "Run complete" toast --
  // conf/[id]/page.tsx handleRun onSuccess → toast({ title: "Run complete", ... })
  await expect(page.getByText("Run complete", { exact: false }).first()).toBeVisible({
    timeout: 120_000,
  });

  // -- Backend probe: poll cross-conf event feed for this conf's RUN_COMPLETE --
  // spec: BACKEND.md §Event Catalogue — RUN_COMPLETE detail carries run_id + conf_id
  const deadline = Date.now() + 120_000;
  while (Date.now() < deadline) {
    const evResp = await adminApi.get(`${GLOBAL_EVENT_API}?limit=50`);
    if (evResp.ok()) {
      const evBody = (await evResp.json()) as {
        events: Array<{ event_type: string; detail?: Record<string, unknown> }>;
      };
      const found = evBody.events.find(
        (e) =>
          e.event_type === "METAGEN.RUN_COMPLETE" &&
          (e.detail as Record<string, unknown>)?.["conf_id"] === confId,
      );
      if (found) {
        const detail = (found.detail ?? {}) as Record<string, unknown>;
        expect(typeof detail["run_id"]).toBe("string");
        expect(detail["dry_run"]).toBe(false);
        return detail["run_id"] as string;
      }
    }
    await new Promise((res) => setTimeout(res, 3_000));
  }
  throw new Error(`No METAGEN.RUN_COMPLETE event for conf_id=${confId} within deadline`);
}

/**
 * Wait for a freshly generated candidate to be committed and queryable in the
 * backend, THEN reload the per-dataset UI so its render reflects the committed
 * state.
 *
 * Root cause this guards (observed dev-cluster flake, steps 7/8): after a conf run
 * completes (RUN_COMPLETE observed in the event feed), the per-dataset item page is
 * sometimes loaded/asserted before the freshly generated candidates are queryable/
 * rendered — an eventual-consistency / render lag between "run done" and "candidate
 * visible", not a product bug (the api-wired arc + 35 spot tests pass on the same
 * cluster).
 *
 * The fix polls the AUTHORITATIVE backend state first (per-dataset items, then item
 * detail for the open llm_approved candidate), and only once the backend confirms an
 * open candidate exists does it reload the page so the React tree re-fetches the
 * committed rows. This is NOT a blind timeout bump (cf. memory feedback_no_increase_
 * timeout): we wait on real committed state, then sync the UI to it.
 *
 * Returns the matched item — `{ itemId, candidateId }` — or `null` when the bounded
 * poll finds no open candidate of `kind`. Callers decide whether a null is a stub
 * regression (fail loud) or a benign real-LLM no-op.
 *
 * @param predicate optional extra filter on the matched item (e.g. status !== approved)
 */
async function waitForOpenCandidateThenReload(
  page: Page,
  adminApi: APIRequestContext,
  datasetUrnEnc: string,
  kind: string,
  predicate: (item: { item_id: string; kind: string; status: string }) => boolean = () => true,
  timeoutMs = 90_000,
): Promise<{ itemId: string; candidateId: string } | null> {
  const itemsUrl = `/api/v1/spoke/common/data/${datasetUrnEnc}/attr/metagen/item`;
  const deadline = Date.now() + timeoutMs;
  let match: { itemId: string; candidateId: string } | null = null;

  // -- Backend readiness poll: an open (llm_approved) candidate of `kind` is committed --
  while (Date.now() < deadline) {
    const itemsResp = await adminApi.get(itemsUrl);
    if (itemsResp.ok()) {
      const itemsBody = (await itemsResp.json()) as {
        items: Array<{ item_id: string; kind: string; status: string }>;
      };
      const candidateItems = itemsBody.items.filter((i) => i.kind === kind && predicate(i));
      for (const item of candidateItems) {
        const detailResp = await adminApi.get(
          `${itemsUrl}/${encodeURIComponent(item.item_id)}`,
        );
        if (detailResp.ok()) {
          const detailBody = (await detailResp.json()) as {
            candidates: Array<{ candidate_id: string; status: string }>;
          };
          const llmApproved = detailBody.candidates.find((c) => c.status === "llm_approved");
          if (llmApproved) {
            match = { itemId: item.item_id, candidateId: llmApproved.candidate_id };
            break;
          }
        }
      }
    }
    if (match) break;
    await new Promise((res) => setTimeout(res, 3_000));
  }

  if (!match) return null;

  // -- Sync the UI to the committed backend state: reload so the per-dataset page
  //    re-fetches the now-queryable candidate rows, then wait for the dataset header. --
  await page.reload();
  await expect(page.getByText(decodeURIComponent(datasetUrnEnc), { exact: false }).first()).toBeVisible({
    timeout: 15_000,
  });
  return match;
}

// ─────────────────────────────────────────────────────────────────────────────
// Step 1 — /metagen redirects to /metagen/conf (conf list)
// spec: FRONTEND_METAGEN.md §Routes — /metagen → 302 /metagen/conf
// ─────────────────────────────────────────────────────────────────────────────
test("UC4 step 1 — /metagen redirects to the conf list", async ({ page }) => {
  await page.goto(METAGEN_URL);
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: redirect lands on /metagen/conf --
  await expect(page).toHaveURL(/\/metagen\/conf$/, { timeout: 15_000 });

  // -- UI assertion: conf-list heading present --
  // conf-list.tsx: <h1>Metadata Generation</h1>
  await expect(
    page.getByRole("heading", { name: "Metadata Generation", exact: true }),
  ).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: "Create conf" action present for the writer (admin) role --
  // conf-list.tsx: <Link href="/metagen/conf/new">Create conf</Link>
  await expect(page.getByRole("link", { name: /create conf/i })).toBeVisible();
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 2 — Create two confs over different dataset groups via /metagen/conf/new
// spec: USE_CASE_en.md §UC4 — conf collection over different dataset groups
// spec: FRONTEND_METAGEN.md §Page contracts — /metagen/conf/new POSTs /spoke/metagen/conf
// ─────────────────────────────────────────────────────────────────────────────
test("UC4 step 2 — create conf EU and conf OE over different dataset groups", async ({
  page,
  adminApi,
}) => {
  // ── 2a: conf EU (scoped to eu_profiles) ───────────────────────────────────
  await page.goto(CONF_NEW_URL);
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page.getByRole("heading", { name: "Create conf", exact: true })).toBeVisible({
    timeout: 15_000,
  });

  // -- UI gesture: name --
  // conf-form.tsx: <Input id="metagen-conf-name">
  await page.locator("#metagen-conf-name").fill(CONF_EU_NAME);

  // -- UI gesture: is_enabled --
  // conf-form.tsx: <Checkbox id="metagen-conf-is-enabled">
  const euEnabled = page.locator("#metagen-conf-is-enabled");
  if (!(await euEnabled.isChecked().catch(() => false))) await euEnabled.click();

  // -- UI gesture: schedule_tier "daily" via Radix Select --
  // conf-form.tsx: <SelectTrigger id="metagen-conf-schedule-tier">
  await page.locator("#metagen-conf-schedule-tier").click();
  await page.getByRole("option", { name: "daily", exact: true }).click();

  // -- UI gesture: dataset_filter dataset_urns=[eu_profiles] --
  await fillDatasetUrnFilter(page, EU_PROFILES_URN);

  // -- UI gesture: result_limit + overwrite_pending --
  await page.locator("#metagen-conf-result-limit").fill("3");
  const euOverwrite = page.locator("#metagen-conf-overwrite-pending");
  if (!(await euOverwrite.isChecked().catch(() => false))) await euOverwrite.click();

  // -- UI gesture: submit --
  // conf-form.tsx: <Button type="submit">Create conf</Button>
  await page.getByRole("button", { name: "Create conf", exact: true }).click();

  // -- UI assertion: toast "Conf created" + redirect to the conf detail page --
  // new/page.tsx onSuccess → toast + router.push(/metagen/conf/{id})
  await expect(page.getByText("Conf created", { exact: false }).first()).toBeVisible({
    timeout: 20_000,
  });
  await expect(page).toHaveURL(/\/metagen\/conf\/[^/]+$/, { timeout: 15_000 });

  // -- Backend probe (dual confirmation): conf EU exists with the right scope --
  // spec: API.md §Metadata Generation — POST /metagen/conf → 201; round-trips fields
  const euListResp = await adminApi.get(`${CONF_API}?limit=100`);
  expect(euListResp.status()).toBe(200);
  const euList = (await euListResp.json()) as {
    confs: Array<{
      id: string;
      name: string;
      is_enabled: boolean;
      schedule_tier: string | null;
      dataset_filter: Record<string, unknown>;
      result_limit: number;
    }>;
  };
  const euConf = euList.confs.find((c) => c.name === CONF_EU_NAME);
  expect(euConf, `conf ${CONF_EU_NAME} must exist after create`).toBeTruthy();
  confEuId = euConf!.id;
  expect(euConf!.is_enabled).toBe(true);
  expect(euConf!.schedule_tier).toBe("daily");
  expect(euConf!.result_limit).toBe(3);
  expect(euConf!.dataset_filter).toEqual({ dataset_urns: [EU_PROFILES_URN] });

  // ── 2b: conf OE (scoped to orders.events) ─────────────────────────────────
  await page.goto(CONF_NEW_URL);
  await expect(page.getByRole("heading", { name: "Create conf", exact: true })).toBeVisible({
    timeout: 15_000,
  });

  await page.locator("#metagen-conf-name").fill(CONF_OE_NAME);
  const oeEnabled = page.locator("#metagen-conf-is-enabled");
  if (!(await oeEnabled.isChecked().catch(() => false))) await oeEnabled.click();
  await page.locator("#metagen-conf-schedule-tier").click();
  await page.getByRole("option", { name: "daily", exact: true }).click();
  await fillDatasetUrnFilter(page, ORDERS_EVENTS_URN);
  await page.locator("#metagen-conf-result-limit").fill("3");
  const oeOverwrite = page.locator("#metagen-conf-overwrite-pending");
  if (!(await oeOverwrite.isChecked().catch(() => false))) await oeOverwrite.click();
  await page.getByRole("button", { name: "Create conf", exact: true }).click();
  await expect(page.getByText("Conf created", { exact: false }).first()).toBeVisible({
    timeout: 20_000,
  });

  // -- Backend probe: conf OE exists scoped to orders.events --
  const oeListResp = await adminApi.get(`${CONF_API}?limit=100`);
  expect(oeListResp.status()).toBe(200);
  const oeList = (await oeListResp.json()) as {
    confs: Array<{ id: string; name: string; dataset_filter: Record<string, unknown> }>;
  };
  const oeConf = oeList.confs.find((c) => c.name === CONF_OE_NAME);
  expect(oeConf, `conf ${CONF_OE_NAME} must exist after create`).toBeTruthy();
  confOeId = oeConf!.id;
  expect(oeConf!.dataset_filter).toEqual({ dataset_urns: [ORDERS_EVENTS_URN] });

  // -- UI assertion: both confs render in the conf list --
  await page.goto(CONF_LIST_URL);
  await expect(page.getByRole("link", { name: CONF_EU_NAME, exact: true })).toBeVisible({
    timeout: 15_000,
  });
  await expect(page.getByRole("link", { name: CONF_OE_NAME, exact: true })).toBeVisible();
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 3 — Opt each dataset in via /data/[urn] (MetaGen panel) boundary form
// spec: USE_CASE_en.md §UC4 — API Mapping — per-dataset boundary opt-in
// spec: FRONTEND_METAGEN.md §Page contracts — /data/[urn] MetaGen panel PUTs .../attr/metagen/boundary
// ─────────────────────────────────────────────────────────────────────────────
test("UC4 step 3 — opt eu_profiles and orders.events in via per-dataset boundaries", async ({
  page,
  adminApi,
}) => {
  if (!confEuId || !confOeId) test.skip(true, "step 2 did not create both confs");

  // ── 3a: eu_profiles boundary (dataset + column descriptions) ──────────────
  await page.goto(EU_DATASET_URL);
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page.getByText(EU_PROFILES_URN, { exact: false }).first()).toBeVisible({
    timeout: 15_000,
  });

  // -- UI assertion: boundary section heading present --
  // metagen-data-panel.tsx: <h3>attr/metagen/boundary</h3> (matched via getByText)
  await expect(page.getByText("attr/metagen/boundary", { exact: true })).toBeVisible({
    timeout: 10_000,
  });

  await enterBoundaryEditMode(page);

  // -- UI gesture: is_enabled --
  // boundary-form.tsx: <Checkbox id="boundary-is-enabled">
  const euIsEnabled = page.locator("#boundary-is-enabled");
  await expect(euIsEnabled).toBeVisible({ timeout: 10_000 });
  if (!(await euIsEnabled.isChecked().catch(() => false))) await euIsEnabled.click();

  // -- UI gesture: allow dataset.description + column.description --
  // boundary-form.tsx: <Checkbox id="boundary-allowed-{kind}">
  const euDatasetDesc = page.locator("#boundary-allowed-dataset\\.description");
  await expect(euDatasetDesc).toBeVisible({ timeout: 10_000 });
  if (!(await euDatasetDesc.isChecked().catch(() => false))) await euDatasetDesc.click();
  const euColDesc = page.locator("#boundary-allowed-column\\.description");
  if (!(await euColDesc.isChecked().catch(() => false))) await euColDesc.click();

  // -- UI gesture: submit --
  // boundary-form.tsx: <Button type="submit">Save boundary</Button>
  await page.getByRole("button", { name: "Save boundary" }).click();

  // -- UI assertion: toast "Boundary saved" + read-only allowed badges --
  await expect(page.getByText("Boundary saved", { exact: false }).first()).toBeVisible({
    timeout: 20_000,
  });
  euBoundaryCreated = true;
  await expect(page.getByText("dataset.description", { exact: true }).first()).toBeVisible({
    timeout: 15_000,
  });

  // -- Backend probe: eu_profiles boundary echoes URN + allowed --
  // spec: USE_CASE_en.md §UC4 — API Mapping
  const euResp = await adminApi.get(EU_BOUNDARY_API);
  expect(euResp.status()).toBe(200);
  const euBody = (await euResp.json()) as {
    dataset_urn: string;
    is_enabled: boolean;
    allowed: string[];
  };
  expect(euBody.dataset_urn).toBe(EU_PROFILES_URN);
  expect(euBody.is_enabled).toBe(true);
  expect(new Set(euBody.allowed)).toEqual(
    new Set(["dataset.description", "column.description"]),
  );

  // ── 3b: orders.events boundary (column descriptions only) ─────────────────
  await page.goto(OE_DATASET_URL);
  await expect(page.getByText(ORDERS_EVENTS_URN, { exact: false }).first()).toBeVisible({
    timeout: 15_000,
  });
  await enterBoundaryEditMode(page);

  const oeIsEnabled = page.locator("#boundary-is-enabled");
  await expect(oeIsEnabled).toBeVisible({ timeout: 10_000 });
  if (!(await oeIsEnabled.isChecked().catch(() => false))) await oeIsEnabled.click();
  const oeColDesc = page.locator("#boundary-allowed-column\\.description");
  await expect(oeColDesc).toBeVisible({ timeout: 10_000 });
  if (!(await oeColDesc.isChecked().catch(() => false))) await oeColDesc.click();
  const oeDatasetDesc = page.locator("#boundary-allowed-dataset\\.description");
  if (await oeDatasetDesc.isChecked().catch(() => false)) await oeDatasetDesc.click();

  await page.getByRole("button", { name: "Save boundary" }).click();
  await expect(page.getByText("Boundary saved", { exact: false }).first()).toBeVisible({
    timeout: 20_000,
  });
  oeBoundaryCreated = true;

  // -- Backend probe: orders.events boundary --
  const oeResp = await adminApi.get(OE_BOUNDARY_API);
  expect(oeResp.status()).toBe(200);
  const oeBody = (await oeResp.json()) as {
    dataset_urn: string;
    is_enabled: boolean;
    allowed: string[];
  };
  expect(oeBody.dataset_urn).toBe(ORDERS_EVENTS_URN);
  expect(oeBody.is_enabled).toBe(true);
  expect(oeBody.allowed).toContain("column.description");
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 4 — Run conf OE then conf EU from their detail pages
// spec: USE_CASE_en.md §UC4 — per-conf run; FRONTEND_METAGEN.md §Page contracts
// spec: API.md §Metadata Generation — POST /spoke/metagen/conf/{conf_id}/method/run
// ─────────────────────────────────────────────────────────────────────────────
test("UC4 step 4 — run conf OE then conf EU; each RUN_COMPLETE carries its conf_id", async ({
  page,
  adminApi,
}) => {
  if (!confEuId || !confOeId || !euBoundaryCreated) {
    test.skip(true, "steps 2-3 did not complete");
  }

  // Run conf OE first, then conf EU (the EU run is the one later steps review).
  oeRunId = await runConfFromDetail(page, adminApi, confOeId!, CONF_OE_NAME);
  euRunId = await runConfFromDetail(page, adminApi, confEuId!, CONF_EU_NAME);

  expect(oeRunId).toBeTruthy();
  expect(euRunId).toBeTruthy();
  expect(oeRunId).not.toBe(euRunId);
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 5 — Per-conf events on /metagen/conf/[id] show this conf's RUN_COMPLETE only
// spec: FRONTEND_METAGEN.md §Conf create / detail — GET /spoke/metagen/conf/{id}/event
// ─────────────────────────────────────────────────────────────────────────────
test("UC4 step 5 — conf EU detail shows its own RUN_COMPLETE; OE run does not leak", async ({
  page,
  adminApi,
}) => {
  if (!confEuId || !euRunId) test.skip(true, "step 4 did not complete");

  await page.goto(`/metagen/conf/${confEuId}`);
  await expect(page.getByRole("heading", { name: CONF_EU_NAME, exact: true })).toBeVisible({
    timeout: 15_000,
  });

  // -- UI assertion: "Run events" section + RUN_COMPLETE row --
  // conf/[id]/page.tsx: <h2>Run events</h2> over MetagenEventTable
  await expect(page.getByText("Run events", { exact: true })).toBeVisible({ timeout: 10_000 });
  await expect(
    page.getByText("METAGEN.RUN_COMPLETE", { exact: false }).first(),
  ).toBeVisible({ timeout: 30_000 });

  // -- Backend probe: per-conf event feed carries the EU run_id, and ONLY EU's run --
  // spec: API.md §Metadata Generation — /conf/{conf_id}/event is per-conf
  const evResp = await adminApi.get(`${CONF_API}/${confEuId}/event?limit=50`);
  expect(evResp.status()).toBe(200);
  const evBody = (await evResp.json()) as {
    events: Array<{ event_type: string; detail?: Record<string, unknown> }>;
    offset: number;
    limit: number;
    total_count: number;
  };
  expect(evBody).toHaveProperty("offset");
  expect(evBody).toHaveProperty("limit");
  expect(evBody).toHaveProperty("total_count");
  const euRunEvent = evBody.events.find(
    (e) =>
      e.event_type === "METAGEN.RUN_COMPLETE" &&
      (e.detail as Record<string, unknown>)?.["run_id"] === euRunId,
  );
  expect(euRunEvent, `EU run_id=${euRunId} must be in conf EU's event feed`).toBeTruthy();
  const oeLeak = evBody.events.find(
    (e) => (e.detail as Record<string, unknown>)?.["run_id"] === oeRunId,
  );
  expect(oeLeak, "OE run must not appear in conf EU's per-conf event feed").toBeFalsy();
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 6 — /metagen/result per-dataset rollup + cross-conf event union
// spec: FRONTEND_METAGEN.md §Result rollup — GET /spoke/metagen/dataset, /spoke/metagen/event
// ─────────────────────────────────────────────────────────────────────────────
test("UC4 step 6 — result rollup lists datasets; cross-conf events show both runs", async ({
  page,
  adminApi,
}) => {
  if (!euRunId || !oeRunId) test.skip(true, "step 4 did not complete");

  await page.goto(RESULT_URL);
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: result-rollup heading + per-dataset rollup + events sections --
  // result/page.tsx: <h1>Result rollup</h1>, per-dataset rollup + Run events sections
  await expect(page.getByRole("heading", { name: "Result rollup", exact: true })).toBeVisible({
    timeout: 15_000,
  });
  await expect(
    page.getByText("datasets (per-dataset rollup)", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("Run events (cross-conf)", { exact: true })).toBeVisible();

  // -- UI assertion: cross-conf RUN_COMPLETE visible in the events section --
  await expect(
    page.getByText("METAGEN.RUN_COMPLETE", { exact: false }).first(),
  ).toBeVisible({ timeout: 30_000 });

  // -- UI assertion: the rollup table renders a row linking to eu_profiles --
  // dataset-table.tsx: dataset cell is a <Link href=/data/{urn}> with the URN text.
  // The two runs (EU on eu_profiles, OE on orders.events) produce items on both
  // datasets, so the rollup must surface at least the eu_profiles row.
  // spec: FRONTEND_METAGEN.md §Result rollup — one row per dataset, dataset_urn links to /data/[urn]
  await expect(
    page.getByRole("link", { name: EU_PROFILES_URN, exact: false }).first(),
  ).toBeVisible({ timeout: 30_000 });

  // -- Backend probe: cross-conf /event union contains BOTH confs' runs --
  // spec: API.md §Metadata Generation — /metagen/event union
  const evResp = await adminApi.get(`${GLOBAL_EVENT_API}?limit=100`);
  expect(evResp.status()).toBe(200);
  const evBody = (await evResp.json()) as {
    events: Array<{ event_type: string; detail?: Record<string, unknown> }>;
  };
  const runIds = new Set(
    evBody.events
      .filter((e) => e.event_type === "METAGEN.RUN_COMPLETE")
      .map((e) => (e.detail as Record<string, unknown>)?.["run_id"]),
  );
  expect(runIds.has(euRunId), "cross-conf union must include EU run").toBeTruthy();
  expect(runIds.has(oeRunId), "cross-conf union must include OE run").toBeTruthy();

  // -- Backend probe (dual confirmation): per-dataset rollup carries one row per
  //    dataset with candidate-level counts + boundary; eu_profiles and orders.events
  //    each appear with item_count ≥ 1 after their runs. --
  // spec: API.md §Metadata Generation — GET /spoke/metagen/dataset row shape:
  //   dataset_urn, is_enabled, allowed, item_count, approved/rejected/candidate_count,
  //   last_modified_at
  const dsResp = await adminApi.get(`${GLOBAL_DATASET_API}?limit=100`);
  expect(dsResp.status()).toBe(200);
  const dsBody = (await dsResp.json()) as {
    datasets: Array<{
      dataset_urn: string;
      is_enabled: boolean;
      allowed: string[];
      item_count: number;
      approved_count: number;
      rejected_count: number;
      candidate_count: number;
      last_modified_at: string | null;
    }>;
    offset: number;
    limit: number;
    total_count: number;
  };
  expect(dsBody).toHaveProperty("offset");
  expect(dsBody).toHaveProperty("total_count");
  expect(Array.isArray(dsBody.datasets)).toBe(true);
  const byUrn = new Map(dsBody.datasets.map((d) => [d.dataset_urn, d]));
  const euRow = byUrn.get(EU_PROFILES_URN);
  expect(euRow, "eu_profiles must appear as a rollup row after its run").toBeTruthy();
  // eu_profiles boundary was enabled in step 3 with both kinds allowed.
  expect(euRow!.is_enabled, "eu_profiles boundary is enabled").toBe(true);
  expect(euRow!.allowed).toContain("dataset.description");
  expect(euRow!.item_count, "eu_profiles rollup item_count ≥ 1").toBeGreaterThanOrEqual(1);
  // candidate_count is candidate-level and ≥ item_count when candidates exist.
  expect(euRow!.candidate_count, "eu_profiles candidate_count ≥ 1").toBeGreaterThanOrEqual(1);
  for (const d of dsBody.datasets) {
    expect(typeof d.is_enabled).toBe("boolean");
    expect(Array.isArray(d.allowed)).toBe(true);
    // candidate_count is the total; approved + rejected never exceed it.
    expect(d.approved_count + d.rejected_count).toBeLessThanOrEqual(d.candidate_count);
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 7 — Approve an eu_profiles dataset.description candidate
// spec: USE_CASE_en.md §UC4 — Review; FRONTEND_METAGEN.md §Per-dataset candidate review
// ─────────────────────────────────────────────────────────────────────────────
test("UC4 step 7 — approve eu_profiles dataset.description candidate (conf_name = conf EU)", async ({
  page,
  adminApi,
}) => {
  if (!euRunId) test.skip(true, "step 4 did not complete");

  await page.goto(EU_DATASET_URL);
  await expect(page.getByText(EU_PROFILES_URN, { exact: false }).first()).toBeVisible({
    timeout: 15_000,
  });

  // -- UI assertion: items section + dataset.description foldable panel --
  // metagen-data-panel.tsx: <h3>attr/metagen/item</h3> + a CollapsiblePanel titled
  //   "dataset.description" (default-open) holding the ItemKindTable.
  await expect(page.getByText("attr/metagen/item", { exact: true })).toBeVisible({
    timeout: 10_000,
  });
  await expect(
    page.getByText("dataset.description", { exact: true }).first(),
  ).toBeVisible({ timeout: 30_000 });

  // -- Backend readiness poll + UI sync: wait until the freshly generated
  //    dataset.description llm_approved candidate is committed/queryable, then
  //    reload so the per-dataset page render reflects committed state (guards the
  //    observed run-complete→candidate-visible render lag without a blind timeout). --
  const ready = await waitForOpenCandidateThenReload(
    page,
    adminApi,
    EU_PROFILES_ENC,
    "dataset.description",
  );

  // -- Backend probe: find a dataset.description item + its llm_approved candidate --
  const itemsResp = await adminApi.get(
    `/api/v1/spoke/common/data/${EU_PROFILES_ENC}/attr/metagen/item`,
  );
  expect(itemsResp.status()).toBe(200);
  const itemsBody = (await itemsResp.json()) as {
    items: Array<{ item_id: string; kind: string; status: string }>;
  };
  const dsDescItem = itemsBody.items.find((i) => i.kind === "dataset.description");
  expect(dsDescItem, "no dataset.description item for eu_profiles after run").toBeTruthy();

  const dsDescItemEnc = encodeURIComponent(dsDescItem!.item_id);
  const detailResp = await adminApi.get(
    `/api/v1/spoke/common/data/${EU_PROFILES_ENC}/attr/metagen/item/${dsDescItemEnc}`,
  );
  expect(detailResp.status()).toBe(200);
  const detailBody = (await detailResp.json()) as {
    candidates: Array<{ candidate_id: string; status: string; conf_name: string | null }>;
  };
  const llmApproved = detailBody.candidates.find((c) => c.status === "llm_approved");
  // Under stub mode candidate generation is deterministic — the stub Producer
  // emits one candidate per target item and the stub Reviewer accepts it — so a
  // dataset.description item with NO llm_approved candidate is a stub regression,
  // not a benign skip. The readiness poll above already gave the backend a bounded
  // window to commit it. Assert the candidate PRECONDITION (mirrors the api-wired
  // counts.candidates_added >= 1 expectation) so a missing candidate FAILS the
  // test rather than silently no-opping (which would also skip step 9).
  // Real-LLM mode (stub_llm_client=false) may legitimately produce nothing, so
  // there the graceful early-return applies.
  // spec: BACKEND_LLM.md §Test Mode — metagen stub emits one candidate per item
  // spec: BACKEND.md §Event Catalogue — counts.candidates_added ≥ 1 under stub
  if (!llmApproved) {
    if (stubLlmClient) {
      throw new Error(
        "[uc4 step 7] STUB regression: no llm_approved candidate on eu_profiles " +
          "dataset.description after the conf EU run (bounded readiness poll also " +
          `found ${ready ? "one then it vanished" : "none"}). Under stub mode generation ` +
          "is deterministic (one candidate per target item), so the candidate must exist. " +
          "Check src/workflows/_stubs.py metagen_validate branch and " +
          "src/backend/metagen/prompts.py TARGET ITEMS block format.",
      );
    }
    console.warn("[uc4 step 7] real-LLM mode produced no llm_approved candidate; skipping approve.");
    return;
  }
  approvedEuDescCandidateId = llmApproved.candidate_id;
  // Per-candidate conf_name is the producing conf (conf EU here).
  // spec: FRONTEND_METAGEN.md §Per-dataset — candidates carry conf_id/conf_name
  expect(llmApproved.conf_name).toBe(CONF_EU_NAME);

  // -- UI gesture: the dataset.description ItemKindTable renders one candidate row
  //    per candidate (panel default-open; no expand gesture). Scope to the conf EU,
  //    llm_approved row via the row testid + data-attributes so the acted-on row
  //    is the one the backend probe tracked, then Approve it. --
  // item-kind-table.tsx: <tr data-testid="metagen-candidate-row"
  //   data-conf-name={conf_name} data-candidate-status={status}> with a per-row
  //   Approve button + producing-conf badge + status badge.
  const euDescRow = page
    .locator(
      `[data-testid="metagen-candidate-row"][data-conf-name="${CONF_EU_NAME}"]` +
        `[data-candidate-status="llm_approved"]`,
    )
    .first();
  await expect(euDescRow).toBeVisible({ timeout: 20_000 });

  // -- UI assertion: producing conf name badge + llm_approved status badge in-row --
  await expect(euDescRow.getByText("llm_approved", { exact: true }).first()).toBeVisible();
  await expect(euDescRow.getByText(CONF_EU_NAME, { exact: true }).first()).toBeVisible();

  // -- UI gesture: Approve → ConfirmDialog → Approve --
  // item-kind-table.tsx: per-row Approve button → ConfirmDialog title "Approve candidate"
  await euDescRow.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Approve candidate", exact: true }),
  ).toBeVisible({ timeout: 10_000 });
  await page.getByRole("button", { name: "Approve", exact: true }).last().click();

  // -- UI assertion: toast "Candidate approved" --
  await expect(page.getByText("Candidate approved", { exact: false }).first()).toBeVisible({
    timeout: 30_000,
  });

  // -- Backend probe: candidate is now approved --
  // spec: BACKEND.md §Approval flow — review transitions status to 'approved'
  const verifyResp = await adminApi.get(
    `/api/v1/spoke/common/data/${EU_PROFILES_ENC}/attr/metagen/item/${dsDescItemEnc}`,
  );
  expect(verifyResp.status()).toBe(200);
  const verifyBody = (await verifyResp.json()) as {
    candidates: Array<{ candidate_id: string; status: string }>;
  };
  const approved = verifyBody.candidates.find(
    (c) => c.candidate_id === approvedEuDescCandidateId,
  );
  expect(approved?.status).toBe("approved");
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 8 — Reject an eu_profiles column.description candidate
// spec: USE_CASE_en.md §UC4 — Review; FRONTEND_METAGEN.md §Per-dataset candidate review
// ─────────────────────────────────────────────────────────────────────────────
test("UC4 step 8 — reject eu_profiles column.description candidate", async ({
  page,
  adminApi,
}) => {
  if (!euRunId) test.skip(true, "step 4 did not complete");

  await page.goto(EU_DATASET_URL);
  await expect(page.getByText(EU_PROFILES_URN, { exact: false }).first()).toBeVisible({
    timeout: 15_000,
  });

  // -- Backend readiness poll + UI sync: wait until a non-approved column.description
  //    item carries a committed llm_approved candidate, then reload so the per-dataset
  //    page render surfaces the open candidate's "Review" card. This is the exact
  //    eventual-consistency lag the observed step-8 flake hit (backend had the open
  //    candidate but the UI had not rendered its Review button yet) — wait on the
  //    authoritative state, then sync the UI, instead of bumping a timeout. --
  await waitForOpenCandidateThenReload(
    page,
    adminApi,
    EU_PROFILES_ENC,
    "column.description",
    (i) => i.status !== "approved",
  );

  // -- Backend probe: pick a non-approved column.description item with llm_approved candidate --
  const itemsResp = await adminApi.get(
    `/api/v1/spoke/common/data/${EU_PROFILES_ENC}/attr/metagen/item`,
  );
  expect(itemsResp.status()).toBe(200);
  const itemsBody = (await itemsResp.json()) as {
    items: Array<{ item_id: string; kind: string; field_path: string | null; status: string }>;
  };
  const colItem = itemsBody.items.find(
    (i) => i.kind === "column.description" && i.status !== "approved",
  );
  // Stub mode is deterministic: eu_profiles has 8 masked column descriptions, all
  // boundary-allowed, none approved in step 7 — so a non-approved
  // column.description item MUST exist. A missing one is a stub regression, not a
  // benign skip (it would also cascade to skip step 9's reject assertion).
  // spec: BACKEND_LLM.md §Test Mode — stub emits one candidate per column item
  if (!colItem) {
    if (stubLlmClient) {
      throw new Error(
        "[uc4 step 8] STUB regression: no non-approved column.description item for " +
          "eu_profiles after the conf EU run. Under stub mode all 8 masked column " +
          "descriptions yield items deterministically. Check the masking seed and " +
          "src/workflows/_stubs.py metagen_validate branch.",
      );
    }
    console.warn("[uc4 step 8] real-LLM mode produced no column.description item; skipping reject.");
    return;
  }
  const colItemEnc = encodeURIComponent(colItem.item_id);
  const detailResp = await adminApi.get(
    `/api/v1/spoke/common/data/${EU_PROFILES_ENC}/attr/metagen/item/${colItemEnc}`,
  );
  expect(detailResp.status()).toBe(200);
  const detailBody = (await detailResp.json()) as {
    candidates: Array<{ candidate_id: string; status: string }>;
  };
  const llmApproved = detailBody.candidates.find((c) => c.status === "llm_approved");
  // Same stub-determinism precondition as step 7: the chosen non-approved column
  // item must carry an llm_approved candidate under stub mode.
  // spec: BACKEND_LLM.md §Test Mode — stub Reviewer accepts → llm_approved
  if (!llmApproved) {
    if (stubLlmClient) {
      throw new Error(
        "[uc4 step 8] STUB regression: column.description item has no llm_approved " +
          "candidate under stub mode. Check src/workflows/_stubs.py metagen_validate " +
          "branch and src/backend/metagen/prompts.py TARGET ITEMS block format.",
      );
    }
    console.warn("[uc4 step 8] real-LLM mode: no llm_approved candidate on column item; skipping.");
    return;
  }
  rejectedEuColCandidateId = llmApproved.candidate_id;

  // -- UI assertion: column.description foldable panel (default-open) --
  await expect(
    page.getByText("column.description", { exact: true }).first(),
  ).toBeVisible({ timeout: 30_000 });

  // -- UI gesture: reject the candidate of the SAME column the backend probe tracked
  //    (colItem.field_path / rejectedEuColCandidateId). The column.description
  //    ItemKindTable groups rows by column: the field_path cell (rowSpan) leads the
  //    column group, and each candidate is a <tr data-testid="metagen-candidate-row">.
  //    At this point only conf EU has run on eu_profiles (RIVAL runs in step 8b), so
  //    each column has exactly one conf-EU llm_approved candidate row. We scope to the
  //    row that carries the tracked column's field_path text AND the conf-EU /
  //    llm_approved attributes, so the acted-on candidate is the tracked one. --
  // item-kind-table.tsx: leading <td>{field_path}</td> + per-row Approve/Reject.
  const colFieldPath = colItem.field_path ?? "";
  const colRow = page
    .locator(
      `[data-testid="metagen-candidate-row"][data-conf-name="${CONF_EU_NAME}"]` +
        `[data-candidate-status="llm_approved"]`,
    )
    .filter({ hasText: colFieldPath })
    .first();
  // The row must render: the backend probe confirmed this column has an open
  // llm_approved conf-EU candidate, and we reloaded after the readiness poll. A
  // missing row under stub mode is a real UI bug (the per-kind table is not
  // surfacing the column), not a benign all-finalized state.
  const rowVisible = await colRow
    .waitFor({ state: "visible", timeout: 20_000 })
    .then(() => true)
    .catch(() => false);
  if (!rowVisible) {
    if (stubLlmClient) {
      throw new Error(
        "[uc4 step 8] STUB regression: backend has an open llm_approved conf-EU " +
          `candidate on column "${colFieldPath}" but its row did not render in the ` +
          "column.description ItemKindTable after readiness poll + reload + bounded " +
          "wait. The per-kind table (item-kind-table.tsx) is not surfacing this column.",
      );
    }
    console.warn("[uc4 step 8] real-LLM mode: tracked column row did not render; skipping.");
    return;
  }

  // -- UI assertion: this row's candidate is llm_approved --
  await expect(colRow.getByText("llm_approved", { exact: true }).first()).toBeVisible({
    timeout: 15_000,
  });

  // -- UI gesture: Reject this row's candidate → ConfirmDialog → Reject --
  // item-kind-table.tsx: per-row Reject button (eligible on llm_approved) →
  //   ConfirmDialog title "Reject candidate"
  const rejectButton = colRow.getByRole("button", { name: "Reject", exact: true });
  await expect(rejectButton).toBeVisible({ timeout: 10_000 });
  await rejectButton.click();
  await expect(
    page.getByRole("heading", { name: "Reject candidate", exact: true }),
  ).toBeVisible({ timeout: 10_000 });
  await page.getByRole("button", { name: "Reject", exact: true }).last().click();

  // -- UI assertion: toast "Candidate rejected" --
  await expect(page.getByText("Candidate rejected", { exact: false }).first()).toBeVisible({
    timeout: 30_000,
  });

  // -- Backend probe: candidate is now rejected --
  // spec: BACKEND.md §Approval flow — reject transitions status to 'rejected'
  const verifyResp = await adminApi.get(
    `/api/v1/spoke/common/data/${EU_PROFILES_ENC}/attr/metagen/item/${colItemEnc}`,
  );
  expect(verifyResp.status()).toBe(200);
  const verifyBody = (await verifyResp.json()) as {
    candidates: Array<{ candidate_id: string; status: string }>;
  };
  const rejected = verifyBody.candidates.find(
    (c) => c.candidate_id === rejectedEuColCandidateId,
  );
  expect(rejected?.status).toBe("rejected");
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 8b — Cross-conf approval exclusivity on a SHARED item
// The headline UC4 invariant: approving a candidate supersedes any approved
// sibling GLOBALLY across confs (the partial unique index UNIQUE
// (dataset_urn, item_id) WHERE status='approved' holds across all confs). A RIVAL
// conf also scoped to eu_profiles produces its own llm_approved candidates on the
// SAME column items as conf EU. We approve conf EU's candidate on a shared item,
// then approve conf RIVAL's candidate on that same item — the second approval must
// demote conf EU's just-approved sibling back to llm_approved, leaving exactly one
// approved candidate (RIVAL's).
//
// The approval gestures run through the per-dataset UI (ItemKindTable candidate
// row [data-testid="metagen-candidate-row"] Approve → ConfirmDialog). The end-state
// invariant is read back from the backend GET item-detail (the source of truth —
// the POST echo is not used), mirroring the api-wired step 8d exactly. The UI is
// independently confirmed to surface exactly one approved metagen-candidate-row
// whose producing-conf badge is RIVAL's.
//
// spec: USE_CASE_en.md §UC4 — Review (one approved description per item)
// spec: BACKEND.md §Approval flow — approving flips the previously-approved
//   sibling (possibly from a different conf) back to llm_approved in one txn
// spec: BACKEND_SCHEMA.md §metagen_candidates — partial unique index
//   UNIQUE (dataset_urn, item_id) WHERE status='approved' (global across confs)
// spec: mirrors tests/integration/api_wired/test_uc4_01_metadata_generation.py step 8d
// ─────────────────────────────────────────────────────────────────────────────
test("UC4 step 8b — cross-conf approval supersedes the sibling globally (one approved per item)", async ({
  page,
  adminApi,
}) => {
  if (!euRunId || !confEuId) test.skip(true, "steps 2-4 did not complete");
  // This invariant is exercised deterministically only under stub mode (each conf
  // emits one candidate per open column item, guaranteeing a shared two-conf item).
  // Real-LLM mode may not produce a candidate from both confs on the same item.
  test.skip(
    !stubLlmClient,
    "real-LLM mode: a shared two-conf candidate item is not guaranteed; skip cross-conf demotion",
  );

  // -- Setup: create RIVAL conf scoped to eu_profiles and run it --
  // Conf creation UI is already covered by step 2; here we provision RIVAL via the
  // public API as setup for the invariant.
  // spec: API.md §Metadata Generation — POST /metagen/conf → 201
  const rivalResp = await adminApi.post(CONF_API, {
    data: {
      name: CONF_RIVAL_NAME,
      is_enabled: true,
      dataset_filter: { dataset_urns: [EU_PROFILES_URN] },
      result_limit: 3,
      overwrite_pending: true,
    },
  });
  expect(rivalResp.status(), `POST conf RIVAL must return 201: ${await rivalResp.text()}`).toBe(
    201,
  );
  confRivalId = ((await rivalResp.json()) as { id: string }).id;

  const rivalRun = await adminApi.post(`${CONF_API}/${confRivalId}/method/run`, {
    timeout: 120_000,
  });
  expect(rivalRun.status(), `POST conf RIVAL run must return 200: ${await rivalRun.text()}`).toBe(
    200,
  );
  expect(((await rivalRun.json()) as { conf_id: string }).conf_id).toBe(confRivalId);

  // -- Find a SHARED eu_profiles column item holding llm_approved candidates from
  //    BOTH conf EU and conf RIVAL. dataset.description is already approved (step 7)
  //    and the step-8 column was rejected, so scan the remaining column items. --
  const itemsResp = await adminApi.get(
    `/api/v1/spoke/common/data/${EU_PROFILES_ENC}/attr/metagen/item`,
  );
  expect(itemsResp.status()).toBe(200);
  const itemsBody = (await itemsResp.json()) as {
    items: Array<{ item_id: string; kind: string; field_path: string | null; status: string }>;
  };

  // The shared item is always a column.description item (the scan below filters on
  // item.kind === "column.description").
  let sharedItemId: string | null = null;
  let sharedFieldPath: string | null = null;
  let euCandId: string | null = null;
  let rivalCandId: string | null = null;
  for (const item of itemsBody.items) {
    if (item.kind !== "column.description" || item.status === "approved") continue;
    // The step-8 rejected item carries no llm_approved candidate, so the two-conf
    // check below naturally excludes it — no explicit skip needed.
    const detailResp = await adminApi.get(
      `/api/v1/spoke/common/data/${EU_PROFILES_ENC}/attr/metagen/item/${encodeURIComponent(
        item.item_id,
      )}`,
    );
    if (detailResp.status() !== 200) continue;
    const detail = (await detailResp.json()) as {
      candidates: Array<{ candidate_id: string; status: string; conf_id: string | null }>;
    };
    const euC = detail.candidates.find(
      (c) => c.status === "llm_approved" && c.conf_id === confEuId,
    );
    const rivalC = detail.candidates.find(
      (c) => c.status === "llm_approved" && c.conf_id === confRivalId,
    );
    if (euC && rivalC) {
      sharedItemId = item.item_id;
      sharedFieldPath = item.field_path;
      euCandId = euC.candidate_id;
      rivalCandId = rivalC.candidate_id;
      break;
    }
  }
  // Under stub mode a shared two-conf item MUST exist; absence is a stub regression,
  // not a benign skip. Fail loud (mirrors api-wired step 8d assertion).
  expect(
    sharedItemId,
    "STUB regression: no eu_profiles column item carries llm_approved candidates from " +
      "BOTH conf EU and conf RIVAL after both ran over the shared dataset. Each conf's " +
      "stub Producer emits one candidate per open column item, so a shared item must exist. " +
      "Check src/workflows/_stubs.py metagen_validate branch.",
  ).toBeTruthy();
  const sharedItemEnc = encodeURIComponent(sharedItemId!);

  // -- UI gesture: on the per-dataset page, approve conf EU's candidate on the SHARED
  //    column. The column.description ItemKindTable groups rows by column; the shared
  //    column carries two candidate rows (conf EU + conf RIVAL, both llm_approved).
  //    Scope to the row carrying the shared column's field_path AND the conf-EU /
  //    llm_approved attributes so the tracked id (euCandId on sharedItemId) and the
  //    acted-on candidate are the same — the same tracked-vs-acted discipline as
  //    step 8. A page-wide conf-EU row .last() would pick an arbitrary column's EU
  //    candidate, not the shared column, and the sharedItemEnc verify below would
  //    then fail. --
  await page.goto(EU_DATASET_URL);
  await expect(page.getByText(EU_PROFILES_URN, { exact: false }).first()).toBeVisible({
    timeout: 15_000,
  });

  // item-kind-table.tsx: <tr data-testid="metagen-candidate-row"
  //   data-conf-name={conf_name} data-candidate-status={status}> with the leading
  //   field_path cell (rowSpan) on the column group. The shared column's EU row
  //   carries the field_path text (the rowSpan field cell is the group's first row).
  const sharedFieldText = sharedFieldPath ?? "";
  const euRow = page
    .locator(
      `[data-testid="metagen-candidate-row"][data-conf-name="${CONF_EU_NAME}"]` +
        `[data-candidate-status="llm_approved"]`,
    )
    .filter({ hasText: sharedFieldText })
    .first();
  await expect(
    euRow,
    `conf EU llm_approved row for shared column "${sharedFieldText}" must render`,
  ).toBeVisible({ timeout: 20_000 });
  await euRow.getByRole("button", { name: "Approve", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Approve candidate", exact: true }),
  ).toBeVisible({ timeout: 10_000 });
  await page.getByRole("button", { name: "Approve", exact: true }).last().click();
  await expect(page.getByText("Candidate approved", { exact: false }).first()).toBeVisible({
    timeout: 30_000,
  });

  // -- Backend probe: conf EU's candidate is now the sole approved one (precondition
  //    for the demotion that follows). --
  const afterEu = await adminApi.get(
    `/api/v1/spoke/common/data/${EU_PROFILES_ENC}/attr/metagen/item/${sharedItemEnc}`,
  );
  expect(afterEu.status()).toBe(200);
  const afterEuBody = (await afterEu.json()) as {
    candidates: Array<{ candidate_id: string; status: string; conf_id: string | null }>;
  };
  const euApprovedNow = afterEuBody.candidates.filter((c) => c.status === "approved");
  expect(euApprovedNow.length, "exactly one approved candidate after approving EU").toBe(1);
  expect(euApprovedNow[0]!.candidate_id).toBe(euCandId);

  // -- RIVAL cross-conf approval: driven via the public review API by candidate_id. --
  // Division of labour for this step: the EU approval above exercises the real UI
  // approve gesture on a shared item (ItemKindTable candidate row
  // [data-testid="metagen-candidate-row"] Approve → ConfirmDialog), giving genuine
  // UI coverage of the approve flow. The RIVAL cross-conf approval is driven
  // through the public review API by candidate_id (not a sibling-click) for
  // DETERMINISTIC targeting: the metagen-candidate-row DOM exposes no candidate_id,
  // so selecting the correct sibling among multiple candidate rows after the
  // post-approval re-render is non-deterministic and would act on an arbitrary
  // candidate. The cross-conf demotion invariant itself is asserted authoritatively
  // from GET item-detail below (the global one-approved-per-item check), independent
  // of which surface triggered the approval. The UI approve/reject gestures are
  // already covered by steps 7 and 8 (and the EU approval just above).
  // spec: API.md §Metadata Generation — POST .../candidate/{id}/method/review
  const rivalReview = await adminApi.post(
    `/api/v1/spoke/common/data/${EU_PROFILES_ENC}/attr/metagen/item/${sharedItemEnc}/candidate/${rivalCandId}/method/review`,
    { data: { verdict: "approve", reason: "uc4-e2e cross-conf demotion invariant" } },
  );
  expect(
    rivalReview.status(),
    `RIVAL cross-conf approve via review API must return 200: ${await rivalReview.text()}`,
  ).toBe(200);

  // -- Backend probe (authoritative invariant): exactly one approved candidate, and
  //    it is RIVAL's; conf EU's previously-approved candidate is demoted back to
  //    llm_approved. Read truth from GET item-detail (not the POST echo). --
  // spec: BACKEND_SCHEMA.md §metagen_candidates — global one-approved-per-item
  const afterRival = await adminApi.get(
    `/api/v1/spoke/common/data/${EU_PROFILES_ENC}/attr/metagen/item/${sharedItemEnc}`,
  );
  expect(afterRival.status()).toBe(200);
  const afterRivalBody = (await afterRival.json()) as {
    candidates: Array<{ candidate_id: string; status: string; conf_id: string | null }>;
  };
  const approvedAfter = afterRivalBody.candidates.filter((c) => c.status === "approved");
  expect(
    approvedAfter.length,
    "exactly one approved candidate may exist per item, globally across confs",
  ).toBe(1);
  expect(
    approvedAfter[0]!.candidate_id,
    "conf RIVAL's candidate must be the sole approved candidate after its approval",
  ).toBe(rivalCandId);
  expect(approvedAfter[0]!.conf_id, "the sole approved candidate must belong to conf RIVAL").toBe(
    confRivalId,
  );
  const euAfter = afterRivalBody.candidates.find((c) => c.candidate_id === euCandId);
  expect(
    euAfter?.status,
    "conf EU's previously-approved candidate must be demoted back to llm_approved",
  ).toBe("llm_approved");

  // -- UI confirmation: the shared column now surfaces exactly one approved candidate
  //    row, carrying conf RIVAL's producing-conf badge. The column.description
  //    ItemKindTable re-fetches the item after review, so the demoted EU row reads
  //    llm_approved and RIVAL's reads approved. RIVAL approved exactly one candidate
  //    (on the shared column), so the (RIVAL, approved) row is unique. --
  await page.reload();
  await expect(page.getByText(EU_PROFILES_URN, { exact: false }).first()).toBeVisible({
    timeout: 15_000,
  });
  const sharedFieldTextAfter = sharedFieldPath ?? "";
  const approvedRow = page
    .locator(
      `[data-testid="metagen-candidate-row"][data-conf-name="${CONF_RIVAL_NAME}"]` +
        `[data-candidate-status="approved"]`,
    )
    .filter({ hasText: sharedFieldTextAfter })
    .first();
  await expect(
    approvedRow,
    "the approved candidate row must carry conf RIVAL's producing-conf badge",
  ).toBeVisible({ timeout: 20_000 });
  // The row's status badge reads "approved" and its conf badge is RIVAL's.
  await expect(approvedRow.getByText("approved", { exact: true }).first()).toBeVisible();
  await expect(approvedRow.getByText(CONF_RIVAL_NAME, { exact: true }).first()).toBeVisible();
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 9 — Per-dataset events show CANDIDATE_APPROVE + CANDIDATE_REJECT
// spec: USE_CASE_en.md §UC4 — candidate review creates audit events
// spec: BACKEND.md §Event Catalogue — CANDIDATE_APPROVE / CANDIDATE_REJECT detail
// ─────────────────────────────────────────────────────────────────────────────
test("UC4 step 9 — per-dataset events include CANDIDATE_APPROVE and CANDIDATE_REJECT", async ({
  page,
  adminApi,
}) => {
  // Under stub mode steps 7 and 8 are guaranteed to have produced an approve and
  // a reject (they throw otherwise), so BOTH ids must be set. Asserting this here
  // closes the cascade where a skipped 7/8 silently skips 9 too. Real-LLM mode may
  // legitimately have produced no candidate to review, so the skip applies there.
  if (stubLlmClient) {
    expect(
      approvedEuDescCandidateId,
      "stub mode: step 7 must have approved a dataset.description candidate",
    ).toBeTruthy();
    expect(
      rejectedEuColCandidateId,
      "stub mode: step 8 must have rejected a column.description candidate",
    ).toBeTruthy();
  } else if (!approvedEuDescCandidateId && !rejectedEuColCandidateId) {
    test.skip(true, "real-LLM mode: steps 7/8 produced no review actions");
  }

  await page.goto(EU_DATASET_URL);
  await expect(page.getByText(EU_PROFILES_URN, { exact: false }).first()).toBeVisible({
    timeout: 15_000,
  });

  // -- UI assertion: the metagen review events surface in the unified "Events" panel --
  // The former per-feature "event/metagen" section is folded into the unified
  // Events panel (a single timeline with a major-type filter; default all checked).
  // spec: FRONTEND_BASIC.md §Per-dataset page (Events panel).
  const eventsPanel = page.getByRole("button", { name: /events/i }).first();
  await expect(eventsPanel).toBeVisible({ timeout: 10_000 });
  if ((await eventsPanel.getAttribute("aria-expanded")) === "false") {
    await eventsPanel.click();
  }

  if (approvedEuDescCandidateId) {
    await expect(
      page.getByText("METAGEN.CANDIDATE_APPROVE", { exact: false }).first(),
    ).toBeVisible({ timeout: 30_000 });
  }
  if (rejectedEuColCandidateId) {
    await expect(
      page.getByText("METAGEN.CANDIDATE_REJECT", { exact: false }).first(),
    ).toBeVisible({ timeout: 30_000 });
  }

  // -- Backend probe: per-dataset event feed carries the review events with detail keys --
  const evResp = await adminApi.get(
    `/api/v1/spoke/common/data/${EU_PROFILES_ENC}/event/metagen?limit=20`,
  );
  expect(evResp.status()).toBe(200);
  const evBody = (await evResp.json()) as {
    events: Array<{ event_type: string; detail?: Record<string, unknown> }>;
    offset: number;
    limit: number;
    total_count: number;
  };
  expect(evBody).toHaveProperty("offset");
  expect(evBody).toHaveProperty("total_count");

  if (approvedEuDescCandidateId) {
    const ev = evBody.events.find((e) => e.event_type === "METAGEN.CANDIDATE_APPROVE");
    expect(ev, "CANDIDATE_APPROVE event missing after approval").toBeTruthy();
    expect(ev!.detail).toHaveProperty("item_id");
    expect(ev!.detail).toHaveProperty("candidate_id");
    expect(ev!.detail).toHaveProperty("reason");
  }
  if (rejectedEuColCandidateId) {
    const ev = evBody.events.find((e) => e.event_type === "METAGEN.CANDIDATE_REJECT");
    expect(ev, "CANDIDATE_REJECT event missing after rejection").toBeTruthy();
    expect(ev!.detail).toHaveProperty("item_id");
    expect(ev!.detail).toHaveProperty("candidate_id");
    expect(ev!.detail).toHaveProperty("reason");
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 10 — /metagen/uncovered lists undocumented datasets; toggle widens coverage
// spec: FRONTEND_METAGEN.md §Uncovered — GET /spoke/metagen/uncovered, include_disallowed
// ─────────────────────────────────────────────────────────────────────────────
test("UC4 step 10 — uncovered view + include_disallowed toggle", async ({
  page,
  adminApi,
}) => {
  if (!confEuId || !confOeId) test.skip(true, "step 2 did not create both confs");

  await page.goto(UNCOVERED_URL);
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: heading + toggle present --
  // uncovered/page.tsx: <h1>Uncovered datasets</h1>, Checkbox id="uncovered-include-disallowed"
  await expect(
    page.getByRole("heading", { name: "Uncovered datasets", exact: true }),
  ).toBeVisible({ timeout: 15_000 });
  const toggle = page.locator("#uncovered-include-disallowed");
  await expect(toggle).toBeVisible({ timeout: 10_000 });

  // -- Backend probe: default (off) returns only no_conf_match rows --
  // spec: FRONTEND_METAGEN.md §Uncovered — off shows no_conf_match only
  const offResp = await adminApi.get(UNCOVERED_API);
  expect(offResp.status()).toBe(200);
  const offBody = (await offResp.json()) as {
    datasets: Array<{ dataset_urn: string; reason: string }>;
    offset: number;
    limit: number;
    total_count: number;
  };
  expect(offBody).toHaveProperty("offset");
  expect(offBody).toHaveProperty("total_count");
  // The reset-seed estate registers many datasets (customers/orders/shipping/
  // catalog schemas) but only eu_profiles + orders.events are scoped by an
  // enabled conf, so the no_conf_match set is non-empty by construction. Assert
  // non-empty BEFORE iterating so the reason check below cannot pass vacuously on
  // an empty estate (which would also mean the seed baseline is wrong).
  // spec: API.md §Metadata Generation — uncovered = registered datasets reached
  //   by no enabled conf; reason=no_conf_match
  expect(
    offBody.total_count,
    "uncovered (off) must list ≥1 no_conf_match dataset given the reset-seed estate " +
      "has registered datasets outside the two conf scopes",
  ).toBeGreaterThan(0);
  expect(offBody.datasets.length).toBeGreaterThan(0);
  for (const row of offBody.datasets) {
    expect(row.reason).toBe("no_conf_match");
  }

  // -- UI gesture: enable include_disallowed --
  if (!(await toggle.isChecked().catch(() => false))) await toggle.click();

  // -- Backend probe: on returns no_conf_match ∪ boundary_blocked --
  // spec: FRONTEND_METAGEN.md §Uncovered — on additionally shows boundary_blocked rows
  const onResp = await adminApi.get(`${UNCOVERED_API}?include_disallowed=true`);
  expect(onResp.status()).toBe(200);
  const onBody = (await onResp.json()) as {
    datasets: Array<{ dataset_urn: string; reason: string }>;
    total_count: number;
  };
  // The on-set is a superset of the non-empty off-set, so it is also non-empty;
  // assert before iterating so the reason classification below exercises real rows.
  expect(onBody.datasets.length).toBeGreaterThan(0);
  for (const row of onBody.datasets) {
    expect(["no_conf_match", "boundary_blocked"]).toContain(row.reason);
  }
  // Widening cannot drop rows: the on-set is a superset of the off-set.
  expect(onBody.total_count).toBeGreaterThanOrEqual(offBody.total_count);

  // -- UI assertion: non-empty widened set renders rows; otherwise the empty state --
  if (onBody.datasets.length > 0) {
    await expect(
      page.getByRole("cell", { name: onBody.datasets[0]!.dataset_urn, exact: false }).first(),
    ).toBeVisible({ timeout: 15_000 });
  } else {
    await expect(page.getByText(/no uncovered datasets/i)).toBeVisible({ timeout: 15_000 });
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Real-LLM add-on: EU run produced ≥1 candidate.
// Skips unless stub_llm_client is false in /admin/conf (mirrors api-wired gating).
// spec: BACKEND.md §Event Catalogue — METAGEN.RUN_COMPLETE detail.counts.candidates_added
// spec: TESTING.md §Stub Toggles — real-LLM variant: PATCH /admin/conf stub_llm_client=false
// ─────────────────────────────────────────────────────────────────────────────
test("UC4 real-LLM — EU run produced ≥1 candidate (gated on stub_llm_client=false)", async ({
  adminApi,
}) => {
  test.skip(stubLlmClient, "stub_llm_client is true — real-LLM assertion skipped");
  if (!euRunId) test.skip(true, "step 4 did not complete");

  const evResp = await adminApi.get(`${GLOBAL_EVENT_API}?limit=100`);
  expect(evResp.status()).toBe(200);
  const evBody = (await evResp.json()) as {
    events: Array<{ event_type: string; detail?: Record<string, unknown> }>;
  };
  const euRunEvent = evBody.events.find(
    (e) =>
      e.event_type === "METAGEN.RUN_COMPLETE" &&
      (e.detail as Record<string, unknown>)?.["run_id"] === euRunId,
  );
  expect(euRunEvent, "EU run RUN_COMPLETE must be present for real-LLM assertion").toBeTruthy();
  const counts = (euRunEvent!.detail as Record<string, unknown>)?.["counts"] as
    | Record<string, number>
    | undefined;
  expect(counts).toBeDefined();
  // spec: BACKEND.md §Event Catalogue — METAGEN.RUN_COMPLETE detail.counts carries
  //   candidates_added on a real (non-dry-run) run; real LLM must produce ≥1.
  expect(counts!["candidates_added"]).toBeGreaterThanOrEqual(1);
});
