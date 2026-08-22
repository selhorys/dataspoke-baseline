---
name: slowapi-bucket-scope
description: slowapi key_style="endpoint" buckets per (caller, route fn) — it does NOT give the per-user budget API.md documents; application_limits is the global-scope knob, and unmatched paths are never charged
metadata:
  type: project
---

slowapi computes its bucket key in `__evaluate_limits` as
`args = [key_func(request), lim.scope or _endpoint_key]`, where `_endpoint_key` is the
request path under `key_style="url"` (the default) and the endpoint function name under
`key_style="endpoint"`. **Neither gives one budget per caller.** Only
`Limiter(application_limits=[...])` builds its `LimitGroup` with a literal
`scope="global"` (`slowapi/extension.py:196-211`), collapsing every route into one bucket
per key — and application limits are evaluated only when `in_middleware=True`.

Two residual holes survive even a correct `application_limits` wiring, both in
`slowapi/middleware.py`:

- `_should_exempt(limiter, handler)` returns **True when `handler is None`**, so any
  request that resolves to no endpoint (every 404) is charged nothing. Verified 2026-07-30:
  with the budget at 3, eight `GET /api/v1/does-not-exist` all returned 404 and a following
  `/redoc` still had its full 3.
- Routes passed through `limiter.exempt(...)` skip the application limit too — the
  `endpoint_func_name in self._exempt_routes` guard returns before limits are assembled.

**Why:** `spec/API.md` §Middleware Stack and `spec/feature/AUTH.md` both say the default
limit is "120 req/min **per user**". Wiring it through `default_limits` +
`key_style="endpoint"` silently multiplies that by the endpoint count (134 leaf routes in
this app ⇒ ~16k req/min per caller). The divergence is invisible in a single-route test;
it only shows when you burst two different endpoints and watch both get a full budget.

**How to apply:** whenever a change touches `key_style`, `default_limits`,
`application_limits`, `exempt`, or `scope=` on a limit, burst *two distinct endpoints* plus
*one unmatched path* from one caller and confirm the bucket behaviour against the spec
wording. Deviating from a priority-1 API.md statement needs explicit user authorization —
making the code match the spec does not.

Related: [[xff-trust-radius-rate-limit]], [[offload-fix-all-callsites]]
