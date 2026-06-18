/**
 * Ground spec: /metagen/conf/[id] — clicking "Edit" enters edit mode WITHOUT
 * submitting/saving the conf form. Real-browser regression guard for the
 * Edit/Save-morph defect (memory project_frontend_button_submit_morph).
 *
 * The header renders `editing ? Cancel : Edit` in one conditional slot. If React
 * reuses the same DOM node across the ternary, the Edit click can perform the
 * browser's default submit on the now-morphed control, firing a stray
 * PUT /spoke/metagen/conf/{id} and "Conf saved" toast instead of entering edit
 * mode. Distinct `key` props (conf-edit / conf-cancel in conf/[id]/page.tsx)
 * give each branch its own node and prevent the morph. jsdom cannot model the
 * default-action phase, so this real-Chromium spec is the genuine guard.
 *
 * Independent: seeds one conf via REST, runs the gesture, deletes it in afterAll.
 *
 * Assertions:
 *   (a) clicking Edit fires NO PUT /spoke/metagen/conf/{id} request;
 *   (b) edit mode is entered — Cancel + a "Save conf" submit become available
 *       and the form fields are enabled (is_enabled checkbox no longer disabled);
 *   (c) no "Conf saved" toast fires (the defect's tell).
 *
 * spec: spec/feature/FRONTEND_METAGEN.md §Conf create / detail — the detail page
 *   edits fields via PUT, fired only by Save, never by Edit
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

  // -- Precondition: view mode — Edit visible, no Save, fields disabled --
  // conf/[id]/page.tsx: header renders Edit (not Cancel) when not editing;
  // conf-form.tsx renders the submit button only when !disabled.
  const editButton = page.getByRole("button", { name: "Edit", exact: true });
  await expect(editButton).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("button", { name: "Save conf", exact: true })).toHaveCount(0);

  const isEnabledCheckbox = page.locator("#metagen-conf-is-enabled");
  await expect(isEnabledCheckbox).toBeVisible({ timeout: 10_000 });
  await expect(isEnabledCheckbox).toBeDisabled();

  // -- Gesture: click Edit --
  await editButton.click();

  // -- (b) edit mode entered — Cancel + Save conf appear, Edit gone, fields enabled --
  await expect(page.getByRole("button", { name: "Cancel", exact: true })).toBeVisible({
    timeout: 10_000,
  });
  await expect(page.getByRole("button", { name: "Save conf", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Edit", exact: true })).toHaveCount(0);
  await expect(isEnabledCheckbox).toBeEnabled();

  // -- (c) no "Conf saved" toast fired (the defect's tell) --
  // conf/[id]/page.tsx: toast({ title: "Conf saved" }) only on Save success
  await expect(page.getByText("Conf saved", { exact: true })).toHaveCount(0);

  // -- (a) core assertion: NO PUT fired on Edit --
  // Give any stray default-action submit a moment to surface before asserting.
  await page.waitForTimeout(500);
  expect(
    confPuts,
    `clicking Edit must not PUT the conf; observed: ${JSON.stringify(confPuts)}`,
  ).toHaveLength(0);
});
