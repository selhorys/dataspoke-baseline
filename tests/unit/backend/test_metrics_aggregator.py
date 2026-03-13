"""Unit tests for aggregate_health_scores (mocked infrastructure)."""

from unittest.mock import AsyncMock, MagicMock, patch

from src.backend.metrics.aggregator import aggregate_health_scores
from tests.unit.backend.conftest import make_quality_score_mock


def _make_ownership(owner_urn: str | None):
    if owner_urn is None:
        return None
    ownership = MagicMock()
    owner = MagicMock()
    owner.owner = owner_urn
    ownership.owners = [owner]
    return ownership


def _make_dept_row(owner_urn: str, department: str):
    row = MagicMock()
    row.owner_urn = owner_urn
    row.department = department
    return row


@patch("src.backend.metrics.aggregator.compute_quality_score")
async def test_aggregate_two_departments(mock_score, datahub, db, cache):
    datahub.enumerate_datasets = AsyncMock(return_value=["urn:ds1", "urn:ds2", "urn:ds3"])

    scores = {
        "urn:ds1": make_quality_score_mock(80.0),
        "urn:ds2": make_quality_score_mock(40.0),
        "urn:ds3": make_quality_score_mock(60.0),
    }
    mock_score.side_effect = lambda dh, urn, cache=None: scores[urn]

    ownerships = {
        "urn:ds1": _make_ownership("owner:alice"),
        "urn:ds2": _make_ownership("owner:alice"),
        "urn:ds3": _make_ownership("owner:bob"),
    }
    datahub.get_aspect = AsyncMock(side_effect=lambda urn, cls: ownerships[urn])

    dept_rows = [
        _make_dept_row("owner:alice", "Engineering"),
        _make_dept_row("owner:bob", "Analytics"),
    ]
    dept_result = MagicMock()
    dept_result.scalars.return_value.all.return_value = dept_rows
    db.execute = AsyncMock(return_value=dept_result)

    result = await aggregate_health_scores(datahub, db, cache=cache)

    assert "Engineering" in result
    assert "Analytics" in result
    assert result["Engineering"].dataset_count == 2
    assert result["Engineering"].avg_score == 60.0
    assert result["Analytics"].dataset_count == 1
    assert result["Analytics"].avg_score == 60.0


@patch("src.backend.metrics.aggregator.compute_quality_score")
async def test_aggregate_unknown_department(mock_score, datahub, db, cache):
    datahub.enumerate_datasets = AsyncMock(return_value=["urn:ds1"])
    mock_score.return_value = make_quality_score_mock(70.0)
    datahub.get_aspect = AsyncMock(return_value=_make_ownership("owner:unknown"))

    # No department mapping
    dept_result = MagicMock()
    dept_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=dept_result)

    result = await aggregate_health_scores(datahub, db, cache=cache)
    assert "Unknown" in result
    assert result["Unknown"].dataset_count == 1


async def test_aggregate_empty_datasets(datahub, db, cache):
    datahub.enumerate_datasets = AsyncMock(return_value=[])

    result = await aggregate_health_scores(datahub, db, cache=cache)
    assert result == {}


@patch("src.backend.metrics.aggregator.compute_quality_score")
async def test_aggregate_handles_errors(mock_score, datahub, db, cache):
    datahub.enumerate_datasets = AsyncMock(return_value=["urn:ds1", "urn:ds2"])

    # First dataset fails, second succeeds
    mock_score.side_effect = [Exception("DataHub error"), make_quality_score_mock(50.0)]
    datahub.get_aspect = AsyncMock(return_value=None)

    dept_result = MagicMock()
    dept_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=dept_result)

    result = await aggregate_health_scores(datahub, db, cache=cache)
    # Only one dataset succeeded → grouped under "Unknown" (no owner)
    assert "Unknown" in result
    assert result["Unknown"].dataset_count == 1
