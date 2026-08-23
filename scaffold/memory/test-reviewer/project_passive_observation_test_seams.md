---
name: passive-observation-test-seams
description: req3 (#154/#159/#160/#161/#162) passive-observation run — mutation-verified test seams across both review cycles; the spot-stub break the interface-violation re-raise causes (fixed); which predicates have zero always-run coverage; the shared-catalog-URN scoping trap in the spot summary-counts read-back
metadata:
  type: project
---

req3 passive-observation (`_observe_last_ingested`, `_observed_at`, `get_latest_run_event`,
`get_events_for_source(dataset_urn=)`, freshness two-tier, frontend `selectLatestRunEvent`).

**The one that bit, now fixed.** `_observe_last_ingested` re-raises `AttributeError` by
design (BACKEND.md §Best-Effort Operations: a duck-typed double missing the method must not
pass green). Four spot modules build exactly such a double and drive
`IngestionService.sync()`: `test_ingestion_wrapper_linkage.py`,
`test_ingestion_cli_pipeline_inheritance.py`, `test_internal_activities.py`,
`test_datahub_api_health.py`. All four now define `get_last_ingested`.
**Generalise:** whenever a sweep/service gains a *new client method*, grep
`tests/integration/**` for hand-written stub classes of that client — the AsyncMock-based
unit fixture auto-creates the attribute, so the whole Python unit tier is blind to it.
The 4 files are exactly those defining `async def list_ingestion_sources`; that grep is the
complete enumeration.

**Cycle-2 finding worth carrying: shared catalog URNs make an events read-back estate-wide.**
`test_internal_activities.py::test_sync_summary_counts_state_changes_not_rows_examined`
phases 9-10 read back `WHERE detail->>'source'='last_ingested_observation' AND
detail->>'dataset_urn' = ANY(:urns)` with `_SYNC_DS_A/_B` — which are byte-identical to
uc1_02's `_CATALOG_TITLE_URN`/`_CATALOG_EDITIONS_URN`, the one api-wired arc that books that
producer. `reset_ingestion_sources()` deletes `ingestion_source(_dataset)` only, **never
`events`**, so a spot re-run after api-wired (or after an aborted prior run) sees 4 rows.
Scope any `events` read-back/cleanup by `entity_id`, not by dataset URN + producer.

**Mutation results measured across both cycles.** KILLED at `uv run pytest tests/unit/`:
4c booking on CLI wrappers; 4c restricted to PASSIVE; producer term swapped in the written
detail *and* in the dedup read; the lastIngested dedup skip; interface violation swallowed;
sweep handing 4c an empty source list; observation booked as FAIL; freshness
tier-preference swap; the `evidence_tier` label; `scrollId` sent on the first page;
`_with_retry` bypassed (via `call_count == RETRY_MAX_ATTEMPTS`). Vitest: deleting
`select: selectLatestRunEvent`; whitelist dropped; blacklist null-safety inverted;
`find`→last-match; a single-producer blacklist.

**SURVIVED the whole unit suite (3141 passed) — spot-only by design, so Stage E's spot run
is load-bearing:** `get_events_for_source`'s dataset predicate removed entirely; its
`IS NULL` disjunct dropped; tier-2's dry-run exclusion removed; tier-1's
`detail->>'source'` producer filter removed. A fake session cannot prove a `WHERE` clause,
so this is correct routing — but #160-backend/#161/the dry-run fix have **zero** always-run
coverage.

**Well covered, do not re-flag:** `_observed_at` bounds (offsets are proportions of
`_OBSERVED_AT_MAX_SKEW`, never wall-clock); the four-term identity; the constant-bind-count
pin (`render_postcompile=True`, compares `counts[3] == counts[300]`, not a literal);
`dataset_urn` keyword-only via `inspect.signature` with a `sig.bind` backstop; the
`attr/ingestion` `get_events_for_source.assert_not_awaited()` wiring pin; `limit=1000` on
the list-view probe (FRONTEND_INGESTION.md spells out `limit` max `1000`); the 6-column
E2E header anchor (spec enumerates the six).

**`spec=` is not enough where tests assign the stub.** `AsyncMock(spec=IngestionService)`
survives a renamed service method because plain `spec` restricts *reads* only; every test in
`tests/unit/api/routers/spoke/common/data/test_ingestion.py` assigns
`mock_svc.get_latest_run_event = AsyncMock(...)`. `spec_set=` kills the mutant.

**Unanchored rule still:** the constant-bind-count / 32767 rule lives only in the plan and
the helper docstring — `grep -rn 32767 spec/` is empty. The test says so in a NOTE instead
of fabricating a citation; that is the right handling.

Related: [[run-id-filter-then-assert-tautology]], [[sync-sweep-counter-vacuity]],
[[dead-assert-tuple-ruff-blind]], [[feedback-review-method]].
