/**
 * Ground spec: /metagen/conf/[id] — clicking "Edit" enters edit mode WITHOUT
 * submitting/saving the conf form, and Save (in the page header) PUTs the conf.
 * Real-browser regression guard for the Edit/Save-morph defect
 * (memory project_frontend_button_submit_morph).
 *
 * The Save/Create submit lives in the top-right page header (external submit via
 * <Button type="submit" form="metagen-conf-form">), mirroring OntoGen. The header
 * renders `editing ? (Save, Cancel) : (Edit, Run, Delete)` in one conditional slot.
 * If React reuses the same DOM node across the ternary, the Edit click can perform
 * the browser's default submit on the now-morphed control, firing a stray
 * PUT /spoke/metagen/conf/{id} and "Conf saved" toast instead of entering edit
 * mode. Distinct `key` props (conf-edit / conf-save / conf-cancel in
 * conf/[id]/page.tsx) give each branch its own node and prevent the morph. jsdom
 * cannot model the default-action phase, so this real-Chromium spec is the guard.
 *
 * Independent: seeds one conf via REST, runs the gestures, deletes it in afterAll.
 *
 * Assertions:
 *   (a) read-mode renders the conf as a plain-text VIEW (MetagenConfView), not a
 *       disabled form — no form control is in the DOM, the view value renders;
 *   (b) clicking Edit fires NO PUT /spoke/metagen/conf/{id} request;
 *   (c) edit mode is entered — Save + Cancel become visible, Run/Delete hidden,
 *       and the editable form replaces the view (is_enabled checkbox appears, enabled);
 *   (d) no "Conf saved" toast fires on Edit (the defect's tell);
 *   (e) positive leg — editing result_limit and clicking the header Save fires
 *       exactly one PUT, shows a "Conf saved" toast, and the change reads back
 *       over REST.
 *
 * spec: spec/feature/FRONTEND_METAGEN.md §Conf detail — the detail page opens as a
 *   read-only view (conf fields as plain text); Edit swaps it for the form; PUT is
 *   fired only by Save (header external submit), never by Edit
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group, real-session role
 */

import { test, expect } from "../../fixtures/index";

const CONF_API = "/api/v1/spoke/metagen/conf";
const CONF_NAME = `ground-edit-${Date.now().toString(36)}`;

let confId: string | null = null;

test.beforeAll(async ({ adminApi }) => {
  const resp = await adminApi.post(CONF_API, {
    data: {
      name: CONF_NAME,
      is_enabled: false,
      schedule_tier: null,
      dataset_filter: {},
      result_limit: 3,
      overwrite_pending: true,
    },
  });
  expect([200, 201]).toContain(resp.status());
  confId = ((await resp.json()) as { id: string }).id;
});

test.afterAll(async ({ adminApi }) => {
  if (confId) await adminApi.delete(`${CONF_API}/${confId}`).catch(() => null);
});

