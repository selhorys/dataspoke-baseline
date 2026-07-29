---
name: sync-sweep-no-unit-coverage
description: IngestionService.sync() has zero unit coverage — a green `uv run pytest tests/unit/` proves nothing about sweep changes; only spot integration exercises it
metadata:
  type: feedback
---

When a generator changes `IngestionService.sync()` (the `datahub-sync-hourly` sweep in
`src/backend/ingestion/service.py`) and reports "full unit suite green", treat that as **no
evidence at all** for the changed code. Nothing under `tests/unit/` calls `.sync()`; every
caller is in `tests/integration/spot/` (`test_ingestion_wrapper_linkage.py`,
`test_ingestion_cli_pipeline_inheritance.py`, `test_ingestion_sources.py`) or `api_wired/`,
all of which need a live cluster.

**Why:** the sweep is one ~400-line async method that drives a real Postgres session with
`pg_insert(...).on_conflict_do_update(...)` and `rowcount` inspection. Mocking it usefully is
impractical, so nobody has. The pure helpers it calls (`build_matcher`,
`has_selection_patterns`, `parse_recipe` in `src/shared/models/ingestion.py`) *are*
unit-covered, which makes a green run look reassuring while the sweep body is untouched.

**How to apply:** when reviewing a sweep change, (a) verify the pure-helper behaviour yourself
with `uv run python -` against the real code rather than trusting the suite, (b) reason through
the DB semantics by hand (ORM column types, ON CONFLICT WHERE guards, `rowcount` values), and
(c) state in the review that the only executable verification is the deferred spot test, so the
human knows what is still unproven before deploying. Related: [[asyncpg-str-uuid-column]],
[[exists-subquery-autocorrelate]] — both are DB-shape bugs that unit mocks cannot catch.

**Coupling worth re-checking on every sweep review:** when `build_matcher` degrades a source to
match-nothing (malformed regex, wrongly-shaped pattern value, SDK import failure), step 2's
stale-prune loop then deletes *every* stored `derivation='matched'` row for that source — the
degradation path and the prune path sit ~80 lines apart, so it reads as safe locally. Ask what the
sweep *deletes*, not just what it fails to add.
