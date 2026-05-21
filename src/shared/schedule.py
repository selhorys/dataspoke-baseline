"""Shared schedule constants for DataSpoke.

These constants feed the metrics per-dataset freshness window calculations
and other schedule-aware services. They must stay in sync with the Airflow
DAG schedule definitions in ``src/workflows/dags/``.
"""

# Seconds per schedule tier — mirrors the Airflow tier-DAG cadences.
SCHEDULE_TIER_SECONDS: dict[str, int] = {
    "hourly": 3600,
    "daily": 86400,
    "weekly": 604800,
}

# Passive ingestion sync period in seconds.
# MUST mirror the ``@hourly`` schedule of the ``ingestion-passive-hourly`` DAG
# defined in ``src/workflows/dags/ingestion_passive_hourly.py``.
PASSIVE_SYNC_PERIOD_SEC: int = 3600

# Multiplier applied to a dataset's nominal cadence to form its freshness window.
# Doubles the nominal cadence to tolerate transient late ingestion.
LATE_INGESTION_FACTOR: int = 2
