"""Shared fixtures and helpers for spot integration tests.

Provides the ``http_client`` fixture (activity_server-backed) used by
ingestion, validation, generation, and metrics modules.  Also exposes
ingestion-specific connection constants and cleanup helpers reused by
both ``test_ingestion_service`` and ``test_ingestion_workflow``.
"""

import os

import httpx
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.conftest import make_test_urn

# ── Shared fixture ─────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def http_client(activity_server):
    """HTTP client pointing at the real activity server."""
    async with httpx.AsyncClient(
        base_url=f"http://localhost:{activity_server.port}",
        timeout=120.0,
    ) as client:
        yield client


# ── Ingestion connection constants (resolved from dev_env/.env) ────────────

EXAMPLE_PG_LOCATOR = {
    "host": "localhost",
    "port": int(
        os.environ.get(
            "DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PORT_FORWARD_PORT", "9102"
        )
    ),
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
    "bootstrap_servers": os.environ.get(
        "DATASPOKE_DEV_KUBE_DUMMY_DATA_KAFKA_PORT_FORWARDED_BROKERS",
        "localhost:9104",
    ),
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
            " AND event_type LIKE 'ingestion.%'"
        ),
        {"urn": dataset_urn},
    )


async def delete_kestra_flow(kestra_client, flow_id: str) -> None:
    """Delete a Kestra flow; ignores errors if not found."""
    try:
        await kestra_client.delete_flow(flow_id)
    except Exception:
        pass
