/**
 * Ground spec: /ingestion/sources/[id] — clicking "Edit" enters recipe edit mode
 * WITHOUT submitting/saving, and the header "Save" PUTs the source.
 * Real-browser regression guard for the Edit/Save-morph defect
 * (memory project_frontend_button_submit_morph).
 *
 * The recipe section header renders `isEditing ? (Save, Cancel) : (Edit, Delete)`
 * in one conditional slot, and Save is an external submit
 * (<Button type="submit" form="ingestion-recipe-form">) targeting the
 * RecipeYamlEditor's <form>. If React reuses the same DOM node across the
 * ternary, the Edit click can perform the browser's default submit on the
 * now-morphed control, firing a stray PUT /spoke/ingestion/sources/{id} and a
 * "Source updated" toast instead of entering edit mode. Distinct `key` props
 * (recipe-edit / recipe-delete vs recipe-save / recipe-cancel in
 * sources/[id]/page.tsx) give each branch its own node and prevent the morph.
 * jsdom cannot model the default-action phase, so this real-Chromium spec is the guard.
 *
 * Independent: seeds one ACTIVE_CUSTOM_MANAGED source via REST (a dummy recipe —
 * create validates shape, not connectivity), runs the gestures, deletes it in afterAll.
 *
 * Assertions:
 *   (a) read-mode renders the recipe as a highlighted <pre> VIEW, not a textarea;
 *   (b) clicking Edit fires NO PUT /spoke/ingestion/sources/{id};
 *   (c) edit mode is entered — Save + Cancel become visible, Edit/Delete hidden,
 *       and the recipe textarea appears; Save is the header external-submit;
 *   (d) no "Source updated" toast fires on Edit (the defect's tell);
 *   (e) positive leg — editing the recipe and clicking the header Save fires
 *       exactly one PUT, shows a "Source updated" toast, and the change reads
 *       back over REST.
 *
 * spec: spec/feature/FRONTEND_INGESTION.md §Source Detail §Recipe — edit-mode
 *   surfaces Save/Cancel in the section header; PUT is fired only by Save (header
 *   external submit), never by Edit
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group, real-session role
 */

import { test, expect } from "../../fixtures/index";

const SOURCES_API = "/api/v1/spoke/ingestion/sources";
const SOURCE_NAME = `ground-edit-${Date.now().toString(36)}`;

let sourceId: string | null = null;

test.beforeAll(async ({ adminApi }) => {
  const resp = await adminApi.post(SOURCES_API, {
    data: {
      mode: "ACTIVE_CUSTOM_MANAGED",
      name: SOURCE_NAME,
      schedule: "0 0 * * *",
      recipe: {
        source: { type: "postgres", config: { host_port: "pg.example:5432" } },
      },
    },
  });
  expect([200, 201]).toContain(resp.status());
  sourceId = ((await resp.json()) as { id: string }).id;
});

test.afterAll(async ({ adminApi }) => {
  if (sourceId) await adminApi.delete(`${SOURCES_API}/${sourceId}`).catch(() => null);
});

