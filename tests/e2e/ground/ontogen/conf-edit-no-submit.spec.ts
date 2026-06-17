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
 *   (a) clicking Edit fires NO PUT /spoke/ontogen/attr/conf request;
 *   (b) edit mode is entered — Save + Cancel become visible and the form fields
 *       are enabled (is_enabled checkbox no longer disabled).
 *
 * spec: spec/feature/FRONTEND_ONTOGEN.md §Navigation — /ontogen/conf hosts the
 *   Run + Edit conf editor (Editor/Admin only); the conf is a singleton (no Delete).
 * spec: spec/feature/FRONTEND_ONTOGEN.md §Page contracts — /ontogen/conf:
 *   PUT /spoke/ontogen/attr/conf is fired only by Save, never by Edit.
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group, real-session role.
 */

import { test, expect } from "../../fixtures/index";

// The route Save (and only Save) PUTs to. Matched as a substring of the request URL.
const CONF_PUT_PATH = "/api/v1/spoke/ontogen/attr/conf";

// Admin-only — filename convention (`*.spec.ts` → admin project). The admin role is a
// writer, so the top-right Edit/Run header controls render. Do not override storageState.

// ─────────────────────────────────────────────────────────────────────────────
// Clicking Edit enters edit mode without firing PUT /spoke/ontogen/attr/conf.
// ─────────────────────────────────────────────────────────────────────────────
test("/ontogen/conf — clicking Edit enters edit mode and does NOT PUT the conf", async ({
  page,
}) => {
  // Capture every PUT to the conf endpoint for the lifetime of the page.
  // The defect manifests as such a request firing on the Edit click.
  const confPuts: string[] = [];
  page.on("request", (req) => {
    if (req.method() === "PUT" && req.url().includes(CONF_PUT_PATH)) {
      confPuts.push(req.url());
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

  // -- Precondition: view mode — Edit visible, Save absent, fields disabled --
  // spec: conf/page.tsx — header renders Edit + Run when not editing; conf-form disabled
  const editButton = page.getByRole("button", { name: /^edit$/i });
  await expect(editButton).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("button", { name: /^save$/i })).toHaveCount(0);

  // The is_enabled checkbox (Radix renders a <button role="checkbox">) is the
  // disabled/enabled probe — disabled in view mode.
  // spec: conf-form.tsx — Checkbox id="conf-is-enabled" disabled={!editing}
  const isEnabledCheckbox = page.locator("#conf-is-enabled");
  await expect(isEnabledCheckbox).toBeVisible({ timeout: 10_000 });
  await expect(isEnabledCheckbox).toBeDisabled();

  // -- UI gesture: click Edit --
  await editButton.click();

  // -- UI assertion (b): edit mode entered — Save + Cancel visible, Edit gone --
  // spec: conf/page.tsx — editing ? (Save, Cancel) : (Edit, Run)
  await expect(page.getByRole("button", { name: /^save$/i })).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole("button", { name: /^cancel$/i })).toBeVisible();
  await expect(page.getByRole("button", { name: /^edit$/i })).toHaveCount(0);

  // -- UI assertion (b cont.): form fields are now enabled --
  // spec: conf-form.tsx — fields enabled when disabled={!editing} is false
  await expect(isEnabledCheckbox).toBeEnabled();

  // -- UI assertion: no "Configuration saved" toast fired (the defect's tell) --
  // spec: conf/page.tsx — toast({ title: "Configuration saved" }) only on Save success
  await expect(page.getByText("Configuration saved", { exact: true })).toHaveCount(0);

  // -- Core assertion (a): NO PUT /spoke/ontogen/attr/conf was made on Edit --
  // Give any stray default-action submit a moment to surface before asserting.
  await page.waitForTimeout(500);
  expect(
    confPuts,
    `clicking Edit must not PUT the conf; observed: ${JSON.stringify(confPuts)}`
  ).toHaveLength(0);
});
