"""Shared fixtures and helpers for spot integration tests.

Provides the ``http_client`` fixture pointing at the in-cluster DataSpoke
API via nginx-ingress, used by ingestion, validation, generation, and metrics modules.
Also exposes ingestion-specific connection constants and cleanup helpers
reused by both ``test_ingestion_service`` and ``test_ingestion_workflow``.
"""

import os

import httpx
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.conftest import make_test_urn

# ── Shared fixture ─────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def http_client():
    """HTTP client pointing at the in-cluster DataSpoke API via ingress."""
    domain = os.environ.get("DATASPOKE_DEV_INGRESS_DOMAIN", "")
    if domain:
        base_url = f"http://app.{domain}"
    else:
        port = os.environ.get("DATASPOKE_API_PORT", "8002")
        base_url = f"http://localhost:{port}"
    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=120.0,
    ) as client:
        yield client


# ── Ingestion connection constants (resolved from dev_env/.env) ────────────

EXAMPLE_PG_LOCATOR = {
    "host": os.environ["DATASPOKE_EXAMPLE_PG_HOST"],
    "port": int(os.environ["DATASPOKE_EXAMPLE_PG_PORT"]),
}
EXAMPLE_PG_IDENTIFIER = {
    "database": os.environ.get(
        "DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_DB", "example_db"
    ),
    "schema_name": "catalog",
}
EXAMPLE_PG_AUTH = {
    "username": os.environ.get(
        "DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_USER", "postgres"
    ),
    "password": os.environ.get(
        "DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PASSWORD", ""
    ),
}

EXAMPLE_KAFKA_LOCATOR = {
    "bootstrap_servers": os.environ["DATASPOKE_EXAMPLE_KAFKA_BROKERS"],
}
EXAMPLE_KAFKA_IDENTIFIER = {
    "topic": "imazon.orders.events",
    "cluster": os.environ.get(
        "DATASPOKE_DEV_KUBE_DUMMY_DATA_KAFKA_INSTANCE", "example_kafka"
    ),
}


# ── Ingestion helpers ──────────────────────────────────────────────────────


def make_ingestion_urn(suffix: str) -> str:
    """Build a test dataset URN for ingestion tests."""
    return make_test_urn("ingestion", suffix)


async def delete_ingestion_config_db(
    session: AsyncSession, dataset_urn: str
) -> None:
    """Directly remove a config row from PostgreSQL (for finally blocks)."""
    await session.execute(
        text(
            "DELETE FROM dataspoke.ingestion_configs WHERE dataset_urn = :urn"
        ),
        {"urn": dataset_urn},
    )


async def delete_ingestion_events_db(
    session: AsyncSession, dataset_urn: str
) -> None:
    """Remove ingestion events for a dataset URN (for finally blocks)."""
    await session.execute(
        text(
            "DELETE FROM dataspoke.events"
            " WHERE entity_id = :urn"
            " AND entity_type = 'dataset'"
            " AND event_type LIKE 'INGESTION.%'"
        ),
        {"urn": dataset_urn},
    )


async def delete_airflow_dag_run(airflow_client, dag_id: str, dag_run_id: str) -> None:
    """Delete an Airflow DAG run; ignores errors if not found."""
    try:
        await airflow_client.delete_dag_run(dag_id, dag_run_id)
    except Exception:
        pass


# ── Validation helpers ──────────────────────────────────────────────────────


def make_validation_urn(suffix: str) -> str:
    """Build a test dataset URN for validation tests."""
    return make_test_urn("validation", suffix)


async def delete_validation_config_db(
    session: AsyncSession, dataset_urn: str
) -> None:
    """Directly remove a validation config row from PostgreSQL (for finally blocks)."""
    await session.execute(
        text(
            "DELETE FROM dataspoke.validation_configs WHERE dataset_urn = :urn"
        ),
        {"urn": dataset_urn},
    )


async def delete_validation_events_db(
    session: AsyncSession, dataset_urn: str
) -> None:
    """Remove validation events for a dataset URN (for finally blocks)."""
    await session.execute(
        text(
            "DELETE FROM dataspoke.events"
            " WHERE entity_id = :urn"
            " AND entity_type = 'dataset'"
            " AND event_type LIKE 'VALIDATION.%'"
        ),
        {"urn": dataset_urn},
    )


async def delete_validation_results_db(
    session: AsyncSession, dataset_urn: str
) -> None:
    """Remove validation results for a dataset URN (for finally blocks)."""
    await session.execute(
        text(
            "DELETE FROM dataspoke.validation_results WHERE dataset_urn = :urn"
        ),
        {"urn": dataset_urn},
    )


# ── Dataset registry helpers ──────────────────────────────────────────────


async def seed_dataset_registry(
    session: AsyncSession, dataset_urn: str, datahub_registered: bool = True,
) -> None:
    """Pre-seed a dataset_registry row (for synthetic URN tests)."""
    await session.execute(
        text(
            "INSERT INTO dataspoke.dataset_registry (dataset_urn, datahub_registered)"
            " VALUES (:urn, :reg)"
            " ON CONFLICT (dataset_urn) DO UPDATE SET datahub_registered = EXCLUDED.datahub_registered"
        ),
        {"urn": dataset_urn, "reg": datahub_registered},
    )
    await session.commit()


async def delete_dataset_registry_db(
    session: AsyncSession, dataset_urn: str,
) -> None:
    """Remove a dataset_registry row (for finally blocks)."""
    await session.execute(
        text("DELETE FROM dataspoke.dataset_registry WHERE dataset_urn = :urn"),
        {"urn": dataset_urn},
    )


# ── Metrics helpers ──────────────────────────────────────────────────────


async def delete_metric_definition_db(
    session: AsyncSession, metric_id: str
) -> None:
    """Directly remove a metric_definitions row from PostgreSQL (for finally blocks)."""
    await session.execute(
        text(
            "DELETE FROM dataspoke.metric_definitions WHERE id = :id"
        ),
        {"id": metric_id},
    )


async def delete_metric_results_db(
    session: AsyncSession, metric_id: str
) -> None:
    """Remove metric results for a metric ID (for finally blocks)."""
    await session.execute(
        text(
            "DELETE FROM dataspoke.metric_results WHERE metric_id = :id"
        ),
        {"id": metric_id},
    )


async def delete_metric_events_db(
    session: AsyncSession, metric_id: str
) -> None:
    """Remove metric events for a metric ID (for finally blocks)."""
    await session.execute(
        text(
            "DELETE FROM dataspoke.events"
            " WHERE entity_id = :id"
            " AND entity_type = 'metric'"
        ),
        {"id": metric_id},
    )
