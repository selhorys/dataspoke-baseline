"""Source of truth for Airflow DAG IDs that DataSpoke expects."""

ON_DEMAND_DAG_IDS: tuple[str, ...] = (
    "generation",
    "metrics",
    "embedding-sync",
    "ontology-rebuild",
)
PERIODIC_TIERS: tuple[str, ...] = ("hourly", "daily", "weekly")
PERIODIC_DOMAINS: tuple[str, ...] = ("ingestion", "metrics", "validation")
PERIODIC_DAG_IDS: tuple[str, ...] = tuple(
    f"{domain}-periodic-{tier}"
    for domain in PERIODIC_DOMAINS
    for tier in PERIODIC_TIERS
)
SYNC_DAG_IDS: tuple[str, ...] = ("datahub-sync-daily",)
ALL_DAG_IDS: frozenset[str] = frozenset(
    ON_DEMAND_DAG_IDS + PERIODIC_DAG_IDS + SYNC_DAG_IDS
)
