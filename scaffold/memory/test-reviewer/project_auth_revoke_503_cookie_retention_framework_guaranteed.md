---
name: auth-revoke-503-cookie-retention-framework-guaranteed
description: The revoke-503 "cookie retained" assertion is guaranteed by FastAPI (injected Response mutations are discarded when the endpoint raises), not by the impl — near-tautological
metadata:
  type: project
---

`tests/unit/api/routers/test_auth_routes.py::test_revoke_redis_unreachable_retains_refresh_cookie`
asserts `resp.headers.get("set-cookie") is None` on the 503 path.

That outcome is **structurally guaranteed by FastAPI**: header/cookie mutations on an
injected `response: Response` are merged only after the endpoint *returns*. If it raises,
the exception handler builds a fresh `JSONResponse` and no `Set-Cookie` is ever emitted.
So a deliberately fail-open impl (`except StorageUnavailableError: response.delete_cookie(...); raise`)
would **also** pass this test.

**Why:** the test's real bug-detection power comes entirely from its `assert resp.status_code == 503`
backstop (which duplicates the preceding test), not from the cookie assertion. It only
discriminates a variant that returns an explicit `JSONResponse(503)` + delete_cookie.
Verified empirically by the code reviewer, see
`.claude/agent-memory/reviewer/project_fastapi_injected_response_error_path.md`.

**How to apply:** treat "no Set-Cookie on an error path" assertions in FastAPI as
characterization, not as proof the impl retains a cookie. The load-bearing protection for
this invariant is a **spec line** in AUTH.md §Refresh & revoke, not the test — if revoke is
ever refactored to return an explicit Response, nothing in spec or test forbids clearing.
Related: [[auth-revoke-204-vs-401-unspecced]].
