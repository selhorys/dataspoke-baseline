"""Unit tests for the datahub-sync-hourly DAG definition.

Tests cover:
- datahub-sync-hourly is registered in _EXPECTED_DAGS in admin.py
- The DAG file exists in the dags/ directory
- The DAG file contains the correct dag_id string and expected structural markers
- ALL_DAG_IDS exactly matches the 15 DAG IDs from spec/feature/BACKEND.md §DAG Catalogue

Note: airflow is not installed in the unit-test Python environment
(it runs in-cluster only). These tests verify the DAG file at the
source level rather than loading it through the Airflow DagBag.
"""
# spec: BACKEND.md §DAG Catalogue

from __future__ import annotations

from pathlib import Path

_DAGS_DIR = Path(__file__).resolve().parents[4] / "src" / "workflows" / "dags"
_DAG_ID = "datahub-sync-hourly"

# Exact set of 15 DAG IDs from spec/feature/BACKEND.md §DAG Catalogue.
# 4 ingestion (3 active tier + 1 sync hourly)
# 3 metrics tier (hourly/daily/weekly)
# 3 metagen tier (hourly/daily/weekly)
# 3 ontogen tier (hourly/daily/weekly)
# 1 on-demand (metrics)
# 1 sync (auth-role-sync-daily)
# Total = 15
_EXPECTED_ALL_DAG_IDS: frozenset[str] = frozenset({
    # Ingestion — active scheduled tiers
    "ingestion-active-hourly",
    "ingestion-active-daily",
    "ingestion-active-weekly",
    # Ingestion — sync sweep (consolidated DataHub reconciliation)
    "datahub-sync-hourly",
    # Metrics — scheduled tiers
    "metrics-hourly",
    "metrics-daily",
    "metrics-weekly",
    # Metagen — scheduled tiers
    "metagen-hourly",
    "metagen-daily",
    "metagen-weekly",
    # Ontogen — scheduled tiers
    "ontogen-hourly",
    "ontogen-daily",
    "ontogen-weekly",
    # On-demand (API-triggered Airflow DAGs)
    "metrics",
    # Sync
    "auth-role-sync-daily",
})


# ---------------------------------------------------------------------------
# _EXPECTED_DAGS registration
# ---------------------------------------------------------------------------


def test_datahub_sync_hourly_in_expected_dags():
    """datahub-sync-hourly must appear in the _EXPECTED_DAGS frozenset so that
    /internal/admin/dags/verify flags it as missing when Airflow is not yet loaded."""
    from src.api.routers.admin import _EXPECTED_DAGS

    assert _DAG_ID in _EXPECTED_DAGS, (
        f"'{_DAG_ID}' not found in _EXPECTED_DAGS. "
        "Add it to src/api/routers/admin.py."
    )


# ---------------------------------------------------------------------------
# DAG file structural checks (no Airflow import required)
# ---------------------------------------------------------------------------


def test_datahub_sync_hourly_dag_file_exists():
    """The datahub_sync_hourly.py DAG file must exist in the dags directory."""
    dag_file = _DAGS_DIR / "datahub_sync_hourly.py"
    assert dag_file.is_file(), f"DAG file not found: {dag_file}"


def test_datahub_sync_hourly_dag_id_string_present():
    """The DAG file must reference the 'datahub-sync-hourly' ID (literal or via variable)."""
    dag_file = _DAGS_DIR / "datahub_sync_hourly.py"
    source = dag_file.read_text()
    id_present = (
        f'"{_DAG_ID}"' in source
        or f"'{_DAG_ID}'" in source
    )
    assert id_present, (
        f"'{_DAG_ID}' string not found anywhere in {dag_file.name}"
    )


def test_datahub_sync_hourly_calls_ingestion_sync_endpoint():
    """The DAG task must target the /internal/activities/ingestion/sync endpoint."""
    dag_file = _DAGS_DIR / "datahub_sync_hourly.py"
    source = dag_file.read_text()
    assert "/internal/activities/ingestion/sync" in source, (
        "DAG task does not reference /internal/activities/ingestion/sync"
    )


def test_datahub_sync_hourly_is_in_ingestion_sync_dag_ids():
    """datahub-sync-hourly must be registered in INGESTION_SYNC_DAG_IDS and ALL_DAG_IDS."""
    from src.workflows.registry import ALL_DAG_IDS, INGESTION_SYNC_DAG_IDS

    assert _DAG_ID in INGESTION_SYNC_DAG_IDS, (
        f"'{_DAG_ID}' not in INGESTION_SYNC_DAG_IDS — verify src/workflows/registry.py"
    )
    assert _DAG_ID in ALL_DAG_IDS, (
        f"'{_DAG_ID}' not in ALL_DAG_IDS — verify src/workflows/registry.py"
    )


# ---------------------------------------------------------------------------
# F1: ALL_DAG_IDS exhaustiveness check against spec catalogue
# ---------------------------------------------------------------------------


def test_all_dag_ids_exactly_matches_spec_catalogue():
    """ALL_DAG_IDS must be set-equal to the 15 DAG IDs enumerated in
    spec/feature/BACKEND.md §DAG Catalogue.

    This test prevents silent drift — a DAG added to the registry without a
    corresponding spec entry (or vice-versa) will be caught here.

    spec: BACKEND.md §DAG Catalogue
    """
    # spec: BACKEND.md §DAG Catalogue — 15 DAGs total across all tiers and modes
    from src.workflows.registry import ALL_DAG_IDS

    extra_in_impl = ALL_DAG_IDS - _EXPECTED_ALL_DAG_IDS
    missing_from_impl = _EXPECTED_ALL_DAG_IDS - ALL_DAG_IDS

    assert not extra_in_impl, (
        f"ALL_DAG_IDS contains DAG IDs not in spec catalogue: {extra_in_impl}. "
        "Add them to spec/feature/BACKEND.md §DAG Catalogue or remove from registry."
    )
    assert not missing_from_impl, (
        f"ALL_DAG_IDS is missing DAG IDs from spec catalogue: {missing_from_impl}. "
        "Add them to src/workflows/registry.py or remove from spec."
    )
