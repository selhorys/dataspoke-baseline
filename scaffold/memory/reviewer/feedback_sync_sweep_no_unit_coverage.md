---
name: sync-sweep-no-unit-coverage
description: IngestionService.sync()'s ~400-line sweep BODY has no unit coverage — only its health side effect does (via a stubbed _run_sweep); a green tests/unit/ proves nothing about sweep changes
metadata:
  type: feedback
---

When a generator changes the sweep body inside `IngestionService.sync()` (the `datahub-sync-hourly`
sweep in `src/backend/ingestion/service.py`) and reports "full unit suite green", treat that as
**no evidence at all** for the changed code.

The one unit class that calls `.sync()` —
`tests/unit/backend/ingestion/test_service.py::TestSyncReportsApiHealth` — replaces `_run_sweep`
with a stub *and* replaces `_report_api_health` with a recorder, so it covers only the
status/re-raise/message contract of the health side effect. The sweep body and the reporter's own
internals are never executed. Every real driver is in `tests/integration/spot/`
(`test_datahub_api_health.py`, `test_ingestion_wrapper_linkage.py`,
`test_ingestion_cli_pipeline_inheritance.py`, `test_internal_activities.py`,
`test_ingestion_sources.py`) or `api_wired/`, all of which need a live cluster.

**Why:** the sweep is one ~400-line async method driving a real Postgres session with
`pg_insert(...).on_conflict_do_update(...)` and `rowcount` inspection. Mocking it usefully is
impractical, so nobody has. The pure helpers it calls (`build_matcher`, `has_selection_patterns`,
`parse_recipe` in `src/shared/models/ingestion.py`) *are* unit-covered, which makes a green run look
reassuring while the sweep body is untouched.

**How to apply:** when reviewing a sweep change, (a) verify the behaviour yourself with
`uv run python -` against the real code rather than trusting the suite — a ~20-line harness that
patches `report_peripheral_health` and inspects identities is usually enough, (b) reason through the
DB semantics by hand (ORM column types, ON CONFLICT WHERE guards, `rowcount` values), and (c) state
in the review that the only executable verification is the deferred spot test, so the human knows
what is still unproven before deploying. Related: [[asyncpg-str-uuid-column]],
[[feedback-exists-subquery-autocorrelate]], [[asyncsession-bind-seam]].

**Coupling worth re-checking on every sweep review:** when `build_matcher` degrades a source to
match-nothing (malformed regex, wrongly-shaped pattern value, SDK import failure), step 2's
stale-prune loop then deletes *every* stored `derivation='matched'` row for that source — the
degradation path and the prune path sit ~80 lines apart, so it reads as safe locally. Ask what the
sweep *deletes*, not just what it fails to add.

**Who writes the `datahub-api` singleton row:** the spot fixture `silence_api_health_report` patches
`IngestionService._report_api_health` itself (not `SessionLocal`), so it survives reporter-internals
changes. In-process sweep drivers are exactly the four spot modules above plus
`tests/integration/util/__main__.py::_datahub_sync`; `test_ingestion_owning_source.py` builds an
`IngestionService` but never calls `sync()`.
