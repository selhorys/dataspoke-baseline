---
name: metagen-event-from-to-ignored
description: RESOLVED (issue #90) — metagen event routes now take from/to; the durable lesson is that a URL builder emitting a param never proves the server reads it
metadata:
  type: project
---

**Status: resolved.** Issue #90 Stage 1 replaced `after` with
`from_time`/`to_time` (`alias="from"`/`alias="to"`, inclusive `>=`/`<=`,
independent guards) on `get_metagen_conf_event` and `get_metagen_events` in
`src/api/routers/spoke/metagen.py`, and dropped a declared-but-unread `cursor`.
Both metagen range pickers are now genuinely server-filtered.

**Why it happened:** the routes declared `after`, the frontend
(`src/frontend/lib/api/metagen.ts`) built `?from=&to=`, and FastAPI silently
ignores unknown query params — so both range pickers were decorative while
presenting a working control. Surfaced during review of issue #89 (time-range
presets emitting a dead upper bound).

**How to apply:** the trap generalizes past metagen. When a plan or completion
report claims a filter/param works, read the *router signature*, not the URL
builder. A green frontend typecheck proves the client type tolerates the param,
never that the server reads it. Corollary for reviewers: an aliased param is
only reachable under its alias — dump `app.openapi()["paths"][p]["get"]
["parameters"]` to see the real wire names. This class of drift is invisible to
the repo's test suite — see [[spec-conformance-paths-only]].
Related: [[shared-response-model-unpopulated-field]].
