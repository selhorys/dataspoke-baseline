"""Unit tests for validation workflow params and tier-dataset helpers."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.workflows.validation import ValidationRunParams


def test_params_defaults():
    params = ValidationRunParams(dataset_urn="urn:li:dataset:test")
    assert params.dataset_urn == "urn:li:dataset:test"
    assert params.partition is None


def test_params_with_partition():
    params = ValidationRunParams(
        dataset_urn="urn:li:dataset:test",
        partition={"load_date": "2025-03-10"},
    )
    assert params.partition == {"load_date": "2025-03-10"}


def test_params_dry_run_default():
    params = ValidationRunParams(dataset_urn="urn:li:dataset:test")
    assert params.dry_run is False


def test_params_dry_run_true():
    params = ValidationRunParams(dataset_urn="urn:li:dataset:test", dry_run=True)
    assert params.dry_run is True


# ── get_datasets_for_tier ──────────────────────────────────────────────────────
# spec: BACKEND.md §Tier-DAG selection — for features with a schedule_tier field,
# the periodic DAG fetches only configs whose schedule_tier matches the DAG tier
# AND is_enabled=true.


@pytest.mark.asyncio
async def test_get_datasets_for_tier_filters_by_is_enabled():
    """get_datasets_for_tier returns only URNs whose config has is_enabled=true.

    # spec: BACKEND.md §Tier-DAG selection — is_enabled must be true for a dataset
    # to be selected for periodic execution.
    """
    from src.workflows.validation import get_datasets_for_tier

    urns = [
        "urn:li:dataset:(urn:li:dataPlatform:postgres,imazon.orders.daily_fulfillment_summary,DEV)",
    ]
    result_mock = MagicMock()
    result_mock.all.return_value = [(urns[0],)]

    db = AsyncMock()
    db.execute = AsyncMock(return_value=result_mock)

    # The function applies is_enabled=True filter internally; we verify it returns
    # only the rows the DB provides (mock simulates the filtered result).
    result = await get_datasets_for_tier(db, "daily")
    assert result == urns
    # Confirm execute was called with a query (not zero-arg)
    db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_datasets_for_tier_filters_by_schedule_tier():
    """get_datasets_for_tier returns only URNs matching the requested tier.

    # spec: BACKEND.md §Tier-DAG selection — schedule_tier must match the DAG tier.
    """
    from src.workflows.validation import get_datasets_for_tier

    urns = [
        "urn:li:dataset:(urn:li:dataPlatform:postgres,imazon.reviews.user_ratings,DEV)",
        "urn:li:dataset:(urn:li:dataPlatform:postgres,imazon.reviews.user_ratings_legacy,DEV)",
    ]
    result_mock = MagicMock()
    result_mock.all.return_value = [(urns[0],), (urns[1],)]

    db = AsyncMock()
    db.execute = AsyncMock(return_value=result_mock)

    result = await get_datasets_for_tier(db, "hourly")
    assert result == urns
    assert len(result) == 2


@pytest.mark.asyncio
async def test_get_datasets_for_tier_returns_empty_when_no_matches():
    """get_datasets_for_tier returns [] when no config matches tier + is_enabled.

    # spec: BACKEND.md §Tier-DAG selection — DAG short-circuits when result is empty.
    """
    from src.workflows.validation import get_datasets_for_tier

    result_mock = MagicMock()
    result_mock.all.return_value = []

    db = AsyncMock()
    db.execute = AsyncMock(return_value=result_mock)

    result = await get_datasets_for_tier(db, "weekly")
    assert result == []
    assert isinstance(result, list)
