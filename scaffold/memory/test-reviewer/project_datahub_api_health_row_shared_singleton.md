---
name: datahub-api-health-row-shared-singleton
description: Every REST /internal/activities/ingestion/sync call makes the API pod write the datahub-api peripheral_health row, so a module-local snapshot/restore fixture cannot keep it clean
metadata:
  type: project
---

`IngestionService.sync()` reports `datahub-api` health as a side effect (#102), so **any**
caller moves that cluster-wide singleton row — including the ~9 pre-existing REST calls to
`POST /internal/activities/ingestion/sync` in
`tests/integration/spot/test_internal_activities.py`, which run inside the API pod where
`SessionLocal` resolves correctly.

Consequence observed: `tests/integration/spot/test_datahub_api_health.py`'s
`restored_api_health` fixture correctly restored the row to *absent*, then
`test_internal_activities` (later in the alphabet) left it at `status='ok'`. So the
fixture's guarantee is module-local only, and its `before` snapshot is
ordering-dependent rather than a stable "absent".

Corollary for **in-process** sweeps (`await service.sync()` driven directly from a spot
test): `_report_api_health` opens the module-level `src.shared.db.session.SessionLocal`,
which out of cluster resolves to `localhost:5432`. The connect failure is caught and
logged at ERROR with `exc_info=True`. It is invisible on a passing run (pytest only prints
captured logs for failures) but on a *failing* run it dominates the tail of the output and
masks the real assertion — it cost me one misdiagnosis during the #102/#103 review.

Promoting `DATASPOKE_DEV_POSTGRES_*` → `DATASPOKE_POSTGRES_*` in
`tests/integration/conftest.py::_promote_test_runtime_overrides` *would* work — verified
that `src.shared.db.session` is not in `sys.modules` after the conftest import block, so
its import-time engine construction happens after the promotion. But it would also let
three more sweep-driving spot modules write the row with no snapshot/restore. Prefer
patching `_report_api_health` to a no-op in the modules that do not own the row.

**How to apply:** for any new "side effect of running" health/telemetry row, ask which
*other* existing tests now trigger it, not just the new module.
