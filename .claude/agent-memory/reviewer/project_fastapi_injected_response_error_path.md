---
name: fastapi-injected-response-error-path
description: FastAPI-injected Response header/cookie mutations are discarded when the endpoint raises — exception handlers return a fresh JSONResponse, so error paths never emit Set-Cookie
metadata:
  type: project
---

A route that takes `response: Response` (FastAPI-injected) and calls
`response.set_cookie` / `delete_cookie` only reaches the client if the endpoint
**returns**. FastAPI merges the sub-response headers into the real response
*after* `run_endpoint_function` returns; if the endpoint raises, that merge never
runs and the registered exception handler builds a fresh `JSONResponse`. Net
effect: **no `Set-Cookie` is emitted on any error path.**

Verified empirically (2026-07-17, issue #59 revoke fail-closed review) against the
real `create_app()` with `get_redis` overridden to raise `RedisError`:
`POST /auth/token/revoke` → `503 STORAGE_UNAVAILABLE`, `set-cookie: []`.

**Why:** this decides whether "fail-closed" is genuinely correct or only
superficially returns the right status. For revoke, the refresh token stays live
on Redis-down, so the cookie *must* be retained — and it is, doubly so (the
`delete_cookie` line is also after the `await` that raises).

**How to apply:** when reviewing any route that claims cookie/header behavior on
an error path, don't reason from the handler's status code alone — the injected
`Response` is a no-op once an exception escapes. Also note exception-handler
lookup walks `type(exc).__mro__`, so a specific handler
(`StorageUnavailableError` → 503) always beats a broader one
(`DataSpokeError` → 500) regardless of `add_exception_handler` registration
order. Related: [[project-revoke-swallows-storage-unavailable-bug]].
