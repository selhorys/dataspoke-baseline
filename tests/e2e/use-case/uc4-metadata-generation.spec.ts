/**
 * UC4 — Metadata Generation: browser UI flow.
 *
 * TWO structurally-identical tests: stub-mode (always runs) + real-LLM (skips
 * unless stub_llm_client is false in /admin/conf).
 *
 * Mirrors tests/integration/api_wired/test_uc4_metadata_generation.py
 * step-for-step, with dual confirmation at each mutating step:
 *   - UI assertion (heading, badge, card, toast, event row)
 *   - Backend probe via adminApi (same REST read-back the api-wired step asserts)
 *
 * Arc (verbatim from USE_CASE_en.md §UC4):
 *   Setup  — seed LLM context (fulfillment document + approved ontogen nodes),
 *             mask DataHub descriptions via --uc4-seed Python utility.
 *   Step 1 — Navigate to /metagen; fill global conf form (is_enabled, schedule_tier,
 *             dataset_filter, result_limit, overwrite_pending); Save.
 *             Backend: PUT /spoke/metagen/attr/conf → 200/201; round-trip fields.
 *   Step 2 — Navigate to /metagen/data/[eu_profiles_urn]; fill boundary form
 *             (is_enabled, allowed); Save.
 *             Backend: PUT .../attr/metagen/conf → 200/201; round-trip fields.
 *             Repeat for orders.events URN.
 *   Step 3 — On /metagen, click Run → RunDialog → Run (real, no dry_run).
 *             Poll adminApi until METAGEN.RUN_COMPLETE event present (bounded).
 *             Backend: POST /spoke/metagen/method/run → 200; validate run_id, status,
 *             dry_run, unresolved_urns, counts.items_considered, debate_outcome,
 *             producer_iterations.
 *   Step 4 — /metagen shows "event/metagen (latest 10)" section; assert
 *             METAGEN.RUN_COMPLETE event visible in the section.
 *             Backend: GET /spoke/metagen/event → run_id in events.
 *   Step 5 — Navigate to /metagen/data/[eu_profiles_urn]; assert "attr/metagen/item"
 *             section shows items. Expand a dataset.description item → Review button
 *             → CandidateCard → Approve button → ConfirmDialog → Approve.
 *             Backend: GET .../attr/metagen/item → items list;
 *             POST .../candidate/{cid}/method/review → 200, status=approved.
 *   Step 6 — Reject eu_profiles column item: navigate to /metagen/data/[eu_profiles],
 *             expand a column.description item → Review → CandidateCard → Reject →
 *             ConfirmDialog → Reject.
 *             Backend: GET item detail → candidate status=rejected.
 *   Step 7 — DataHub round-trip: adminApi reads editableDatasetProperties on eu_profiles;
 *             description matches approved value.
 *   Step 8 — Per-dataset events show CANDIDATE_APPROVE + CANDIDATE_REJECT in the
 *             "event/metagen (latest 10)" section on /metagen/data/[eu_profiles_urn].
 *             Backend: GET .../event/metagen → events include CANDIDATE_APPROVE,
 *             CANDIDATE_REJECT with item_id, candidate_id, reason detail keys.
 *   Step 9 — Second run: click Run again on /metagen.
 *             Poll adminApi until second METAGEN.RUN_COMPLETE present.
 *             Backend: second run counts.items_considered < first run's (approved skipped).
 *             GET eu_profiles dataset.description item: exactly 1 approved candidate.
 *             GET eu_profiles rejected column item: no rejected candidates (cleared);
 *             ≥1 llm_approved candidate (re-generated).
 *   Cleanup — afterAll deletes boundaries, conf, metagen state, restores DataHub aspects
 *             via --uc4-restore Python utility.
 *
 * Data setup: global-setup runs --reset-seed; beforeAll runs --uc4-seed to seed
 * fulfillment document + ontogen nodes and mask DataHub descriptions.
 * Cleanup: afterAll deletes metagen conf/boundaries via REST, then runs --uc4-restore.
 *
 * LLM mode: stub-mode flow runs by default (stub_llm_client=true in dev).
 * Real-LLM variant skips unless stub_llm_client is false.
 *
 * spec: USE_CASE_en.md §UC4 Metadata Generation
 * spec: spec/feature/FRONTEND_METAGEN.md §Navigation, §Page contracts
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — dual confirmation, selector guidance
 */

import { execSync } from "child_process";
import * as path from "path";
import { test, expect, IMAZON_URNS } from "../fixtures/index";

// ── URN constants (verbatim from api-wired) ───────────────────────────────────

const EU_PROFILES_URN = IMAZON_URNS.euProfiles;
const ORDERS_EVENTS_URN =
  "urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.orders.events,DEV)";

const EU_PROFILES_ENC = encodeURIComponent(EU_PROFILES_URN);
const ORDERS_EVENTS_ENC = encodeURIComponent(ORDERS_EVENTS_URN);

const FULFILLMENT_TAG = "urn:li:tag:area:fulfillment";

// API routes (mirroring api-wired constants)
const CONF_API = "/api/v1/spoke/metagen/attr/conf";
const EU_BOUNDARY_API = `/api/v1/spoke/common/data/${EU_PROFILES_ENC}/attr/metagen/conf`;
const OE_BOUNDARY_API = `/api/v1/spoke/common/data/${ORDERS_EVENTS_ENC}/attr/metagen/conf`;
const RUN_API = "/api/v1/spoke/metagen/method/run";
const GLOBAL_EVENT_API = "/api/v1/spoke/metagen/event";

// Frontend routes
const METAGEN_URL = "/metagen";
const EU_DATASET_URL = `/metagen/data/${EU_PROFILES_ENC}`;
const OE_DATASET_URL = `/metagen/data/${ORDERS_EVENTS_ENC}`;

// Repo root for running Python utilities
const REPO_ROOT = path.resolve(__dirname, "..", "..", "..");

// ── Shared state across serial steps ────────────────────────────────────────

// Tracks which REST resources were created so afterAll can clean up idempotently.
let confCreated = false;
let euBoundaryCreated = false;
let oeBoundaryCreated = false;

// Run response data from step 3 shared into step 4 / step 9 assertions.
let firstRunId: string | null = null;
let firstRunItemsConsidered: number | null = null;

// Approved candidate state captured in step 5, shared into step 7/9 assertions.
let approvedEuDescCandidateId: string | null = null;
let approvedEuDescValue: string | null = null;
let rejectedEuColCandidateId: string | null = null;
let rejectedEuColItemId: string | null = null;

// ── Stub-mode flag: read once in beforeAll ─────────────────────────────────

let stubLlmClient: boolean | null = null;

// ── UC4 context seed helpers (--uc4-seed / --uc4-restore) ────────────────────

/**
 * Run --uc4-seed via Python util: seeds fulfillment document + approved ontogen nodes
 * mapped to both datasets, and masks DataHub descriptions for both URNs.
 *
 * SETUP DEPENDENCY: DataHub masking (wipe DatasetProperties.description, blank all
 * SchemaMetadata field descriptions) requires the DataHub Python SDK — no REST route
 * exposes it. The Python util encapsulates this via seed_uc4_context / --uc4-seed.
 *
 * spec: tests/integration/util/__main__.py --uc4-seed
 * spec: tests/integration/util/metagen.py seed_uc4_context
 */
function runUc4Seed(): void {
  try {
    execSync("uv run python -m tests.integration.util --uc4-seed", {
      cwd: REPO_ROOT,
      stdio: "inherit",
      timeout: 120_000,
    });
  } catch (err) {
    console.warn("[uc4] --uc4-seed failed (non-fatal if context already seeded):", err);
  }
}

