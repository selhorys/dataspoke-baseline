---
name: auth-serialization-untested-rows
description: Status of AUTH.md §Serialization's four credential-write rows — reset/request and POST /auth/api-tokens now have a real two-transaction spot race; PATCH /auth/me and reset/confirm are still asserted only by unit fake or helper name
metadata:
  type: project
---

`spec/feature/AUTH.md §Serialization of credential-creating writes` names four writes
(`PATCH /auth/me` password, `POST /auth/api-tokens`, reset/confirm, reset/request).
They fail differently — check each separately on any auth test diff.

- **reset/request + `POST /auth/api-tokens`** — `tests/integration/spot/test_auth_credential_write_serialization.py`
  runs a genuine two-transaction race against real PostgreSQL. The holder side is the **real**
  `users.bind_google_identity` running uncommitted (unbound row = the binding branch; row pre-seeded
  with the same `sub` = the `bound=False` no-op branch, which still holds `lock_user`'s row lock for
  its transaction's life). Blocked-ness is observed via `pg_blocking_pids`; the HTTP halves add a
  pre-dispatch "blocks nobody" probe, `len(waiters) == 1`, and a `pg_stat_activity.query` narrowing
  on `FOR UPDATE` + `dataspoke.users`. That last assertion is the *positive* evidence that the API
  pod takes the lock — see below on why mutation testing cannot supply it.
  This is the only layer that fails when `.with_for_update()` is dropped from `users.lock_user`, and
  the only layer that catches the `epoch_at_read` alias trap in `reset.issue_reset_token`
  ([[populate-existing-alias-fake-trap]]) — the unit routing fake never waits.
  **Mutation-testing caveat, do not misread it.** Removing `.with_for_update()` locally gives
  3 failed / 1 passed. That is correct, not a gap: the API pod runs the unmutated image, so a local
  mutation can only reach the *holder* side. `test_a_bind_committing_mid_flight_refuses_the_api_token_mint`
  keeps passing because its holder's uncommitted `UPDATE` (from the real bind) takes a row-level
  write lock regardless of `FOR UPDATE`. Proving the pod-side lock needs a rebuild+redeploy, or the
  `query` narrowing that is already there.

- **reset/request epoch decline** and **reset/confirm re-read under the lock** —
  `tests/unit/api/auth/test_reset.py` uses `_ResetRoutingSession` (query-routing by
  compiled statement text: `password_reset_tokens` / `users` / `FOR UPDATE`), which closes the
  old always-same-`AsyncMock` hole. Residual caveat: the decline case is modelled with a
  *separate* locked object — see [[populate-existing-alias-fake-trap]].
- **The "cannot be tested without a configured SMTP peripheral" justification is false.**
  `issue_reset_token` takes `notification_service` as an argument and unit tests pass an
  `AsyncMock`; at spot the dev env runs `stub_notification_service`. Do not accept that excuse.
- **`PATCH /auth/me` route wiring** is still unobservable from a single HTTP response, but *not*
  untestable. Patching `src.backend.auth.users.lock_user` (not the `revalidate_under_user_lock`
  helper) lets the real re-check run against the unit `client` fixture: an unsuperseded locked row
  gives `200` + password write, a bumped one gives `401 UNAUTHORIZED` + no write. Prefer that pair
  over `mock_revalidate.assert_awaited_once()`, which pins a helper name and passes against a route
  that swallows the failure.
- **Still uncovered at any layer:** the PAT-carried variant of `PATCH /auth/me` /
  `POST /auth/api-tokens` that must fail `401 TOKEN_REVOKED` under the same lock (AUTH.md L290-296),
  and `issue_reset_token`'s `locked is None` branch (row hard-deleted under the lock,
  `reason="user_deleted"`).

**Why:** issue #75's credential reset is only total if these four re-checks exist; the spec says
so explicitly ("what makes the credential reset claim ... true rather than aspirational").

**How to apply:** grep the changed impl for newly added `db.execute` calls and confirm the mock
session can distinguish them before believing a green run.
Related: [[project-auth-email-storage-case-divergence]], [[dead-assert-tuple-ruff-blind]].
