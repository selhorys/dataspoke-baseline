"""Unit tests for the ingestion workflow module."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


# ── get_datasets_for_tier ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_datasets_for_tier_returns_urns():
    from src.workflows.ingestion import get_datasets_for_tier

    urns = [
        "urn:li:dataset:(urn:li:dataPlatform:postgres,db1.public.t1,PROD)",
        "urn:li:dataset:(urn:li:dataPlatform:postgres,db1.public.t2,PROD)",
    ]
    row_mock_1 = (urns[0],)
    row_mock_2 = (urns[1],)

    result_mock = MagicMock()
    result_mock.all.return_value = [row_mock_1, row_mock_2]

    db = AsyncMock()
    db.execute = AsyncMock(return_value=result_mock)

    result = await get_datasets_for_tier(db, "daily")
    assert result == urns


@pytest.mark.asyncio
async def test_get_datasets_for_tier_empty():
    from src.workflows.ingestion import get_datasets_for_tier

    result_mock = MagicMock()
    result_mock.all.return_value = []

    db = AsyncMock()
    db.execute = AsyncMock(return_value=result_mock)

    result = await get_datasets_for_tier(db, "hourly")
    assert result == []


@pytest.mark.asyncio
async def test_get_datasets_for_tier_weekly():
    """weekly tier query should work identically."""
    from src.workflows.ingestion import get_datasets_for_tier

    urns = ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.schema.table,PROD)"]
    result_mock = MagicMock()
    result_mock.all.return_value = [(urns[0],)]

    db = AsyncMock()
    db.execute = AsyncMock(return_value=result_mock)

    result = await get_datasets_for_tier(db, "weekly")
    assert result == urns
