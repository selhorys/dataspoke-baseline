/**
 * UC3 — Ontology Generation: browser UI flow.
 *
 * Mirrors tests/integration/api_wired/test_uc3_ontology_generation.py step-for-step,
 * with dual confirmation at each mutating step:
 *   - UI assertion (heading, toast, tabs, badge, panel text)
 *   - Backend probe via adminApi (same REST read-back the api-wired step asserts)
 *
 * TWO structurally identical variants:
 *   - Stub-mode variant (stub_llm_client=true, dev default): all steps run.
 *     Under stub the LLM Producer returns an empty payload, so zero rows are
 *     persisted; the per-row evidence loop is intentionally a no-op.
 *   - Real-LLM variant: skips when stub_llm_client=true.  Adds an assertion that
 *     at least one node/edge/triple row was persisted.
 *
 * Steps (verbatim from USE_CASE_en.md §UC3 Imazon Example):
 *   1. Navigate to /ontogen/conf; fill is_enabled + schedule_tier; Save.
 *      Backend: PUT /spoke/ontogen/attr/conf → 200/201; round-trips fields.
 *   2. Navigate to /ontogen/seed; click "+ New Seed"; paste domain Markdown; Save seed.
 *      Backend: GET /spoke/ontogen/attr/seed → seed_id present; preview + updated_at set.
 *   3. Navigate to /ontogen; click Run button → RunDialog → Run.
 *      Backend poll until ONTOGEN.RUN_COMPLETE event appears; assert OntogenRunSummary shape.
 *   4. GET /spoke/ontogen/event → find ONTOGEN.RUN_COMPLETE; assert debate fields.
 *   5. On /ontogen, assert Nodes/Edges/Triples tabs render panels (no-op on count under stub).
 *      Backend: GET result/{node,edge,triple} → standard envelope shape each.
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

  // -- UI assertion: page heading rendered --
  // spec: FRONTEND_ONTOGEN.md §Page contracts — conf page h1 "OntoGen — Configuration"
  // ontogen/conf/page.tsx line 66: <h1>OntoGen — Configuration</h1>
  await expect(
    page.getByRole("heading", { name: "OntoGen — Configuration", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: Edit button visible (conf may exist from prior run; skip create branch) --
  // If the conf already exists the page shows Edit/Delete buttons.
  // If it does not exist yet it shows an EmptyState with no Edit button.
  // We use adminApi to PUT the conf directly, then reload to get into the known-good state,
  // avoiding brittle conditional UI branching in the spec.
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

  // -- UI assertion: Edit button now visible (conf present) --
  // spec: FRONTEND_ONTOGEN.md §Page contracts — canWrite → Edit + Delete buttons
  // ontogen/conf/page.tsx lines 97-109: Edit + Delete buttons when conf present and not editing
  await expect(page.getByRole("button", { name: "Edit" })).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole("button", { name: "Delete" })).toBeVisible();

  // -- UI assertion: the form renders (is_enabled checkbox, schedule_tier select) --
  // spec: FRONTEND_ONTOGEN.md §Page contracts — conf-form fields: is_enabled, schedule_tier
  // conf-form.tsx line 96: Checkbox id="conf-is-enabled"
  await expect(page.locator("#conf-is-enabled")).toBeVisible();
  // conf-form.tsx line 121: SelectTrigger id="conf-schedule-tier"
  await expect(page.locator("#conf-schedule-tier")).toBeVisible();

  // -- UI gesture: click Edit to enable the form --
  await page.getByRole("button", { name: "Edit" }).click();

  // The form is now in editing mode; "Save configuration" button is visible.
  // conf-form.tsx line 155: <Button type="submit">Save configuration</Button>
  await expect(page.getByRole("button", { name: "Save configuration" })).toBeVisible({ timeout: 5_000 });

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
  await page.getByRole("button", { name: "Save configuration" }).click();

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
});

// ─────────────────────────────────────────────────────────────────────────────
// Step 3 — Trigger real (non-dry-run) inference via /ontogen Run button
// spec: USE_CASE_en.md §UC3 §Run semantics — non-dry-run persists rows
// spec: FRONTEND_ONTOGEN.md §Page contracts — /ontogen: POST method/run via RunDialog
// ─────────────────────────────────────────────────────────────────────────────
test("UC3 step 3 (stub mode) — trigger Run from /ontogen; assert OntogenRunSummary shape", async ({
  page,
  adminApi,
}) => {
  if (!confCreated || !seedId) test.skip(true, "steps 1/2 did not complete");

  // Navigate to the main ontogen page.
  // spec: FRONTEND_ONTOGEN.md §Navigation — /ontogen → Browser + review
  await page.goto("/ontogen");
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: page heading --
  // spec: FRONTEND_ONTOGEN.md §Page contracts — /ontogen h1 "Ontology Generation"
  // ontogen/page.tsx line 43: <h1>Ontology Generation</h1>
  await expect(
    page.getByRole("heading", { name: "Ontology Generation", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: tabs visible (Nodes, Edges, Triples, Navigator) --
  // spec: FRONTEND_ONTOGEN.md §Page contracts — tabs rendered for nodes/edges/triples/navigator
  // ontogen/page.tsx lines 53-57: TabsTrigger values nodes/edges/triples/navigator
  await expect(page.getByRole("tab", { name: "Nodes" })).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole("tab", { name: "Edges" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Triples" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "Navigator" })).toBeVisible();

  // -- UI assertion: Run button visible (canWrite=Admin) --
  // spec: FRONTEND_ONTOGEN.md §Page contracts — Run button: only for Editor/Admin
  // ontogen/page.tsx line 45: <Button>Run</Button> (or "Running…" when pending)
  await expect(page.getByRole("button", { name: "Run" })).toBeVisible({ timeout: 10_000 });

  // -- UI gesture: click Run to open RunDialog --
  // spec: FRONTEND_ONTOGEN.md §Page contracts — Run triggers RunDialog
  // run-dialog.tsx: <Dialog> with title "Run ontology inference"
  await page.getByRole("button", { name: "Run" }).click();

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
  // spec: USE_CASE_en.md §UC3 §Run semantics — 200, dry_run=false, counts dict
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
// Step 4 — /ontogen page: Nodes/Edges/Triples tabs render; result envelope shape
// spec: USE_CASE_en.md §UC3 §API Mapping — list endpoints return paginated envelopes
// spec: FRONTEND_ONTOGEN.md §Page contracts — /ontogen: tabs show node/edge/triple panels
// ─────────────────────────────────────────────────────────────────────────────
test("UC3 step 4 (stub mode) — /ontogen tabs render panels; result envelopes valid", async ({
  page,
  adminApi,
}) => {
  if (!confCreated) test.skip(true, "step 1 did not create conf");

  await page.goto("/ontogen");
  await expect(page).not.toHaveURL(/\/login/);

  await expect(
    page.getByRole("heading", { name: "Ontology Generation", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: Nodes tab selected by default; panel renders --
  // ontogen/page.tsx line 51: <Tabs defaultValue="nodes">
  // NodesPanel renders either "No ontology nodes yet." or a list of nodes.
  // Under stub mode there are zero rows; the empty-state message is shown.
  // nodes-panel.tsx line 47: <p>No ontology nodes yet.</p>
  // We assert the tab is visible and the panel content renders (not a JS error).
  await expect(page.getByRole("tab", { name: "Nodes" })).toBeVisible({ timeout: 10_000 });

  // The "Nodes" tab content is the default; assert panel loaded (either empty-state or rows).
  // Under stub: "No ontology nodes yet." is the expected text.
  // Under real LLM (skipped here): rows may be present.
  // Either way, asserting the panel renders without error is the correct check.
  // Wait for loading skeletons to disappear — the panel has a loading branch.
  // Assert that the tab content area is visible (nodes tab is active by default).
  // Use toBeVisible with a short timeout; if Skeletons clear in ~2s this passes.
  await expect(page.getByRole("tab", { name: "Nodes" })).toBeVisible({ timeout: 5_000 });
  // After any async load, either the empty-state or a node list renders — not a crash.
  // We cannot assert "No ontology nodes yet." because real-LLM runs may have left nodes.
  // Instead assert the tab panel is in a non-error state: no destructive error text.
  await expect(
    page.getByText("Failed to load nodes:", { exact: false })
  ).not.toBeVisible({ timeout: 5_000 });

  // -- UI gesture: click Edges tab --
  // ontogen/page.tsx line 53: TabsTrigger value="edges"
  await page.getByRole("tab", { name: "Edges" }).click();
  await expect(
    page.getByText("Failed to load edges:", { exact: false })
  ).not.toBeVisible({ timeout: 5_000 });

  // -- UI gesture: click Triples tab --
  await page.getByRole("tab", { name: "Triples" }).click();
  await expect(
    page.getByText("Failed to load triples:", { exact: false })
  ).not.toBeVisible({ timeout: 5_000 });

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
  // UI path would be: Edit → uncheck is_enabled → Save configuration.
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

  await page.goto("/ontogen");
  await expect(page).not.toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: "Ontology Generation", exact: true })
  ).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("button", { name: "Run" })).toBeVisible({ timeout: 10_000 });

  // Open RunDialog → leave dry_run unchecked → Run.
  await page.getByRole("button", { name: "Run" }).click();
  await expect(
    page.getByRole("heading", { name: "Run ontology inference", exact: true })
  ).toBeVisible({ timeout: 5_000 });
  await page.getByRole("button", { name: "Run" }).last().click();

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

  // OntogenRunSummary shape.
  const runResp = await adminApi.post(RUN_API);
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
// Real-LLM step 4 — result envelopes + evidence.debate keys; assert any_rows_found
// spec: USE_CASE_en.md §UC3 — real LLM must persist ≥1 row
// spec: BACKEND_LLM.md §Evidence shape — debate transcript in evidence JSONB
// ─────────────────────────────────────────────────────────────────────────────
test("UC3 real-LLM step 4 — result envelopes valid; ≥1 row persisted; evidence.debate keys present", async ({
  page,
  adminApi,
}) => {
  const stubLlm = await readStubLlmClient(adminApi);
  test.skip(stubLlm, "stub_llm_client=true; set false via PATCH /admin/conf to run real-LLM tests");
  if (!realConfCreated) test.skip(true, "real-LLM step 1 did not complete");

  await page.goto("/ontogen");
  await expect(page).not.toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: "Ontology Generation", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // Assert tabs render without errors.
  for (const tabName of ["Nodes", "Edges", "Triples"] as const) {
    await page.getByRole("tab", { name: tabName }).click();
    await expect(
      page.getByText(`Failed to load ${tabName.toLowerCase()}:`, { exact: false })
    ).not.toBeVisible({ timeout: 5_000 });
  }

  // Backend: result envelopes + per-row evidence.debate.
  // spec: BACKEND_LLM.md §Evidence shape — debate transcript in evidence JSONB
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

    const rows = listBody[listKey] as Array<{ id: string }>;
    for (const row of rows) {
      anyRowsFound = true;
      const attrResp = await adminApi.get(
        `/api/v1/spoke/ontogen/result/${resultType}/${row.id}/attr`
      );
      expect(attrResp.status()).toBe(200);
      const attrBody = (await attrResp.json()) as {
        evidence?: { debate?: Record<string, unknown> } | null;
      };
      const debate = attrBody.evidence?.debate;
      expect(
        debate,
        `${resultType} ${row.id} evidence missing 'debate' key`
      ).toBeTruthy();
      for (const key of [
        "turns_completed",
        "outcome",
        "final_reviewer_verdict",
        "rag_anchors",
        "history",
      ]) {
        expect(key in debate!, `evidence.debate for ${resultType} ${row.id} missing '${key}'`).toBe(
          true
        );
      }
      // spec: BACKEND_LLM.md §Termination
      expect(["accept", "turns_exhausted", "cycle_detected"]).toContain(debate!["outcome"]);
    }
  }

  // spec: BACKEND_LLM.md §Test Mode — real LLM must persist ≥1 row
  expect(anyRowsFound, "Real LLM run produced zero rows — verify prompt/filter pipeline").toBe(true);
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
