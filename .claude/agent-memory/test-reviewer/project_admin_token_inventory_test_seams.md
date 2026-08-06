---
name: admin-token-inventory-test-seams
description: Issue #145 admin API-token inventory — which claims are specced, which are only in code, and the badge/sr-only detail that shapes both Vitest and Playwright selectors
metadata:
  type: project
---

Anchors for the cross-user admin API-token inventory (`GET /admin/api-tokens`,
`GET /admin/users/{id}/api-tokens`, `AUTH.API_TOKEN_REVOKED`, `/profile/tokens`
All-tokens scope).

**Now specced — do not re-flag as NOT-A-SPEC-CLAIM.** `AUTH.md §Revoked-token
visibility` carries: "Either ordering places nulls last and is tiebroken by token
id, so paging an inventory returns each token exactly once regardless of the
requested `sort`." That covers both the NULLS LAST and the `id`-tiebreak
assertions. The older `BACKEND.md §Validation Service` ("null `updated_at` last")
analogy is no longer needed.

**Still unspecced (code-derived only):**
- Caller-scoped query key (`["admin","api-tokens",callerId,…]` in
  `lib/api/admin.ts`). `FRONTEND_BASIC.md` says nothing about per-caller cache
  scoping or a sign-out purge. `admin.ts` itself calls the key "defence in depth"
  and names the cache purge as the real mechanism.
- The drawer copy on `/admin/users`: `Showing N of T tokens.`,
  `No active tokens.`, `No tokens.` — none appear in FRONTEND_BASIC.md.
- Sending `include_revoked=false` explicitly (spec only says "off by default").

**Stated but unpinned by any test:** the *lean* self item shape.
`AUTH.md §Revoked-token visibility` says "`revoked_at` is not on the self item
shape" and `API.md §Admin` says owner identity + `revoked_at` "stay off
`GET /auth/api-tokens`", but no test asserts `set(item) == {id,name,
role_snapshot,created_at,last_used_at,expires_at}`. Likewise "GET
/auth/api-tokens … offers no opt-in" (`?include_revoked=true` on the self route).
The `SHARED_PAGINATED_MODELS` comment in
`tests/unit/spec_conformance/test_response_envelope.py` leans on this distinction.

**`TokenStatusBadge` shape (components/token-status-badge.tsx).** The badge
renders the one-word status as a direct text node plus an `sr-only` `<span>`
carrying "— Revoked <stamp>" / "— Expired <stamp>" for the revoked/expired
states.
- Vitest `getByText("revoked")` still works: TL's `getNodeText` reads only direct
  child text nodes, so the sr-only span is not concatenated.
- Playwright `toHaveText("revoked")` cannot match — it reads full `textContent`.
  Use `getByTestId("token-status")` + `toHaveAttribute("data-status", …)`.
  See [[playwright-tohavetext-regex-not-normalized]].

**Test placement.** The row-level filter/order behaviour lives in spot
(`tests/integration/spot/test_auth_api_tokens.py`, module-scoped `token_inventory`
fixture seeding two users, a 4-row `created_at` tie, NULL/non-NULL `last_used_at`,
an expired row and a revoked row via raw SQL). The unit layer
(`tests/unit/api/auth/test_api_tokens.py`) compiles `list_page`'s two statements
and reads SELECT list / WHERE / ORDER BY — that is the only always-run seam, since
`list_page` returns whatever the mocked session was told to return.
