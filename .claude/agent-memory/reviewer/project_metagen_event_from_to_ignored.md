---
name: metagen-event-from-to-ignored
description: /spoke/metagen/event and /spoke/metagen/conf/{id}/event accept `after`, not `from`/`to` — the frontend sends from/to and FastAPI silently drops them
metadata:
  type: project
---

The two cross-conf/per-conf metagen event routes in
`src/api/routers/spoke/metagen.py` (`get_metagen_conf_event`,
`get_metagen_events`) declare `event_type`, `after`, `limit`, `offset`,
`sort` — **no** `from` / `to`. Every other event/result route in the repo
declares `to_time: datetime | None = Query(default=None, alias="to")`.

`src/frontend/lib/api/metagen.ts` (`useMetagenConfEvents`, `useMetagenEvents`)
builds `?from=&to=` and never sends `after`. FastAPI ignores unknown query
params, so both metagen range pickers are **decorative** — the panels always
return the full unfiltered feed.

`spec/API.md` §Query Parameters says `from`/`to` are "used on `result` and
`event` endpoints", so spec and impl disagree here.

**Why:** surfaced while reviewing issue #89 (time-range presets emitting a dead
upper bound). The plan classified both metagen pages as "acute — dead on
arrival"; they were never server-filtered at all, so the bug never applied
there and the fix is a no-op for them.

**How to apply:** when reviewing anything that claims a range filter works on a
metagen event feed, or when a plan enumerates `resolveRange` call sites, check
the router signature — do not assume `from`/`to` exist just because the URL
builder emits them. Same trap for any endpoint: a green typecheck proves the
*client* type tolerates the param, never that the *server* reads it.
Related: [[shared-response-model-unpopulated-field]].
