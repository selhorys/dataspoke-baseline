---
name: module-dummy-data-registry-reconcile
description: How dataset_registry gets provisioned for integration modules — the pre-existing _mark_registry_registered UPDATE vs the new full-sweep reconcile, and the _PROVISIONED_BASELINE skip-guard gap that leaves it stale
metadata:
  type: project
---

`tests/integration/conftest.py::module_dummy_data` now calls
`tests/integration/util/datahub.py::sync_dataset_registry()` (a full
`IngestionService.sync()`) after its DataHub ingest legs. Facts measured while reviewing
that change — none visible from the diff.

**1. Two independent registry writers in the fixture path, not one.**
`ingest_pg_datasets` / `ingest_kafka_datasets` **already** end with
`datahub.py::_mark_registry_registered(urns)` — a raw
`UPDATE dataset_registry SET datahub_registered=TRUE … WHERE dataset_urn = ANY($1) AND
datahub_registered = FALSE`. It **updates existing rows only; it never inserts**.
Measured: flip `customers.eu_profiles` to `datahub_registered=false`, no-op
`sync_dataset_registry`, run `test_metagen_views.py::test_metagen_covered_datasets_view`
→ **still green**, because the ingest leg repairs the flag. So the reconcile's
load-bearing contribution is *not* the flag — it is (a) INSERTing rows for URNs with no
row at all (the `--reset-all` TRUNCATE state; `--reset-all` does **not** run
`_datahub_sync`, only `--reset-seed` does) and (b) the step-3 attribute columns
(`tag_urns`/`origin`/`is_primary`/`attrs_synced_at`) that tag- and origin-predicates
resolve against. Any docstring saying the reconcile is what makes a just-ingested URN
resolve is imprecise; say "inserts the absent row / refreshes the attribute columns".

**2. `dataset_registry` is NOT solely written by the sweep.** Three writers:
`reconcile_registry` (the sweep), `ensure_dataset_registered` (`src/backend/validation/
service.py:142`, on validation-conf PUT — inserts), `sync_with_datahub`
(`src/api/routers/admin.py:179`, the targeted admin endpoint). Reject any "sole writer of
dataset_registry" claim.

**3. The `_PROVISIONED_BASELINE` skip guard does not cover the registry.** Its stated
invariant (`conftest.py:529-536`) is scoped to the *source* stores — "no test body mutates
the source example-postgres/Kafka, hard-deletes the example DataHub datasets, or perturbs
their core aspects". Test bodies **do** mutate `dataset_registry`: a stub sweep with a
handcrafted `enumerate_datasets` soft-flags every non-enumerated row false and commits
(step 2b). Concrete spot exposure (collection order verified):
`test_ingestion_cli_pipeline_inheritance` (2 catalog URNs) poisons →
{freshness_evidence, owning_source, run_event_and_timeline} get no repair;
`test_internal_activities` (`_SYNC_DS_A/_B` = catalog.title_master/editions) poisons →
{test_metagen_conf, test_metagen_review, test_metagen_run} share requirement
`({catalog},…)` with the standing baseline, so they skip provisioning entirely. They pass
only because their `_TEST_URN`/`_TEST_URN2` happen to be exactly those two stub URNs.
`test_ingestion_wrapper_linkage`'s stub returns `[]` and `reconcile_registry` skips the
deregister pass on empty input, so it is harmless.

**4. Blast radius of a full sweep inside a module fixture.** It also writes
`ingestion_source` (mirror + stale removal), `ingestion_source_dataset`, INGESTION
`events`, and the shared `datahub-api` `peripheral_health` row — confirmed by reading the
row after a fixture run (`last_ok_at` stamped by the fixture, `_report_api_health` binds to
the injected session's engine so the host-side write **succeeds**). Contained today only
because the `test_ingestion_*` modules call `dataspoke_db.reset_ingestion_sources()` at
their own start. Related: [[datahub-api-health-row-shared-singleton]],
[[sync-sweep-counter-vacuity]].

**How to apply:** when a provisioning fixture grows a new store, check whether the skip
guard's *stated* invariant was extended to that store — and whether a cheaper targeted
helper (here: making `_mark_registry_registered` an UPSERT) covers the actual gap.
