"""Source of truth for Airflow DAG IDs that DataSpoke expects.

Spec: spec/feature/BACKEND.md §DAG Catalogue
"""

TIERS: tuple[str, ...] = ("hourly", "daily", "weekly")

INGESTION_ACTIVE_DAG_IDS: tuple[str, ...] = tuple(
    f"ingestion-active-{tier}" for tier in TIERS
)
INGESTION_SYNC_DAG_IDS: tuple[str, ...] = ("datahub-sync-hourly",)
METRICS_TIER_DAG_IDS: tuple[str, ...] = tuple(f"metrics-{tier}" for tier in TIERS)
METAGEN_TIER_DAG_IDS: tuple[str, ...] = tuple(f"metagen-{tier}" for tier in TIERS)
ONTOGEN_TIER_DAG_IDS: tuple[str, ...] = tuple(f"ontogen-{tier}" for tier in TIERS)

ON_DEMAND_DAG_IDS: tuple[str, ...] = ("metrics",)
SYNC_DAG_IDS: tuple[str, ...] = ("auth-role-sync-daily",)

ALL_DAG_IDS: frozenset[str] = frozenset(
    INGESTION_ACTIVE_DAG_IDS
    + INGESTION_SYNC_DAG_IDS
    + METRICS_TIER_DAG_IDS
    + METAGEN_TIER_DAG_IDS
    + ONTOGEN_TIER_DAG_IDS
    + ON_DEMAND_DAG_IDS
    + SYNC_DAG_IDS
)