test("/ingestion/sources/[id] — clicking Edit enters edit mode and does NOT PUT the source", async ({
  page,
}) => {
  // Capture every PUT to this source for the lifetime of the page; the defect
  // manifests as such a request firing on the Edit click.
  const sourcePuts: string[] = [];
  const putPath = `${SOURCES_API}/${sourceId}`;
  page.on("request", (req) => {
    if (req.method() === "PUT" && req.url().includes(putPath)) {
      sourcePuts.push(req.url());
    }
  });

  await page.goto(`/ingestion/sources/${sourceId}`);
  await expect(page).not.toHaveURL(/\/login/);

  // -- Heading rendered (source name) --
  await expect(page.getByRole("heading", { name: SOURCE_NAME, exact: true })).toBeVisible({
    timeout: 15_000,
  });

  // -- Precondition: read-mode header = Edit + Delete; no Save --
  const editButton = page.getByRole("button", { name: /^edit$/i });
  await expect(editButton).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("button", { name: /^delete$/i })).toBeVisible();
  await expect(page.getByRole("button", { name: /^save$/i })).toHaveCount(0);

  // -- (a) read-mode renders the recipe as a highlighted <pre>, not a textarea --
  // recipe-yaml-editor.tsx: editable textarea carries aria-label="recipe YAML".
  const recipeEditor = page.getByLabel("recipe YAML");
  await expect(recipeEditor).toHaveCount(0);

  // -- Gesture: click Edit --
  await editButton.click();

  // -- (c) edit mode entered — Save + Cancel appear, Edit/Delete gone; textarea present --
  const saveButton = page.getByRole("button", { name: /^save$/i });
  await expect(saveButton).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole("button", { name: /^cancel$/i })).toBeVisible();
  await expect(page.getByRole("button", { name: /^edit$/i })).toHaveCount(0);
  await expect(page.getByRole("button", { name: /^delete$/i })).toHaveCount(0);
  await expect(recipeEditor).toBeVisible({ timeout: 10_000 });
  await expect(recipeEditor).toBeEnabled();

  // -- Save is a header external-submit bound to the recipe form --
  await expect(saveButton).toHaveAttribute("type", "submit");
  await expect(saveButton).toHaveAttribute("form", "ingestion-recipe-form");

  // -- (d) no "Source updated" toast fired (the defect's tell) --
  await expect(page.getByText("Source updated", { exact: true })).toHaveCount(0);

  // -- (b) core assertion: NO PUT fired on Edit --
  await page.waitForTimeout(500);
  expect(
    sourcePuts,
    `clicking Edit must not PUT the source; observed: ${JSON.stringify(sourcePuts)}`,
  ).toHaveLength(0);
});

test("/ingestion/sources/[id] — header Save PUTs the edited recipe exactly once", async ({
  page,
  adminApi,
}) => {
  const sourcePuts: string[] = [];
  const putPath = `${SOURCES_API}/${sourceId}`;
  page.on("request", (req) => {
    if (req.method() === "PUT" && req.url().includes(putPath)) {
      sourcePuts.push(req.url());
    }
  });

  await page.goto(`/ingestion/sources/${sourceId}`);
  await expect(page).not.toHaveURL(/\/login/);
  await expect(page.getByRole("heading", { name: SOURCE_NAME, exact: true })).toBeVisible({
    timeout: 15_000,
  });

  // Enter edit mode.
  await page.getByRole("button", { name: /^edit$/i }).click();
  const recipeEditor = page.getByLabel("recipe YAML");
  await expect(recipeEditor).toBeEnabled({ timeout: 10_000 });

  // Edit the recipe: bump the host port 5432 → 5433 (a safe, single-occurrence
  // text replacement against the editor's serialized YAML).
  const current = await recipeEditor.inputValue();
  expect(current).toContain("5432");
  await recipeEditor.fill(current.replace("5432", "5433"));

  // Click the header Save (external submit).
  await page.getByRole("button", { name: /^save$/i }).click();

  // "Source updated" toast appears.
  await expect(page.getByText("Source updated", { exact: true })).toBeVisible({ timeout: 10_000 });

  // Exactly one PUT fired.
  await page.waitForTimeout(500);
  expect(
    sourcePuts,
    `Save must PUT the source exactly once; observed: ${JSON.stringify(sourcePuts)}`,
  ).toHaveLength(1);

  // Header returns to read mode (Edit/Delete restored).
  await expect(page.getByRole("button", { name: /^edit$/i })).toBeVisible({ timeout: 10_000 });

  // -- Dual confirmation: REST read-back of the persisted recipe change --
  const readResp = await adminApi.get(putPath);
  expect(readResp.ok(), `GET ${putPath} failed: ${await readResp.text()}`).toBeTruthy();
  const persisted = (await readResp.json()) as {
    recipe: { source: { config: { host_port: string } } };
  };
  expect(persisted.recipe.source.config.host_port).toBe("pg.example:5433");
});
