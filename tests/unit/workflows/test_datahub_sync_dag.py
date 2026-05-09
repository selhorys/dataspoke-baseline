"""Unit tests for the datahub-sync-daily DAG definition.

Tests cover:
- datahub-sync-daily is registered in _EXPECTED_DAGS in admin.py
- The DAG file exists in the dags/ directory
- The DAG file contains the correct dag_id string and expected structural markers
- ALL_DAG_IDS exactly matches the 17 DAG IDs from spec/feature/BACKEND.md §DAG Catalogue

Note: airflow is not installed in the unit-test Python environment
(it runs in-cluster only). These tests verify the DAG file at the
source level rather than loading it through the Airflow DagBag.
"""
# spec: BACKEND.md §DAG Catalogue

from __future__ import annotations

from pathlib import Path

_DAGS_DIR = Path(__file__).resolve().parents[3] / "src" / "workflows" / "dags"
_DAG_ID = "datahub-sync-daily"

# Exact set of 17 DAG IDs from spec/feature/BACKEND.md §DAG Catalogue.
# Validation no longer has scheduled DAGs — DataSpoke validation is a passive
# result-store; external pipelines POST results on their own schedule.
# 4 ingestion (3 active tier + 1 passive hourly)
# 3 metrics tier (hourly/daily/weekly)
# 3 metagen tier (hourly/daily/weekly)
# 3 ontogen tier (hourly/daily/weekly)
# 3 on-demand (metagen, metrics, ontogen)
# 1 sync (datahub-sync-daily)
# Total = 17
_EXPECTED_ALL_DAG_IDS: frozenset[str] = frozenset({
    # Ingestion — active scheduled tiers
    "ingestion-active-hourly",
    "ingestion-active-daily",
    "ingestion-active-weekly",
    # Ingestion — passive sync
    "ingestion-passive-hourly",
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
    # On-demand (API-triggered)
    "metagen",
    "metrics",
    "ontogen",
    # Sync
    "datahub-sync-daily",
})


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
    """The DAG file must reference the 'datahub-sync-daily' ID (literal or via variable)."""
    dag_file = _DAGS_DIR / "datahub_sync_daily.py"
    source = dag_file.read_text()
    # The ID may appear as a literal or assigned to _DAG_ID then referenced via dag_id=_DAG_ID.
    id_present = (
        f'"{_DAG_ID}"' in source
        or f"'{_DAG_ID}'" in source
    )
    assert id_present, (
        f"'{_DAG_ID}' string not found anywhere in {dag_file.name}"
    )


def test_datahub_sync_daily_calls_sync_endpoint():
    """The DAG task must target the /internal/activities/datahub/sync endpoint."""
    dag_file = _DAGS_DIR / "datahub_sync_daily.py"
    source = dag_file.read_text()
    assert "/internal/activities/datahub/sync" in source, (
        "DAG task does not reference /internal/activities/datahub/sync"
    )


def test_datahub_sync_daily_is_in_sync_dag_ids():
    """datahub-sync-daily must be registered in SYNC_DAG_IDS and ALL_DAG_IDS."""
    from src.workflows.registry import ALL_DAG_IDS, SYNC_DAG_IDS

    assert _DAG_ID in SYNC_DAG_IDS, (
        f"'{_DAG_ID}' not in SYNC_DAG_IDS — verify src/workflows/registry.py"
    )
    assert _DAG_ID in ALL_DAG_IDS, (
        f"'{_DAG_ID}' not in ALL_DAG_IDS — verify src/workflows/registry.py"
    )


# ---------------------------------------------------------------------------
# F1: ALL_DAG_IDS exhaustiveness check against spec catalogue
# ---------------------------------------------------------------------------


def test_all_dag_ids_exactly_matches_spec_catalogue():
    """ALL_DAG_IDS must be set-equal to the 21 DAG IDs enumerated in
    spec/feature/BACKEND.md §DAG Catalogue.

    This test prevents silent drift — a DAG added to the registry without a
    corresponding spec entry (or vice-versa) will be caught here.

    spec: BACKEND.md §DAG Catalogue
    """
    # spec: BACKEND.md §DAG Catalogue — 21 DAGs total across all tiers and modes
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
