"""Unit tests for metrics workflow: tier query logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


# ── get_metrics_for_tier ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_metrics_for_tier_returns_ids():
    from src.workflows.metrics import get_metrics_for_tier

    metric_ids = ["imazon.doc_coverage", "imazon.freshness_rate"]
    result_mock = MagicMock()
    result_mock.all.return_value = [(mid,) for mid in metric_ids]

    db = AsyncMock()
    db.execute = AsyncMock(return_value=result_mock)

    result = await get_metrics_for_tier(db, "daily")
    assert result == metric_ids


@pytest.mark.asyncio
async def test_get_metrics_for_tier_empty():
    from src.workflows.metrics import get_metrics_for_tier

    result_mock = MagicMock()
    result_mock.all.return_value = []

    db = AsyncMock()
    db.execute = AsyncMock(return_value=result_mock)

    result = await get_metrics_for_tier(db, "hourly")
    assert result == []


@pytest.mark.asyncio
async def test_get_metrics_for_tier_weekly():
    """weekly tier query should work identically."""
    from src.workflows.metrics import get_metrics_for_tier

    metric_ids = ["imazon.weekly_summary"]
    result_mock = MagicMock()
    result_mock.all.return_value = [(metric_ids[0],)]

    db = AsyncMock()
    db.execute = AsyncMock(return_value=result_mock)

    result = await get_metrics_for_tier(db, "weekly")
    assert result == metric_ids
