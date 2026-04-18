"""Unit tests for the datahub-sync-daily DAG definition.

Tests cover:
- datahub-sync-daily is registered in _EXPECTED_DAGS in admin.py
- The DAG file exists in the dags/ directory
- The DAG file contains the correct dag_id string and expected structural markers

Note: airflow is not installed in the unit-test Python environment
(it runs in-cluster only). These tests verify the DAG file at the
source level rather than loading it through the Airflow DagBag.
"""

from __future__ import annotations

from pathlib import Path


_DAGS_DIR = Path(__file__).resolve().parents[3] / "src" / "workflows" / "dags"
_DAG_ID = "datahub-sync-daily"


# ---------------------------------------------------------------------------
# _EXPECTED_DAGS registration
# ---------------------------------------------------------------------------


def test_datahub_sync_daily_in_expected_dags():
    """datahub-sync-daily must appear in the _EXPECTED_DAGS frozenset so that
    /internal/admin/dags/verify flags it as missing when Airflow is not yet loaded."""
    from src.api.routers.admin import _EXPECTED_DAGS

    assert _DAG_ID in _EXPECTED_DAGS, (
        f"'{_DAG_ID}' not found in _EXPECTED_DAGS. "
        "Add it to _SYNC_DAGS in src/api/routers/admin.py."
    )


# ---------------------------------------------------------------------------
# DAG file structural checks (no Airflow import required)
# ---------------------------------------------------------------------------


def test_datahub_sync_daily_dag_file_exists():
    """The datahub_sync_daily.py DAG file must exist in the dags directory."""
    dag_file = _DAGS_DIR / "datahub_sync_daily.py"
    assert dag_file.is_file(), f"DAG file not found: {dag_file}"


def test_datahub_sync_daily_dag_id_string_present():
    """The DAG file must declare dag_id='datahub-sync-daily' (exact string)."""
    dag_file = _DAGS_DIR / "datahub_sync_daily.py"
    source = dag_file.read_text()
    assert f'dag_id="{_DAG_ID}"' in source or f"dag_id='{_DAG_ID}'" in source, (
        f"dag_id='{_DAG_ID}' not found in {dag_file.name}"
    )


def test_datahub_sync_daily_calls_sync_endpoint():
    """The DAG task must target the /internal/admin/datahub/sync endpoint."""
    dag_file = _DAGS_DIR / "datahub_sync_daily.py"
    source = dag_file.read_text()
    assert "/internal/admin/datahub/sync" in source, (
        "DAG task does not reference /internal/admin/datahub/sync"
    )


def test_datahub_sync_daily_is_singleton():
    """datahub-sync-daily is a singleton DAG (not in _PERIODIC_DAGS)."""
    from src.api.routers.admin import _PERIODIC_DAGS, _SYNC_DAGS

    # Must appear in _SYNC_DAGS
    assert _DAG_ID in _SYNC_DAGS, (
        f"'{_DAG_ID}' not in _SYNC_DAGS — verify admin.py DAG registration"
    )
    # Must NOT appear in _PERIODIC_DAGS (those are per-tier batch DAGs)
    assert _DAG_ID not in _PERIODIC_DAGS, (
        f"'{_DAG_ID}' erroneously added to _PERIODIC_DAGS"
    )
