---
name: dormant-middleware-blast-radius
description: When a diff repairs a middleware that was silently inert, enumerate every route class that becomes governed for the first time - probes, /internal machine callbacks, docs routes - and measure the per-request cost it adds
metadata:
  type: feedback
---

A change that makes a previously **inert** middleware actually fire is not a bug
fix with a small diff footprint — it is a behaviour change across the entire
route table. Review it that way.

**Why:** the slowapi rate-limit repair in this tree (FastAPI 0.138's lazy
`_IncludedRouter` made `SlowAPIMiddleware` exempt every `include_router` route)
was scoped as "fix the resolver + exempt `/health`". Exempting the k8s probes was
called out in the task; nothing else was. The repair silently put a 120/min
per-(IP, path) budget on `/internal/activities/*`, which Airflow DAGs fan out one
POST per source/metric through `HttpOperator.partial(...).expand(...)` — a 429
fails the task, and the default 3 retries at 10s all land inside the same
fixed window. It also moved a **synchronous** `limits.storage.RedisStorage` call
onto the event loop for every request; with Redis blackholed the first request
cost 4119 ms and later ones periodically 2013 ms (slowapi's backoff re-probe),
worker-wide, where previously no application route paid anything.

**How to apply:** when a diff activates dormant request-path machinery, run three
checks before scoring completeness.
1. Enumerate the route classes that were previously ungoverned and now are —
   walk `app.router.routes` recursively and print the govern/exempt decision per
   route, don't reason from the diff. Machine-to-machine control-plane routes
   (`/internal/*`) and infra probes are the two that bite.
2. Ask what the newly-live code costs per request. If it does blocking I/O
   inside an `async def dispatch`, measure it against an unreachable dependency
   (`10.255.255.1`) — the number is the whole worker's stall, not one request's.
3. Re-read the spec clauses that describe the feature. Claims that were
   unfalsifiable while the code was inert (here: API.md requiring
   `Retry-After` + `X-RateLimit-*` on the 429, and "per user" when slowapi's
   `key_style` defaults to `"url"`) become live violations the moment it fires.

Related: [[verify-branch-reachability-rationales]], [[spec-conformance-paths-only]]
