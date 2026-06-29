---
name: admin-dags-spot-real-airflow
description: admin/dags schedule-control spot tests legitimately drive REAL Airflow — exception to spot-is-stub-only; don't flag
metadata:
  type: project
---

The `/admin/dags` schedule-control spot suite (`tests/integration/spot/test_admin_dags_schedule.py`)
drives REAL Airflow via the `airflow_client` fixture (real `get_dag_paused_states`/`set_dag_paused`,
asserts live `is_paused` flips), not a stub.

**Why:** this contradicts the general spot-is-stub-only rule, but `/admin/dags` is operational
schedule control with no LLM/stub dimension and is not part of any USE_CASE pipeline, so it cannot
live in api-wired (which mirrors UC stories). The 503 `AIRFLOW_UNAVAILABLE` failure mode (which
needs Airflow down) is instead covered by `tests/unit/backend/admin/test_dag_control_service.py`
injecting a fake `AirflowClient` that raises `httpx.ConnectError`/`ReadTimeout`.

**How to apply:** when reviewing admin/dags (or similar no-pipeline admin) tests, do NOT flag the
real-Airflow spot placement or the absence of an api-wired test as a coverage gap — the spot suite
"alone owns it." The fold rule (`paused` ⟺ all members paused; `mixed` ⟺ some-but-not-all) and error
codes anchor to spec/API.md L580-585 + L895/L932 and spec/feature/BACKEND.md §Schedule Control.
