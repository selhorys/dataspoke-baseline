/**
 * Ground spec: /profile/tokens page — narrow UI-flow tests.
 *
 * Concerns, one per test:
 *   1. the page renders for the signed-in Admin;
 *   2. mint → one-shot reveal (`dsk_` token) → row in the list → revoke via the
 *      ConfirmDialog → gone;
 *   3. the Copy button on the reveal dialog actually copies where the app is
 *      served. `navigator.clipboard` exists only in a secure context, and the
 *      dev deployment is plain HTTP, so this is the only layer that can prove
 *      the selection fallback works — jsdom has neither the Clipboard API nor
 *      `document.execCommand`, and the colocated Vitest suite has to stub both;
 *   4. the Admin-only All-tokens scope: Owner + Status columns, another user's
 *      token listed with its owner, revoke through the admin route, the row
 *      leaving the default view, and returning under "Show revoked" as revoked
 *      with no revoke action left.
 *
 * spec: spec/feature/FRONTEND_BASIC.md §API tokens (`/profile/tokens`) — the
 *   Admin-only scope control, the Owner/Status columns, the "Show revoked"
 *   checkbox carrying `include_revoked`, "New token" hidden in the All-tokens
 *   scope, revoke through `DELETE /admin/users/{id}/api-tokens/{token_id}`
 *   addressed by the row's `user_id`, and "a revoked row exposes no revoke
 *   action".
 * spec: spec/feature/FRONTEND_BASIC.md §Routing — /profile/tokens:
 *   GET/POST /auth/api-tokens, DELETE /auth/api-tokens/{id},
 *   GET /admin/api-tokens, DELETE /admin/users/{id}/api-tokens/{token_id}
 * spec: spec/feature/AUTH.md §Revoked-token visibility — the admin reads exclude
 *   revoked rows by default and take `include_revoked=true` to bring them back;
 *   revocation sets `revoked_at` rather than removing the row.
 * spec: spec/TESTING.md §End-to-End (E2E) Testing — dual confirmation, setup in
 *   hooks and through the API, ConfirmDialog, selector guidance.
 */

import { test, expect } from "../../fixtures/index";
import { apiBaseUrl, ADMIN_EMAIL } from "../../fixtures/env";

// ── Constants ─────────────────────────────────────────────────────────────────

/** Name for the throwaway token minted in the mint→revoke flow. */
const THROWAWAY_TOKEN_NAME = "e2e-ground-tokens-throwaway";

/** Name for the throwaway token minted by the clipboard test. */
const CLIPBOARD_TOKEN_NAME = "e2e-ground-tokens-clipboard";

/**
 * The Editor provisioned by global-setup (TEST_USERS). Minting is self-only —
 * no route mints for another user — so the All-tokens fixture is created by
 * signing in as this user over the API and minting as them.
 */
const EDITOR_EMAIL = "e2e-editor@test.dataspoke.example.com";
const EDITOR_PASSWORD = "e2e-editor-password";

/**
 * Family prefix for the Editor's inventory fixture. Revocation sets
 * `revoked_at` rather than removing the row, so a fixed name would accumulate
 * withdrawn namesakes across runs; the setup below withdraws the whole family
 * by prefix and mints one row under a name unique to this attempt.
 */
const EDITOR_TOKEN_PREFIX = "e2e-ground-tokens-editor";
const EDITOR_TOKEN_NAME = `${EDITOR_TOKEN_PREFIX}-${Date.now()}`;

// ── Module state ──────────────────────────────────────────────────────────────

/** ID of the token minted in test 2; used in afterAll for cleanup. */
let mintedTokenId: string | null = null;

/** ID of the token minted in test 3; used in afterAll for cleanup. */
let clipboardTokenId: string | null = null;

/** The Editor's user id and the id of their inventory-fixture token. */
let editorUserId: string | null = null;
let editorTokenId: string | null = null;

// ── Setup ─────────────────────────────────────────────────────────────────────

/**
 * [API-fired — setup, no UI surface] Mint one token owned by the Editor, so the
 * All-tokens scope has a row the signed-in Admin does not own. Idempotent: the
 * whole `EDITOR_TOKEN_PREFIX` family is withdrawn first, so a group retry
 * replays cleanly over a previous attempt's leftovers.
 * spec: spec/TESTING.md §E2E §Execution discipline — "Setup fires through the
 *   API, not the browser"; "each setup path pre-deletes by natural key and
 *   accepts the upsert/absent status codes".
 * spec: spec/feature/AUTH.md §API Tokens — "Minting is owner-only in every
 *   case: no route mints a token for anyone but its caller."
 */
