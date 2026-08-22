---
name: oauth-302-test-seams
description: Issue #83 /auth/google/* 302 contract — which halves of the contract table have a test seam, and the two mutations that survived the whole 2856-test unit suite (route-level except-breadth, success-path verbatim redirect)
metadata:
  type: project
---

`spec/API.md §OAuth browser-redirect contract` has a four-row table. The #83 test
stage covered the two *failure* rows hard and left the other two open. Measured
by mutating `src/api/routers/auth.py` and running `uv run pytest tests/unit/`
(2856 tests) after each:

**Killed (do not re-report as gaps):** `urljoin` → naive concat (path not
discarded), `quote(safe='')` dropped, `_OAUTH_ERROR_CODES` membership filter
dropped, WARNING↔ERROR level swap, `status_code=302` → `307`, a code removed
from `_OAUTH_ERROR_CODES`, `@auth_route_limit` removed from a Google route.
`tests/unit/api/auth/test_oauth_error_redirect.py` +
`tests/unit/spec_conformance/test_oauth_redirect_contract.py` catch all seven.

**Survived green — the real seams:**

1. **Route-level `except` breadth.** Narrowing *both* routes from
   `except Exception as exc:` to `except DataSpokeError as exc:` leaves 2856/2856
   green. `test_exception_outside_the_taxonomy_redirects_without_an_error_parameter`
   calls the **helper** `_oauth_error_redirect(RuntimeError(...))` directly, so it
   proves nothing about what the route bodies catch — yet "an exception outside
   the error taxonomy … redirects" is the spec sentence, and authlib's OIDC
   discovery fetch / a raw `DBAPIError` are exactly what would escape to a 500.
   A route-level test is ~15 lines and kills it: patch
   `src.api.routers.auth.oauth_google.is_configured` with
   `side_effect=RuntimeError("boom")`, `patch.object(auth_limiter, "enabled", False)`,
   call `get_google_login(request=<real starlette Request>)` /
   `get_google_callback(request=…, db=MagicMock())`, assert `302` and `"?" not in
   location`. The callback variant also pins `db.rollback()` (verified: passes on
   the shipped impl, fails under the narrowed-except mutation).
2. **Success row.** Rewriting `redirect_url = settings.oauth_post_login_redirect`
   to an origin-only derivation **and** deleting `_set_refresh_cookie(...)` also
   leaves 2856/2856 green. The asymmetry is deliberate and spec'd (error = origin
   + `/oauth-error`; success = configured value *verbatim, path included*), and
   `_oauth_error_url` now pins only one half — a refactor unifying them breaks
   sign-in silently.

Skip topology (both are correct per `spec/TESTING.md §Assertion Discipline`, but
know which side runs): the spot module and E2E test 7 gate on whether the install
provisions Google credentials. `helm-charts/.env.dev.example` ships them **empty**,
so a fresh dev env runs the `OAUTH_NOT_CONFIGURED` pair; an install with real
creds runs only the `OAUTH_STATE_MISMATCH` callback test and skips E2E test 7 —
the only real-stack binding of the page to its producer. Driving
`GET /auth/google/callback?code=x&state=y` instead of `/login` makes that tie-in
unconditional: it 302s to `/oauth-error?error=<code>` on *both* configurations.

Related: [[dead-assert-tuple-ruff-blind]], [[auth-revoke-503-cookie-retention-framework-guaranteed]]
