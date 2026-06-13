/**
 * Ground spec: /settings page — narrow UI-flow tests.
 *
 * Concern: the settings page renders, and the theme toggle and locale selector
 * are purely client-side (localStorage-only, no API). This spec proves that:
 *   1. The page renders with the correct heading and the theme + language sections.
 *   2. Clicking a theme button ("Dark") causes the HTML element to carry the dark
 *      class (next-themes applies it to <html>), i.e. the toggle mutates the DOM.
 *   3. Changing the locale via the Radix Select persists the choice to localStorage.
 *
 * Minimal: this spec does NOT duplicate presentational logic from the Vitest unit
 * tests; it only proves the real page responds to user interaction against the live
 * stack. No API probes because there is no API surface for these settings.
 *
 * spec: spec/feature/FRONTEND_BASIC.md §Routing — /settings: theme + locale toggle,
 *   persisted in localStorage only (no API).
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — ground group proves real-stack
 *   UI flows; presentational assertions stay in Vitest.
 */

import { test, expect } from "../../fixtures/index";

// ─────────────────────────────────────────────────────────────────────────────
// Test 1 — /settings page renders with Theme and Language sections
// spec: FRONTEND_BASIC.md §Routing — /settings: h1 "Settings"; Theme section;
//   Language (locale) section.
// ─────────────────────────────────────────────────────────────────────────────

test("/settings — page renders with Theme and Language sections", async ({ page }) => {
  await page.goto("/settings");
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: page heading --
  // spec: settings/page.tsx — h1 "Settings"
  await expect(
    page.getByRole("heading", { name: "Settings", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: Theme section heading --
  // spec: settings/page.tsx — h2 "Theme"
  await expect(
    page.getByRole("heading", { name: "Theme", exact: true })
  ).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: theme buttons visible (Light / Dark / System) --
  // spec: settings/page.tsx — three Buttons: "Light", "Dark", "System"
  await expect(page.getByRole("button", { name: "Light", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Dark", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "System", exact: true })).toBeVisible();

  // -- UI assertion: Language section heading --
  // spec: settings/page.tsx — h2 "Language"
  await expect(
    page.getByRole("heading", { name: "Language", exact: true })
  ).toBeVisible({ timeout: 10_000 });

  // -- UI assertion: locale Radix Select (combobox) visible --
  // spec: settings/page.tsx — Select value={locale} → SelectTrigger (role="combobox")
  await expect(page.getByRole("combobox").first()).toBeVisible();
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 2 — Theme toggle: clicking "Dark" applies the dark class to <html>
// spec: settings/page.tsx — Button onClick={() => setTheme(t.value)}; next-themes
//   applies the class to <html>. Proves real-page DOM mutation against the live stack.
// Note: We restore by clicking "Light" at the end so subsequent tests see a neutral theme.
// ─────────────────────────────────────────────────────────────────────────────

test("/settings — theme toggle changes html class (Dark → dark applied; restored)", async ({
  page,
}) => {
  await page.goto("/settings");
  await expect(page).not.toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: "Settings", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI gesture: click "Dark" --
  // spec: settings/page.tsx — Button "Dark" → setTheme("dark")
  await page.getByRole("button", { name: "Dark", exact: true }).click();

  // -- UI assertion: <html> element now carries class "dark" --
  // next-themes sets the theme class on the <html> element.
  // spec: settings/page.tsx — next-themes manages html class/data-theme.
  // We assert on the html element's class attribute rather than a data-testid.
  // The class may be "dark" alone or "dark <other-classes>".
  await expect(page.locator("html")).toHaveClass(/\bdark\b/, { timeout: 5_000 });

  // -- UI assertion: "Dark" button carries the "default" variant (visually selected) --
  // spec: settings/page.tsx — Button variant={theme === t.value ? "default" : "outline"}
  // "default" variant renders with a contrasting background; "outline" does not.
  // We cannot assert CSS directly, but we can verify the button's aria state or
  // data-* attribute if present. In the absence of a semantic selection indicator,
  // we skip this sub-assertion (presentational, covered by Vitest).
  // RISK FLAG: no semantic selected indicator on the theme buttons; this is a Vitest concern.

  // Restore to "Light" so other tests start with a clean theme state.
  await page.getByRole("button", { name: "Light", exact: true }).click();
  await expect(page.locator("html")).not.toHaveClass(/\bdark\b/, { timeout: 5_000 });
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 3 — Locale toggle persists selection to localStorage
// spec: settings/page.tsx — const LOCALE_KEY = "dataspoke:locale";
//   setLocale(l) → localStorage.setItem(LOCALE_KEY, l).
// Proves the real page persists the locale; does not test translation wiring
// (spec notes "translations are not yet wired up").
// ─────────────────────────────────────────────────────────────────────────────

test("/settings — locale selector persists to localStorage", async ({ page }) => {
  await page.goto("/settings");
  await expect(page).not.toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: "Settings", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // Read the current locale from localStorage (may be "en", "ko", or null).
  // spec: settings/page.tsx — const LOCALE_KEY = "dataspoke:locale"
  const LOCALE_KEY = "dataspoke:locale";
  const initialLocale = (await page.evaluate(
    (key: string) => localStorage.getItem(key),
    LOCALE_KEY
  )) as string | null;

  // Choose a locale that is different from the current one (default to "ko" if
  // current is "en" or null, else "en").
  const targetLocale = initialLocale === "ko" ? "en" : "ko";
  const targetOptionName = targetLocale === "ko" ? "Korean" : "English";

  // -- UI gesture: open the locale Radix Select and pick the target locale --
  // spec: settings/page.tsx — Select value={locale} onValueChange={setLocale}
  //   SelectTrigger (role="combobox"); SelectItem value="en" "English"; value="ko" "Korean"
  const localeTrigger = page.getByRole("combobox").first();
  await expect(localeTrigger).toBeVisible({ timeout: 10_000 });
  await localeTrigger.click();
  await page.getByRole("option", { name: targetOptionName, exact: true }).click();

  // -- UI assertion: localStorage now holds the target locale --
  // spec: settings/page.tsx — localStorage.setItem(LOCALE_KEY, l)
  const storedLocale = await page.evaluate(
    (key: string) => localStorage.getItem(key),
    LOCALE_KEY
  );
  expect(storedLocale).toBe(targetLocale);

  // -- UI assertion: the combobox trigger shows the selected locale label --
  // next-themes / the Select component reflects the new value in the trigger.
  // SelectValue renders the selected item text inside the trigger.
  await expect(localeTrigger).toHaveText(targetOptionName, { timeout: 5_000 });

  // Restore to the original locale (or "en" if none was set).
  const restoreLocale = initialLocale ?? "en";
  const restoreOptionName = restoreLocale === "ko" ? "Korean" : "English";
  await localeTrigger.click();
  await page.getByRole("option", { name: restoreOptionName, exact: true }).click();
  const restoredLocale = await page.evaluate(
    (key: string) => localStorage.getItem(key),
    LOCALE_KEY
  );
  expect(restoredLocale).toBe(restoreLocale);
});