test.beforeAll(async ({ adminApi }) => {
  const usersResp = await adminApi.get("/api/v1/admin/users?limit=100");
  expect(usersResp.status()).toBe(200);
  const usersBody = (await usersResp.json()) as {
    users: Array<{ id: string; email: string }>;
  };
  const editor = usersBody.users.find((u) => u.email === EDITOR_EMAIL);
  expect(
    editor,
    `${EDITOR_EMAIL} must be provisioned by global-setup before this suite runs`
  ).toBeTruthy();
  editorUserId = editor!.id;

  // Withdraw any leftover fixture tokens from an earlier attempt (404 = already
  // gone, which is success for a pre-delete).
  const leftoverResp = await adminApi.get(
    `/api/v1/admin/api-tokens?user_id=${editorUserId}&include_revoked=true&limit=100`
  );
  expect(leftoverResp.status()).toBe(200);
  const leftoverBody = (await leftoverResp.json()) as {
    tokens: Array<{ id: string; name: string; revoked_at: string | null }>;
  };
  for (const t of leftoverBody.tokens) {
    if (t.name.startsWith(EDITOR_TOKEN_PREFIX) && t.revoked_at === null) {
      await adminApi.delete(`/api/v1/admin/users/${editorUserId}/api-tokens/${t.id}`);
    }
  }

  // Sign in as the Editor and mint as them — the only way a token can end up
  // owned by someone other than the caller.
  const loginResp = await fetch(`${apiBaseUrl()}/api/v1/auth/token`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email: EDITOR_EMAIL, password: EDITOR_PASSWORD }),
  });
  expect(loginResp.status).toBe(200);
  const { access_token: editorAccessToken } = (await loginResp.json()) as {
    access_token: string;
  };

  const mintResp = await fetch(`${apiBaseUrl()}/api/v1/auth/api-tokens`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${editorAccessToken}`,
    },
    body: JSON.stringify({ name: EDITOR_TOKEN_NAME }),
  });
  expect(mintResp.status).toBe(201);
  const mintBody = (await mintResp.json()) as { id: string };
  editorTokenId = mintBody.id;
});

// ── Cleanup ───────────────────────────────────────────────────────────────────

test.afterAll(async ({ adminApi }) => {
  // If a token was not revoked during its test (e.g. failure mid-flow),
  // withdraw it via adminApi to leave the env clean.
  for (const id of [mintedTokenId, clipboardTokenId]) {
    if (id) await adminApi.delete(`/api/v1/auth/api-tokens/${id}`);
  }
  if (editorUserId && editorTokenId) {
    await adminApi.delete(`/api/v1/admin/users/${editorUserId}/api-tokens/${editorTokenId}`);
  }
  // Also scan the caller's own list for any leftover throwaway tokens by name.
  const listResp = await adminApi.get("/api/v1/auth/api-tokens");
  if (listResp.ok()) {
    const body = (await listResp.json()) as { tokens: Array<{ id: string; name: string }> };
    for (const t of body.tokens) {
      if (t.name === THROWAWAY_TOKEN_NAME || t.name === CLIPBOARD_TOKEN_NAME) {
        await adminApi.delete(`/api/v1/auth/api-tokens/${t.id}`);
      }
    }
  }
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 1 — /profile/tokens renders the token list and "New token" button
// spec: FRONTEND_BASIC.md §Authentication (API tokens) — GET /auth/api-tokens populates the table;
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
  // spec: FRONTEND_BASIC.md §API tokens — the My-tokens scope is the default for
  //   every role, and it carries the mint control.
  await expect(page.getByRole("button", { name: "New token" })).toBeVisible({ timeout: 10_000 });
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 2 — Mint a token (dialog shows dsk_ token), confirm in list, revoke via
//           ConfirmDialog, confirm gone.
// spec: FRONTEND_BASIC.md §Authentication (API tokens) — Dialog "New API token" → Create → one-shot
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
  // spec: FRONTEND_BASIC.md §Authentication (API tokens) — token reveal dialog shows "Your new token";
  //   the raw token starts with dsk_.
  await expect(
    page.getByRole("heading", { name: "Your new token", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: token text starts with dsk_ --
  // spec: FRONTEND_BASIC.md §Authentication (API tokens) — "dsk_AbCdEf1234ZyXw..."
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
  // spec: FRONTEND_BASIC.md §Authentication (API tokens) — POST /auth/api-tokens creates a token row.
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
  // spec: FRONTEND_BASIC.md §Authentication (API tokens) — DELETE /auth/api-tokens/{id} removes the token.
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

// ─────────────────────────────────────────────────────────────────────────────
// Test 3 — The reveal dialog's Copy button copies without the async Clipboard API.
// spec: FRONTEND_BASIC.md §API tokens (`/profile/tokens`) — "the clipboard copy
//   button is the primary action — the user must transfer the token to wherever
//   it will be used before closing the dialog. Closing without copy means the
//   user must revoke and re-mint."
// Why here: `navigator.clipboard` is exposed to secure contexts only, and the dev
//   deployment is plain HTTP, so the copy runs through the hidden-textarea +
//   document.execCommand fallback. jsdom implements neither mechanism, so the
//   colocated Vitest suite can only exercise stubs; this is the layer where the
//   real browser decides.
// ─────────────────────────────────────────────────────────────────────────────

test("/profile/tokens — Copy token succeeds with no async Clipboard API available", async ({
  page,
  adminApi,
}) => {
  // Pre-flight: withdraw any leftover clipboard-test token by name.
  const preListResp = await adminApi.get("/api/v1/auth/api-tokens");
  if (preListResp.ok()) {
    const preBody = (await preListResp.json()) as { tokens: Array<{ id: string; name: string }> };
    for (const t of preBody.tokens) {
      if (t.name === CLIPBOARD_TOKEN_NAME) {
        await adminApi.delete(`/api/v1/auth/api-tokens/${t.id}`);
      }
    }
  }

  // Pin the condition the fix exists for: no async Clipboard API on the page.
  // On the plain-HTTP dev deployment this is already the browser's own state;
  // shaping it explicitly makes the fallback the only path under test whatever
  // scheme PLAYWRIGHT_BASE_URL points at.
  // `globalThis` is the browser window inside addInitScript/evaluate; referenced
  // this way so the spec needs no DOM lib in the E2E tsconfig.
  await page.addInitScript(() => {
    const g = globalThis as unknown as { navigator: object };
    Object.defineProperty(g.navigator, "clipboard", { value: undefined, configurable: true });
  });

  await page.goto("/profile/tokens");
  await expect(page).not.toHaveURL(/\/login/);

  // Backstop: the shaping actually took, so a pass below cannot come from the
  // Clipboard API quietly doing the work.
  expect(
    await page.evaluate(() => {
      const g = globalThis as unknown as { navigator: { clipboard?: unknown } };
      return g.navigator.clipboard === undefined;
    })
  ).toBe(true);

  await expect(
    page.getByRole("heading", { name: "API Tokens", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI gesture: mint a token so the one-shot reveal dialog is on screen --
  await page.getByRole("button", { name: "New token" }).click();
  await expect(
    page.getByRole("heading", { name: "New API token", exact: true })
  ).toBeVisible({ timeout: 10_000 });
  await page.locator("#token-name").fill(CLIPBOARD_TOKEN_NAME);
  await page.getByRole("button", { name: "Create", exact: true }).click();

  await expect(
    page.getByRole("heading", { name: "Your new token", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // Record the token so cleanup can withdraw it whatever happens next.
  const listResp = await adminApi.get("/api/v1/auth/api-tokens");
  expect(listResp.status()).toBe(200);
  const listBody = (await listResp.json()) as { tokens: Array<{ id: string; name: string }> };
  const minted = listBody.tokens.find((t) => t.name === CLIPBOARD_TOKEN_NAME);
  expect(minted, `Token "${CLIPBOARD_TOKEN_NAME}" not found after mint`).toBeTruthy();
  clipboardTokenId = minted!.id;

  // -- UI gesture: press Copy token --
  // spec: profile/tokens/page.tsx — Button aria-label="Copy token" → copyToClipboard
  await page.getByRole("button", { name: "Copy token" }).click();

  // -- UI assertion: the dialog reports the copy happened --
  // The footer button reads "Done" only after copyToClipboard resolved true.
  // Before the fix the unguarded navigator.clipboard.writeText threw a TypeError
  // into a floating promise, so the button stayed inert on "Close".
  await expect(page.getByRole("button", { name: "Done", exact: true })).toBeVisible({
    timeout: 10_000,
  });

  // -- UI assertion: no failure toast --
  // spec: profile/tokens/page.tsx — a failed copy raises a destructive
  //   "Copy failed" toast telling the user to copy manually. Asserted after the
  //   positive signal above, so this is an ordered check and not a race.
  await expect(page.getByText("Copy failed")).toHaveCount(0);

  // -- UI assertion: the fallback left the document as it found it --
  // The hidden textarea it mounts inside the dialog is removed again, so no
  // stray node is left behind for the user to tab into.
  expect(await page.locator("textarea").count()).toBe(0);

  // -- UI assertion: the reveal dialog is still open and still shows the token --
  // Copying must not dismiss the one-shot dialog: the token cannot be shown again.
  await expect(page.getByRole("heading", { name: "Your new token", exact: true })).toBeVisible();
  expect((await page.locator("code").first().textContent()) ?? "").toMatch(/^dsk_/);

  // The dialog is still interactive after the copy — its own button closes it,
  // which a broken focus restore would not survive.
  await page.getByRole("button", { name: "Done", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "Your new token", exact: true })
  ).not.toBeVisible({ timeout: 10_000 });
  const revokeResp = await adminApi.delete(`/api/v1/auth/api-tokens/${clipboardTokenId}`);
  expect(revokeResp.status()).toBe(204);
  clipboardTokenId = null;
});

// ─────────────────────────────────────────────────────────────────────────────
// Test 4 — The Admin-only All-tokens scope: another user's token, revoked
//           through the admin route, then read back under "Show revoked".
// spec: FRONTEND_BASIC.md §API tokens (`/profile/tokens`) — "The scope control
//   renders for Admins only"; All tokens "adds the Owner and Status columns…
//   plus a 'Show revoked' checkbox carrying the route's `include_revoked` param,
//   off by default"; "'New token' is hidden in the All-tokens scope"; "Revoke in
//   that scope goes through DELETE /admin/users/{id}/api-tokens/{token_id}
//   addressed by the row's `user_id`… a revoked row exposes no revoke action".
// spec: AUTH.md §Revoked-token visibility — the default view excludes revoked
//   rows; `include_revoked=true` brings them back, carrying `revoked_at`.
// ─────────────────────────────────────────────────────────────────────────────

test("/profile/tokens — All tokens scope lists another user's token and revokes it", async ({
  page,
  adminApi,
}) => {
  // Backend probe first: the fixture token exists, is owned by the Editor, and
  // is not revoked. Gating the UI assertions on confirmed backend state keeps
  // "the API never produced it" apart from "the UI did not render it".
  const beforeResp = await adminApi.get("/api/v1/admin/api-tokens?limit=100");
  expect(beforeResp.status()).toBe(200);
  const beforeBody = (await beforeResp.json()) as {
    total_count: number;
    tokens: Array<{
      id: string;
      name: string;
      user_id: string;
      user_email: string;
      revoked_at: string | null;
    }>;
  };
  const fixture = beforeBody.tokens.find((t) => t.name === EDITOR_TOKEN_NAME);
  expect(fixture, `Editor fixture token "${EDITOR_TOKEN_NAME}" must be listed`).toBeTruthy();
  expect(fixture!.user_email).toBe(EDITOR_EMAIL);
  expect(fixture!.user_id).toBe(editorUserId);
  expect(fixture!.revoked_at).toBeNull();
  // The inventory is cross-user: it also carries the signed-in Admin's own
  // probe token, so this is not merely a per-user list under another name.
  expect(new Set(beforeBody.tokens.map((t) => t.user_email)).size).toBeGreaterThan(1);
  // One of the caller's own tokens, used below to anchor the My-tokens table to
  // a settled state before anything is asserted absent from it. Read off the
  // inventory rather than hardcoded, so it does not couple to how global-setup
  // names the probe token.
  const callerToken = beforeBody.tokens.find((t) => t.user_email === ADMIN_EMAIL);
  expect(
    callerToken,
    `the signed-in Admin (${ADMIN_EMAIL}) must hold at least one token for the My-tokens anchor`
  ).toBeTruthy();

  await page.goto("/profile/tokens");
  await expect(page).not.toHaveURL(/\/login/);
  await expect(
    page.getByRole("heading", { name: "API Tokens", exact: true })
  ).toBeVisible({ timeout: 15_000 });

  // -- UI assertion: the Admin gets the scope control, defaulting to My tokens --
  await expect(page.getByRole("tab", { name: "My tokens" })).toBeVisible({ timeout: 10_000 });
  await expect(page.getByRole("tab", { name: "All tokens" })).toBeVisible();
  await expect(page.getByRole("tab", { name: "My tokens" })).toHaveAttribute(
    "aria-selected",
    "true"
  );
  // Anchor the absence assertion below to a settled table: the caller's own
  // token is rendered, so the My-tokens read has resolved and what follows is
  // read against rows rather than against a Skeleton.
  await expect(page.getByRole("cell", { name: callerToken!.name })).toBeVisible({
    timeout: 15_000,
  });
  // The default scope shows the caller's own tokens only — the Editor's token is
  // not in it.
  await expect(page.getByText(EDITOR_TOKEN_NAME, { exact: true })).toHaveCount(0);

  // -- UI gesture: switch to the All-tokens scope --
  await page.getByRole("tab", { name: "All tokens" }).click();

  // -- UI assertion: Owner and Status columns appear --
  await expect(page.getByRole("columnheader", { name: "Owner" })).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("columnheader", { name: "Status" })).toBeVisible();

  // -- UI assertion: "New token" is gone — minting is self-only --
  await expect(page.getByRole("button", { name: "New token" })).toHaveCount(0);

  // -- UI assertion: "Show revoked" is offered and starts off --
  const showRevoked = page.getByLabel("Show revoked");
  await expect(showRevoked).toBeVisible();
  await expect(showRevoked).toHaveAttribute("data-state", "unchecked");

  // -- UI assertion: the Editor's token is listed with its owner and status --
  const editorRow = page.getByRole("row").filter({ hasText: EDITOR_TOKEN_NAME });
  await expect(editorRow).toHaveCount(1, { timeout: 15_000 });
  await expect(editorRow.getByRole("cell", { name: EDITOR_EMAIL })).toBeVisible();
  await expect(editorRow.getByTestId("token-status")).toHaveAttribute("data-status", "active");

  // -- UI gesture: revoke the Editor's token from the All-tokens scope --
  await editorRow.getByRole("button", { name: "Revoke" }).click();

  // -- UI assertion: the confirm names the token and its owner --
  // spec: FRONTEND_BASIC.md §API tokens — the confirm copy is single-valued
  //   because every revoke in this scope goes through the admin route.
  const confirmDialog = page
    .getByRole("dialog")
    .filter({ has: page.getByRole("heading", { name: "Revoke token", exact: true }) });
  await expect(confirmDialog).toBeVisible({ timeout: 10_000 });
  await expect(confirmDialog.getByText(EDITOR_TOKEN_NAME, { exact: false })).toBeVisible();
  await expect(confirmDialog.getByText(EDITOR_EMAIL, { exact: false })).toBeVisible();

  await confirmDialog.getByRole("button", { name: "Revoke", exact: true }).click();

  // -- UI assertion: revoke reported, and the row leaves the default view --
  await expect(
    page.getByText("Token revoked.", { exact: true }).first()
  ).toBeVisible({ timeout: 15_000 });
  await expect(page.getByRole("row").filter({ hasText: EDITOR_TOKEN_NAME })).toHaveCount(0, {
    timeout: 15_000,
  });

  // -- Backend probe (dual confirmation): revoked, not deleted --
  // spec: AUTH.md §Revoked-token visibility — the default admin read excludes it;
  //   include_revoked=true returns it carrying `revoked_at`.
  const defaultResp = await adminApi.get("/api/v1/admin/api-tokens?limit=100");
  expect(defaultResp.status()).toBe(200);
  const defaultBody = (await defaultResp.json()) as { tokens: Array<{ name: string }> };
  expect(defaultBody.tokens.some((t) => t.name === EDITOR_TOKEN_NAME)).toBe(false);

  const withRevokedResp = await adminApi.get(
    "/api/v1/admin/api-tokens?include_revoked=true&limit=100"
  );
  expect(withRevokedResp.status()).toBe(200);
  const withRevokedBody = (await withRevokedResp.json()) as {
    tokens: Array<{ id: string; name: string; revoked_at: string | null; user_id: string }>;
  };
  const revoked = withRevokedBody.tokens.find((t) => t.name === EDITOR_TOKEN_NAME);
  expect(revoked, "The revoked token must still exist under include_revoked=true").toBeTruthy();
  expect(revoked!.revoked_at).not.toBeNull();
  expect(revoked!.user_id).toBe(editorUserId);
  expect(revoked!.id).toBe(editorTokenId);

  // -- UI gesture: turn on "Show revoked" --
  await showRevoked.click();

  // -- UI assertion: the row returns, labelled revoked, with no revoke action --
  const revokedRow = page.getByRole("row").filter({ hasText: EDITOR_TOKEN_NAME });
  await expect(revokedRow).toHaveCount(1, { timeout: 15_000 });
  await expect(revokedRow.getByTestId("token-status")).toHaveAttribute("data-status", "revoked");
  await expect(revokedRow.getByRole("button", { name: "Revoke" })).toHaveCount(0);

  // afterAll's revoke of this token is idempotent, so no bookkeeping is needed.
});
