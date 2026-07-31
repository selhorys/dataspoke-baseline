---
name: oauth-302-only-failure-surface
description: /auth/google/* 302 on every outcome (issue #83) — what all three review rounds closed, plus the three that survive: the deployment-wide 10/min sign-in bucket, hide_parameters, and uvicorn's access log carrying the callback query string
metadata:
  type: project
---

Issue #83 turned `GET /auth/google/login` and `GET /auth/google/callback` into
browser-redirect endpoints end to end: `302 <ui>/oauth-error?error=<code>` on
every failure the handler **body** produces, no JSON envelope. Contract:
spec/API.md §OAuth browser-redirect contract. Redirect target is
`urljoin(settings.oauth_post_login_redirect, "/oauth-error")` — env/ConfigMap
only (`config.oauthPostLoginRedirect`), never runtime-DB-settable.

**Closed — do not re-report:**

- `except Exception`, not `DataSpokeError`: authlib's OIDC discovery fetch and
  raw driver errors no longer escape to a 500.
- `_OAUTH_ERROR_CODES` frozenset allowlists the 5 spec'd codes; anything else
  redirects with **no** `error` param and logs at ERROR.
- `_handle_oauth_not_configured` (503) removed from `src/api/main.py`; safe —
  grepped, `OAuthNotConfiguredError` is raised only by these two routes.
- Frontend `/oauth-error` selects copy from a **`Map`** (was `Object.hasOwn` in
  an earlier draft), never echoes the param to the DOM.
- `exc_info=True` dropped from the `oauth_route_error` branch.
- **The caller-key bypass.** Both routes carry `@auth_route_limit("10/minute")`.
  Re-measured round 3 (httpx ASGITransport, real `create_app()`, `MemoryStorage`
  + `FixedWindowRateLimiter` swapped into both limiters): 10× 302 then 429 with a
  **fresh `Authorization: Bearer dsk_<random>` per request**, no `set-cookie` on
  any 302, 429 not swallowed into a redirect (the check runs in `guarded`,
  outside the handler's `try`), buckets per route (`/login` exhausted leaves
  `/callback` at 302).
- **The trace_id decoy.** `RequestLoggingMiddleware` publishes
  `request.state.trace_id`; `_request_actor` reads it back with a `""` fallback,
  never re-mints. Measured: `request_started`, `oauth_route_refused`,
  `request_finished` and the `X-Trace-Id` response header all carry one id.
- **Location injection via the config value.** Measured `_oauth_error_url` +
  `RedirectResponse` over `"...\r\nX-Injected: 1"` (urljoin strips the CRLF,
  Starlette percent-encodes the space), `"javascript:alert(1)"` (degrades to the
  relative `/oauth-error`), `"//evil/x"`, `"https://u:p@host/"`. No header split.
- **Log forging via `X-Trace-Id`.** Measured `X-Trace-Id: aaa error_code=SUCCESS
  path=/fake` — structlog's default ConsoleRenderer `repr()`s it, so it renders
  quoted and cannot forge sibling `key=value` fields. (There is no
  `structlog.configure` anywhere in `src/`; the default config is what ships.)

**Three that survive round 3 — re-check before re-reporting:**

1. **The 10/min bucket is deployment-wide, not per client.** `auth_limiter` keys
   on `get_remote_address`; shipped `config.trustedProxyIps: "127.0.0.1"` means
   uvicorn ignores `X-Forwarded-For`, so every external caller presents the
   ingress pod IP. Ten anonymous GETs/min to `/auth/google/login` — a route that
   accepts **no** credential and only 302s to Google — deny Google sign-in
   deployment-wide. Fixing the caller-key bypass is what exposed this; the remedy
   is a wider `trustedProxyIps` default or a separate budget for the
   credential-*less* login route, not a revert. Same shape for `/auth/token` (10)
   and `/auth/register` (5). Any "10/min is fine for a NAT'd office" argument is
   reasoning about an IP this plane never sees.
2. **`hide_parameters` is still absent** from `create_async_engine`
   (`src/shared/db/session.py:16`) — grepped, the only occurrence of the word in
   the repo is the *comment* in `auth.py` explaining the workaround. There is no
   `add_exception_handler(Exception, …)` in `main.py`, so any unhandled
   `DBAPIError` on any route reaches uvicorn as a full traceback with the failing
   statement's bind values. The OAuth path only side-steps it locally.
3. **uvicorn's access log defeats the handler-level scrub.** `docker-images/api/
   Dockerfile:28` is a bare `uvicorn …` (no `--no-access-log`), and uvicorn's
   `get_path_with_query_string` appends the query string, so every Google
   sign-in writes `GET /api/v1/auth/google/callback?state=…&code=4/0A…` to the
   same stdout the carefully-scrubbed `oauth_route_*` lines go to. The
   structlog middleware already logs every request (path only).

**Also still open:** `src/api/middleware/**` and `src/api/main.py` are *not* in
the security-reviewer glob list, yet this diff edited `logging.py` (what lands in
`request.state`), `rate_limit.py` (`_get_client_ip_key`, `auth_route_limit`,
`DEFAULT_LIMIT_EXEMPT_PATH_PREFIXES`) and `main.py` (removed an exception
handler; also holds CORS `allow_origins`, `SessionMiddleware` secret/`https_only`
and the rate-limit middleware registration). All three reached review only
because `src/api/routers/**` matched in the same diff.

`_error_json` (`src/api/main.py:124`) still re-derives `trace_id` from the header
only — measured: the 429 envelope carries `trace_id: ""` while its log line
carries the uuid. `request.state.trace_id` now exists to fix it.

`get_db` is `async with SessionLocal(): yield` — no teardown commit, which is
what makes the callback's swallow-and-redirect safe. If it ever gains one, every
route that catches its own error instead of re-raising becomes a partial-write
hazard.

Related: [[default-rate-limit-plane-enforcement]],
[[forwarded-allow-ips-trust-radius]], [[auth-credential-carrier-inventory]],
[[auth-fail-closed-spans-layers]], [[reviewer-config-is-generator-writable]]
