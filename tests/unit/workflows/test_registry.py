"""Unit tests for the workflow DAG registry (registry.py).

Pins the exact set of DAG IDs per spec/feature/BACKEND.md §DAG Catalogue.
Also verifies tier structure for tier-based DAG groups.

spec: feature/BACKEND.md §DAG Catalogue — 15 DAGs total:
  4 ingestion (3 active tier + 1 sync hourly)
  3 metrics tier (hourly/daily/weekly)
  3 metagen tier (hourly/daily/weekly)
  3 ontogen tier (hourly/daily/weekly)
  1 on-demand (metrics)
  1 sync (auth-role-sync-daily)
"""


from src.workflows.registry import (
    ALL_DAG_IDS,
    INGESTION_ACTIVE_DAG_IDS,
    INGESTION_SYNC_DAG_IDS,
    METAGEN_TIER_DAG_IDS,
    METRICS_TIER_DAG_IDS,
    ON_DEMAND_DAG_IDS,
    ONTOGEN_TIER_DAG_IDS,
    SYNC_DAG_IDS,
)

# Exact set per spec/feature/BACKEND.md §DAG Catalogue.
_SPEC_ALL_DAG_IDS: frozenset[str] = frozenset({
    # Ingestion — active scheduled tiers
    "ingestion-active-hourly",
    "ingestion-active-daily",
    "ingestion-active-weekly",
    # Ingestion — consolidated DataHub sync sweep
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

_TIERS = ("hourly", "daily", "weekly")


# ── ALL_DAG_IDS exhaustiveness ────────────────────────────────────────────────


def test_all_dag_ids_exact_match_spec_catalogue() -> None:
    """ALL_DAG_IDS must equal the 15 DAG IDs enumerated in spec/feature/BACKEND.md §DAG Catalogue.

    spec: feature/BACKEND.md §DAG Catalogue.
    """
    extra = ALL_DAG_IDS - _SPEC_ALL_DAG_IDS
    missing = _SPEC_ALL_DAG_IDS - ALL_DAG_IDS
    assert not extra, (
        f"ALL_DAG_IDS contains IDs not in spec: {extra}. "
        "Remove from registry or add to spec."
    )
    assert not missing, (
        f"ALL_DAG_IDS is missing IDs from spec: {missing}. "
        "Add to registry or remove from spec."
    )


def test_all_dag_ids_is_frozenset() -> None:
    """ALL_DAG_IDS must be a frozenset (immutable).

    spec: feature/BACKEND.md §DAG Catalogue — registry is the SSOT.
    """
    assert isinstance(ALL_DAG_IDS, frozenset)


def test_all_dag_ids_has_exactly_15_entries() -> None:
    """ALL_DAG_IDS must have exactly 15 entries per spec.

    spec: feature/BACKEND.md §DAG Catalogue.
    """
    assert len(ALL_DAG_IDS) == 15, (
        f"Expected 15 DAG IDs, got {len(ALL_DAG_IDS)}. "
        f"ALL_DAG_IDS: {sorted(ALL_DAG_IDS)}"
    )


# ── INGESTION_ACTIVE_DAG_IDS ──────────────────────────────────────────────────


def test_ingestion_active_dag_ids_cover_all_tiers() -> None:
    """INGESTION_ACTIVE_DAG_IDS must contain one entry per tier (hourly/daily/weekly).

    spec: feature/BACKEND.md §DAG Catalogue — ingestion active: one DAG per tier.
    """
    for tier in _TIERS:
        expected = f"ingestion-active-{tier}"
        assert expected in INGESTION_ACTIVE_DAG_IDS, (
            f"'{expected}' missing from INGESTION_ACTIVE_DAG_IDS."
        )


def test_ingestion_active_dag_ids_count() -> None:
    """INGESTION_ACTIVE_DAG_IDS must have exactly 3 entries (one per tier).

    spec: feature/BACKEND.md §DAG Catalogue.
    """
    assert len(INGESTION_ACTIVE_DAG_IDS) == 3


# ── INGESTION_SYNC_DAG_IDS ────────────────────────────────────────────────────


def test_ingestion_sync_dag_ids_contains_datahub_sync_hourly() -> None:
    """INGESTION_SYNC_DAG_IDS must contain datahub-sync-hourly.

    spec: feature/BACKEND.md §DAG Catalogue — consolidated DataHub sync sweep runs hourly.
    """
    assert "datahub-sync-hourly" in INGESTION_SYNC_DAG_IDS


def test_ingestion_sync_dag_ids_count() -> None:
    """INGESTION_SYNC_DAG_IDS must have exactly 1 entry.

    spec: feature/BACKEND.md §DAG Catalogue.
    """
    assert len(INGESTION_SYNC_DAG_IDS) == 1


# ── METRICS_TIER_DAG_IDS ──────────────────────────────────────────────────────


def test_metrics_tier_dag_ids_cover_all_tiers() -> None:
    """METRICS_TIER_DAG_IDS must contain one entry per tier.

    spec: feature/BACKEND.md §DAG Catalogue — metrics: hourly/daily/weekly.
    """
    for tier in _TIERS:
        assert f"metrics-{tier}" in METRICS_TIER_DAG_IDS


# ── METAGEN_TIER_DAG_IDS ──────────────────────────────────────────────────────


def test_metagen_tier_dag_ids_cover_all_tiers() -> None:
    """METAGEN_TIER_DAG_IDS must contain one entry per tier.

    spec: feature/BACKEND.md §DAG Catalogue — metagen: hourly/daily/weekly.
    """
    for tier in _TIERS:
        assert f"metagen-{tier}" in METAGEN_TIER_DAG_IDS


# ── ONTOGEN_TIER_DAG_IDS ──────────────────────────────────────────────────────


def test_ontogen_tier_dag_ids_cover_all_tiers() -> None:
    """ONTOGEN_TIER_DAG_IDS must contain one entry per tier.

    spec: feature/BACKEND.md §DAG Catalogue — ontogen: hourly/daily/weekly.
    """
    for tier in _TIERS:
        assert f"ontogen-{tier}" in ONTOGEN_TIER_DAG_IDS


# ── ON_DEMAND_DAG_IDS ─────────────────────────────────────────────────────────


def test_on_demand_dag_ids_is_exactly_metrics() -> None:
    """ON_DEMAND_DAG_IDS pins the API-triggered Airflow DAGs.

    spec: feature/BACKEND.md §DAG Catalogue — on-demand (API-triggered) DAGs.
    """
    assert set(ON_DEMAND_DAG_IDS) == {"metrics"}


# ── SYNC_DAG_IDS ──────────────────────────────────────────────────────────────


def test_sync_dag_ids_contains_auth_role_sync_daily() -> None:
    """SYNC_DAG_IDS must contain 'auth-role-sync-daily'.

    spec: feature/BACKEND.md §DAG Catalogue — auth role sync DAG.
    """
    assert "auth-role-sync-daily" in SYNC_DAG_IDS


# ── Union covers ALL_DAG_IDS ──────────────────────────────────────────────────


def test_all_dag_ids_is_union_of_component_sets() -> None:
    """ALL_DAG_IDS must equal the union of all component DAG ID tuples.

    spec: feature/BACKEND.md §DAG Catalogue — ALL_DAG_IDS is the complete set.
    """
    union = frozenset(
        list(INGESTION_ACTIVE_DAG_IDS)
        + list(INGESTION_SYNC_DAG_IDS)
        + list(METRICS_TIER_DAG_IDS)
        + list(METAGEN_TIER_DAG_IDS)
        + list(ONTOGEN_TIER_DAG_IDS)
        + list(ON_DEMAND_DAG_IDS)
        + list(SYNC_DAG_IDS)
    )
    assert ALL_DAG_IDS == union, (
        f"ALL_DAG_IDS does not equal the union of component sets.\n"
        f"  Extra in ALL_DAG_IDS: {ALL_DAG_IDS - union}\n"
        f"  Missing from ALL_DAG_IDS: {union - ALL_DAG_IDS}"
    )
