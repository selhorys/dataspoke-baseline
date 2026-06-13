/**
 * Ground spec: /profile/tokens page — narrow UI-flow tests.
 *
 * Concern: the API tokens page renders; the admin can mint a new token (the
 * one-shot reveal dialog shows a `dsk_` token); the token appears in the list;
 * and it can be revoked via the ConfirmDialog, after which it is gone from the
 * list. Thorough afterAll cleanup deletes any leftover token via adminApi.
 *
 * spec: spec/feature/FRONTEND_BASIC.md §API tokens (/profile/tokens)
 * spec: spec/feature/FRONTEND_BASIC.md §Routing — /profile/tokens: GET /auth/api-tokens,
 *   POST /auth/api-tokens, DELETE /auth/api-tokens/{id}
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — dual confirmation, ConfirmDialog,
 *   selector guidance
 */

import { test, expect } from "../../fixtures/index";

// ── Constants ─────────────────────────────────────────────────────────────────

/** Name for the throwaway token minted in this test. */
const THROWAWAY_TOKEN_NAME = "e2e-ground-tokens-throwaway";

// ── Module state ──────────────────────────────────────────────────────────────

/** ID of the minted token; used in afterAll for cleanup. */
let mintedTokenId: string | null = null;

// ── Cleanup ───────────────────────────────────────────────────────────────────

