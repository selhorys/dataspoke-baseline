---
name: spec-conformance-86-anchors
description: spec_conformance package anchors — verified route/error/time-range-param counts, the allowlist-citation gap, and why a green run proves declaration but never behaviour
metadata:
  type: project
---

`tests/unit/spec_conformance/` (issue #86 Phase 0) compares FastAPI routes and error codes
against `spec/API.md`. Independently verified at review time (2026-07-25):

- 124 catalogued registered routes == 124 `§Route Catalogue` rows, 0 drift both ways.
  134 walked leaves - 2 framework (`/openapi.json`, `/redoc`; `docs_url` is `None`) - 8
  `/internal/activities/*`. No normalisation collisions on either side, so the clean
  result is real, not collapse-induced.
- 57 `§Application Error Codes` rows; 7 `BACKEND.md §Exception-to-HTTP Mapping` rows;
  26 codes overlap. Only `BAD_REQUEST` + `INGESTION_DISABLED` drift.

**Why:** later #86 phases will re-run these comparisons; these are the baselines that
prove a future green run is not vacuous.

**How to apply:**
- `spec/TESTING.md` contains **no** allowlist rule — the string "allowlist" appears
  nowhere in it. Any `Spec: spec/TESTING.md §Assertion Discipline (allowlists are
  asserted in both directions)` citation is a citation-existence violation until the
  `spec` agent adds the bullet. See [[dead-assert-tuple-ruff-blind]] for the sibling
  case where TESTING.md *does* carry the rule but the linter cannot see it.
- Drift found but deliberately left untested by Phase 0, so it is NOT caught by any
  test: `EntityNotFoundError` raise sites use 15 entity types vs 8 in its docstring —
  `USER_NOT_FOUND`, `SEED_NOT_FOUND`, `METAGEN_{BOUNDARY,CANDIDATE,ITEM}_NOT_FOUND` are
  absent from API.md; so are the class defaults `NOTIFICATION_FAILED` /
  `EVENT_PROCESSING_FAILED`; and the `exceptions.py` **module** docstring mapping block
  is stale (unparsed by the checker — it reads ClassDefs only).

**Time-range param module (issue #90, 2026-07-26).** `test_time_range_params.py` adds the
first *query-param* check to this package (paths were the only axis before — that is how
`/spoke/metagen/event` shipped `after` instead of `from`/`to` under a green suite).
Independently verified: 14 in-scope routes (12 `event` + 2 `attr/.../result`), one
allowlist entry (`validation/result (missing: to)`, the `until` deviation, paired with a
test proving `until` is declared). Removing the two metagen aliases in memory makes the
main assertion fail — the guard is genuinely sensitive, and would have failed pre-fix.

**Its hard limit: declaration, not behaviour.** It proves a route *declares* a param, never
that the handler reads it — the same fix deleted a declared-but-unread `cursor`. So
"conformance is green" still says nothing about a filter working. The always-run seam for
that is the capturing-`get_db` override + `stmt.compile(dialect=postgresql.dialect())`
pattern in `tests/unit/api/routers/test_pagination_sort_sweep.py` (used there for
`ORDER BY`); it works for any router that builds its query inline, so "the behaviour needs
a cluster" is not a valid excuse for leaving a filter's semantics to unrun spot tests.

**Two rationale claims in `test_time_range_params.py` that do not hold (re-verified 2026-07-26).**
Its docstring says the §Route Catalogue rows "do not spell their query params out inline, so there
is nothing per-route to read" — **11 of 126 rows do**, including `API.md:266`, the very row its own
allowlist entry quotes for the `until` deviation. And "making the route rows individually parseable
is issue #87" over-claims: #87 is scoped to **response shapes** ("a structured response-field
representation"), not query params, so closing #87 will not remove this module's residual. The
design decision (apply the class-level rule, allowlist the documented deviation) is sound; only the
absolute phrasing is wrong.

Behavioural residual now partly closed — see [[metagen-from-to-test-seam]] for what the always-run
seam does and does not catch.
