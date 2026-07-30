---
name: default-rate-limit-plane-enforcement
description: The default SlowAPI plane enforces via RouteResolvingSlowAPIMiddleware, bucket = (caller key, literal scope "global"); the caller half is attacker-chosen — any Bearer dsk_<random> mints a fresh bucket — so the whole default budget is bypassable with one header
metadata:
  type: project
---

`SlowAPIMiddleware` enforced **nothing** on any application route: FastAPI
0.138's lazy `include_router` puts `_IncludedRouter` objects in
`app.router.routes`, they carry no `.endpoint`, slowapi's `_find_route_handler`
returns `None`, and `_should_exempt(limiter, None)` is `True`.
`RouteResolvingSlowAPIMiddleware` (src/api/middleware/rate_limit.py) fixes that
by recursing through `_IncludedRouter.effective_candidates()`. 134 leaf routes,
0 unresolved static routes, 16 exempt.

Six durable properties to re-check on any rate-limit diff (verify empirically —
httpx `ASGITransport` + `DATASPOKE_RATE_LIMIT_PER_MINUTE=3`; reading the diff
will not tell you):

1. **Bucket = (caller key, "global").** `Limiter(application_limits=[...])`
   builds its `LimitGroup` with a literal `scope="global"`
   (slowapi extension.py:196-211), which `__evaluate_limits` uses in place of
   the endpoint key. `key_style="endpoint"` is still set but no longer selects
   the bucket. Measured: `/redoc` → [200,200,200,429,429], then 404 paths → 429
   immediately. Registered as `default_limits` instead, the scope falls back to
   the route function and one caller gets ~134 budgets.
2. **The caller key is the whole security model, and it is attacker-chosen.**
   `_get_user_key` order: `Bearer <dsk_…>` → `pat:sha256(token)[:32]`;
   `Bearer <jwt>` → verified `sub`; no Bearer + refresh cookie → verified `sub`;
   else `get_remote_address`. The PAT branch hashes the token **without
   verifying it exists**, so `Authorization: Bearer dsk_<random-per-request>`
   is a complete bypass — measured 8/8 `200` on `/redoc` after the same client's
   anonymous bucket was already `429`. Only signature-verified branches are
   safe. This also contradicts spec/API.md:999 and
   spec/feature/AUTH.md §Client-IP attribution, both of which say a request
   without an access token is bucketed by the observed address.
3. **The address branch collapses behind the ingress.** With
   `config.trustedProxyIps: "127.0.0.1"` (shipped default) uvicorn discards
   `X-Forwarded-For`, so every external caller is the nginx-ingress pod IP: one
   deployment-wide bucket on the default plane, and — since the fail-closed auth
   plane keys on the address *unconditionally* (`_get_client_ip_key`) — a
   deployment-wide 10/min login and 5/min register budget. 10 req/min from one
   host denies login to everyone.
4. **Exemptions are the security surface.** `DEFAULT_LIMIT_EXEMPT_PATHS` =
   `{"/health"}` (all three k8s probes target it — safe) and
   `DEFAULT_LIMIT_EXEMPT_PATH_PREFIXES` = `("/internal/",)` → 15 routes incl.
   `POST /internal/admin/bootstrap`. The module docstring justifies the second
   with "every caller arrives from one pod IP" — **false**: `api-ingress.yaml`
   publishes `path: /` `Prefix` on `api.<domain>`, so `/internal/*` is
   internet-reachable and now uncapped. `X-Internal-Token` (constant-time
   compare, 256-bit) is the only control.
5. **Unmatched paths ARE charged** (`_unmatched_route` sentinel replaced the
   old fail-open `None`), but the four `auth_route_limit` routes are
   `limiter.exempt`-ed from the middleware plane and check inside the endpoint,
   i.e. *after* FastAPI body validation — measured 15/15 `422` unmetered by
   both planes.
6. **Fail-open is the degradation mode.** `verify_route_resolution` now really
   resolves a probe (static route behind an included router, currently
   `GET /health`) instead of returning on the type check alone; it logs
   `rate_limit_route_resolution_unsupported` with reason
   `included_router_type_missing` / `probe_unconstructible` / `probe_unresolved`.

**Why:** the enforcement flip is invisible in a diff — it reads as a lookup
repair but changes every route's runtime behaviour — and every subsequent
regression in this file has been in the *key function*, not the plumbing.

Related: [[slowapi-blocking-storage-event-loop]],
[[forwarded-allow-ips-trust-radius]], [[auth-fail-closed-spans-layers]]