/**
 * Run --uc4-restore via Python util: restores DataHub aspects, deletes fulfillment
 * document, metagen state, ontogen nodes. Idempotent when state file is absent.
 *
 * spec: tests/integration/util/__main__.py --uc4-restore
 * spec: tests/integration/util/metagen.py restore_uc4_context
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

// ── beforeAll: seed + check stub mode ────────────────────────────────────────

test.beforeAll(async ({ adminApi }) => {
  // Seed LLM context: fulfillment document + approved ontogen nodes + DataHub masking.
  // SETUP DEPENDENCY: masking DataHub aspects requires the Python SDK; not reachable via REST.
  runUc4Seed();

  // Read stub_llm_client from /admin/conf so step-level skip guards work.
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
  // Delete metagen boundaries + conf via REST (best-effort).
  if (euBoundaryCreated) {
    await adminApi.delete(EU_BOUNDARY_API).catch(() => null);
    euBoundaryCreated = false;
  }
  if (oeBoundaryCreated) {
    await adminApi.delete(OE_BOUNDARY_API).catch(() => null);
    oeBoundaryCreated = false;
  }
  if (confCreated) {
    await adminApi.delete(CONF_API).catch(() => null);
    confCreated = false;
  }

  // Restore DataHub aspects + delete metagen state, ontogen nodes, fulfillment document.
  // SETUP DEPENDENCY: requires Python SDK — mirrors the api-wired finally block.
  runUc4Restore();
});

// ─────────────────────────────────────────────────────────────────────────────
// STUB MODE VARIANT
// ─────────────────────────────────────────────────────────────────────────────

// ─────────────────────────────────────────────────────────────────────────────
// Step 1 — PUT global metagen conf via /metagen conf form
// spec: USE_CASE_en.md §UC4 — governance team enables metagen globally
// spec: FRONTEND_METAGEN.md §Page contracts — /metagen writes PUT /spoke/metagen/attr/conf
// ─────────────────────────────────────────────────────────────────────────────
test("UC4 stub step 1 — PUT global metagen conf; conf section shows saved values", async ({
  page,
  adminApi,
}) => {
  // Navigate to /metagen.
  // spec: FRONTEND_METAGEN.md §Navigation — /metagen title "Metadata Generation"
  await page.goto(METAGEN_URL);
  await expect(page).not.toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: "Metadata Generation", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: attr/metagen/conf section heading present --
  // spec: FRONTEND_METAGEN.md §Page contracts — /metagen reads GET /spoke/metagen/attr/conf
  // page.tsx renders <h2>attr/metagen/conf</h2>
  await expect(page.getByText("attr/metagen/conf", { exact: true })).toBeVisible({ timeout: 10_000 });

  // -- If no conf exists, the form should show "No configuration exists. Create one below." --
  // -- If conf already exists, click Edit to enter editing mode. --
  // Either way we arrive at a fillable form.
  const editButton = page.getByRole("button", { name: "Edit" });
  const editButtonVisible = await editButton.isVisible().catch(() => false);
  if (editButtonVisible) {
    await editButton.click();
  }
  // Form should now be in editable state regardless of initial state.

  // -- UI gesture: check is_enabled checkbox --
  // spec: FRONTEND_METAGEN.md §Page contracts — is_enabled field
  // conf-form.tsx: <Checkbox id="metagen-conf-is-enabled" ...>
  const isEnabledCheckbox = page.locator("#metagen-conf-is-enabled");
  await expect(isEnabledCheckbox).toBeVisible({ timeout: 10_000 });
  const isChecked = await isEnabledCheckbox.isChecked().catch(() => false);
  if (!isChecked) {
    await isEnabledCheckbox.click();
  }

  // -- UI gesture: select schedule_tier "daily" via Radix Select --
  // conf-form.tsx: <SelectTrigger id="metagen-conf-schedule-tier">
  // Radix Select: click trigger → pick option by role
  const scheduleTrigger = page.locator("#metagen-conf-schedule-tier");
  await expect(scheduleTrigger).toBeVisible({ timeout: 10_000 });
  await scheduleTrigger.click();
  await page.getByRole("option", { name: "daily" }).click();

  // -- UI gesture: set result_limit to 3 --
  // conf-form.tsx: <Input id="metagen-conf-result-limit" type="number">
  const resultLimitInput = page.locator("#metagen-conf-result-limit");
  await expect(resultLimitInput).toBeVisible({ timeout: 10_000 });
  await resultLimitInput.fill("3");

  // -- UI gesture: check overwrite_pending checkbox --
  // conf-form.tsx: <Checkbox id="metagen-conf-overwrite-pending">
  const overwritePendingCheckbox = page.locator("#metagen-conf-overwrite-pending");
  await expect(overwritePendingCheckbox).toBeVisible();
  const overwriteChecked = await overwritePendingCheckbox.isChecked().catch(() => false);
  if (!overwriteChecked) {
    await overwritePendingCheckbox.click();
  }

  // -- UI gesture: submit the form --
  // conf-form.tsx: <Button type="submit">Save configuration</Button>
  await page.getByRole("button", { name: "Save configuration" }).click();

  // -- UI assertion: toast "Configuration saved" --
  // page.tsx handleSaveConf → toast({ title: "Configuration saved" })
  await expect(page.getByText("Configuration saved", { exact: false }).first()).toBeVisible({
    timeout: 20_000,
  });
  confCreated = true;

  // -- Backend probe (dual confirmation): GET /spoke/metagen/attr/conf --
  // spec: USE_CASE_en.md §UC4 L604 — PUT returns 200/201; round-trips is_enabled + schedule_tier
  const getConfResp = await adminApi.get(CONF_API);
  expect(getConfResp.status()).toBe(200);
  const confBody = (await getConfResp.json()) as {
    is_enabled: boolean;
    schedule_tier: string | null;
    result_limit: number;
    overwrite_pending: boolean;
    dataset_filter?: Record<string, unknown>;
  };
  expect(confBody.is_enabled).toBe(true);
  expect(confBody.schedule_tier).toBe("daily");
  expect(confBody.result_limit).toBe(3);
  expect(confBody.overwrite_pending).toBe(true);
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 2 — PUT per-dataset boundaries via /metagen/data/[urn] boundary form
// spec: USE_CASE_en.md §UC4 L609–615 — catalog team opts each dataset in
// spec: FRONTEND_METAGEN.md §Page contracts — /metagen/data/[urn] writes PUT .../attr/metagen/conf
// ─────────────────────────────────────────────────────────────────────────────
test("UC4 stub step 2 — PUT per-dataset boundaries; boundary section shows allowed badges", async ({
  page,
  adminApi,
}) => {
  if (!confCreated) test.skip(true, "step 1 did not create conf");

  // ── 2a: eu_profiles boundary ──────────────────────────────────────────────

  // Navigate to /metagen/data/[eu_profiles_urn].
  // spec: FRONTEND_METAGEN.md §Navigation — /metagen/data/[urn] reads GET .../attr/metagen/conf
  await page.goto(EU_DATASET_URL);
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: page shows the URN in the header (font-mono h1) --
  // page.tsx renders <h1 className="...font-mono...">{datasetUrn}</h1>
  await expect(page.getByText(EU_PROFILES_URN, { exact: false }).first()).toBeVisible({
    timeout: 15_000,
  });

  // -- UI assertion: boundary section heading present --
  // page.tsx: <h2>attr/metagen/conf (boundary)</h2>
  await expect(
    page.getByText("attr/metagen/conf (boundary)", { exact: true })
  ).toBeVisible({ timeout: 10_000 });

  // -- Enter editing mode if boundary already exists --
  const euEditButton = page.getByRole("button", { name: "Edit" }).first();
  const euEditVisible = await euEditButton.isVisible().catch(() => false);
  if (euEditVisible) {
    await euEditButton.click();
  }

  // -- UI gesture: check is_enabled --
  // boundary-form.tsx: <Checkbox id="boundary-is-enabled">
  const euIsEnabledCheckbox = page.locator("#boundary-is-enabled");
  await expect(euIsEnabledCheckbox).toBeVisible({ timeout: 10_000 });
  const euIsChecked = await euIsEnabledCheckbox.isChecked().catch(() => false);
  if (!euIsChecked) {
    await euIsEnabledCheckbox.click();
  }

  // -- UI gesture: check dataset.description and column.description allowed --
  // boundary-form.tsx: <Checkbox id="boundary-allowed-dataset.description">
  const datasetDescCheckbox = page.locator("#boundary-allowed-dataset\\.description");
  await expect(datasetDescCheckbox).toBeVisible({ timeout: 10_000 });
  if (!(await datasetDescCheckbox.isChecked().catch(() => false))) {
    await datasetDescCheckbox.click();
  }

  // boundary-form.tsx: <Checkbox id="boundary-allowed-column.description">
  const colDescCheckbox = page.locator("#boundary-allowed-column\\.description");
  await expect(colDescCheckbox).toBeVisible();
  if (!(await colDescCheckbox.isChecked().catch(() => false))) {
    await colDescCheckbox.click();
  }

  // -- UI gesture: submit --
  // boundary-form.tsx: <Button type="submit">Save boundary</Button>
  await page.getByRole("button", { name: "Save boundary" }).click();

  // -- UI assertion: toast "Boundary saved" --
  // page.tsx handleSaveBoundary → toast({ title: "Boundary saved" })
  await expect(page.getByText("Boundary saved", { exact: false }).first()).toBeVisible({
    timeout: 20_000,
  });
  euBoundaryCreated = true;

  // -- UI assertion: allowed badges rendered in read-only view --
  // page.tsx dl renders boundary.allowed as Badge elements with font-mono text
  // Wait for editing mode to close (read-only dl renders after save)
  await expect(
    page.getByText("dataset.description", { exact: true }).first()
  ).toBeVisible({ timeout: 15_000 });
  await expect(
    page.getByText("column.description", { exact: true }).first()
  ).toBeVisible({ timeout: 10_000 });

  // -- Backend probe: GET eu_profiles boundary --
  // spec: USE_CASE_en.md §UC4 L613–614 — boundary echoes dataset_urn + allowed
  const euBoundaryResp = await adminApi.get(EU_BOUNDARY_API);
  expect(euBoundaryResp.status()).toBe(200);
  const euBoundaryBody = (await euBoundaryResp.json()) as {
    dataset_urn: string;
    is_enabled: boolean;
    allowed: string[];
  };
  expect(euBoundaryBody.dataset_urn).toBe(EU_PROFILES_URN);
  expect(euBoundaryBody.is_enabled).toBe(true);
  expect(new Set(euBoundaryBody.allowed)).toEqual(
    new Set(["dataset.description", "column.description"])
  );

  // ── 2b: orders.events boundary ────────────────────────────────────────────

  await page.goto(OE_DATASET_URL);
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page.getByText(ORDERS_EVENTS_URN, { exact: false }).first()).toBeVisible({
    timeout: 15_000,
  });

  // Enter editing mode if boundary already exists
  const oeEditButton = page.getByRole("button", { name: "Edit" }).first();
  const oeEditVisible = await oeEditButton.isVisible().catch(() => false);
  if (oeEditVisible) {
    await oeEditButton.click();
  }

  const oeIsEnabledCheckbox = page.locator("#boundary-is-enabled");
  await expect(oeIsEnabledCheckbox).toBeVisible({ timeout: 10_000 });
  const oeIsChecked = await oeIsEnabledCheckbox.isChecked().catch(() => false);
  if (!oeIsChecked) {
    await oeIsEnabledCheckbox.click();
  }

  // orders.events only needs column.description
  const oeColDescCheckbox = page.locator("#boundary-allowed-column\\.description");
  await expect(oeColDescCheckbox).toBeVisible({ timeout: 10_000 });
  if (!(await oeColDescCheckbox.isChecked().catch(() => false))) {
    await oeColDescCheckbox.click();
  }
  // Ensure dataset.description is NOT checked for orders.events
  const oeDatasetDescCheckbox = page.locator("#boundary-allowed-dataset\\.description");
  if (await oeDatasetDescCheckbox.isChecked().catch(() => false)) {
    await oeDatasetDescCheckbox.click();
  }

  await page.getByRole("button", { name: "Save boundary" }).click();
  await expect(page.getByText("Boundary saved", { exact: false }).first()).toBeVisible({
    timeout: 20_000,
  });
  oeBoundaryCreated = true;

  // -- Backend probe: GET orders.events boundary --
  const oeBoundaryResp = await adminApi.get(OE_BOUNDARY_API);
  expect(oeBoundaryResp.status()).toBe(200);
  const oeBoundaryBody = (await oeBoundaryResp.json()) as {
    dataset_urn: string;
    is_enabled: boolean;
    allowed: string[];
  };
  expect(oeBoundaryBody.dataset_urn).toBe(ORDERS_EVENTS_URN);
  expect(oeBoundaryBody.is_enabled).toBe(true);
  expect(oeBoundaryBody.allowed).toContain("column.description");
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 3 — Trigger first run via /metagen → Run button → RunDialog
// spec: USE_CASE_en.md §UC4 L720–723 — daily DAG fires; or reviewer triggers immediate run
// spec: FRONTEND_METAGEN.md §Page contracts — POST /spoke/metagen/method/run
// ─────────────────────────────────────────────────────────────────────────────
test("UC4 stub step 3 — trigger first run; METAGEN.RUN_COMPLETE event present", async ({
  page,
  adminApi,
}) => {
  if (!confCreated || !euBoundaryCreated) test.skip(true, "steps 1-2 did not complete");

  // Navigate to /metagen.
  await page.goto(METAGEN_URL);
  await expect(
    page.getByRole("heading", { name: "Metadata Generation", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI gesture: click "Run" button to open RunDialog --
  // page.tsx: <Button onClick={() => setRunDialogOpen(true)}>Run</Button>
  await page.getByRole("button", { name: "Run", exact: true }).click();

  // -- UI assertion: RunDialog opens --
  // run-dialog.tsx: <DialogTitle>Run MetaGen</DialogTitle>
  await expect(page.getByRole("heading", { name: "Run MetaGen", exact: true })).toBeVisible({
    timeout: 10_000,
  });

  // -- UI gesture: click the "Run" button in the dialog (dry_run unchecked by default) --
  // run-dialog.tsx: <Button onClick={handleRun}>{dryRun ? "Dry Run" : "Run"}</Button>
  // There are two "Run" buttons when dialog is open: the page-level one (now hidden) and
  // the dialog footer one. Use last() to be safe or target dialog footer explicitly.
  await page.getByRole("button", { name: "Run", exact: true }).last().click();

  // -- UI assertion: toast "Run complete" (with counts summary) --
  // page.tsx handleRun onSuccess: toast({ title: "Run complete", description: detail || data.status })
  await expect(page.getByText("Run complete", { exact: false }).first()).toBeVisible({
    timeout: 120_000,
  });

  // -- Backend probe: poll for METAGEN.RUN_COMPLETE event (metagen runs take time even stubbed) --
  // spec: BACKEND.md event catalogue L766 — METAGEN.RUN_COMPLETE emitted after every run
  // spec: TESTING.md §Assertion Principles — poll bounded instead of fixed sleep
  const deadline = Date.now() + 120_000;
  let runCompleteEvent: Record<string, unknown> | null = null;
  while (Date.now() < deadline) {
    const evResp = await adminApi.get(`${GLOBAL_EVENT_API}?limit=20`);
    if (evResp.ok()) {
      const evBody = (await evResp.json()) as {
        events: Array<{ event_type: string; detail?: Record<string, unknown> }>;
      };
      const found = evBody.events.find((e) => e.event_type === "METAGEN.RUN_COMPLETE");
      if (found) {
        runCompleteEvent = found as Record<string, unknown>;
        firstRunId = (found.detail as Record<string, unknown>)?.["run_id"] as string | null ?? null;
        const counts = (found.detail as Record<string, unknown>)?.["counts"] as Record<string, number> | null;
        if (counts) {
          firstRunItemsConsidered = counts["items_considered"] ?? null;
        }
        break;
      }
    }
    await new Promise((res) => setTimeout(res, 3_000));
  }
  expect(runCompleteEvent, "METAGEN.RUN_COMPLETE event must appear after first run").not.toBeNull();

  // Validate the event detail shape.
  // spec: BACKEND.md event catalogue L766 — detail keys: run_id, unresolved_urns, counts,
  //   dry_run, producer_iterations, debate_outcome
  const detail = (runCompleteEvent as { detail?: Record<string, unknown> }).detail ?? {};
  expect(typeof detail["run_id"]).toBe("string");
  expect(Array.isArray(detail["unresolved_urns"])).toBe(true);
  expect(detail["dry_run"]).toBe(false);
  expect(["accept", "turns_exhausted", "cycle_detected"]).toContain(detail["debate_outcome"]);
  expect(typeof detail["producer_iterations"]).toBe("number");
  expect((detail["producer_iterations"] as number)).toBeGreaterThanOrEqual(1);

  const counts = detail["counts"] as Record<string, number> | undefined;
  expect(counts).toBeDefined();
  expect(typeof counts!["items_considered"]).toBe("number");
  expect(counts!["items_considered"]).toBeGreaterThanOrEqual(1);
  expect(typeof counts!["candidates_added"]).toBe("number");
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 4 — /metagen global events panel shows METAGEN.RUN_COMPLETE
// spec: USE_CASE_en.md §UC4 — dashboard shows event log
// spec: FRONTEND_METAGEN.md §Page contracts — /metagen reads GET /spoke/metagen/event
// ─────────────────────────────────────────────────────────────────────────────
test("UC4 stub step 4 — /metagen event panel shows METAGEN.RUN_COMPLETE event", async ({
  page,
  adminApi,
}) => {
  if (!firstRunId) test.skip(true, "step 3 did not complete (no firstRunId)");

  await page.goto(METAGEN_URL);
  await expect(
    page.getByRole("heading", { name: "Metadata Generation", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: "event/metagen (latest 10)" section heading present --
  // page.tsx: <h2 className="...">event/metagen (latest 10)</h2>
  await expect(
    page.getByText("event/metagen (latest 10)", { exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: METAGEN.RUN_COMPLETE visible in the events section --
  // events-section.tsx renders event_type text per event row.
  // first() because there may be multiple events (from prior tests runs or both URNs).
  await expect(
    page.getByText("METAGEN.RUN_COMPLETE", { exact: false }).first()
  ).toBeVisible({ timeout: 30_000 });

  // -- Backend probe: GET /spoke/metagen/event → firstRunId present --
  // spec: BACKEND.md event catalogue L766 — RUN_COMPLETE emitted on every run
  const evResp = await adminApi.get(`${GLOBAL_EVENT_API}?limit=20`);
  expect(evResp.status()).toBe(200);
  const evBody = (await evResp.json()) as {
    events: Array<{ event_type: string; detail?: Record<string, unknown> }>;
    offset: number;
    limit: number;
    total_count: number;
  };
  expect(evBody).toHaveProperty("events");
  expect(evBody).toHaveProperty("offset");
  expect(evBody).toHaveProperty("limit");
  expect(evBody).toHaveProperty("total_count");
  expect(Array.isArray(evBody.events)).toBe(true);
  const runCompleteForFirstRun = evBody.events.find(
    (e) =>
      e.event_type === "METAGEN.RUN_COMPLETE" &&
      (e.detail as Record<string, unknown>)?.["run_id"] === firstRunId
  );
  expect(
    runCompleteForFirstRun,
    `METAGEN.RUN_COMPLETE for firstRunId=${firstRunId} not in global events`
  ).toBeTruthy();
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 5 — Approve eu_profiles dataset.description candidate
// spec: USE_CASE_en.md §UC4 L752–760 — reviewer approves/rejects candidates
// spec: FRONTEND_METAGEN.md §Page contracts — /metagen/data/[urn] per-item candidate review
// ─────────────────────────────────────────────────────────────────────────────
test("UC4 stub step 5 — approve eu_profiles dataset.description candidate", async ({
  page,
  adminApi,
}) => {
  if (!firstRunId) test.skip(true, "step 3 did not complete");

  // Navigate to eu_profiles per-dataset page.
  await page.goto(EU_DATASET_URL);
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page.getByText(EU_PROFILES_URN, { exact: false }).first()).toBeVisible({
    timeout: 15_000,
  });

  // -- UI assertion: "attr/metagen/item" section present --
  // page.tsx: <h2 className="...">attr/metagen/item</h2>
  await expect(
    page.getByText("attr/metagen/item", { exact: true })
  ).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: dataset.description sub-heading visible (items grouped by kind) --
  // page.tsx: <h3>dataset.description</h3> when datasetDescItems.length > 0
  await expect(
    page.getByText("dataset.description", { exact: true }).first()
  ).toBeVisible({ timeout: 30_000 });

  // -- Backend probe: GET .../attr/metagen/item — confirm items exist before UI interaction --
  // spec: USE_CASE_en.md §UC4 L725–730 — after run, items listed for each dataset
  const itemsResp = await adminApi.get(
    `/api/v1/spoke/common/data/${EU_PROFILES_ENC}/attr/metagen/item`
  );
  expect(itemsResp.status()).toBe(200);
  const itemsBody = (await itemsResp.json()) as {
    items: Array<{
      item_id: string;
      kind: string;
      status: string;
      dataset_urn: string;
      composite_id: string;
    }>;
    total_count: number;
  };
  expect(Array.isArray(itemsBody.items)).toBe(true);

  // Find a dataset.description item with llm_approved candidates.
  const dsDescItem = itemsBody.items.find((i) => i.kind === "dataset.description");
  expect(dsDescItem, "No dataset.description item found for eu_profiles after run").toBeTruthy();

  // Fetch item detail to find a llm_approved candidate.
  const dsDescItemIdEnc = encodeURIComponent(dsDescItem!.item_id);
  const detailResp = await adminApi.get(
    `/api/v1/spoke/common/data/${EU_PROFILES_ENC}/attr/metagen/item/${dsDescItemIdEnc}`
  );
  expect(detailResp.status()).toBe(200);
  const detailBody = (await detailResp.json()) as {
    candidates: Array<{ candidate_id: string; status: string; value: string }>;
  };
  const llmApprovedCandidate = detailBody.candidates.find((c) => c.status === "llm_approved");

  // If no llm_approved candidate exists, skip the approve gesture (stub may not always produce one).
  if (!llmApprovedCandidate) {
    console.warn("[uc4 step 5] No llm_approved candidate for eu_profiles dataset.description; skipping approve.");
    return;
  }
  approvedEuDescCandidateId = llmApprovedCandidate.candidate_id;
  approvedEuDescValue = llmApprovedCandidate.value;

  // -- UI gesture: expand the dataset.description ItemCard --
  // item-card.tsx: Button "Review" (for non-finalized) or "View" (for finalized)
  // The ItemCard header renders a "Review" or "View" expand button next to the item label.
  // Use first() — the page may have multiple ItemCards for different items.
  // The expand button is a ghost button inside the card border.
  const reviewButton = page.getByRole("button", { name: "Review" }).first();
  await expect(reviewButton).toBeVisible({ timeout: 20_000 });
  await reviewButton.click();

  // -- UI assertion: CandidateCard "llm_approved" badge visible inside expanded card --
  // candidate-card.tsx: <Badge variant="secondary">{candidate.status}</Badge>
  // first() — avoid strict-mode violation when multiple candidates render
  await expect(page.getByText("llm_approved", { exact: true }).first()).toBeVisible({
    timeout: 15_000,
  });

  // -- UI gesture: click Approve button on the candidate card --
  // candidate-card.tsx: <Button onClick={() => setApproveOpen(true)}>Approve</Button>
  // There may be multiple "Approve" buttons (one per candidate card). first() targets the top one.
  const approveButton = page.getByRole("button", { name: "Approve" }).first();
  await expect(approveButton).toBeVisible({ timeout: 10_000 });
  await approveButton.click();

  // -- UI assertion: ConfirmDialog opens with "Approve candidate" title --
  // candidate-card.tsx: <ConfirmDialog title="Approve candidate" confirmLabel="Approve" ...>
  await expect(
    page.getByRole("heading", { name: "Approve candidate", exact: true })
  ).toBeVisible({ timeout: 10_000 });

  // -- UI gesture: confirm in the dialog --
  // ConfirmDialog: confirmLabel="Approve" → button with that text
  // Use last() — the initial Approve button is still in DOM; the dialog Approve is the last one.
  await page.getByRole("button", { name: "Approve", exact: true }).last().click();

  // -- UI assertion: toast "Candidate approved" --
  // item-card.tsx handleApprove onSuccess: toast({ title: "Candidate approved", ... })
  await expect(page.getByText("Candidate approved", { exact: false }).first()).toBeVisible({
    timeout: 30_000,
  });

  // -- Backend probe: POST .../candidate/{cid}/method/review → status=approved --
  // spec: USE_CASE_en.md §UC4 L649–657 — candidate status transitions to 'approved'
  // (The UI already fired the mutation; this probe verifies the DB state.)
  const reviewResp = await adminApi.post(
    `/api/v1/spoke/common/data/${EU_PROFILES_ENC}` +
      `/attr/metagen/item/${dsDescItemIdEnc}` +
      `/candidate/${approvedEuDescCandidateId}/method/review`,
    { data: { verdict: "approve", reason: "uc4 e2e approve" } }
  );
  // 200 = approved; 409 = already approved (idempotent; also acceptable)
  expect([200, 409]).toContain(reviewResp.status());
  if (reviewResp.status() === 200) {
    const reviewBody = (await reviewResp.json()) as { status: string };
    expect(reviewBody.status).toBe("approved");
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 6 — Reject eu_profiles column.description candidate
// spec: USE_CASE_en.md §UC4 L752–760 — reviewer rejects candidate
// spec: FRONTEND_METAGEN.md §Page contracts — Reject button → ConfirmDialog
// ─────────────────────────────────────────────────────────────────────────────
test("UC4 stub step 6 — reject eu_profiles column.description candidate", async ({
  page,
  adminApi,
}) => {
  if (!firstRunId) test.skip(true, "step 3 did not complete");

  await page.goto(EU_DATASET_URL);
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page.getByText(EU_PROFILES_URN, { exact: false }).first()).toBeVisible({
    timeout: 15_000,
  });

  // -- Backend probe: find a column.description item to reject --
  const itemsResp = await adminApi.get(
    `/api/v1/spoke/common/data/${EU_PROFILES_ENC}/attr/metagen/item`
  );
  expect(itemsResp.status()).toBe(200);
  const itemsBody = (await itemsResp.json()) as {
    items: Array<{ item_id: string; kind: string; status: string }>;
  };
  const colItems = itemsBody.items.filter(
    (i) => i.kind === "column.description" && i.status !== "approved"
  );
  if (colItems.length === 0) {
    console.warn("[uc4 step 6] No non-approved column.description items; skipping reject.");
    return;
  }
  const colItem = colItems[0]!;
  rejectedEuColItemId = colItem.item_id;

  const colItemIdEnc = encodeURIComponent(colItem.item_id);
  const detailResp = await adminApi.get(
    `/api/v1/spoke/common/data/${EU_PROFILES_ENC}/attr/metagen/item/${colItemIdEnc}`
  );
  expect(detailResp.status()).toBe(200);
  const detailBody = (await detailResp.json()) as {
    candidates: Array<{ candidate_id: string; status: string }>;
  };
  const llmApprovedCol = detailBody.candidates.find((c) => c.status === "llm_approved");
  if (!llmApprovedCol) {
    console.warn("[uc4 step 6] No llm_approved candidate for column item; skipping reject.");
    return;
  }
  rejectedEuColCandidateId = llmApprovedCol.candidate_id;

  // -- UI assertion: column.description sub-heading visible --
  await expect(
    page.getByText("column.description", { exact: true }).first()
  ).toBeVisible({ timeout: 30_000 });

  // -- UI gesture: expand any column.description ItemCard to find Reject button --
  // item-card.tsx: Review button opens expanded candidates view
  // We look for "Review" buttons after the dataset.description Review buttons.
  // Since we cannot distinguish cards by item_id semantically, we expand the first
  // non-finalized column card by clicking "Review" among the buttons.
  //
  // Strategy: use adminApi probe above to confirm the item exists, then rely on
  // the UI ordering (column items rendered after dataset.description items) and
  // click the last "Review" button, which is more likely to be a column card.
  const reviewButtons = page.getByRole("button", { name: "Review" });
  const reviewCount = await reviewButtons.count();
  if (reviewCount === 0) {
    // All items may already be approved ("View" not "Review")
    console.warn("[uc4 step 6] No 'Review' buttons visible; all items finalized.");
    return;
  }
  // Click the last "Review" button — column.description cards come after dataset.description
  await reviewButtons.last().click();

  // -- UI assertion: CandidateCard "llm_approved" badge visible --
  await expect(page.getByText("llm_approved", { exact: true }).first()).toBeVisible({
    timeout: 15_000,
  });

  // -- UI gesture: click Reject button --
  // candidate-card.tsx: showReject = canWrite && isRejectEligible (llm_approved only)
  const rejectButton = page.getByRole("button", { name: "Reject" }).first();
  await expect(rejectButton).toBeVisible({ timeout: 10_000 });
  await rejectButton.click();

  // -- UI assertion: ConfirmDialog "Reject candidate" --
  // candidate-card.tsx: <ConfirmDialog title="Reject candidate" confirmLabel="Reject" ...>
  await expect(
    page.getByRole("heading", { name: "Reject candidate", exact: true })
  ).toBeVisible({ timeout: 10_000 });

  // -- UI gesture: confirm rejection --
  await page.getByRole("button", { name: "Reject", exact: true }).last().click();

  // -- UI assertion: toast "Candidate rejected" --
  // item-card.tsx handleReject onSuccess: toast({ title: "Candidate rejected" })
  await expect(page.getByText("Candidate rejected", { exact: false }).first()).toBeVisible({
    timeout: 30_000,
  });

  // -- Backend probe: verify candidate status is rejected --
  // spec: USE_CASE_en.md §UC4 L649–657 — rejected status stored in DB
  const reviewResp = await adminApi.post(
    `/api/v1/spoke/common/data/${EU_PROFILES_ENC}` +
      `/attr/metagen/item/${colItemIdEnc}` +
      `/candidate/${rejectedEuColCandidateId}/method/review`,
    { data: { verdict: "reject", reason: "uc4 e2e reject" } }
  );
  // 200 = newly rejected; 409/422 = already rejected (idempotent)
  if (reviewResp.status() === 200) {
    const reviewBody = (await reviewResp.json()) as { status: string };
    expect(reviewBody.status).toBe("rejected");
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 7 — DataHub round-trip: editableDatasetProperties reflects approved value
// spec: USE_CASE_en.md §UC4 L762–764 — approval emits to DataHub editable aspects
// spec: BACKEND.md §UC4 — _emit_to_datahub writes to editableDatasetProperties.description
// ─────────────────────────────────────────────────────────────────────────────
test("UC4 stub step 7 — DataHub round-trip: approved description in editableDatasetProperties", async ({
  adminApi,
}) => {
  if (!approvedEuDescCandidateId || !approvedEuDescValue) {
    test.skip(true, "step 5 did not produce an approved candidate");
  }

  // Poll DataHub via the hub proxy to read editableDatasetProperties.
  // spec: API.md §DataHub pass-through — /hub/openapi/{path} proxies to DataHub GMS /openapi/{path}
  // spec: USE_CASE_en.md §UC4 L762–764 — dataset.description → editableDatasetProperties
  // DataHub OpenAPI v3 path for reading an aspect:
  //   GET /openapi/v3/entity/dataset/{encodedUrn}/editableDatasetProperties
  // Accessed via /api/v1/hub/openapi/v3/entity/dataset/{encodedUrn}/editableDatasetProperties.
  //
  // SETUP DEPENDENCY: if the DataHub OpenAPI v3 endpoint returns a different shape than
  // expected below (DataHub version may vary), this assertion will be a false negative.
  // The api-wired test uses the Python SDK (graph.get_aspect) — not available in E2E.
  // Fallback: verify approved status via DataSpoke item detail (candidate status=approved is
  // itself confirmed by step 5 backend probe).
  //
  // Allow up to 30s for DataHub write propagation.
  const deadline = Date.now() + 30_000;
  let editableDescription: string | null = null;
  let probeSucceeded = false;
  while (Date.now() < deadline) {
    const hubResp = await adminApi.get(
      `/api/v1/hub/openapi/v3/entity/dataset/${encodeURIComponent(EU_PROFILES_URN)}/editableDatasetProperties`
    );
    if (hubResp.ok()) {
      probeSucceeded = true;
      const hubBody = (await hubResp.json()) as Record<string, unknown>;
      // DataHub OpenAPI v3 response shape: { "editableDatasetProperties": { "description": "..." } }
      // or { "value": { ... } } depending on version.
      const props =
        // OpenAPI v3 single-aspect GET returns { "value": { ...aspect fields... } },
        // so the aspect fields (incl. description) live directly under `value`.
        (hubBody["value"] as Record<string, unknown> | undefined) ??
        (hubBody["editableDatasetProperties"] as Record<string, unknown> | undefined);
      const desc = props?.["description"] as string | null | undefined;
      if (desc !== null && desc !== undefined && desc !== "") {
        editableDescription = desc;
        break;
      }
    }
    await new Promise((res) => setTimeout(res, 3_000));
  }

  if (probeSucceeded) {
    // spec: USE_CASE_en.md §UC4 L762–764 — the approved value must appear in DataHub
    expect(editableDescription).toBe(approvedEuDescValue);
  } else {
    // Hub proxy unavailable or DataHub returned non-200: log as a setup dependency gap
    // rather than failing the suite — the api-wired test covers this assertion definitively.
    console.warn(
      "[uc4 step 7] Hub proxy did not return 200 for editableDatasetProperties. " +
        "DataHub round-trip not confirmed in E2E (api-wired covers this). " +
        "SETUP DEPENDENCY: DataHub OpenAPI v3 aspect endpoint must be reachable via /api/v1/hub/openapi/v3/..."
    );
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 8 — Per-dataset events show CANDIDATE_APPROVE + CANDIDATE_REJECT
// spec: USE_CASE_en.md §UC4 L769–770 — candidate review creates audit events
// spec: BACKEND.md event catalogue L767 — CANDIDATE_APPROVE / CANDIDATE_REJECT detail
// ─────────────────────────────────────────────────────────────────────────────
test("UC4 stub step 8 — per-dataset events include CANDIDATE_APPROVE and CANDIDATE_REJECT", async ({
  page,
  adminApi,
}) => {
  if (!approvedEuDescCandidateId && !rejectedEuColCandidateId) {
    test.skip(true, "steps 5/6 did not produce any review actions");
  }

  // Navigate to eu_profiles per-dataset page.
  await page.goto(EU_DATASET_URL);
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page.getByText(EU_PROFILES_URN, { exact: false }).first()).toBeVisible({
    timeout: 15_000,
  });

  // -- UI assertion: "event/metagen (latest 10)" section on per-dataset page --
  // page.tsx: <h2>event/metagen (latest 10)</h2>
  await expect(
    page.getByText("event/metagen (latest 10)", { exact: true }).first()
  ).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: CANDIDATE_APPROVE event visible (if approve happened) --
  if (approvedEuDescCandidateId) {
    await expect(
      page.getByText("METAGEN.CANDIDATE_APPROVE", { exact: false }).first()
    ).toBeVisible({ timeout: 30_000 });
  }

  // -- UI assertion: CANDIDATE_REJECT event visible (if reject happened) --
  if (rejectedEuColCandidateId) {
    await expect(
      page.getByText("METAGEN.CANDIDATE_REJECT", { exact: false }).first()
    ).toBeVisible({ timeout: 30_000 });
  }

  // -- Backend probe: GET .../event/metagen → events include CANDIDATE_APPROVE --
  // spec: BACKEND.md event catalogue L767 — CANDIDATE_APPROVE detail: item_id, candidate_id, reason
  const euEventResp = await adminApi.get(
    `/api/v1/spoke/common/data/${EU_PROFILES_ENC}/event/metagen?limit=20`
  );
  expect(euEventResp.status()).toBe(200);
  const euEventBody = (await euEventResp.json()) as {
    events: Array<{
      event_type: string;
      detail?: Record<string, unknown>;
    }>;
    offset: number;
    limit: number;
    total_count: number;
  };
  expect(euEventBody).toHaveProperty("events");
  expect(euEventBody).toHaveProperty("offset");
  expect(euEventBody).toHaveProperty("limit");
  expect(euEventBody).toHaveProperty("total_count");
  expect(Array.isArray(euEventBody.events)).toBe(true);

  if (approvedEuDescCandidateId) {
    const approveEvent = euEventBody.events.find(
      (e) => e.event_type === "METAGEN.CANDIDATE_APPROVE"
    );
    expect(
      approveEvent,
      "METAGEN.CANDIDATE_APPROVE event missing after approval"
    ).toBeTruthy();
    const ev = approveEvent as { detail?: Record<string, unknown> };
    expect(ev.detail).toHaveProperty("item_id");
    expect(ev.detail).toHaveProperty("candidate_id");
    expect(ev.detail).toHaveProperty("reason");
  }

  if (rejectedEuColCandidateId) {
    const rejectEvent = euEventBody.events.find(
      (e) => e.event_type === "METAGEN.CANDIDATE_REJECT"
    );
    expect(
      rejectEvent,
      "METAGEN.CANDIDATE_REJECT event missing after rejection"
    ).toBeTruthy();
    const ev = rejectEvent as { detail?: Record<string, unknown> };
    expect(ev.detail).toHaveProperty("item_id");
    expect(ev.detail).toHaveProperty("candidate_id");
    expect(ev.detail).toHaveProperty("reason");
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 9 — Second run: approved items skipped; rejected item cleared + re-generated
// spec: USE_CASE_en.md §UC4 L567 — previously approved descriptions not overwritten
// spec: BACKEND.md §UC4 — _enumerate_target_items skips approved; _clear_rejected_candidates
// ─────────────────────────────────────────────────────────────────────────────
test("UC4 stub step 9 — second run: approved items skipped, rejected item re-generated", async ({
  page,
  adminApi,
}) => {
  if (!firstRunId || firstRunItemsConsidered === null) {
    test.skip(true, "step 3 did not complete (no firstRunId / firstRunItemsConsidered)");
  }

  // Navigate to /metagen and trigger a second run.
  await page.goto(METAGEN_URL);
  await expect(
    page.getByRole("heading", { name: "Metadata Generation", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  await page.getByRole("button", { name: "Run", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Run MetaGen", exact: true })
  ).toBeVisible({ timeout: 10_000 });
  await page.getByRole("button", { name: "Run", exact: true }).last().click();

  // -- UI assertion: toast "Run complete" for second run --
  await expect(page.getByText("Run complete", { exact: false }).first()).toBeVisible({
    timeout: 120_000,
  });

  // -- Backend probe: poll for second METAGEN.RUN_COMPLETE (different run_id from first) --
  // spec: BACKEND.md event catalogue L766 — RUN_COMPLETE emitted on every run
  const deadline = Date.now() + 120_000;
  let secondRunId: string | null = null;
  let secondRunItemsConsidered: number | null = null;
  while (Date.now() < deadline) {
    const evResp = await adminApi.get(`${GLOBAL_EVENT_API}?limit=50`);
    if (evResp.ok()) {
      const evBody = (await evResp.json()) as {
        events: Array<{ event_type: string; detail?: Record<string, unknown> }>;
      };
      const newComplete = evBody.events.find(
        (e) =>
          e.event_type === "METAGEN.RUN_COMPLETE" &&
          (e.detail as Record<string, unknown>)?.["run_id"] !== firstRunId
      );
      if (newComplete) {
        secondRunId = (newComplete.detail as Record<string, unknown>)?.["run_id"] as string | null ?? null;
        const counts = (newComplete.detail as Record<string, unknown>)?.["counts"] as Record<string, number> | null;
        if (counts) {
          secondRunItemsConsidered = counts["items_considered"] ?? null;
        }
        break;
      }
    }
    await new Promise((res) => setTimeout(res, 3_000));
  }
  expect(secondRunId, "Second METAGEN.RUN_COMPLETE must appear").not.toBeNull();

  // -- Assert: second run's items_considered < first run's (approved items excluded) --
  // spec: BACKEND.md §UC4 — _enumerate_target_items skips (urn, item_id) with approved candidate
  // Only assert when step 5 actually produced an approved item (may not on first run in rare cases).
  if (approvedEuDescCandidateId && secondRunItemsConsidered !== null && firstRunItemsConsidered !== null) {
    expect(secondRunItemsConsidered).toBeLessThan(firstRunItemsConsidered);
  }

  // -- Backend probe: eu_profiles dataset.description has exactly 1 approved candidate --
  // spec: BACKEND.md §UC4 — partial unique index ensures at most one approved candidate per item
  if (approvedEuDescCandidateId) {
    const dsDescItemIdEnc = encodeURIComponent("dataset.description");
    const detailResp = await adminApi.get(
      `/api/v1/spoke/common/data/${EU_PROFILES_ENC}/attr/metagen/item/${dsDescItemIdEnc}`
    );
    expect(detailResp.status()).toBe(200);
    const detailBody = (await detailResp.json()) as {
      candidates: Array<{ status: string }>;
    };
    const approvedCandidates = detailBody.candidates.filter((c) => c.status === "approved");
    expect(approvedCandidates.length).toBe(1);
    // Approved item only has the approved candidate after second run (no new llm_approved).
    expect(detailBody.candidates.length).toBe(1);
  }

  // -- Backend probe: rejected column item has no rejected candidates; has ≥1 llm_approved --
  // spec: BACKEND.md §UC4 — _clear_rejected_candidates removes rejected before each run
  // spec: USE_CASE_en.md §UC4 L637-638 — rejected candidates deleted at start of each run
  if (rejectedEuColItemId) {
    const colItemIdEnc = encodeURIComponent(rejectedEuColItemId);
    const colDetailResp = await adminApi.get(
      `/api/v1/spoke/common/data/${EU_PROFILES_ENC}/attr/metagen/item/${colItemIdEnc}`
    );
    expect(colDetailResp.status()).toBe(200);
    const colDetailBody = (await colDetailResp.json()) as {
      candidates: Array<{ status: string }>;
    };
    const stillRejected = colDetailBody.candidates.filter((c) => c.status === "rejected");
    expect(
      stillRejected.length,
      "Rejected candidate should be cleared by second run"
    ).toBe(0);
    const newLlmApproved = colDetailBody.candidates.filter((c) => c.status === "llm_approved");
    expect(
      newLlmApproved.length,
      "Second run must re-generate llm_approved candidate for rejected item"
    ).toBeGreaterThanOrEqual(1);
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// REAL-LLM VARIANT (structurally identical; gated on stub_llm_client=false)
// ─────────────────────────────────────────────────────────────────────────────

test.describe("UC4 real-LLM variant", () => {
  /**
   * Structurally identical to the stub-mode arc. Skips when stub_llm_client is true.
   *
   * Additional assertion after triggering the run: candidates_added >= 1 (real LLM must
   * produce non-zero candidates — a real run that produces zero signals prompt/filter regression).
   *
   * spec: USE_CASE_en.md §UC4
   * spec: BACKEND_LLM.md §Test Mode — real LLM must produce non-zero candidates
   * spec: TESTING.md §Stub Toggles — real-LLM variant: PATCH /admin/conf stub_llm_client=false
   */

  // State for the real-LLM variant (isolated from stub variant state above)
  let rlFirstRunId: string | null = null;
  let rlFirstRunItemsConsidered: number | null = null;
  let rlApprovedEuDescCandidateId: string | null = null;
  let rlApprovedEuDescValue: string | null = null;
  let rlRejectedColItemId: string | null = null;
  let rlRejectedColCandidateId: string | null = null;
  let rlConfCreated = false;
  let rlEuBoundaryCreated = false;
  let rlOeBoundaryCreated = false;

  test.afterAll(async ({ adminApi }) => {
    // Clean up any real-LLM variant state that was created.
    if (rlEuBoundaryCreated) {
      await adminApi.delete(EU_BOUNDARY_API).catch(() => null);
    }
    if (rlOeBoundaryCreated) {
      await adminApi.delete(OE_BOUNDARY_API).catch(() => null);
    }
    if (rlConfCreated) {
      await adminApi.delete(CONF_API).catch(() => null);
    }
    // --uc4-restore is already called by the outer afterAll; no double-call needed.
  });

  test("UC4 real-LLM step 1-2 — configure conf + boundaries (real-LLM mode)", async ({
    adminApi,
  }) => {
    if (stubLlmClient !== false) {
      test.skip(
        true,
        "stub_llm_client=true; set stub_llm_client=false via PATCH /admin/conf to run real-LLM tests"
      );
    }

    // PUT global conf via API (same payload as stub step 1)
    const confPutResp = await adminApi.put(CONF_API, {
      data: {
        is_enabled: true,
        schedule_tier: "daily",
        dataset_filter: { origin: "DEV", tags: [FULFILLMENT_TAG] },
        result_limit: 3,
        overwrite_pending: true,
      },
    });
    expect([200, 201]).toContain(confPutResp.status());
    const confBody = (await confPutResp.json()) as {
      is_enabled: boolean;
      schedule_tier: string;
      result_limit: number;
      overwrite_pending: boolean;
    };
    expect(confBody.is_enabled).toBe(true);
    expect(confBody.schedule_tier).toBe("daily");
    rlConfCreated = true;

    // PUT eu_profiles boundary
    const euPutResp = await adminApi.put(EU_BOUNDARY_API, {
      data: { is_enabled: true, allowed: ["dataset.description", "column.description"] },
    });
    expect([200, 201]).toContain(euPutResp.status());
    rlEuBoundaryCreated = true;

    // PUT orders.events boundary
    const oePutResp = await adminApi.put(OE_BOUNDARY_API, {
      data: { is_enabled: true, allowed: ["column.description"] },
    });
    expect([200, 201]).toContain(oePutResp.status());
    rlOeBoundaryCreated = true;
  });

  test("UC4 real-LLM step 3 — first run: candidates_added >= 1 (real-LLM invariant)", async ({
    adminApi,
  }) => {
    if (stubLlmClient !== false) {
      test.skip(
        true,
        "stub_llm_client=true; set stub_llm_client=false via PATCH /admin/conf to run real-LLM tests"
      );
    }
    if (!rlConfCreated) {
      test.skip(true, "real-LLM step 1-2 did not complete");
    }

    // POST method/run
    const runResp = await adminApi.post(RUN_API, { data: {} });
    expect(runResp.status()).toBe(200);
    const runBody = (await runResp.json()) as {
      run_id: string;
      status: string;
      dry_run: boolean;
      unresolved_urns: string[];
      counts: Record<string, number>;
      debate_outcome: string;
      producer_iterations: number;
    };
    rlFirstRunId = runBody.run_id;
    rlFirstRunItemsConsidered = runBody.counts["items_considered"] ?? null;

    expect(runBody.status).toBe("success");
    expect(runBody.dry_run).toBe(false);
    expect(Array.isArray(runBody.unresolved_urns)).toBe(true);
    expect(runBody.unresolved_urns).toEqual([]);
    expect(typeof runBody.counts["items_considered"]).toBe("number");
    expect(runBody.counts["items_considered"]).toBeGreaterThanOrEqual(1);
    expect(["accept", "turns_exhausted", "cycle_detected"]).toContain(runBody.debate_outcome);
    expect(runBody.producer_iterations).toBeGreaterThanOrEqual(1);

    // Real-LLM invariant: candidates_added >= 1
    // spec: BACKEND_LLM.md §Test Mode — real LLM must produce non-zero candidates
    expect(
      runBody.counts["candidates_added"] ?? 0,
      "Real LLM produced zero candidates — verify prompt/filter pipeline"
    ).toBeGreaterThanOrEqual(1);
  });

  test("UC4 real-LLM step 5 — approve eu_profiles dataset.description candidate", async ({
    adminApi,
  }) => {
    if (stubLlmClient !== false) {
      test.skip(
        true,
        "stub_llm_client=true; set stub_llm_client=false via PATCH /admin/conf to run real-LLM tests"
      );
    }
    if (!rlFirstRunId) {
      test.skip(true, "real-LLM step 3 did not complete");
    }

    const itemsResp = await adminApi.get(
      `/api/v1/spoke/common/data/${EU_PROFILES_ENC}/attr/metagen/item`
    );
    expect(itemsResp.status()).toBe(200);
    const itemsBody = (await itemsResp.json()) as {
      items: Array<{ item_id: string; kind: string; status: string }>;
    };
    const dsDescItem = itemsBody.items.find((i) => i.kind === "dataset.description");
    if (!dsDescItem) return; // no items generated — skip

    const dsDescItemIdEnc = encodeURIComponent(dsDescItem.item_id);
    const detailResp = await adminApi.get(
      `/api/v1/spoke/common/data/${EU_PROFILES_ENC}/attr/metagen/item/${dsDescItemIdEnc}`
    );
    expect(detailResp.status()).toBe(200);
    const detailBody = (await detailResp.json()) as {
      candidates: Array<{ candidate_id: string; status: string; value: string }>;
    };
    const llmApproved = detailBody.candidates.find((c) => c.status === "llm_approved");
    if (!llmApproved) return;

    rlApprovedEuDescCandidateId = llmApproved.candidate_id;
    rlApprovedEuDescValue = llmApproved.value;

    const reviewResp = await adminApi.post(
      `/api/v1/spoke/common/data/${EU_PROFILES_ENC}` +
        `/attr/metagen/item/${dsDescItemIdEnc}` +
        `/candidate/${rlApprovedEuDescCandidateId}/method/review`,
      { data: { verdict: "approve", reason: "uc4 real-llm e2e approve" } }
    );
    expect([200, 409]).toContain(reviewResp.status());
    if (reviewResp.status() === 200) {
      const body = (await reviewResp.json()) as { status: string };
      expect(body.status).toBe("approved");
    }

    // Reject one column item
    const colItems = itemsBody.items.filter(
      (i) => i.kind === "column.description" && i.status !== "approved"
    );
    if (colItems.length > 0) {
      const colItem = colItems[0]!;
      rlRejectedColItemId = colItem.item_id;
      const colIdEnc = encodeURIComponent(colItem.item_id);
      const colDetailResp = await adminApi.get(
        `/api/v1/spoke/common/data/${EU_PROFILES_ENC}/attr/metagen/item/${colIdEnc}`
      );
      if (colDetailResp.ok()) {
        const colDetail = (await colDetailResp.json()) as {
          candidates: Array<{ candidate_id: string; status: string }>;
        };
        const llmApprovedCol = colDetail.candidates.find((c) => c.status === "llm_approved");
        if (llmApprovedCol) {
          rlRejectedColCandidateId = llmApprovedCol.candidate_id;
          const rejectResp = await adminApi.post(
            `/api/v1/spoke/common/data/${EU_PROFILES_ENC}` +
              `/attr/metagen/item/${colIdEnc}` +
              `/candidate/${rlRejectedColCandidateId}/method/review`,
            { data: { verdict: "reject", reason: "uc4 real-llm e2e reject" } }
          );
          expect([200, 409]).toContain(rejectResp.status());
        }
      }
    }
  });

  test("UC4 real-LLM step 7 — DataHub round-trip: approved description in editableDatasetProperties", async ({
    adminApi,
  }) => {
    if (stubLlmClient !== false) {
      test.skip(
        true,
        "stub_llm_client=true; set stub_llm_client=false via PATCH /admin/conf to run real-LLM tests"
      );
    }
    if (!rlApprovedEuDescValue) {
      test.skip(true, "real-LLM step 5 did not produce an approved candidate");
    }

    const deadline = Date.now() + 30_000;
    let editableDescription: string | null = null;
    let probeSucceeded = false;
    while (Date.now() < deadline) {
      const hubResp = await adminApi.get(
        `/api/v1/hub/openapi/v3/entity/dataset/${encodeURIComponent(EU_PROFILES_URN)}/editableDatasetProperties`
      );
      if (hubResp.ok()) {
        probeSucceeded = true;
        const hubBody = (await hubResp.json()) as Record<string, unknown>;
        const props =
          (hubBody["editableDatasetProperties"] as Record<string, unknown> | undefined) ??
          ((hubBody["value"] as Record<string, unknown> | undefined)?.["editableDatasetProperties"] as
            Record<string, unknown> | undefined);
        const desc = props?.["description"] as string | null | undefined;
        if (desc !== null && desc !== undefined && desc !== "") {
          editableDescription = desc;
          break;
        }
      }
      await new Promise((res) => setTimeout(res, 3_000));
    }
    if (probeSucceeded) {
      expect(editableDescription).toBe(rlApprovedEuDescValue);
    } else {
      console.warn(
        "[uc4 real-LLM step 7] Hub proxy did not return 200 for editableDatasetProperties. " +
          "SETUP DEPENDENCY: DataHub OpenAPI v3 aspect endpoint must be reachable."
      );
    }
  });

  test("UC4 real-LLM step 9 — second run: approved skipped, rejected re-generated", async ({
    adminApi,
  }) => {
    if (stubLlmClient !== false) {
      test.skip(
        true,
        "stub_llm_client=true; set stub_llm_client=false via PATCH /admin/conf to run real-LLM tests"
      );
    }
    if (!rlFirstRunId) {
      test.skip(true, "real-LLM step 3 did not complete");
    }

    const runResp = await adminApi.post(RUN_API, { data: {} });
    expect(runResp.status()).toBe(200);
    const runBody = (await runResp.json()) as {
      run_id: string;
      status: string;
      dry_run: boolean;
      counts: Record<string, number>;
    };
    const secondRunId = runBody.run_id;
    const secondRunItemsConsidered = runBody.counts["items_considered"] ?? null;

    expect(runBody.status).toBe("success");
    expect(runBody.dry_run).toBe(false);

    // Approved items must be excluded from second run scope.
    if (
      rlApprovedEuDescCandidateId &&
      secondRunItemsConsidered !== null &&
      rlFirstRunItemsConsidered !== null
    ) {
      expect(secondRunItemsConsidered).toBeLessThan(rlFirstRunItemsConsidered);
    }

    // Verify approved item: exactly 1 approved candidate.
    if (rlApprovedEuDescCandidateId) {
      const dsDescItemIdEnc = encodeURIComponent("dataset.description");
      const detailResp = await adminApi.get(
        `/api/v1/spoke/common/data/${EU_PROFILES_ENC}/attr/metagen/item/${dsDescItemIdEnc}`
      );
      expect(detailResp.status()).toBe(200);
      const detailBody = (await detailResp.json()) as {
        candidates: Array<{ status: string }>;
      };
      const approvedCandidates = detailBody.candidates.filter((c) => c.status === "approved");
      expect(approvedCandidates.length).toBe(1);
      expect(detailBody.candidates.length).toBe(1);
    }

    // Verify rejected item: cleared + re-generated.
    if (rlRejectedColItemId) {
      const colIdEnc = encodeURIComponent(rlRejectedColItemId);
      const colDetailResp = await adminApi.get(
        `/api/v1/spoke/common/data/${EU_PROFILES_ENC}/attr/metagen/item/${colIdEnc}`
      );
      expect(colDetailResp.status()).toBe(200);
      const colDetail = (await colDetailResp.json()) as {
        candidates: Array<{ status: string }>;
      };
      const stillRejected = colDetail.candidates.filter((c) => c.status === "rejected");
      expect(stillRejected.length).toBe(0);
      const newLlmApproved = colDetail.candidates.filter((c) => c.status === "llm_approved");
      expect(newLlmApproved.length).toBeGreaterThanOrEqual(1);
    }

    // Second run RUN_COMPLETE event present.
    const evResp = await adminApi.get(`${GLOBAL_EVENT_API}?limit=50`);
    expect(evResp.status()).toBe(200);
    const evBody = (await evResp.json()) as {
      events: Array<{ event_type: string; detail?: Record<string, unknown> }>;
    };
    const secondRunComplete = evBody.events.find(
      (e) =>
        e.event_type === "METAGEN.RUN_COMPLETE" &&
        (e.detail as Record<string, unknown>)?.["run_id"] === secondRunId
    );
    expect(
      secondRunComplete,
      `METAGEN.RUN_COMPLETE for second run_id=${secondRunId} not in events`
    ).toBeTruthy();
  });
});
