"""Shared fixtures and helpers for tests/unit/backend/validation/."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.backend.validation.service import ValidationService

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.orders.daily_fulfillment_summary,DEV)"


def _make_config_row(
    dataset_urn: str = _DATASET_URN,
    description: str = "Daily row count check",
    variables: list[str] | None = None,
    is_removed: bool = False,
) -> MagicMock:
    row = MagicMock()
    row.dataset_urn = dataset_urn
    row.description = description
    row.variables = variables if variables is not None else ["row_cnt", "col1_mean"]
    row.is_removed = is_removed
    row.created_at = datetime.now(tz=UTC)
    row.updated_at = datetime.now(tz=UTC)
    return row


def _make_result_row(
    dataset_urn: str = _DATASET_URN,
    data_time: datetime | None = None,
    score: float = 1.0,
    variables: dict | None = None,
    ingestion_time: datetime | None = None,
) -> MagicMock:
    row = MagicMock()
    row.id = uuid.uuid4()
    row.dataset_urn = dataset_urn
    row.data_time = data_time or datetime(2026, 5, 1, tzinfo=UTC)
    row.score = score
    row.variables = variables or {"row_cnt": 50.0}
    row.ingestion_time = ingestion_time or datetime.now(tz=UTC)
    return row


def _scalar_result(value):
    """Return a mock that behaves like db.execute().scalar_one_or_none()."""
    m = MagicMock()
    m.scalar_one_or_none.return_value = value
    return m


def _scalar_count(n: int):
    """Return a mock that behaves like db.execute().scalar()."""
    m = MagicMock()
    m.scalar.return_value = n
    return m


@pytest.fixture
def svc(datahub: AsyncMock, db: AsyncMock) -> ValidationService:
    return ValidationService(datahub=datahub, db=db)
