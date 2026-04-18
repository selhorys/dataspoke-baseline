"""Unit tests for metrics workflow: DAG ID helpers and tier query logic.

Tests cover:
- PERIODIC_FLOW_PREFIX constant
- schedule_to_flow_id() stability and prefix
- get_metrics_for_tier() DB query logic
"""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.workflows.metrics import (
    PERIODIC_FLOW_PREFIX,
    schedule_to_flow_id,
)


# ── Constants ──────────────────────────────────────────────────────────────────


def test_periodic_flow_prefix():
    assert PERIODIC_FLOW_PREFIX == "metrics-periodic-"


# ── schedule_to_flow_id ────────────────────────────────────────────────────────


def test_schedule_to_flow_id_has_prefix():
    flow_id = schedule_to_flow_id("daily")
    assert flow_id.startswith(PERIODIC_FLOW_PREFIX)


def test_schedule_to_flow_id_total_length():
    # "metrics-periodic-" (18 chars) + 8 hex chars = 26
    flow_id = schedule_to_flow_id("daily")
    assert len(flow_id) == len(PERIODIC_FLOW_PREFIX) + 8


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
    expected = PERIODIC_FLOW_PREFIX + hashlib.md5(tier.encode()).hexdigest()[:8]
    assert schedule_to_flow_id(tier) == expected


def test_schedule_to_flow_id_all_tiers():
    """All three valid tiers produce unique IDs with the correct prefix."""
    ids = [schedule_to_flow_id(t) for t in ("hourly", "daily", "weekly")]
    assert len(set(ids)) == 3
    assert all(i.startswith(PERIODIC_FLOW_PREFIX) for i in ids)


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
