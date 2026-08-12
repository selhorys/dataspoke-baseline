---
name: audit-list-spans-concurrent-stage
description: A review prompt's audit list can name traps owned by a stage still being written — re-run git status and check mtimes before reporting (or excusing) them
metadata:
  type: feedback
---

When the launch prompt enumerates numbered traps, check which stage owns each one before auditing.
On concurrent stages (`backend ∥ frontend` in the req3 plan) the orchestrator may hand every
reviewer the same full trap list, most of which belongs to the sibling stage.

**Why:** on the req3 run I was launched as the Stage C (frontend) reviewer with an audit list that
was mostly Stage B backend traps (`_observed_at`, asyncpg 32767, the `... on Dataset` fragment).
`git status` at minute 0 showed no backend files changed at all; ten minutes later
`src/backend/ingestion/service.py`, `src/shared/datahub/client.py`,
`src/backend/metrics/measurers/ingestion_freshness.py` and
`src/api/routers/spoke/common/data/ingestion.py` were all modified, mtimes one minute old. Reporting
"those traps are unmet" from the minute-0 snapshot would have been flatly wrong, and reviewing the
minute-10 snapshot would have been reviewing a half-written file that has its own reviewer.

**How to apply:** run `git status --short` at the start *and* again before writing findings, and
`ls -lT` the files you are about to judge — an mtime inside your own session is the tell. Scope
findings to the files your generator's report claims, and say explicitly in the summary which audit
items you could not cover and why, so the orchestrator does not read silence as a pass. Cross-stage
consistency is still fair game and cheap: comparing the frontend's mirrored predicate against the
backend's landed `get_latest_run_event` took one grep.
Related: [[feedback_scratchpad_shared_with_parallel_agents]], [[feedback_isolate_failures_concurrent_edit]].
