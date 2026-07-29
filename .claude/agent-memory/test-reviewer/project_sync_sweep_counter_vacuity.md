---
name: sync-sweep-counter-vacuity
description: Which IngestionService.sync() summary counters are reachable at which test tier, why pipeline_links is provably unreachable over REST, and the _StubDataHubForSync seam that makes the step-2 outcome table exact
metadata:
  type: project
---

Reviewing tests for `IngestionService.sync()` (`src/backend/ingestion/service.py`, the
`datahub-sync-hourly` sweep). None of this is visible from the test files.

**1. `pipeline_links` cannot be driven off zero over REST — verified in code, not assumed.**
The only REST path that stamps `systemMetadata.pipelineName` is an `ACTIVE_CUSTOM_MANAGED`
run. `extractors.py` appends a URN to `emitted_urns` only *after* emitting its aspects with
`sysmeta(pipelineName=source_id)`, and `service.py` then writes `derivation='emitted'` rows
for exactly that list. Step 3's upsert carries `where=(derivation != "emitted")`, so every
stamped URN's conflict is filtered → `rowcount == 0` → the counter never moves. A test agent
declining a "drive pipeline_links over REST" directive on this ground is **right**; the
counter belongs at the stub tier.

**2. Tier reachability of the §Sweep summary counters.**
- REST tier, non-zero on a first sweep: `datasets_mapped`, `sources_synced`,
  `sources_zero_coverage`, `sources_pattern_degraded`.
- REST tier, vacuously zero: `pipeline_links` (above), `registry_inserted` (every seeded URN
  is already registered), `events_mirrored` (dev DataHub has no executor to produce an
  execution request), `sources_removed`.
- Stub tier, all reachable: `_StubDataHubForSync` owns `list_ingestion_sources`,
  `enumerate_datasets`, `get_pipeline_names` **and** `list_execution_requests`, so
  `events_mirrored` (return one terminal request), `registry_inserted` (add a URN) and
  `sources_removed` (drop a source between sweeps) are each ~5 lines. A "cannot be reached"
  justification written for the REST tier does **not** transfer to the stub tier — check
  which tier the excuse was written for.

**3. The deterministic seam.** `tests/integration/spot/test_ingestion_wrapper_linkage.py`,
`test_ingestion_cli_pipeline_inheritance.py` and now `test_internal_activities.py` drive
`await service.sync()` directly with a stub. Step 2 derives *both* name and platform by
parsing the URN string, so a handcrafted URN list gives total control over the estate — enough
for **exact equalities** on estate-wide counters and for the branches REST cannot reach: the
"platform has no datasets" gate, the CLI-wrapper no-double-count rule, the pattern-less →
*pruned* row, and a two-segment (athena) name end-to-end.

**4. The stub sweep's side effect is pre-existing, not new.** `sync()` runs
`reconcile_registry` over the handcrafted URN set, soft-flagging every other
`dataset_registry` row `datahub_registered=false`; `reset_ingestion_sources()` empties
`ingestion_source`. `module_dummy_data` does **not** repair the registry. Only three spot
modules read `dataset_registry` — `test_admin.py`, `test_common_data_catalog.py`,
`test_internal_activities.py` — and all sort at or before `test_internal_activities`, so
nothing downstream is affected. The two `test_ingestion_*` modules already leave the same
state earlier in the alphabet.

**How to apply:** when a REST-tier sweep test asserts `second[counter] == 0`, demand a
`first[counter] > 0` backstop *or* the move to the stub tier. Related:
[[run-id-filter-then-assert-tautology]].
