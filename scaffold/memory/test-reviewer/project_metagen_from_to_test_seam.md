---
name: metagen-from-to-test-seam
description: Issue #90 from/to test seam — mutation-survival table for the compiled-SQL unit assertions, why no SQLite alternative exists, and the third unguarded inline-filter site
metadata:
  type: project
---

Issue #90 (`GET /spoke/metagen/{event,conf/{id}/event}` declared `after`, frontend sent
`from`/`to`, FastAPI dropped them, suite green throughout). The fix's always-run behavioural
seam is `tests/unit/api/routers/spoke/test_metagen.py::test_event_routes_filter_on_from_and_to_inclusively`
— capturing `get_db` override + `compiled_sql()` substring assertions on both the count and
rows statements.

**Independently mutation-tested (2026-07-26), full `tests/unit/` suite each time:**

| mutation on `src/api/routers/spoke/metagen.py` | caught |
|---|---|
| revert one route to `after` + `>` (pre-fix) | yes — only that route's parametrize leg; conformance names the route |
| `>=` → `>`; `<=` → `<`; `to_time` declared-but-unread | yes |
| window applied to rows query but not the count | yes (the loop over both statements is load-bearing) |
| **full operator swap** (`<= from_time`, `>= to_time`) | **NO — 2705 passed** |
| **omit-`to` clamps upper bound to `from`** | **NO — 2705 passed** |
| behaviour-preserving `.between(from, to)` refactor | fails (false positive; BETWEEN is inclusive) |

The two survivors are why the presence-anywhere form (`"occurred_at >=" in sql` + both dates
present *somewhere*) is not enough: only bound-adjacent substrings
(`"occurred_at >= '2024-03-01" in sql`) separate the operators from their bounds. A degenerate
`from == to` spot window cannot separate them either — the predicate collapses to equality.

**No SQLite alternative at this tier.** `[dependency-groups] dev` in `pyproject.toml` has no
`aiosqlite`, so `spec/TESTING.md §Unit Testing`'s "SQLite-backed session" option is unavailable
and the compiled-SQL text pin is the only always-run seam. Precedent:
`tests/unit/api/routers/test_pagination_sort_sweep.py:317-357` (same pattern, for `ORDER BY`).
Do not score the SQL-text pinning as a finding on its own — score the specific insensitivities.

**Third inline-filter site, unguarded.** `src/api/routers/spoke/common/data/metagen.py:213-216`
carries byte-identical `>=`/`<=` code for `GET /spoke/common/data/{urn}/event/metagen`. Making
both bounds declared-but-unread there — #90's exact failure mode — passes all 2705 unit tests,
while `test_time_range_params.py` classifies the route in-scope. Every other in-scope route
delegates `from_dt`/`to_dt` to a service, so these three inline sites are the whole risk surface.

Related: [[spec-conformance-86-anchors]], [[rangepicker-day-bounds-unspecced]]
