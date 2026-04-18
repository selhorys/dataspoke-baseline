"""Unit tests for the ingestion workflow module.

Tests cover:
- schedule_to_flow_id() hashing stability and prefix
- get_datasets_for_tier() DB query logic
"""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.workflows.ingestion import (
    PERIODIC_FLOW_PREFIX,
    schedule_to_flow_id,
)


# ── Constants ──────────────────────────────────────────────────────────────────


def test_periodic_flow_prefix():
    assert PERIODIC_FLOW_PREFIX == "ingestion-periodic-"


# ── schedule_to_flow_id ────────────────────────────────────────────────────────


def test_schedule_to_flow_id_has_prefix():
    flow_id = schedule_to_flow_id("daily")
    assert flow_id.startswith(PERIODIC_FLOW_PREFIX)


def test_schedule_to_flow_id_length():
    # prefix (18) + 8 hex chars = 26
    flow_id = schedule_to_flow_id("daily")
    assert len(flow_id) == len(PERIODIC_FLOW_PREFIX) + 8


def test_schedule_to_flow_id_stable():
    """Same schedule always produces the same ID."""
    assert schedule_to_flow_id("daily") == schedule_to_flow_id("daily")


def test_schedule_to_flow_id_different_schedules():
    """Different schedules produce different IDs."""
    assert schedule_to_flow_id("daily") != schedule_to_flow_id("weekly")
    assert schedule_to_flow_id("hourly") != schedule_to_flow_id("daily")


def test_schedule_to_flow_id_known_hash():
    """Regression test — MD5 of 'daily' first 8 chars."""
    expected = "ingestion-periodic-" + hashlib.md5(b"daily").hexdigest()[:8]
    assert schedule_to_flow_id("daily") == expected


def test_schedule_to_flow_id_all_tiers():
    """All three valid tiers produce unique IDs with the correct prefix."""
    ids = [schedule_to_flow_id(t) for t in ("hourly", "daily", "weekly")]
    assert len(set(ids)) == 3
    assert all(i.startswith(PERIODIC_FLOW_PREFIX) for i in ids)


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
