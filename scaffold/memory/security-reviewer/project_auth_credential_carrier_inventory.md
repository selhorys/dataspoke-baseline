---
name: auth-credential-carrier-inventory
description: The 7 credential/authorisation carriers a DataSpoke auth diff must each account for, which control governs each after issue #75's session_epoch, and the users→api_tokens→password_reset_tokens lock order
metadata:
  type: project
---

Issue #75 (Google-link account pre-hijacking) made "invalidate every credential on
the row" a standing invariant. Any future auth diff must enumerate all carriers,
not just the one it touches — the first review round missed in-flight writes, the
second missed reset-token issuance.

**Carrier → governing control** (verified 2026-07-24):

| Carrier | Killed by |
|---|---|
| `users.password_hash` | set NULL in the bind txn |
| access JWT | `ses` claim vs `users.session_epoch` (bearer auth path) |
| refresh JWT | `ses` claim on `/auth/token/refresh`; Redis list is per-token-hash only |
| API token (`dsk_`) | `revoked_at` — **no epoch check on the PAT auth path**, so PAT safety rests entirely on revocation + the row lock |
| `password_reset_tokens` row | DELETE of unused rows in the bind txn |
| Starlette session cookie | not an identity carrier — authlib OAuth state/nonce only |
| `POST /internal/admin/bootstrap` | X-Internal-Token; no-op when any Admin exists |

**Serialization invariant.** Every credential-*creating* write takes
`users` `SELECT…FOR UPDATE` and re-validates its own authoriser under it:
`PATCH /auth/me` password, `POST /auth/api-tokens`,
`POST /auth/password/reset/confirm`, and `issue_reset_token`.

`issue_reset_token` is the non-obvious one, and the lock alone does **not** close
it — the bind holds the lock, releases it on commit, and an INSERT that then
proceeded would land *after* the bind's delete of unused rows, which sweeps only
what was visible at that statement. What closes it is comparing the owner's
`session_epoch` captured on the resolving read against the value re-read under the
lock, and declining the INSERT when it moved. Any future "just take the lock"
suggestion on a *creating* write whose authoriser is not itself lock-protected is
wrong for this reason.

`DELETE /admin/users/{id}/google` (admin unbind) is credential-*destroying*: it
takes the same lock and bumps the epoch but re-validates nothing, which is correct.
Its `409 GOOGLE_IS_ONLY_AUTH_METHOD` guard means it cannot release a binding on a
row with no password — which is every Google-native row.

**Lock order is `users` → `api_tokens` → `password_reset_tokens`** on every
multi-lock path. Preserve it; the `lookup_and_validate` `last_used_at` throttle runs
on its own `SessionLocal()` connection and commits immediately, which is what keeps
it out of the cycle.

**Two mechanics the correctness depends on, both easy to break silently:**
- Isolation is Postgres default READ COMMITTED (nothing in `src/` sets
  `isolation_level`). The re-read after a blocking `FOR UPDATE` sees committed
  state *because of that*. Setting REPEATABLE READ anywhere would convert these
  into serialization failures.
- Every re-read needs `execution_options(populate_existing=True)`:
  `SessionLocal` is `expire_on_commit=False` and `require_authenticated` has already
  loaded `User` (and the `ApiToken` for a PAT caller) into the same session's
  identity map.

Related: [[auth-revoke-refresh-asymmetry]], [[auth-fail-closed-spans-layers]]
