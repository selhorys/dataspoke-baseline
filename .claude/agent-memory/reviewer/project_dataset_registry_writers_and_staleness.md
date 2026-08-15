---
name: dataset-registry-writers-and-staleness
description: dataset_filter scope = dataset_registry rows with datahub_registered=true, and "as fresh as the last sweep" is SPECIFIED (API.md priority 1) — four writers, only the sweep inserts estate rows
metadata:
  type: project
---

"A dataset in DataHub but absent from `dataset_registry` resolves to empty scope" is **specified
behaviour, not a bug**. The priority-1 anchor is `spec/API.md` §`dataset_filter` grammar:
"Resolution is a DataSpoke-side SQL query, not a DataHub search, so a filter's scope is as fresh as
the last attribute sweep." `spec/ARCHITECTURE.md` §Cross-cutting invariants repeats it ("as fresh
as the last sweep"), and `spec/feature/BACKEND.md` §Dataset scope resolution says the query is "run
as one query restricted to `datahub_registered = true`". Convergence is the 2-hourly
`datahub-sync-hourly` DAG; there is no public run-now route (`/admin/dags` is pause/unpause only).

Four writers of `dataset_registry`, and the division of labour is specified in
`spec/feature/BACKEND_SCHEMA.md` §`dataset_registry` — "Creation / reconcile: bulk, by the
`datahub-sync-hourly` sweep … Additionally lazy, via `ensure_dataset_registered()` on
validation-config upsert":
1. `IngestionService.sync()` → `reconcile_registry` (INSERTs estate URNs) + `upsert_dataset_attributes`
   (the ONLY writer of `tag_urns` / `glossary_term_urns` / `origin` / `platform_urn` / `is_primary` /
   `attrs_synced_at`).
2. `ValidationService.upsert_config` → `ensure_dataset_registered` — inserts ONE bare row, and it
   *does* insert with `datahub_registered=True` when DataHub holds the URN, so "only the sweep
   inserts rows DataHub holds" is false as an unqualified claim.
3. `POST /internal/admin/datahub/sync` → `sync_with_datahub` — flag flips only, "No new rows are
   inserted".
4. test-only `tests/integration/util/datahub.py::_mark_registry_registered` — UPDATE, never INSERT.

**Why:** the asymmetry (validation registers a URN, metagen does not) reads like a product
inconsistency and is not — validation has the `422 DATASET_NOT_IN_DATAHUB` precondition gate that
needs per-dataset precision; a metagen `dataset_filter` literal matching nothing is expected to
surface in the run-complete event's `unresolved_urns`.

**How to apply:** reject "the registry should auto-register on any endpoint taking a
`dataset_urn`" proposals, and treat a test that provisions scope by running the real
`IngestionService.sync()` as production-faithful rather than a hack. Related:
[[sync-sweep-no-unit-coverage]].