test("/metagen/conf/[id] — clicking Edit enters edit mode and does NOT PUT the conf", async ({
  page,
}) => {
  // Capture every PUT to this conf for the lifetime of the page; the defect
  // manifests as such a request firing on the Edit click.
  const confPuts: string[] = [];
  const putPath = `${CONF_API}/${confId}`;
  page.on("request", (req) => {
    if (req.method() === "PUT" && req.url().includes(putPath)) {
      confPuts.push(req.url());
    }
  });

  await page.goto(`/metagen/conf/${confId}`);
  await expect(page).not.toHaveURL(/\/login/);

  // -- Heading rendered (conf name) --
  // conf/[id]/page.tsx: <h1>{conf.name}</h1>
  await expect(page.getByRole("heading", { name: CONF_NAME, exact: true })).toBeVisible({
    timeout: 15_000,
  });

  // -- Precondition: read-mode header = Edit + Run + Delete; no Save --
  // conf/[id]/page.tsx: header renders Edit/Run/Delete when not editing.
  const editButton = page.getByRole("button", { name: /^edit$/i });
  await expect(editButton).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("button", { name: /^run$/i })).toBeVisible();
  await expect(page.getByRole("button", { name: /^delete$/i })).toBeVisible();
  await expect(page.getByRole("button", { name: /^save$/i })).toHaveCount(0);

  // -- (a) read-mode renders a plain-text VIEW, not a disabled form. The seeded
  //    conf has overwrite_pending=true → "yes", which only the view renders (the
  //    form represents it as a checkbox + descriptive span). --
  // conf-view.tsx: <FieldValue label="overwrite_pending">{... ? "yes" : "no"}
  await expect(page.getByText("yes", { exact: true })).toBeVisible({ timeout: 10_000 });

  // The form's is_enabled checkbox is absent from the DOM in view mode (no form rendered).
  // conf/[id]/page.tsx: MetagenConfForm rendered only when editing.
  const isEnabledCheckbox = page.locator("#metagen-conf-is-enabled");
  await expect(isEnabledCheckbox).toHaveCount(0);

  // -- Gesture: click Edit --
  await editButton.click();

  // -- (c) edit mode entered — Save + Cancel appear, Edit/Run/Delete gone; the
  //    editable form replaces the view (is_enabled checkbox now present + enabled) --
  const saveButton = page.getByRole("button", { name: /^save$/i });
  await expect(saveButton).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole("button", { name: /^cancel$/i })).toBeVisible();
  await expect(page.getByRole("button", { name: /^edit$/i })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /^run$/i })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /^delete$/i })).toHaveCount(0);
  await expect(isEnabledCheckbox).toBeVisible({ timeout: 10_000 });
  await expect(isEnabledCheckbox).toBeEnabled();

  // -- Save is a header external-submit bound to the conf form --
  // conf/[id]/page.tsx: <Button type="submit" form="metagen-conf-form">Save</Button>
  await expect(saveButton).toHaveAttribute("type", "submit");
  await expect(saveButton).toHaveAttribute("form", "metagen-conf-form");

  // -- (d) no "Conf saved" toast fired (the defect's tell) --
  // conf/[id]/page.tsx: toast({ title: "Conf saved" }) only on Save success
  await expect(page.getByText("Conf saved", { exact: true })).toHaveCount(0);

  // -- (b) core assertion: NO PUT fired on Edit --
  // Give any stray default-action submit a moment to surface before asserting.
  await page.waitForTimeout(500);
  expect(
    confPuts,
    `clicking Edit must not PUT the conf; observed: ${JSON.stringify(confPuts)}`,
  ).toHaveLength(0);
});

test("/metagen/conf/[id] — header Save PUTs the edited conf exactly once", async ({
  page,
  adminApi,
}) => {
  // Positive leg: editing a field and clicking the header Save fires exactly one
  // PUT, shows the "Conf saved" toast, and the change reads back over REST.
  const confPuts: string[] = [];
  const putPath = `${CONF_API}/${confId}`;
  page.on("request", (req) => {
    if (req.method() === "PUT" && req.url().includes(putPath)) {
      confPuts.push(req.url());
    }
  });

  await page.goto(`/metagen/conf/${confId}`);
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page.getByRole("heading", { name: CONF_NAME, exact: true })).toBeVisible({
    timeout: 15_000,
  });

  // Enter edit mode.
  await page.getByRole("button", { name: /^edit$/i }).click();
  const resultLimit = page.locator("#metagen-conf-result-limit");
  await expect(resultLimit).toBeEnabled({ timeout: 10_000 });

  // Edit result_limit 3 → 5.
  const NEW_LIMIT = 5;
  await resultLimit.fill(String(NEW_LIMIT));

  // Click the header Save (external submit).
  await page.getByRole("button", { name: /^save$/i }).click();

  // "Conf saved" toast appears.
  await expect(page.getByText("Conf saved", { exact: true })).toBeVisible({ timeout: 10_000 });

  // Exactly one PUT fired.
  await page.waitForTimeout(500);
  expect(
    confPuts,
    `Save must PUT the conf exactly once; observed: ${JSON.stringify(confPuts)}`,
  ).toHaveLength(1);

  // Header returns to read mode (Edit/Run/Delete restored).
  await expect(page.getByRole("button", { name: /^edit$/i })).toBeVisible({ timeout: 10_000 });

  // -- Dual confirmation: REST read-back of the persisted result_limit --
  const readResp = await adminApi.get(putPath);
  expect(readResp.ok(), `GET ${putPath} failed: ${await readResp.text()}`).toBeTruthy();
  const persisted = (await readResp.json()) as { result_limit: number };
  expect(persisted.result_limit).toBe(NEW_LIMIT);
});
