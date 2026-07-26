/**
 * Ground spec: /ontogen/conf — clicking "Edit" enters edit mode WITHOUT
 * submitting the conf form. Real-browser regression guard for the UC3 defect.
 *
 * Reported defect (confirmed in a real browser): the header renders
 * `editing ? (Save, Cancel) : (Edit, Run)` in the SAME conditional slot. Clicking
 * "Edit" fired PUT /spoke/ontogen/attr/conf + a "Configuration saved" toast and
 * never entered edit mode — React reused the same <button> DOM node across the
 * ternary and morphed it into the type="submit" form="ontogen-conf-form" Save
 * button during the setEditing flush, so the browser performed the click's
 * default submit on the now-submit node. Distinct `key` props on the four
 * conditional buttons fix it (separate DOM node per branch, no morph).
 *
 * jsdom cannot model the default-action phase, so the Vitest unit test passes
 * even on the unfixed code — this spec is the genuine behavioral guard. It runs
 * in a real Chromium under the admin (writer) storageState so the Edit/Run
 * header controls render.
 *
 * Assertions:
 *   (a) read-mode renders the conf as a plain-text VIEW (OntogenConfView), not a
 *       disabled form — no form control is in the DOM, and the rendered is_enabled
 *       value matches the conf read back over REST (dual confirmation);
 *   (b) clicking Edit fires NO conf write request;
 *   (c) edit mode is entered — Save + Cancel become visible and the editable form
 *       replaces the view (is_enabled checkbox appears, enabled);
 *   (d) positive leg — editing default_run_prompt and clicking the header Save
 *       fires exactly one conf write, shows the "Configuration saved" toast, and the
 *       change reads back over REST. Without it nothing proves the request predicate
 *       used by (b) can fire at all, and (b) would pass vacuously if the page moved
 *       to a route or verb the predicate does not match.
 *
 * The conf is a singleton, so (d) snapshots it in beforeAll and restores it in
 * afterAll with an asserted read-back.
 * spec: spec/TESTING.md §Integration Lifecycle & Isolation — "Snapshot → mutate →
 *   verified restore" for any singleton (ontogen conf named explicitly).
 *
 * spec: spec/feature/FRONTEND_ONTOGEN.md §Navigation — /ontogen/conf hosts the
 *   Run + Edit conf editor (Editor/Admin only); the conf is a singleton (no Delete).
 * spec: spec/feature/FRONTEND_ONTOGEN.md §Page contracts (/ontogen/conf) — "**Edit** swaps the view for
 *   the editable form; **Save** persists via `PUT/PATCH .../attr/conf` and **Cancel**
 *   discards." The verb is deliberately unpinned by the spec, so the request predicate
 *   below matches PUT and PATCH alike.
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group, real-session role.
 */

import { test, expect } from "../../fixtures/index";

// The route Save (and only Save) writes to. Matched as a substring of the request URL.
const CONF_PATH = "/api/v1/spoke/ontogen/attr/conf";
// Both verbs the spec sanctions for Save; the guard must not be blind to either.
const WRITE_METHODS = new Set(["PUT", "PATCH"]);

// Admin-only — filename convention (`*.spec.ts` → admin project). The admin role is a
// writer, so the top-right Edit/Run header controls render. Do not override storageState.

interface OntogenConfSnapshot {
  is_enabled: boolean;
  schedule_tier: string | null;
  dataset_filter: Record<string, unknown>;
  default_run_prompt: string | null;
}

let snapshot: OntogenConfSnapshot | null = null;

test.beforeAll(async ({ adminApi }) => {
  const resp = await adminApi.get(CONF_PATH);
  expect(resp.ok(), `GET ${CONF_PATH} failed: ${await resp.text()}`).toBeTruthy();
  const conf = (await resp.json()) as OntogenConfSnapshot;
  snapshot = {
    is_enabled: conf.is_enabled,
    schedule_tier: conf.schedule_tier ?? null,
    dataset_filter: conf.dataset_filter ?? {},
    default_run_prompt: conf.default_run_prompt ?? null,
  };
});

