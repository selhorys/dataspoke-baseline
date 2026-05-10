"""Shared fixtures and helpers for tests/unit/backend/ingestion/."""

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from src.backend.ingestion.service import IngestionService

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.public.users,PROD)"
_LOCATOR = {"host": "db.example.com", "port": 5432}
_IDENTIFIER = {"database": "mydb", "schema_name": "public", "table": "users"}
_AUTH = {"username": "user", "secret_ref": "pw"}


def _make_config_row(
    dataset_urn: str = _DATASET_URN,
    platform: str = "postgres",
    locator: dict | None = None,
    identifier: dict | None = None,
    auth: dict | None = None,
    is_enabled: bool = False,
    mode: str = "active-custom",
    schedule_tier: str | None = "daily",
    workflow_dag_id: str | None = None,
    status: str = "OK",
):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.dataset_urn = dataset_urn
    row.platform = platform
    row.locator = locator or _LOCATOR
    row.identifier = identifier or _IDENTIFIER
    row.auth = auth if auth is not None else _AUTH
    row.is_enabled = is_enabled
    row.mode = mode
    row.schedule_tier = schedule_tier
    row.workflow_dag_id = workflow_dag_id
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
