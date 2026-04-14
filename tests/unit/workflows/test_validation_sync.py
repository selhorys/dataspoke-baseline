"""Unit tests for validation_sync: schedule tier helpers.

Tests cover:
- schedule_to_flow_id() stability and prefix
- get_datasets_for_tier() DB query logic
"""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.workflows.validation_sync import (
    FLOW_PREFIX,
    schedule_to_flow_id,
)


# ── schedule_to_flow_id ────────────────────────────────────────────────────────


def test_schedule_to_flow_id_has_prefix():
    flow_id = schedule_to_flow_id("daily")
    assert flow_id.startswith(FLOW_PREFIX)


def test_schedule_to_flow_id_total_length():
    # "validation-periodic-" (20 chars) + 8 hex chars = 28
    flow_id = schedule_to_flow_id("daily")
    assert len(flow_id) == len(FLOW_PREFIX) + 8


def test_schedule_to_flow_id_stable_for_same_tier():
    """Same tier always produces the same flow ID."""
    assert schedule_to_flow_id("daily") == schedule_to_flow_id("daily")


def test_schedule_to_flow_id_different_tiers_produce_different_ids():
    """Distinct tiers must produce distinct flow IDs."""
    assert schedule_to_flow_id("daily") != schedule_to_flow_id("weekly")
    assert schedule_to_flow_id("hourly") != schedule_to_flow_id("daily")


def test_schedule_to_flow_id_deterministic_hash():
    """Regression: flow ID matches MD5 of tier bytes (first 8 hex chars)."""
    tier = "daily"
    expected = FLOW_PREFIX + hashlib.md5(tier.encode()).hexdigest()[:8]
    assert schedule_to_flow_id(tier) == expected


def test_schedule_to_flow_id_all_tiers():
    """All three valid tiers produce unique IDs with the correct prefix."""
    ids = [schedule_to_flow_id(t) for t in ("hourly", "daily", "weekly")]
    assert len(set(ids)) == 3
    assert all(i.startswith(FLOW_PREFIX) for i in ids)


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
