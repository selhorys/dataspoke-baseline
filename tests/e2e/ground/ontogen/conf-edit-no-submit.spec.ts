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
 *   (b) clicking Edit fires NO PUT /spoke/ontogen/attr/conf request;
 *   (c) edit mode is entered — Save + Cancel become visible and the editable form
 *       replaces the view (is_enabled checkbox appears, enabled).
 *
 * spec: spec/feature/FRONTEND_ONTOGEN.md §Navigation — /ontogen/conf hosts the
 *   Run + Edit conf editor (Editor/Admin only); the conf is a singleton (no Delete).
 * spec: spec/feature/FRONTEND_ONTOGEN.md §/ontogen/conf — when not editing it shows
 *   a read-only view of the conf fields as plain text; Edit swaps the view for the
 *   form; PUT /spoke/ontogen/attr/conf is fired only by Save, never by Edit.
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
  adminApi,
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

  // -- Precondition: view mode — Edit visible, Save absent --
  // spec: conf/page.tsx — header renders Edit + Run when not editing.
  const editButton = page.getByRole("button", { name: /^edit$/i });
  await expect(editButton).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("button", { name: /^save$/i })).toHaveCount(0);

  // -- UI assertion (a): read-mode renders a plain-text VIEW, not a disabled form.
  //    OntogenConfView shows is_enabled as "enabled"/"disabled" text; confirm the
  //    rendered value against the conf read back over REST (dual confirmation). --
  // spec: conf-view.tsx — <FieldValue label="is_enabled">{... ? "enabled" : "disabled"}
  const confResp = await adminApi.get("/api/v1/spoke/ontogen/attr/conf");
  expect(confResp.ok(), `GET /spoke/ontogen/attr/conf failed: ${await confResp.text()}`).toBeTruthy();
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

  // -- Core assertion (b): NO PUT /spoke/ontogen/attr/conf was made on Edit --
  // Give any stray default-action submit a moment to surface before asserting.
  await page.waitForTimeout(500);
  expect(
    confPuts,
    `clicking Edit must not PUT the conf; observed: ${JSON.stringify(confPuts)}`
  ).toHaveLength(0);
});
