/**
 * UC3 — Ontology Generation: browser UI flow.
 *
 * Mirrors tests/integration/api_wired/test_uc3_01_ontology_generation.py step-for-step,
 * with dual confirmation at each mutating step:
 *   - UI assertion (heading, toast, tabs, badge, panel text)
 *   - Backend probe via adminApi (same REST read-back the api-wired step asserts)
 *
 * TWO structurally identical variants:
 *   - Stub-mode variant (stub_llm_client=true, dev default): all steps run.
 *     Under stub the LLM Producer returns an empty payload, so zero rows are
 *     persisted; the per-row run_id check is intentionally a no-op.
 *   - Real-LLM variant: skips when stub_llm_client=true.  Adds an assertion that
 *     at least one node/edge/triple row was persisted carrying this run's run_id
 *     (the row's link to its creating run's Langfuse session).
 *
 * OntoGen layout (FRONTEND_ONTOGEN.md §Navigation): the sidebar entry is a foldable
 * group with children conf · seed · result. /ontogen redirects to /ontogen/result.
 * Run + Edit controls live top-right on /ontogen/conf (Editor/Admin only); the conf is a
 * singleton so there is no Delete. /ontogen/result is the triple-ontology browser.
 *
 * Steps (verbatim from USE_CASE_en.md §UC3 Imazon Example):
 *   1. Navigate to /ontogen/conf; assert Run + Edit controls; fill is_enabled + schedule_tier; Save.
 *      Backend: PUT /spoke/ontogen/attr/conf → 200/201; round-trips fields.
 *   2. Navigate to /ontogen/seed; click "+ New Seed"; paste domain Markdown; Save seed.
 *      The seed ships disabled (badge "disabled"); click Enable so it joins inference.
 *      Backend: GET /spoke/ontogen/attr/seed → seed_id present; is_enabled false→true;
 *      preview + updated_at set.
 *   3. On /ontogen/conf, click Run button → RunDialog ("Run ontology inference") → Run.
 *      Backend poll until ONTOGEN.RUN_COMPLETE event appears; assert OntogenRunSummary shape.
 *   4. GET /spoke/ontogen/event → find ONTOGEN.RUN_COMPLETE; assert debate fields.
 *   5. On /ontogen/result, assert Nodes/Edges/Triples/Graph tabs: result tabs render as compact
 *      tables with an All/Approved/Unapproved status filter and an Evidence column linking each row
 *      to its run's Langfuse session; the Graph tab mounts its force-directed canvas (no-op on count
 *      under stub). A revoke flow (reject an approved row → rejected) is data-conditional and
 *      round-trips when ≥1 row exists.
 *      Backend: GET result/{node,edge,triple} → standard envelope shape each; rows carry run_id.
 *   6. Cleanup: DELETE seed; PATCH conf disabled.
 *
 * spec: USE_CASE_en.md §UC3
 * spec: spec/feature/FRONTEND_ONTOGEN.md §Page contracts
 * spec: spec/feature/BACKEND_LLM.md §Adversarial Debate Framework
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — dual confirmation, selector guidance
 */

import { test, expect, IMAZON_URNS } from "../fixtures/index";

// ── Constants (verbatim from api-wired test) ────────────────────────────────

// API routes
const CONF_API = "/api/v1/spoke/ontogen/attr/conf";
const SEED_API = "/api/v1/spoke/ontogen/attr/seed";
const EVENT_API = "/api/v1/spoke/ontogen/event?limit=20";
const RUN_API = "/api/v1/spoke/ontogen/method/run";

// The seed Markdown body (abbreviated version — keeps row under SeedEditor paste limit).
const SEED_MD =
  "# Imazon Bookstore Domain\n\n" +
  "Imazon is an online retailer specialising in books. The storefront sells " +
  "individual titles, identified by ISBN-13, in multiple physical and digital " +
  "formats — Hardcover, Paperback, eBook, and Audiobook.\n\n" +
  "Customers place *orders* that bundle one or more *order lines*. Treat *order* " +
  "as the header concept and *order line* as the per-book row. Prefer business-domain " +
  "language over warehouse schema names whenever both are available.";

// Conf payload (mirrors api-wired UC3 step 1).
const CONF_PAYLOAD = {
  is_enabled: true,
  schedule_tier: "daily",
  dataset_filter: { origin: "DEV", tags: ["urn:li:tag:area:catalog"] },
};

// Admin-only — filename convention (*.spec.ts → admin project). Do not override storageState.

// ── Module-level state shared across serial step tests ─────────────────────────

let seedId: string | null = null;
// Track conf state so afterAll can patch disabled if a test creates it.
let confCreated = false;

// ── Cleanup: disable conf + delete seed after all steps ───────────────────────

