"""Shared fixtures and helpers for tests/unit/backend/validation/."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.backend.validation.service import ValidationService

_DATASET_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,"
    "example_db.orders.daily_fulfillment_summary,DEV)"
)


def _var(name: str, description: str = "") -> dict[str, str]:
    """Build a {name, description} variable object (the JSONB column shape)."""
    return {"name": name, "description": description}


#: The factory default of ``validation_configs.attribute``, spelled out rather than
#: imported so a change to the impl constant fails a test instead of following it
#: silently. spec: VALIDATION.md §Rule Configuration — `cadence_unit` defaults to
#: `86400`, `cadence_offset` to `0`.
_DEFAULT_ATTRIBUTE: dict[str, int] = {"cadence_unit": 86400, "cadence_offset": 0}


def _make_config_row(
    dataset_urn: str = _DATASET_URN,
    description: str = "Daily row count check",
    variables: list[dict[str, str]] | None = None,
    attribute: dict[str, int] | None = None,
    parameter: list[dict[str, str]] | None = None,
) -> MagicMock:
    """Mock a ValidationConfig ORM row.

    ``variables`` is a JSONB array of ``{name, description}`` dicts per
    BACKEND_SCHEMA.md §validation_configs.

    ``attribute`` is set explicitly (defaulting to the factory cadence) because the
    column is ``NOT NULL`` and always holds a complete object — leaving it as a bare
    auto-mock would let a test read a value the database cannot produce. ``parameter``
    is the optional-by-absence section, so ``None`` is a real stored state here rather
    than an unset attribute.
    """
    row = MagicMock()
    row.dataset_urn = dataset_urn
    row.description = description
    row.variables = (
        variables
        if variables is not None
        else [_var("row_cnt", "Daily row count"), _var("col1_mean", "Mean of col1")]
    )
    row.attribute = dict(attribute) if attribute is not None else dict(_DEFAULT_ATTRIBUTE)
    row.parameter = parameter
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
