---
name: auth-serialization-untested-rows
description: Status of AUTH.md §Serialization's four credential-write rows — reset.py's two are now covered by a routing fake (with an aliasing caveat); the PATCH /auth/me + POST /auth/api-tokens rows are still asserted by helper name rather than behaviour
metadata:
  type: project
---

`spec/feature/AUTH.md §Serialization of credential-creating writes` names four writes
(`PATCH /auth/me` password, `POST /auth/api-tokens`, reset/confirm, reset/request).
They fail differently — check each separately on any auth test diff.

- **reset/request epoch decline** and **reset/confirm re-read under the lock** —
  `tests/unit/api/auth/test_reset.py` now uses `_ResetRoutingSession` (query-routing by
  compiled statement text: `password_reset_tokens` / `users` / `FOR UPDATE`), which closes the
  old always-same-`AsyncMock` hole. Residual caveat: the decline case is modelled with a
  *separate* locked object — see [[populate-existing-alias-fake-trap]].
- **The "cannot be tested without a configured SMTP peripheral" justification is false.**
  `issue_reset_token` takes `notification_service` as an argument and unit tests pass an
  `AsyncMock`; at spot the dev env runs `stub_notification_service`. Do not accept that excuse.
  A genuine two-session race *is* expressible at spot (session A holds `lock_user`'s row lock
  and bumps the epoch; session B's `issue_reset_token` blocks and then observes the increment) —
  spot may call dataspoke Python directly, and `test_auth_google_credential_reset.py` already does.
- **`PATCH /auth/me` + `POST /auth/api-tokens` route wiring** is unobservable from a single
  HTTP response, but *not* untestable. Patching `src.backend.auth.users.lock_user` (not the
  `revalidate_under_user_lock` helper) lets the real re-check run against the unit `client`
  fixture: an unsuperseded locked row gives `200` + password write, a bumped one gives
  `401 UNAUTHORIZED` + no write. Prefer that pair over `mock_revalidate.assert_awaited_once()`,
  which pins a helper name and passes against a route that swallows the failure.

**Why:** issue #75's credential reset is only total if these four re-checks exist; the spec says
so explicitly ("what makes the credential reset claim ... true rather than aspirational").

**How to apply:** grep the changed impl for newly added `db.execute` calls and confirm the mock
session can distinguish them before believing a green run.
Related: [[project-auth-email-storage-case-divergence]], [[dead-assert-tuple-ruff-blind]].