test.afterAll(async ({ adminApi }) => {
  // Best-effort cleanup regardless of which steps ran.
  if (seedId) {
    await adminApi.delete(`${SEED_API}/${seedId}`);
    seedId = null;
  }
  if (confCreated) {
    await adminApi.patch(CONF_API, { data: { is_enabled: false } });
    confCreated = false;
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 1 — Enable ontogen conf via /ontogen/conf
// spec: USE_CASE_en.md §UC3 §Conf — "The governance team enables ontology generation."
// spec: FRONTEND_ONTOGEN.md §Page contracts — /ontogen/conf: PUT /spoke/ontogen/attr/conf
// ─────────────────────────────────────────────────────────────────────────────
test("UC3 step 1 — enable ontogen conf on /ontogen/conf page", async ({
  page,
  adminApi,
}) => {
  // Navigate to the conf page.
  // spec: FRONTEND_ONTOGEN.md §Navigation — /ontogen/conf → conf editor
  await page.goto("/ontogen/conf");
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: page heading rendered (convenience landmark) --
  // The heading text is a navigational landmark, not the binding invariant. The binding
  // route → surface mapping (FRONTEND_ONTOGEN.md §Navigation / §Page contracts — /ontogen/conf
  // hosts the Run+Edit conf editor) is asserted below via the URL, the Run+Edit/no-Delete
  // control set, and the conf-form fields.
  await expect(
    page.getByRole("heading", { name: "OntoGen — Configuration", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: foldable OntoGen sidebar group reveals conf · seed · result --
  // spec: FRONTEND_ONTOGEN.md §Navigation — "OntoGen sidebar entry is a foldable group
  //   with three children — conf · seed · result."
  // The group is a role=button with accessible name "OntoGen" (aria-expanded toggles its
  // children). Navigating to /ontogen/conf auto-expands it; assert the child links exist.
  const ontogenGroup = page.getByRole("button", { name: /OntoGen/ });
  await expect(ontogenGroup).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole("link", { name: "conf", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "seed", exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: "result", exact: true })).toBeVisible();

  // -- UI assertion: conf controls present (conf may exist from prior run; skip create branch) --
  // The conf page renders Edit/Run controls only when the singleton conf exists; otherwise an
  // EmptyState. We use adminApi to PUT the conf directly, then reload to get into the known-good
  // state, avoiding brittle conditional UI branching in the spec.
  //
  // Idempotent setup: PUT conf regardless of prior state.
  const putResp = await adminApi.put(CONF_API, { data: CONF_PAYLOAD });
  expect([200, 201]).toContain(putResp.status());
  confCreated = true;

  // Reload to see conf reflected in the form.
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "OntoGen — Configuration", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: Run + Edit controls visible top-right (canWrite=Admin); no Delete --
  // spec: FRONTEND_ONTOGEN.md §Page contracts — "Edit and Run controls sit top-right; the conf
  //   is a singleton so the UI exposes no Delete."
  // spec: FRONTEND_ONTOGEN.md §Page contracts — "renders the singleton conf with Edit and Run
  //   controls at the top-right ... there is no Delete."
  await expect(page.getByRole("button", { name: "Edit" })).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole("button", { name: "Run" })).toBeVisible();
  await expect(page.getByRole("button", { name: "Delete" })).toHaveCount(0);

  // -- UI assertion: the form renders (is_enabled checkbox, schedule_tier select) --
  // spec: FRONTEND_ONTOGEN.md §Page contracts — conf-form fields: is_enabled, schedule_tier
  // conf-form.tsx line 96: Checkbox id="conf-is-enabled"
  await expect(page.locator("#conf-is-enabled")).toBeVisible();
  // conf-form.tsx line 121: SelectTrigger id="conf-schedule-tier"
  await expect(page.locator("#conf-schedule-tier")).toBeVisible();

  // -- UI gesture: click Edit to enable the form --
  await page.getByRole("button", { name: "Edit" }).click();

  // The form is now in editing mode; the top-right header "Save" button is visible.
  // ontogen/conf/page.tsx: <Button form={CONF_FORM_ID} type="submit">Save</Button>
  await expect(page.getByRole("button", { name: "Save", exact: true })).toBeVisible({ timeout: 5_000 });

  // -- UI gesture: check is_enabled checkbox (set to checked/enabled) --
  // spec: FRONTEND_ONTOGEN.md §Page contracts — is_enabled controls DAG enable/disable
  const enabledCheckbox = page.locator("#conf-is-enabled");
  // Ensure it is checked.
  if (!(await enabledCheckbox.isChecked())) {
    await enabledCheckbox.check();
  }

  // -- UI gesture: select "daily" schedule_tier --
  // spec: FRONTEND_ONTOGEN.md §Page contracts — schedule_tier: hourly/daily/weekly
  // conf-form.tsx line 120: SelectTrigger id="conf-schedule-tier" (Radix Select)
  await page.locator("#conf-schedule-tier").click();
  await page.getByRole("option", { name: "daily" }).click();

  // -- UI gesture: submit the form --
  await page.getByRole("button", { name: "Save", exact: true }).click();

  // -- UI assertion: "Configuration saved" toast --
  // spec: FRONTEND_ONTOGEN.md §Page contracts — on save success, toast "Configuration saved"
  // ontogen/conf/page.tsx line 41: toast({ title: "Configuration saved" })
  await expect(page.getByText("Configuration saved", { exact: false }).first()).toBeVisible({ timeout: 15_000 });

  // -- Backend probe (dual confirmation): GET /spoke/ontogen/attr/conf --
  // spec: USE_CASE_en.md §UC3 §Conf — PUT round-trips is_enabled, schedule_tier, dataset_filter
  const getResp = await adminApi.get(CONF_API);
  expect(getResp.status()).toBe(200);
  const conf = (await getResp.json()) as {
    is_enabled: boolean;
    schedule_tier: string | null;
    dataset_filter: Record<string, unknown> | null;
  };
  expect(conf.is_enabled).toBe(true);
  expect(conf.schedule_tier).toBe("daily");
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 2 — Create Markdown seed via /ontogen/seed
// spec: USE_CASE_en.md §UC3 — "They post a domain seed (Markdown) to steer the LLM."
// spec: FRONTEND_ONTOGEN.md §Page contracts — /ontogen/seed: POST .../attr/seed
// ─────────────────────────────────────────────────────────────────────────────
test("UC3 step 2 — create domain seed on /ontogen/seed page", async ({
  page,
  adminApi,
}) => {
  if (!confCreated) test.skip(true, "step 1 did not create conf");

  // Navigate to the seed library page.
  // spec: FRONTEND_ONTOGEN.md §Navigation — /ontogen/seed → Seed Library
  await page.goto("/ontogen/seed");
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: page heading --
  // spec: FRONTEND_ONTOGEN.md §Page contracts — seed page h1 "OntoGen — Seed Library"
  // ontogen/seed/page.tsx line 37: <h1>OntoGen — Seed Library</h1>
  await expect(
    page.getByRole("heading", { name: "OntoGen — Seed Library", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: "+ New Seed" button visible (canWrite) --
  // ontogen/seed/page.tsx line 44: <Button>+ New Seed</Button>
  await expect(page.getByRole("button", { name: "+ New Seed" })).toBeVisible({ timeout: 10_000 });

  // -- UI gesture: click "+ New Seed" to open the create card --
  await page.getByRole("button", { name: "+ New Seed" }).click();

  // -- UI assertion: "New seed" card header visible --
  // ontogen/seed/page.tsx line 150: <p className="mb-3 text-sm font-medium">New seed</p>
  await expect(page.getByText("New seed", { exact: true })).toBeVisible({ timeout: 5_000 });

  // -- UI gesture: type the seed Markdown into the SeedEditor textarea --
  // spec: FRONTEND_ONTOGEN.md §Page contracts — SeedEditor textarea (rows=16, font-mono)
  // seed-editor.tsx: <Textarea rows={16} placeholder="# Ontology seed…" />
  // The textarea has no aria-label; locate by placeholder text.
  const seedTextarea = page.getByPlaceholder("# Ontology seed");
  await expect(seedTextarea).toBeVisible({ timeout: 5_000 });
  await seedTextarea.fill(SEED_MD);

  // -- UI gesture: click "Save seed" button --
  // seed-editor.tsx line 76: <Button>Save seed</Button>
  await page.getByRole("button", { name: "Save seed" }).click();

  // -- UI assertion: "Seed created" toast --
  // ontogen/seed/page.tsx line 141: toast({ title: "Seed created" })
  await expect(page.getByText("Seed created", { exact: false }).first()).toBeVisible({ timeout: 15_000 });

  // -- Backend probe (dual confirmation): GET /spoke/ontogen/attr/seed --
  // spec: USE_CASE_en.md §UC3 §Seeds — GET attr/seed returns [{seed_id, preview, updated_at}]
  const listResp = await adminApi.get(`${SEED_API}?limit=50`);
  expect(listResp.status()).toBe(200);
  const listBody = (await listResp.json()) as {
    seeds: Array<{ seed_id: string; preview: string; updated_at: string }>;
  };
  expect(Array.isArray(listBody.seeds)).toBe(true);
  expect(listBody.seeds.length).toBeGreaterThanOrEqual(1);

  // Find the most recently created seed by updated_at (our seed must be in the list).
  // Use the preview to identify it — the SeedEditor sends the full body; preview is a prefix.
  const ourSeed = listBody.seeds.find((s) =>
    s.preview && SEED_MD.startsWith(s.preview.slice(0, 20))
  );
  // Fall back to the most recently updated seed if preview matching fails.
  const resolvedSeed =
    ourSeed ??
    listBody.seeds.reduce((a, b) =>
      new Date(a.updated_at) > new Date(b.updated_at) ? a : b
    );
  expect(resolvedSeed, "Could not find newly created seed in list").toBeTruthy();
  seedId = resolvedSeed.seed_id;
  expect(seedId).toBeTruthy();
  // spec: USE_CASE_en.md §UC3 — seed list entry has preview and updated_at
  expect(resolvedSeed.preview).toBeTruthy();
  expect(resolvedSeed.updated_at).toBeTruthy();

  // -- UI assertion: seed row visible in the library list --
  // The SeedListRow renders the seed_id in font-mono and the preview as text.
  // After the toast appears the seed should be visible on the page.
  await expect(page.getByText(resolvedSeed.preview, { exact: false }).first()).toBeVisible({
    timeout: 10_000,
  });

  // -- Backend probe: the new seed ships disabled --
  // spec: USE_CASE_en.md §UC3 — POST attr/seed creates the seed disabled; the list
  // entry carries is_enabled. (Re-fetch to read the is_enabled flag on the row.)
  const listResp2 = await adminApi.get(`${SEED_API}?limit=50`);
  expect(listResp2.status()).toBe(200);
  const listBody2 = (await listResp2.json()) as {
    seeds: Array<{ seed_id: string; is_enabled: boolean }>;
  };
  const ourRow = listBody2.seeds.find((s) => s.seed_id === seedId)!;
  expect(ourRow.is_enabled).toBe(false);

  // -- UI assertion: the seed row shows the "disabled" badge --
  // spec: FRONTEND_ONTOGEN.md §Seed Library — each row shows an enabled/disabled badge.
  // Scope to the row containing our seed_id (truncated font-mono text on the row).
  const seedRow = page.locator('[role="button"]').filter({ hasText: seedId!.slice(0, 8) });
  await expect(seedRow.getByText("disabled", { exact: true }).first()).toBeVisible({
    timeout: 10_000,
  });

  // -- UI gesture: click "Enable" so the seed joins the next inference run --
  // UC3 narrative: "The steward reviews the seed, then enables it so it joins the
  // next inference run."
  // spec: FRONTEND_ONTOGEN.md §Seed Library — per-row Enable/Disable toggle.
  // spec: API.md §PATCH attr/seed/{seed_id}/attr/enabled.
  await seedRow.getByRole("button", { name: "Enable", exact: true }).first().click();
  await expect(page.getByText("Seed enabled", { exact: false }).first()).toBeVisible({
    timeout: 10_000,
  });

  // -- Backend probe (dual confirmation): the seed is now enabled --
  const listResp3 = await adminApi.get(`${SEED_API}?limit=50`);
  expect(listResp3.status()).toBe(200);
  const listBody3 = (await listResp3.json()) as {
    seeds: Array<{ seed_id: string; is_enabled: boolean }>;
  };
  expect(listBody3.seeds.find((s) => s.seed_id === seedId)!.is_enabled).toBe(true);
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 3 — Trigger real (non-dry-run) inference via /ontogen/conf Run button
// spec: spec/feature/BACKEND.md §Ontology Generation Service — non-dry-run persists rows
// spec: FRONTEND_ONTOGEN.md §Page contracts — /ontogen/conf: POST method/run via RunDialog
//   ("Edit and Run controls sit top-right ... Run opens a dialog (POST .../method/run)")
// ─────────────────────────────────────────────────────────────────────────────
test("UC3 step 3 (stub mode) — trigger Run from /ontogen/conf; assert OntogenRunSummary shape", async ({
  page,
  adminApi,
}) => {
  if (!confCreated || !seedId) test.skip(true, "steps 1/2 did not complete");

  // Navigate to the conf page, which hosts the Run control.
  // spec: FRONTEND_ONTOGEN.md §Navigation — /ontogen/conf → Conf editor + Run
  await page.goto("/ontogen/conf");
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: conf page heading (convenience landmark) --
  // Heading text is a landmark only; the binding route → surface invariant (FRONTEND_ONTOGEN.md
  // §Navigation / §Page contracts — /ontogen/conf hosts the Run control) is asserted via the URL
  // and the Run button assertion immediately below.
  await expect(
    page.getByRole("heading", { name: "OntoGen — Configuration", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: Run button visible top-right (canWrite=Admin) --
  // spec: FRONTEND_ONTOGEN.md §Page contracts — Run control on /ontogen/conf, Editor/Admin only
  await expect(page.getByRole("button", { name: "Run" })).toBeVisible({ timeout: 10_000 });

  // -- UI gesture: click Run to open RunDialog --
  // spec: FRONTEND_ONTOGEN.md §Page contracts — "Run opens a dialog (POST .../method/run)"
  // The top-right Run button is the only "Run" button until the dialog opens; click .first().
  await page.getByRole("button", { name: "Run" }).first().click();

  // -- UI assertion: RunDialog visible with title --
  // run-dialog.tsx line 41: <DialogTitle>Run ontology inference</DialogTitle>
  await expect(
    page.getByRole("heading", { name: "Run ontology inference", exact: true })
  ).toBeVisible({ timeout: 5_000 });

  // -- UI assertion: dry-run checkbox is unchecked by default --
  // run-dialog.tsx line 70: Checkbox id="run-dry-run" (default unchecked)
  const dryRunCheckbox = page.locator("#run-dry-run");
  await expect(dryRunCheckbox).toBeVisible();
  // Leave unchecked (real run).

  // -- UI gesture: click "Run" in the dialog (not "Dry run") --
  // run-dialog.tsx line 83-85: <Button>{dryRun ? "Dry run" : "Run"}</Button>
  // Since dry_run is false, the button label is "Run".
  // Use .last() to select the Run button inside the dialog, not the one behind it.
  await page.getByRole("button", { name: "Run" }).last().click();

  // -- UI assertion: "Run complete" or "Dry run complete" toast appears --
  // spec: FRONTEND_ONTOGEN.md §Page contracts — run success: toast with label + counts
  // ontogen/page.tsx lines 25-26: toast title "Run complete" (non-dry-run)
  // The run may take a moment under stub; wait for the toast or the dialog to close.
  await expect(
    page.getByText(/run complete|dry run complete/i).first()
  ).toBeVisible({ timeout: 120_000 });

  // -- Backend probe: GET /spoke/ontogen/event until ONTOGEN.RUN_COMPLETE appears --
  // spec: BACKEND_LLM.md §Wiring — RUN_COMPLETE must follow run_debate
  // Poll bounded — ontogen run may take seconds even when stubbed.
  const deadline = Date.now() + 90_000;
  let runCompleteEvent: Record<string, unknown> | null = null;
  while (Date.now() < deadline) {
    const evResp = await adminApi.get(EVENT_API);
    if (evResp.ok()) {
      const evBody = (await evResp.json()) as {
        events: Array<{ event_type: string; detail: Record<string, unknown> }>;
      };
      runCompleteEvent =
        (evBody.events.find((e) => e.event_type === "ONTOGEN.RUN_COMPLETE") as
          | Record<string, unknown>
          | undefined) ?? null;
      if (runCompleteEvent) break;
    }
    await new Promise((res) => setTimeout(res, 3_000));
  }
  expect(runCompleteEvent, "ONTOGEN.RUN_COMPLETE event not found after method/run").toBeTruthy();

  const detail = (runCompleteEvent as { detail: Record<string, unknown> }).detail;
  // spec: BACKEND_LLM.md §Adversarial Debate Framework §Termination
  const outcome = detail["debate_outcome"];
  expect(
    ["accept", "turns_exhausted", "cycle_detected"],
    `debate_outcome=${String(outcome)} not in canonical set`
  ).toContain(outcome);

  // spec: BACKEND_LLM.md §Inference Loop — producer_iterations ≥ 1
  const prodIter = detail["producer_iterations"];
  expect(typeof prodIter).toBe("number");
  expect(prodIter as number).toBeGreaterThanOrEqual(1);

  // spec: BACKEND_LLM.md §Inference Loop — producer_errors_dropped ≥ 0
  const prodErr = detail["producer_errors_dropped"];
  expect(typeof prodErr).toBe("number");
  expect(prodErr as number).toBeGreaterThanOrEqual(0);

  // -- Backend probe: POST method/run to verify OntogenRunSummary shape --
  // spec: API.md §Ontology Generation (/spoke/ontogen) — method/run: 200, dry_run=false, counts dict
  // NOTE: This fires a SECOND run to probe the shape. Under stub this is fast and harmless.
  const runResp = await adminApi.post(RUN_API);
  expect(runResp.status()).toBe(200);
  const runBody = (await runResp.json()) as {
    status: string;
    dry_run: boolean;
    unresolved_urns: unknown[];
    counts: { nodes_added: number; edges_added: number; triples_added: number };
  };
  expect(typeof runBody.status).toBe("string");
  expect(runBody.status.length).toBeGreaterThan(0);
  expect(runBody.dry_run).toBe(false);
  expect(Array.isArray(runBody.unresolved_urns)).toBe(true);
  const counts = runBody.counts;
  expect(typeof counts).toBe("object");
  expect(typeof counts.nodes_added).toBe("number");
  expect(typeof counts.edges_added).toBe("number");
  expect(typeof counts.triples_added).toBe("number");
  expect(counts.nodes_added).toBeGreaterThanOrEqual(0);
  expect(counts.edges_added).toBeGreaterThanOrEqual(0);
  expect(counts.triples_added).toBeGreaterThanOrEqual(0);
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 4 — /ontogen/result browser: table view + status filter + Graph tab; result envelope shape
// spec: USE_CASE_en.md §UC3 §API Mapping — list endpoints return paginated envelopes
// spec: FRONTEND_ONTOGEN.md §Navigation — /ontogen redirects to /ontogen/result
// spec: FRONTEND_ONTOGEN.md §Page contracts — /ontogen/result: tabs Nodes/Edges/Triples/Graph;
//   each result tab renders a compact table with an All/Approved/Unapproved status filter;
//   the Graph tab hosts the force-directed view.
// ─────────────────────────────────────────────────────────────────────────────
test("UC3 step 4 (stub mode) — /ontogen/result tables + status filter + Graph tab; envelopes valid", async ({
  page,
  adminApi,
}) => {
  if (!confCreated) test.skip(true, "step 1 did not create conf");

  // -- UI assertion: /ontogen lands on the result browser (redirect; mechanism-agnostic) --
  // spec: FRONTEND_ONTOGEN.md §Navigation — "/ontogen redirects to /ontogen/result."
  // The redirect may be server (302) or client; assert the final resting URL + heading, not how.
  await page.goto("/ontogen");
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page).toHaveURL(/\/ontogen\/result\/?$/, { timeout: 15_000 });

  // -- UI assertion: result browser heading (convenience landmark) --
  // Heading text is a landmark only; the binding route → surface invariant (FRONTEND_ONTOGEN.md
  // §Navigation / §Page contracts — /ontogen/result is the triple-ontology browser with
  // Nodes/Edges/Triples/Graph tabs) is asserted via the resting URL and the tab set below.
  await expect(
    page.getByRole("heading", { name: "Ontology Generation", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: tabs visible (Nodes, Edges, Triples, Graph) — Navigator renamed to Graph --
  // spec: FRONTEND_ONTOGEN.md §Page contracts — result browser tabs Nodes/Edges/Triples/Graph
  await expect(page.getByRole("tab", { name: "Nodes" })).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole("tab", { name: "Edges" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Triples" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Graph" })).toBeVisible();
  // The Navigator tab no longer exists (replaced by Graph).
  await expect(page.getByRole("tab", { name: "Navigator" })).toHaveCount(0);

  // -- UI assertion: Nodes tab selected by default; panel renders without error --
  // result/page.tsx — <Tabs defaultValue="nodes">. NodesPanel renders a compact Table when rows
  // exist or an empty-state line otherwise. Under stub there are zero rows; either way the panel
  // must not crash. We assert the absence of the destructive error branch.
  await expect(
    page.getByText("Failed to load nodes:", { exact: false })
  ).not.toBeVisible({ timeout: 5_000 });

  // -- UI assertion: each result tab carries an All/Approved/Unapproved status filter --
  // spec: FRONTEND_ONTOGEN.md §Page contracts — "each result tab carries a status filter
  //   (All / Approved / Unapproved) applied client-side over the fetched set."
  // approval-filter.tsx exposes a Select with aria-label "Status filter".
  const statusFilter = page.getByLabel("Status filter");
  await expect(statusFilter.first()).toBeVisible({ timeout: 5_000 });

  // Snapshot how many table rows the Nodes table shows under each filter mode. Under stub there
  // are zero rows so all modes are empty (count 0); when rows exist (real-LLM / pre-seeded state)
  // the Approved/Unapproved partition must be a subset of All. The binding invariant — switching
  // the filter changes the visible set, never grows it beyond All — holds in both regimes.
  // A table body row is <tr> inside <tbody>; locate within the active Nodes panel.
  const nodesRows = page.getByRole("tabpanel").getByRole("row");
  async function bodyRowCount(): Promise<number> {
    const total = await nodesRows.count();
    // Subtract the header row (<tr> in <thead>) when a table is present; 0 when empty-state.
    return total > 0 ? Math.max(0, total - 1) : 0;
  }
  // mode=all
  await statusFilter.first().click();
  await page.getByRole("option", { name: "All", exact: true }).click();
  const allCount = await bodyRowCount();
  // mode=approved
  await statusFilter.first().click();
  await page.getByRole("option", { name: "Approved", exact: true }).click();
  const approvedCount = await bodyRowCount();
  // mode=unapproved
  await statusFilter.first().click();
  await page.getByRole("option", { name: "Unapproved", exact: true }).click();
  const unapprovedCount = await bodyRowCount();
  // The filtered partitions never exceed the unfiltered set, and approved+unapproved = all
  // (filterByApproval partitions the fetched page). Holds at 0/0/0 under stub.
  expect(approvedCount).toBeLessThanOrEqual(allCount);
  expect(unapprovedCount).toBeLessThanOrEqual(allCount);
  expect(approvedCount + unapprovedCount).toBe(allCount);
  // Reset to All for the remaining assertions.
  await statusFilter.first().click();
  await page.getByRole("option", { name: "All", exact: true }).click();

  // -- UI gesture: click Edges tab; panel renders without error --
  await page.getByRole("tab", { name: "Edges" }).click();
  await expect(
    page.getByText("Failed to load edges:", { exact: false })
  ).not.toBeVisible({ timeout: 5_000 });
  await expect(page.getByLabel("Status filter").first()).toBeVisible();

  // -- UI gesture: click Triples tab; panel renders without error --
  await page.getByRole("tab", { name: "Triples" }).click();
  await expect(
    page.getByText("Failed to load triples:", { exact: false })
  ).not.toBeVisible({ timeout: 5_000 });
  await expect(page.getByLabel("Status filter").first()).toBeVisible();

  // -- UI gesture: click Graph tab; assert the force-graph container mounts --
  // spec: FRONTEND_ONTOGEN.md §Page contracts — "the Graph tab hosts an interactive
  //   force-directed view (All / Approved-only filter)." ontology-graph.tsx tags the canvas host
  //   with data-testid="ontology-graph-canvas" and exposes a Select aria-label "Graph filter".
  await page.getByRole("tab", { name: "Graph" }).click();
  await expect(page.getByTestId("ontology-graph-canvas")).toBeVisible({ timeout: 15_000 });
  // The graph carries an All / Approved-only filter (no Unapproved-only).
  await expect(page.getByLabel("Graph filter")).toBeVisible({ timeout: 5_000 });
  await page.getByLabel("Graph filter").click();
  await expect(page.getByRole("option", { name: "Approved-only", exact: true })).toBeVisible();
  await expect(page.getByRole("option", { name: "Unapproved", exact: false })).toHaveCount(0);
  // Apply Approved-only and confirm the container stays mounted (no crash on filter change).
  await page.getByRole("option", { name: "Approved-only", exact: true }).click();
  await expect(page.getByTestId("ontology-graph-canvas")).toBeVisible();

  // -- Backend probes: GET result/{node,edge,triple} — assert standard envelope shape --
  // spec: USE_CASE_en.md §UC3 §API Mapping — paginated envelope: {nodes|edges|triples, offset, limit, total_count}
  // spec: API.md §Standard Envelope
  for (const [resultType, listKey] of [
    ["node", "nodes"],
    ["edge", "edges"],
    ["triple", "triples"],
  ] as const) {
    const listResp = await adminApi.get(
      `/api/v1/spoke/ontogen/result/${resultType}?offset=0&limit=10`
    );
    expect(listResp.status(), `GET result/${resultType} failed`).toBe(200);
    const listBody = (await listResp.json()) as {
      [key: string]: unknown;
      offset: number;
      limit: number;
      total_count: number;
    };
    expect(listKey in listBody, `result/${resultType} missing list key '${listKey}'`).toBe(true);
    expect(Array.isArray(listBody[listKey])).toBe(true);
    expect(listBody.offset).toBe(0);
    expect(listBody.limit).toBe(10);
    expect(typeof listBody.total_count).toBe("number");
    expect(listBody.total_count).toBeGreaterThanOrEqual(0);

    // Coherence check: if total_count ≤ 10, list length must equal total_count.
    const rows = listBody[listKey] as unknown[];
    if (listBody.total_count <= 10) {
      expect(rows.length).toBe(listBody.total_count);
    }
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 4b — Revoke an approved node from the result table → status flips to rejected
// spec: FRONTEND_ONTOGEN.md §Page contracts — "review actions are status-adaptive; an approved
//   row is revocable (offers Reject)." review-row.tsx surfaces Reject for status==="approved".
// spec: FRONTEND_ONTOGEN.md §Page contracts — an approved row is revocable (offers Reject).
//
// Data-conditional: rows exist only after a real-LLM run (stub runs persist zero). When the result
// set is empty (stub default) the revoke gesture has nothing to act on, so the round-trip is
// skipped — mirroring how step 4 treats the stub no-op. When ≥1 node exists, we drive an approved
// row into the rejected state through the UI and confirm via the backend read-back.
// ─────────────────────────────────────────────────────────────────────────────
test("UC3 step 4b — revoke an approved node via the result table (Reject) round-trips to rejected", async ({
  page,
  adminApi,
}) => {
  if (!confCreated) test.skip(true, "step 1 did not create conf");

  // Find a node to operate on. If none exist (stub mode), skip the revoke round-trip.
  const NODE_LIST = "/api/v1/spoke/ontogen/result/node?offset=0&limit=10";
  const listResp = await adminApi.get(NODE_LIST);
  expect(listResp.status()).toBe(200);
  const listBody = (await listResp.json()) as {
    nodes: Array<{ id: string; name: string; status: string }>;
  };
  if (listBody.nodes.length === 0) {
    test.skip(true, "no ontology nodes (stub run persists zero rows); nothing to revoke");
  }

  // Drive the chosen node into the approved state via the API so the UI presents the revoke
  // (Reject) action. This setup mirrors a prior human approval; the test exercises the *revoke*.
  const target = listBody.nodes[0];
  const REVIEW = (id: string) => `/api/v1/spoke/ontogen/result/node/${id}/method/review`;
  const approveResp = await adminApi.post(REVIEW(target.id), { data: { verdict: "approve" } });
  expect(approveResp.status()).toBe(200);
  const approved = (await approveResp.json()) as { status: string };
  // spec: BACKEND_LLM.md §Review — approve sets status="approved" (no status guard).
  expect(approved.status).toBe("approved");

  // Navigate to the result browser, Nodes tab, and filter to Approved so the target row is shown.
  await page.goto("/ontogen/result");
  await expect(page).not.toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: "Ontology Generation", exact: true })
  ).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("tab", { name: "Nodes" })).toBeVisible({ timeout: 10_000 });

  // Filter to Approved to isolate the approved row.
  await page.getByLabel("Status filter").first().click();
  await page.getByRole("option", { name: "Approved", exact: true }).click();

  // Locate the target node's row by its name (rendered in the Name cell) within the Nodes panel.
  const nodeRow = page
    .getByRole("tabpanel")
    .getByRole("row")
    .filter({ hasText: target.name });
  await expect(nodeRow.first()).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: an approved row offers only Reject (revoke), no Approve --
  // spec: FRONTEND_ONTOGEN.md §Page contracts — approved row → Reject only (reviewActionsForStatus).
  await expect(nodeRow.first().getByRole("button", { name: "Approve" })).toHaveCount(0);

  // -- UI gesture: click Reject in the row to open the reason confirm dialog --
  // spec: FRONTEND_ONTOGEN.md §Page contracts — review reason is entered in the Approve/Reject
  //   confirm popup (no inline reason input); review-row.tsx opens a Dialog with a reason textarea
  //   and a Confirm action that submits { verdict, reason }.
  await nodeRow.first().getByRole("button", { name: "Reject", exact: true }).click();

  // -- UI assertion: the reason confirm dialog opens with a "Reject node" title --
  // review-row.tsx — DialogTitle `${activeVerdict === "approve" ? "Approve" : "Reject"} ${kind}`.
  const rejectDialog = page.getByRole("dialog");
  await expect(
    rejectDialog.getByRole("heading", { name: "Reject node", exact: true })
  ).toBeVisible({ timeout: 5_000 });

  // -- UI gesture: enter an optional reason in the popup (not inline) --
  // review-row.tsx — <textarea placeholder="reason (optional)"> inside the confirm Dialog.
  await rejectDialog.getByPlaceholder(/reason/i).fill("revoking after re-review");

  // -- UI gesture: click Confirm to submit { verdict: "reject", reason } --
  // review-row.tsx — DialogFooter Confirm button fires the review mutation.
  await rejectDialog.getByRole("button", { name: /^confirm$/i }).click();

  // -- UI assertion: a "node rejected" toast confirms the review posted --
  // review-row.tsx — onSuccess toast title `${kind} <past-tense verdict>` → "node rejected".
  await expect(page.getByText(/node rejected/i).first()).toBeVisible({ timeout: 15_000 });

  // -- Backend probe (dual confirmation): GET the node → status === "rejected" --
  // spec: FRONTEND_ONTOGEN.md §Page contracts — revoke (Reject on an approved row) flips it to rejected; the read-back reflects it.
  await expect
    .poll(
      async () => {
        const getResp = await adminApi.get(`/api/v1/spoke/ontogen/result/node/${target.id}`);
        if (!getResp.ok()) return null;
        const body = (await getResp.json()) as { status: string };
        return body.status;
      },
      { timeout: 15_000, message: "node status did not flip to rejected after UI revoke" }
    )
    .toBe("rejected");
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 5 — Cleanup via UI: DELETE seed; PATCH conf disabled
// spec: USE_CASE_en.md §UC3 — cleanup: DELETE seed, PATCH conf disabled
// spec: FRONTEND_ONTOGEN.md §Page contracts — seed Delete button → ConfirmDialog
// ─────────────────────────────────────────────────────────────────────────────
test("UC3 step 5 — delete seed via UI ConfirmDialog; disable conf", async ({
  page,
  adminApi,
}) => {
  if (!seedId || !confCreated) test.skip(true, "steps 1/2 did not complete");

  // Navigate to the seed library.
  await page.goto("/ontogen/seed");
  await expect(page).not.toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: "OntoGen — Seed Library", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI gesture: find the seed row and click its Delete button --
  // spec: FRONTEND_ONTOGEN.md §Page contracts — seed row: Edit + Delete buttons per row
  // ontogen/seed/page.tsx lines 227-233: <Button variant="destructive">Delete</Button>
  // The seed_id is rendered in the row in font-mono; click Delete in that row.
  // We locate the seed by its seed_id text, then find the Delete button in the same row.
  // seed_id is rendered as a <p> with font-mono, unique per row.
  const seedRow = page.locator("li").filter({ hasText: seedId! });
  await expect(seedRow).toBeVisible({ timeout: 10_000 });
  // exact — the row container is itself role="button" whose aggregated name
  // contains "Delete"; only the actual button's name is exactly "Delete".
  await seedRow.getByRole("button", { name: "Delete", exact: true }).click();

  // -- UI assertion: ConfirmDialog appears with "Delete seed" title --
  // ontogen/seed/page.tsx (DeleteSeedDialog) → ConfirmDialog title "Delete seed"
  await expect(page.getByRole("heading", { name: "Delete seed", exact: true })).toBeVisible({
    timeout: 5_000,
  });

  // -- UI gesture: confirm deletion in ConfirmDialog --
  // spec: FRONTEND_BASIC.md §ConfirmDialog — confirmLabel="Delete" button
  await page.getByRole("button", { name: "Delete", exact: true }).last().click();

  // -- UI assertion: "Seed deleted" toast --
  // ontogen/seed/page.tsx line 114: toast({ title: "Seed deleted" })
  await expect(page.getByText("Seed deleted", { exact: false }).first()).toBeVisible({ timeout: 15_000 });

  // -- Backend probe: GET /spoke/ontogen/attr/seed → seed_id no longer present --
  // spec: USE_CASE_en.md §UC3 — DELETE seed; subsequent GET list does not include it
  const listResp = await adminApi.get(`${SEED_API}?limit=50`);
  expect(listResp.status()).toBe(200);
  const listBody = (await listResp.json()) as { seeds: Array<{ seed_id: string }> };
  const deletedSeedStillPresent = listBody.seeds.some((s) => s.seed_id === seedId);
  expect(
    deletedSeedStillPresent,
    `seed_id ${seedId} still present after DELETE`
  ).toBe(false);
  seedId = null; // Mark cleaned up so afterAll does not double-delete.

  // -- Navigate to /ontogen/conf and disable via PATCH (API-fired) --
  // UI path would be: Edit → uncheck is_enabled → Save.
  // We use adminApi for conf disable cleanup to keep the test deterministic
  // (conf page rendering depends on whether conf exists, making UI disable fragile).
  const patchResp = await adminApi.patch(CONF_API, { data: { is_enabled: false } });
  expect(patchResp.status()).toBe(200);
  confCreated = false;

  // -- Backend probe: GET conf → is_enabled=false --
  // spec: USE_CASE_en.md §UC3 — PATCH conf disabled; GET round-trips is_enabled=false
  const getResp = await adminApi.get(CONF_API);
  expect(getResp.status()).toBe(200);
  const conf = (await getResp.json()) as { is_enabled: boolean };
  expect(conf.is_enabled).toBe(false);
});

// ═══════════════════════════════════════════════════════════════════════════════
// REAL-LLM VARIANT
//
// Structurally identical to the stub-mode tests above, but:
//   - Skips when stub_llm_client=true (the dev default).
//   - Adds assertion after step 4: any_rows_found must be true.
// Run by setting stub_llm_client=false via PATCH /api/v1/admin/conf and rerunning.
// ═══════════════════════════════════════════════════════════════════════════════

// Real-LLM module-level state (independent from stub state).
let realSeedId: string | null = null;
let realConfCreated = false;

test.afterAll(async ({ adminApi }) => {
  if (realSeedId) {
    await adminApi.delete(`${SEED_API}/${realSeedId}`);
    realSeedId = null;
  }
  if (realConfCreated) {
    await adminApi.patch(CONF_API, { data: { is_enabled: false } });
    realConfCreated = false;
  }
});

// ── Helper: read stub_llm_client from /admin/conf ──────────────────────────────

async function readStubLlmClient(
  adminApi: import("@playwright/test").APIRequestContext
): Promise<boolean> {
  const resp = await adminApi.get("/api/v1/admin/conf");
  if (!resp.ok()) return true; // fail-safe: treat as stubbed
  const body = (await resp.json()) as { stub_llm_client: boolean };
  return body.stub_llm_client;
}

// ─────────────────────────────────────────────────────────────────────────────
// Real-LLM step 1 — enable ontogen conf (same as stub step 1)
// ─────────────────────────────────────────────────────────────────────────────
test("UC3 real-LLM step 1 — enable ontogen conf", async ({ adminApi }) => {
  const stubLlm = await readStubLlmClient(adminApi);
  test.skip(stubLlm, "stub_llm_client=true; set false via PATCH /admin/conf to run real-LLM tests");

  const putResp = await adminApi.put(CONF_API, { data: CONF_PAYLOAD });
  expect([200, 201]).toContain(putResp.status());
  realConfCreated = true;

  // spec: USE_CASE_en.md §UC3 §Conf — PUT round-trips is_enabled, schedule_tier
  const getResp = await adminApi.get(CONF_API);
  expect(getResp.status()).toBe(200);
  const conf = (await getResp.json()) as { is_enabled: boolean; schedule_tier: string | null };
  expect(conf.is_enabled).toBe(true);
  expect(conf.schedule_tier).toBe("daily");
});

// ─────────────────────────────────────────────────────────────────────────────
// Real-LLM step 2 — create seed via UI (same as stub step 2)
// ─────────────────────────────────────────────────────────────────────────────
test("UC3 real-LLM step 2 — create domain seed", async ({ page, adminApi }) => {
  const stubLlm = await readStubLlmClient(adminApi);
  test.skip(stubLlm, "stub_llm_client=true; set false via PATCH /admin/conf to run real-LLM tests");
  if (!realConfCreated) test.skip(true, "real-LLM step 1 did not complete");

  await page.goto("/ontogen/seed");
  await expect(page).not.toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: "OntoGen — Seed Library", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  await page.getByRole("button", { name: "+ New Seed" }).click();
  await expect(page.getByText("New seed", { exact: true })).toBeVisible({ timeout: 5_000 });

  const seedTextarea = page.getByPlaceholder("# Ontology seed");
  await expect(seedTextarea).toBeVisible({ timeout: 5_000 });
  await seedTextarea.fill(SEED_MD);
  await page.getByRole("button", { name: "Save seed" }).click();
  await expect(page.getByText("Seed created", { exact: false }).first()).toBeVisible({ timeout: 15_000 });

  const listResp = await adminApi.get(`${SEED_API}?limit=50`);
  expect(listResp.status()).toBe(200);
  const listBody = (await listResp.json()) as {
    seeds: Array<{ seed_id: string; preview: string; updated_at: string }>;
  };
  const ourSeed = listBody.seeds.find((s) =>
    s.preview && SEED_MD.startsWith(s.preview.slice(0, 20))
  );
  const resolvedSeed =
    ourSeed ??
    listBody.seeds.reduce((a, b) =>
      new Date(a.updated_at) > new Date(b.updated_at) ? a : b
    );
  expect(resolvedSeed, "Could not find newly created seed in list").toBeTruthy();
  realSeedId = resolvedSeed.seed_id;
  expect(realSeedId).toBeTruthy();
  expect(resolvedSeed.preview).toBeTruthy();
  expect(resolvedSeed.updated_at).toBeTruthy();
});

// ─────────────────────────────────────────────────────────────────────────────
// Real-LLM step 3 — trigger Run; assert RUN_COMPLETE event + OntogenRunSummary
// ─────────────────────────────────────────────────────────────────────────────
test("UC3 real-LLM step 3 — trigger Run; assert OntogenRunSummary shape", async ({
  page,
  adminApi,
}) => {
  const stubLlm = await readStubLlmClient(adminApi);
  test.skip(stubLlm, "stub_llm_client=true; set false via PATCH /admin/conf to run real-LLM tests");
  if (!realConfCreated || !realSeedId) test.skip(true, "real-LLM steps 1/2 did not complete");

  // Run lives on /ontogen/conf (FRONTEND_ONTOGEN.md §Page contracts — Run control top-right).
  await page.goto("/ontogen/conf");
  await expect(page).not.toHaveURL(/\/login/);
  // Heading is a convenience landmark; the binding route → surface invariant (Run control on
  // /ontogen/conf) is asserted via the URL and the Run button assertion below.
  await expect(
    page.getByRole("heading", { name: "OntoGen — Configuration", exact: true })
  ).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("button", { name: "Run" })).toBeVisible({ timeout: 10_000 });

  // Open RunDialog → leave dry_run unchecked → Run. Capture the single run's
  // response for the OntogenRunSummary shape assertion below — do NOT fire a
  // second run: run_id is written only on insert, so a second real-LLM run would
  // mostly reuse rows (keeping run 1's id) and skew step 4's run_id scoping.
  await page.getByRole("button", { name: "Run" }).first().click();
  await expect(
    page.getByRole("heading", { name: "Run ontology inference", exact: true })
  ).toBeVisible({ timeout: 5_000 });
  const runRespPromise = page.waitForResponse(
    (r) => r.url().includes("/spoke/ontogen/method/run") && r.request().method() === "POST",
    { timeout: 300_000 }
  );
  await page.getByRole("button", { name: "Run" }).last().click();
  const runResp = await runRespPromise;

  // Wait for run-complete toast (real LLM may take longer).
  await expect(
    page.getByText(/run complete|dry run complete/i).first()
  ).toBeVisible({ timeout: 300_000 });

  // Poll for ONTOGEN.RUN_COMPLETE event.
  const deadline = Date.now() + 120_000;
  let runCompleteEvent: Record<string, unknown> | null = null;
  while (Date.now() < deadline) {
    const evResp = await adminApi.get(EVENT_API);
    if (evResp.ok()) {
      const evBody = (await evResp.json()) as {
        events: Array<{ event_type: string; detail: Record<string, unknown> }>;
      };
      runCompleteEvent =
        (evBody.events.find((e) => e.event_type === "ONTOGEN.RUN_COMPLETE") as
          | Record<string, unknown>
          | undefined) ?? null;
      if (runCompleteEvent) break;
    }
    await new Promise((res) => setTimeout(res, 3_000));
  }
  expect(runCompleteEvent, "ONTOGEN.RUN_COMPLETE event not found after real-LLM run").toBeTruthy();

  const detail = (runCompleteEvent as { detail: Record<string, unknown> }).detail;
  const outcome = detail["debate_outcome"];
  expect(["accept", "turns_exhausted", "cycle_detected"]).toContain(outcome);
  expect(typeof detail["producer_iterations"]).toBe("number");
  expect(detail["producer_iterations"] as number).toBeGreaterThanOrEqual(1);
  expect(typeof detail["producer_errors_dropped"]).toBe("number");
  expect(detail["producer_errors_dropped"] as number).toBeGreaterThanOrEqual(0);

  // OntogenRunSummary shape (from the single UI run's response, captured above).
  expect(runResp.status()).toBe(200);
  const runBody = (await runResp.json()) as {
    status: string;
    dry_run: boolean;
    unresolved_urns: unknown[];
    counts: { nodes_added: number; edges_added: number; triples_added: number };
  };
  expect(typeof runBody.status).toBe("string");
  expect(runBody.dry_run).toBe(false);
  expect(Array.isArray(runBody.unresolved_urns)).toBe(true);
  expect(runBody.counts.nodes_added).toBeGreaterThanOrEqual(0);
  expect(runBody.counts.edges_added).toBeGreaterThanOrEqual(0);
  expect(runBody.counts.triples_added).toBeGreaterThanOrEqual(0);
});

// ─────────────────────────────────────────────────────────────────────────────
// Real-LLM step 4 — result envelopes + per-row run_id (Langfuse session link); any_rows_found
// spec: USE_CASE_en.md §UC3 — real LLM must persist ≥1 row; open the run's Langfuse session
// spec: BACKEND_LLM.md §Evidence shape — row.run_id = session id; transcript lives in Langfuse
// ─────────────────────────────────────────────────────────────────────────────
test("UC3 real-LLM step 4 — result envelopes valid; ≥1 row persisted; rows carry run_id; Evidence Link present", async ({
  page,
  adminApi,
}) => {
  const stubLlm = await readStubLlmClient(adminApi);
  test.skip(stubLlm, "stub_llm_client=true; set false via PATCH /admin/conf to run real-LLM tests");
  if (!realConfCreated) test.skip(true, "real-LLM step 1 did not complete");

  // /ontogen redirects to /ontogen/result (FRONTEND_ONTOGEN.md §Navigation).
  await page.goto("/ontogen");
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page).toHaveURL(/\/ontogen\/result\/?$/, { timeout: 15_000 });
  // Heading is a convenience landmark; the binding route → surface invariant (/ontogen/result
  // is the triple-ontology browser with Nodes/Edges/Triples tabs) is asserted via the resting
  // URL and the tab assertions below.
  await expect(
    page.getByRole("heading", { name: "Ontology Generation", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // Assert the result tables render without errors and carry the status filter.
  // spec: FRONTEND_ONTOGEN.md §Page contracts — each result tab is a compact table with the
  //   All/Approved/Unapproved filter; the Graph tab hosts the force-directed view.
  for (const tabName of ["Nodes", "Edges", "Triples"] as const) {
    await page.getByRole("tab", { name: tabName }).click();
    await expect(
      page.getByText(`Failed to load ${tabName.toLowerCase()}:`, { exact: false })
    ).not.toBeVisible({ timeout: 5_000 });
    await expect(page.getByLabel("Status filter").first()).toBeVisible({ timeout: 5_000 });
  }

  // Graph tab mounts its force-directed canvas container (real run has ≥1 node to render).
  await page.getByRole("tab", { name: "Graph" }).click();
  await expect(page.getByTestId("ontology-graph-canvas")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByLabel("Graph filter")).toBeVisible({ timeout: 5_000 });

  // Resolve the latest run's id from the RUN_COMPLETE event — it doubles as the
  // Langfuse session id that every row this run persisted points at.
  // spec: BACKEND_LLM.md §Evidence shape — session_id = run_id (detail.run_id).
  const evResp = await adminApi.get(EVENT_API);
  expect(evResp.status()).toBe(200);
  const evBody = (await evResp.json()) as {
    events: Array<{ event_type: string; detail: { run_id?: string } }>;
  };
  const runComplete = evBody.events.find((e) => e.event_type === "ONTOGEN.RUN_COMPLETE");
  expect(runComplete, "no ONTOGEN.RUN_COMPLETE event").toBeTruthy();
  const runId = runComplete!.detail.run_id;
  expect(typeof runId, "RUN_COMPLETE detail.run_id must be a string").toBe("string");
  expect((runId as string).length).toBeGreaterThan(0);

  // Backend: result envelopes + per-row run_id (the row's link to its Langfuse session).
  // spec: BACKEND_LLM.md §Evidence shape — row.run_id = session_id; no persisted transcript.
  let anyRowsFound = false;

  for (const [resultType, listKey] of [
    ["node", "nodes"],
    ["edge", "edges"],
    ["triple", "triples"],
  ] as const) {
    const listResp = await adminApi.get(
      `/api/v1/spoke/ontogen/result/${resultType}?offset=0&limit=10`
    );
    expect(listResp.status()).toBe(200);
    const listBody = (await listResp.json()) as {
      [key: string]: unknown;
      offset: number;
      limit: number;
      total_count: number;
    };
    expect(listKey in listBody).toBe(true);
    expect(Array.isArray(listBody[listKey])).toBe(true);
    expect(listBody.offset).toBe(0);
    expect(listBody.limit).toBe(10);
    expect(typeof listBody.total_count).toBe("number");
    expect(listBody.total_count).toBeGreaterThanOrEqual(0);
    if (listBody.total_count <= 10) {
      expect((listBody[listKey] as unknown[]).length).toBe(listBody.total_count);
    }

    // Every result row exposes a run_id; rows this run produced carry run_id == the
    // RUN_COMPLETE event's run_id (their link to the run's Langfuse session). Rows from
    // prior runs / seeded fixtures carry a different id or null, so scope the "produced
    // ≥1 row" check to this run — anyRowsFound is the discriminating signal (a null or
    // swapped run_id leaves no matching row and fails the assertion below).
    // spec: BACKEND_LLM.md §Evidence shape — row.run_id = session_id
    const rows = listBody[listKey] as Array<{ id: string; run_id: string | null }>;
    for (const row of rows) {
      expect(row, `${resultType} ${row.id} missing run_id field`).toHaveProperty("run_id");
    }
    if (rows.some((r) => r.run_id === runId)) {
      anyRowsFound = true;
    }
  }

  // spec: BACKEND_LLM.md §Test Mode — real LLM must persist ≥1 row stamped with this run's id
  expect(anyRowsFound, "Real LLM run produced zero rows carrying this run's run_id").toBe(true);

  // -- UI assertion: the Nodes table exposes the Evidence column with a Langfuse Link --
  // spec: FRONTEND_ONTOGEN.md §Result table — Evidence column (after Created At) renders a
  //   Link opening the run's Langfuse session in a new tab; the Confidence cell is score-only.
  // evidence-link.tsx renders <a target="_blank" rel="noopener noreferrer">Link</a>.
  await page.getByRole("tab", { name: "Nodes" }).click();
  const nodesPanel = page.getByRole("tabpanel");
  await expect(
    nodesPanel.getByRole("columnheader", { name: "Evidence", exact: true })
  ).toBeVisible({ timeout: 10_000 });
  // The Confidence cell no longer hosts an Evidence button.
  await expect(nodesPanel.getByRole("button", { name: /evidence/i })).toHaveCount(0);
  // When the langfuse host + slug are configured for the browser, the row's Evidence cell
  // is an external Link to .../sessions/{run_id}; otherwise it is an em dash. Assert whichever
  // the configured deployment renders, and that any Link opens a new tab.
  const firstNodeRow = nodesPanel.getByRole("row").nth(1);
  const evidenceLink = firstNodeRow.getByRole("link", { name: "Link", exact: true });
  if (await evidenceLink.count()) {
    await expect(evidenceLink.first()).toHaveAttribute("target", "_blank");
    await expect(evidenceLink.first()).toHaveAttribute("rel", /noopener/);
    await expect(evidenceLink.first()).toHaveAttribute("href", /\/sessions\//);
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Real-LLM step 5 — cleanup: DELETE seed; PATCH conf disabled
// ─────────────────────────────────────────────────────────────────────────────
test("UC3 real-LLM step 5 — cleanup: delete seed + disable conf", async ({ page, adminApi }) => {
  const stubLlm = await readStubLlmClient(adminApi);
  test.skip(stubLlm, "stub_llm_client=true; set false via PATCH /admin/conf to run real-LLM tests");

  if (realSeedId) {
    await page.goto("/ontogen/seed");
    await expect(page).not.toHaveURL(/\/login/);
    await expect(
      page.getByRole("heading", { name: "OntoGen — Seed Library", exact: true })
    ).toBeVisible({ timeout: 15_000 });

    const seedRow = page.locator("li").filter({ hasText: realSeedId });
    await expect(seedRow).toBeVisible({ timeout: 10_000 });
    await seedRow.getByRole("button", { name: "Delete" }).click();
    await expect(page.getByRole("heading", { name: "Delete seed", exact: true })).toBeVisible({
      timeout: 5_000,
    });
    await page.getByRole("button", { name: "Delete", exact: true }).last().click();
    await expect(page.getByText("Seed deleted", { exact: false }).first()).toBeVisible({ timeout: 15_000 });

    const listResp = await adminApi.get(`${SEED_API}?limit=50`);
    expect(listResp.status()).toBe(200);
    const listBody = (await listResp.json()) as { seeds: Array<{ seed_id: string }> };
    expect(listBody.seeds.some((s) => s.seed_id === realSeedId)).toBe(false);
    realSeedId = null;
  }

  if (realConfCreated) {
    const patchResp = await adminApi.patch(CONF_API, { data: { is_enabled: false } });
    expect(patchResp.status()).toBe(200);
    const getResp = await adminApi.get(CONF_API);
    expect(getResp.status()).toBe(200);
    const conf = (await getResp.json()) as { is_enabled: boolean };
    expect(conf.is_enabled).toBe(false);
    realConfCreated = false;
  }
});