test.afterAll(async ({ adminApi }) => {
  // If the token was not revoked during the test (e.g. failure mid-flow),
  // delete it via adminApi to leave the env clean.
  if (mintedTokenId) {
    await adminApi.delete(`/api/v1/auth/api-tokens/${mintedTokenId}`);
  }
  // Also scan the list for any leftover throwaway tokens by name.
  const listResp = await adminApi.get("/api/v1/auth/api-tokens");
  if (listResp.ok()) {
    const body = (await listResp.json()) as { tokens: Array<{ id: string; name: string }> };
    for (const t of body.tokens) {
      if (t.name === THROWAWAY_TOKEN_NAME) {
        await adminApi.delete(`/api/v1/auth/api-tokens/${t.id}`);
      }
    }
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 1 — /profile/tokens renders the token list and "New token" button
// spec: FRONTEND_BASIC.md §API tokens — GET /auth/api-tokens populates the table;
//   h1 "API Tokens"; Button "New token".
// ─────────────────────────────────────────────────────────────────────────────

test("/profile/tokens — page renders with heading and New token button", async ({ page }) => {
  await page.goto("/profile/tokens");
  await expect(page).not.toHaveURL(/\/login/);

  // -- UI assertion: page heading --
  // spec: profile/tokens/page.tsx — h1 "API Tokens"
  await expect(
    page.getByRole("heading", { name: "API Tokens", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: "New token" button visible --
  // spec: profile/tokens/page.tsx — Button size="sm" onClick={() => setMintOpen(true)} "New token"
  await expect(page.getByRole("button", { name: "New token" })).toBeVisible({ timeout: 10_000 });
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 2 — Mint a token (dialog shows dsk_ token), confirm in list, revoke via
//           ConfirmDialog, confirm gone.
// spec: FRONTEND_BASIC.md §API tokens — Dialog "New API token" → Create → one-shot
//   reveal dialog showing dsk_ token → Close → token in table → Revoke →
//   ConfirmDialog → token gone.
// ─────────────────────────────────────────────────────────────────────────────

test("/profile/tokens — mint token → dsk_ in reveal dialog → in list → revoke → gone", async ({
  page,
  adminApi,
}) => {
  // Pre-flight: delete any existing throwaway token by name so the list is clean.
  const preListResp = await adminApi.get("/api/v1/auth/api-tokens");
  if (preListResp.ok()) {
    const preBody = (await preListResp.json()) as { tokens: Array<{ id: string; name: string }> };
    for (const t of preBody.tokens) {
      if (t.name === THROWAWAY_TOKEN_NAME) {
        await adminApi.delete(`/api/v1/auth/api-tokens/${t.id}`);
      }
    }
  }

  await page.goto("/profile/tokens");
  await expect(page).not.toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: "API Tokens", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI gesture: click "New token" button --
  // spec: profile/tokens/page.tsx — Button "New token" → setMintOpen(true)
  await page.getByRole("button", { name: "New token" }).click();

  // -- UI assertion: "New API token" dialog opens --
  // spec: profile/tokens/page.tsx — Dialog DialogTitle "New API token"
  await expect(
    page.getByRole("heading", { name: "New API token", exact: true })
  ).toBeVisible({ timeout: 10_000 });

  // -- UI gesture: fill token name --
  // spec: profile/tokens/page.tsx — Input id="token-name" placeholder="e.g. ci-jenkins"
  await page.locator("#token-name").fill(THROWAWAY_TOKEN_NAME);

  // -- UI gesture: click "Create" --
  // spec: profile/tokens/page.tsx — Button type="submit" "Create"
  await page.getByRole("button", { name: "Create", exact: true }).click();

  // -- UI assertion: one-shot reveal dialog "Your new token" opens --
  // spec: FRONTEND_BASIC.md §API tokens — token reveal dialog shows "Your new token";
  //   the raw token starts with dsk_.
  await expect(
    page.getByRole("heading", { name: "Your new token", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: token text starts with dsk_ --
  // spec: FRONTEND_BASIC.md §API tokens — "dsk_AbCdEf1234ZyXw..."
  // The token is rendered in a <code> element inside the dialog.
  // We can't locate it by role, so locate the code element text — it must start with dsk_.
  const tokenCode = page.locator("code").first();
  await expect(tokenCode).toBeVisible({ timeout: 10_000 });
  const rawToken = (await tokenCode.textContent()) ?? "";
  expect(rawToken.startsWith("dsk_"), `Token must start with dsk_; got: ${rawToken.slice(0, 20)}`).toBe(true);

  // -- UI assertion: Copy token button visible --
  // spec: profile/tokens/page.tsx — Button aria-label="Copy token" in the reveal dialog
  await expect(page.getByRole("button", { name: "Copy token" })).toBeVisible();

  // -- UI gesture: close the reveal dialog --
  // spec: profile/tokens/page.tsx — Button "Close" (or "Done" if copied) → setMintedToken(null)
  // .first() — the dialog also renders an X close button whose accessible name is "Close".
  await page.getByRole("button", { name: /^(Close|Done)$/ }).first().click();

  // -- UI assertion: reveal dialog closed --
  await expect(
    page.getByRole("heading", { name: "Your new token", exact: true })
  ).not.toBeVisible({ timeout: 10_000 });

  // -- UI assertion: the new token name appears in the table --
  // spec: profile/tokens/page.tsx — TableCell className="font-medium" {t.name}
  // TanStack Query invalidates after mint closes; table re-renders.
  await expect(
    page.getByText(THROWAWAY_TOKEN_NAME, { exact: true })
  ).toBeVisible({ timeout: 20_000 });

  // -- Backend probe (dual confirmation): GET /auth/api-tokens → throwaway in list --
  // spec: FRONTEND_BASIC.md §API tokens — POST /auth/api-tokens creates a token row.
  const afterMintResp = await adminApi.get("/api/v1/auth/api-tokens");
  expect(afterMintResp.status()).toBe(200);
  const afterMintBody = (await afterMintResp.json()) as {
    tokens: Array<{ id: string; name: string }>;
  };
  const minted = afterMintBody.tokens.find((t) => t.name === THROWAWAY_TOKEN_NAME);
  expect(minted, `Token "${THROWAWAY_TOKEN_NAME}" not found in list after mint`).toBeTruthy();
  mintedTokenId = minted!.id;

  // -- UI gesture: click Revoke for the throwaway token --
  // spec: profile/tokens/page.tsx — Button variant="destructive" size="sm" "Revoke"
  //   → setRevokeId(t.id) → ConfirmDialog.
  // The Revoke button is in the same row as the token name.
  // Filter the row by token name text, then find the Revoke button within it.
  const tokenRow = page.locator("tr").filter({ hasText: THROWAWAY_TOKEN_NAME });
  const revokeBtn = tokenRow.getByRole("button", { name: "Revoke" });
  await expect(revokeBtn).toBeVisible({ timeout: 10_000 });
  await revokeBtn.click();

  // -- UI assertion: ConfirmDialog "Revoke token" opens --
  // spec: profile/tokens/page.tsx — ConfirmDialog title="Revoke token"
  await expect(
    page.getByRole("heading", { name: "Revoke token", exact: true })
  ).toBeVisible({ timeout: 10_000 });

  // -- UI gesture: confirm revoke --
  // spec: profile/tokens/page.tsx — ConfirmDialog confirmLabel="Revoke"
  // Use last() to avoid matching any prior Revoke button in the DOM.
  await page.getByRole("button", { name: "Revoke", exact: true }).last().click();

  // -- UI assertion: toast "Token revoked." --
  // spec: profile/tokens/page.tsx — toast({ title: "Token revoked." })
  // Toasts render twice (visual + aria-live span) → .first() on the toast text.
  await expect(
    page.getByText("Token revoked.", { exact: true }).first()
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: throwaway token name no longer visible in the table --
  // TanStack Query invalidates on delete success.
  await expect(
    page.getByText(THROWAWAY_TOKEN_NAME, { exact: true })
  ).not.toBeVisible({ timeout: 15_000 });

  // -- Backend probe (dual confirmation): GET /auth/api-tokens → throwaway gone --
  // spec: FRONTEND_BASIC.md §API tokens — DELETE /auth/api-tokens/{id} removes the token.
  const afterRevokeResp = await adminApi.get("/api/v1/auth/api-tokens");
  expect(afterRevokeResp.status()).toBe(200);
  const afterRevokeBody = (await afterRevokeResp.json()) as {
    tokens: Array<{ id: string; name: string }>;
  };
  const stillPresent = afterRevokeBody.tokens.some((t) => t.name === THROWAWAY_TOKEN_NAME);
  expect(stillPresent, "Throwaway token must be absent after revoke").toBe(false);

  // Mark as cleaned up so afterAll does not double-delete.
  mintedTokenId = null;
});
