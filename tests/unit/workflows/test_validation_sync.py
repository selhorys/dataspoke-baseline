"""Unit tests for validation_sync: tier query logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


# ── get_datasets_for_tier ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_datasets_for_tier_returns_urns():
    from src.workflows.validation_sync import get_datasets_for_tier

    urns = [
        "urn:li:dataset:(urn:li:dataPlatform:postgres,db.public.t1,PROD)",
        "urn:li:dataset:(urn:li:dataPlatform:postgres,db.public.t2,PROD)",
    ]
    result_mock = MagicMock()
    result_mock.all.return_value = [(urns[0],), (urns[1],)]

    db = AsyncMock()
    db.execute = AsyncMock(return_value=result_mock)

    result = await get_datasets_for_tier(db, "daily")
    assert result == urns


@pytest.mark.asyncio
async def test_get_datasets_for_tier_empty():
    from src.workflows.validation_sync import get_datasets_for_tier

    result_mock = MagicMock()
    result_mock.all.return_value = []

    db = AsyncMock()
    db.execute = AsyncMock(return_value=result_mock)

    result = await get_datasets_for_tier(db, "weekly")
    assert result == []


@pytest.mark.asyncio
async def test_get_datasets_for_tier_hourly():
    """hourly tier query should work identically."""
    from src.workflows.validation_sync import get_datasets_for_tier

    urns = ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.schema.table,PROD)"]
    result_mock = MagicMock()
    result_mock.all.return_value = [(urns[0],)]

    db = AsyncMock()
    db.execute = AsyncMock(return_value=result_mock)

    result = await get_datasets_for_tier(db, "hourly")
    assert result == urns