test.afterAll(async ({ adminApi }) => {
  if (!snapshot) return;
  const restoreResp = await adminApi.put(CONF_PATH, { data: snapshot });
  expect(
    restoreResp.ok(),
    `restoring the ontogen conf failed: ${await restoreResp.text()}`,
  ).toBeTruthy();
  // Asserted restore: a silent failure here would corrupt every later spec.
  const readBack = await adminApi.get(CONF_PATH);
  expect(readBack.ok()).toBeTruthy();
  const restored = (await readBack.json()) as OntogenConfSnapshot;
  expect(restored.is_enabled).toBe(snapshot.is_enabled);
  expect(restored.schedule_tier ?? null).toBe(snapshot.schedule_tier);
  expect(restored.default_run_prompt ?? null).toBe(snapshot.default_run_prompt);
  snapshot = null;
});

// ─────────────────────────────────────────────────────────────────────────────
// Clicking Edit enters edit mode without firing PUT /spoke/ontogen/attr/conf.
// ─────────────────────────────────────────────────────────────────────────────
test("/ontogen/conf — clicking Edit enters edit mode and does NOT write the conf", async ({
  page,
  adminApi,
}) => {
  // Capture every conf write (PUT or PATCH) for the lifetime of the page.
  // The defect manifests as such a request firing on the Edit click.
  const confWrites: string[] = [];
  page.on("request", (req) => {
    if (WRITE_METHODS.has(req.method()) && req.url().includes(CONF_PATH)) {
      confWrites.push(`${req.method()} ${req.url()}`);
    }
  });

  // Navigate to the conf page.
  // spec: FRONTEND_ONTOGEN.md §Navigation — /ontogen/conf → conf editor
  await page.goto("/ontogen/conf");
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: page heading rendered (navigational landmark) --
  // spec: conf/page.tsx — h1 "OntoGen — Configuration"
  await expect(
    page.getByRole("heading", { name: "OntoGen — Configuration", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- Precondition: view mode — Edit visible, Save absent --
  // spec: conf/page.tsx — header renders Edit + Run when not editing.
  const editButton = page.getByRole("button", { name: /^edit$/i });
  await expect(editButton).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("button", { name: /^save$/i })).toHaveCount(0);

  // -- UI assertion (a): read-mode renders a plain-text VIEW, not a disabled form.
  //    OntogenConfView shows is_enabled as "enabled"/"disabled" text; confirm the
  //    rendered value against the conf read back over REST (dual confirmation). --
  // spec: conf-view.tsx — <FieldValue label="is_enabled">{... ? "enabled" : "disabled"}
  const confResp = await adminApi.get(CONF_PATH);
  expect(confResp.ok(), `GET ${CONF_PATH} failed: ${await confResp.text()}`).toBeTruthy();
  const conf = (await confResp.json()) as { is_enabled: boolean };
  await expect(
    page.getByText(conf.is_enabled ? "enabled" : "disabled", { exact: true }),
  ).toBeVisible({ timeout: 10_000 });

  // The form's is_enabled checkbox is absent from the DOM in view mode (form only
  // rendered when editing).
  // spec: conf/page.tsx — OntogenConfForm rendered only when editing.
  const isEnabledCheckbox = page.locator("#conf-is-enabled");
  await expect(isEnabledCheckbox).toHaveCount(0);

  // -- UI gesture: click Edit --
  await editButton.click();

  // -- UI assertion (c): edit mode entered — Save + Cancel visible, Edit gone --
  // spec: conf/page.tsx — editing ? (Save, Cancel) : (Edit, Run)
  await expect(page.getByRole("button", { name: /^save$/i })).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole("button", { name: /^cancel$/i })).toBeVisible();
  await expect(page.getByRole("button", { name: /^edit$/i })).toHaveCount(0);

  // -- UI assertion (c cont.): the editable form replaces the view — the is_enabled
  //    checkbox now exists and is enabled. --
  // spec: conf-form.tsx — Checkbox id="conf-is-enabled", enabled when editing.
  await expect(isEnabledCheckbox).toBeVisible({ timeout: 10_000 });
  await expect(isEnabledCheckbox).toBeEnabled();

  // -- UI assertion: no "Configuration saved" toast fired (the defect's tell) --
  // spec: conf/page.tsx — toast({ title: "Configuration saved" }) only on Save success
  await expect(page.getByText("Configuration saved", { exact: true })).toHaveCount(0);

  // -- Core assertion (b): NO conf write was made on Edit --
  // Give any stray default-action submit a moment to surface before asserting.
  await page.waitForTimeout(500);
  expect(
    confWrites,
    `clicking Edit must not write the conf; observed: ${JSON.stringify(confWrites)}`
  ).toHaveLength(0);
});

// ─────────────────────────────────────────────────────────────────────────────
// Positive leg: the header Save DOES write the conf — exactly once — so the
// negative assertion above is known to be watching a predicate that can fire.
// ─────────────────────────────────────────────────────────────────────────────
test("/ontogen/conf — header Save writes the edited conf exactly once", async ({
  page,
  adminApi,
}) => {
  const confWrites: string[] = [];
  page.on("request", (req) => {
    if (WRITE_METHODS.has(req.method()) && req.url().includes(CONF_PATH)) {
      confWrites.push(`${req.method()} ${req.url()}`);
    }
  });

  await page.goto("/ontogen/conf");
  await expect(page).not.toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: "OntoGen — Configuration", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // Enter edit mode.
  await page.getByRole("button", { name: /^edit$/i }).click();

  // -- UI gesture: edit default_run_prompt (a free-text field; the singleton's other
  //    fields stay as loaded and are restored wholesale in afterAll) --
  // spec: FRONTEND_ONTOGEN.md §Page contracts (/ontogen/conf) — the conf fields are
  //   is_enabled / schedule_tier / dataset_filter / default_run_prompt, and Edit swaps
  //   the read-only view for the editable form over them.
  //   conf-form.tsx — Textarea id="conf-prompt".
  const prompt = page.locator("#conf-prompt");
  await expect(prompt).toBeEnabled({ timeout: 10_000 });
  const NEW_PROMPT = `ground conf-edit probe ${Date.now().toString(36)}`;
  await prompt.fill(NEW_PROMPT);

  // -- UI gesture: click the header Save (external submit bound to the conf form) --
  // spec: conf/page.tsx — <Button type="submit" form="ontogen-conf-form">Save</Button>
  const saveButton = page.getByRole("button", { name: /^save$/i });
  await expect(saveButton).toHaveAttribute("type", "submit");
  await expect(saveButton).toHaveAttribute("form", "ontogen-conf-form");
  await saveButton.click();

  // -- UI assertion: the success toast fires --
  // spec: conf/page.tsx — toast({ title: "Configuration saved" }) on Save success
  await expect(page.getByText("Configuration saved", { exact: true })).toBeVisible({
    timeout: 10_000,
  });

  // -- Core assertion: exactly one conf write, not zero and not a double submit --
  await page.waitForTimeout(500);
  expect(
    confWrites,
    `Save must write the conf exactly once; observed: ${JSON.stringify(confWrites)}`
  ).toHaveLength(1);

  // -- UI assertion: the header returns to read mode --
  await expect(page.getByRole("button", { name: /^edit$/i })).toBeVisible({ timeout: 10_000 });

  // -- Dual confirmation: REST read-back of the persisted prompt --
  // spec: API.md §Ontology Generation — GET /spoke/ontogen/attr/conf returns default_run_prompt.
  const readResp = await adminApi.get(CONF_PATH);
  expect(readResp.ok(), `GET ${CONF_PATH} failed: ${await readResp.text()}`).toBeTruthy();
  const persisted = (await readResp.json()) as { default_run_prompt: string | null };
  expect(persisted.default_run_prompt).toBe(NEW_PROMPT);
});
