"""Shared fixtures and helpers for tests/unit/backend/ingestion/ — per-source model."""

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from src.backend.ingestion.service import IngestionService

# ── Canonical test constants ───────────────────────────────────────────────────

_SOURCE_ID = str(uuid.uuid4())
_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"

_RECIPE_POSTGRES = {
    "source": {
        "type": "postgres",
        "config": {
            "host_port": "example-pg:5432",
            "database": "example_db",
            "username": "spoke_reader",
            "password": "${dummy-data-pg__password}",
            "schema_pattern": {"allow": ["^catalog$"]},
            "env": "DEV",
        },
    }
}

_RECIPE_NO_SECRET = {
    "source": {
        "type": "postgres",
        "config": {
            "host_port": "example-pg:5432",
            "database": "example_db",
            "username": "spoke_reader",
            "env": "DEV",
        },
    }
}


def _make_source_row(
    *,
    source_id: str | None = None,
    mode: str = "ACTIVE_CUSTOM_MANAGED",
    name: str = "imazon catalog pg",
    platform: str = "postgres",
    recipe: dict | None = None,
    schedule: str | None = "0 0 * * *",
    schedule_tier: str | None = "daily",
    datahub_source_urn: str | None = None,
    status: str = "OK",
) -> MagicMock:
    row = MagicMock()
    row.id = uuid.UUID(source_id) if source_id else uuid.uuid4()
    row.mode = mode
    row.name = name
    row.platform = platform
    row.recipe = recipe if recipe is not None else _RECIPE_POSTGRES
    row.schedule = schedule
    row.schedule_tier = schedule_tier
    row.datahub_source_urn = datahub_source_urn
    row.status = status
    row.created_at = datetime.now(tz=UTC)
    row.updated_at = datetime.now(tz=UTC)
    return row


@pytest.fixture
def service(datahub, db):
    return IngestionService(datahub=datahub, db=db)


@pytest.fixture
def service_with_cache(datahub, db, cache):
    return IngestionService(datahub=datahub, db=db, cache=cache)
