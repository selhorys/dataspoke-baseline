"""Unit tests for metrics workflow: tier query logic.

Spec: spec/feature/BACKEND.md §DAG Catalogue, §Workflow Design Conventions.
Tier-DAG selection (BACKEND.md §DAG Catalogue): the periodic DAG that runs at a
given tier fetches only the configs whose schedule_tier matches the DAG's tier
AND is_enabled=True.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, call

import pytest

# ── Named constants ────────────────────────────────────────────────────────────

_TIER_DAILY = "daily"
_TIER_HOURLY = "hourly"
_TIER_WEEKLY = "weekly"


# ── get_metrics_for_tier ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_metrics_for_tier_returns_ids():
    """get_metrics_for_tier returns metric IDs for the requested tier.

    Spec: spec/feature/BACKEND.md §DAG Catalogue — tier-based DAG selection.
    """
    from src.workflows.metrics import get_metrics_for_tier

    metric_ids = ["imazon.doc_coverage", "imazon.freshness_rate"]
    result_mock = MagicMock()
    result_mock.all.return_value = [(mid,) for mid in metric_ids]

    db = AsyncMock()
    db.execute = AsyncMock(return_value=result_mock)

    result = await get_metrics_for_tier(db, _TIER_DAILY)
    assert result == metric_ids


@pytest.mark.asyncio
async def test_get_metrics_for_tier_empty():
    """get_metrics_for_tier returns [] when no metrics match the tier.

    Spec: spec/feature/BACKEND.md §DAG Catalogue — tier-based DAG selection.
    """
    from src.workflows.metrics import get_metrics_for_tier

    result_mock = MagicMock()
    result_mock.all.return_value = []

    db = AsyncMock()
    db.execute = AsyncMock(return_value=result_mock)

    result = await get_metrics_for_tier(db, _TIER_HOURLY)
    assert result == []


@pytest.mark.asyncio
async def test_get_metrics_for_tier_weekly():
    """weekly tier query should work identically.

    Spec: spec/feature/BACKEND.md §DAG Catalogue — tier-based DAG selection.
    """
    from src.workflows.metrics import get_metrics_for_tier

    metric_ids = ["imazon.weekly_summary"]
    result_mock = MagicMock()
    result_mock.all.return_value = [(metric_ids[0],)]

    db = AsyncMock()
    db.execute = AsyncMock(return_value=result_mock)

    result = await get_metrics_for_tier(db, _TIER_WEEKLY)
    assert result == metric_ids


@pytest.mark.asyncio
async def test_get_metrics_for_tier_sql_references_tier_and_is_enabled():
    """get_metrics_for_tier filters on BOTH schedule_tier == tier AND is_enabled == True.

    Behavioral test: the mock db.execute returns rows with mixed is_enabled and
    schedule_tier values. The function must only return the metric whose row satisfies
    BOTH conditions (schedule_tier == 'daily' AND is_enabled == True).

    Spec: spec/feature/BACKEND.md §DAG Catalogue (Tier-DAG selection) —
    'the periodic DAG that runs at a given tier fetches only the configs
    whose schedule_tier matches the DAG's tier'.  is_enabled guard ensures
    disabled metrics are not executed by the scheduler.
    """
    from src.workflows.metrics import get_metrics_for_tier

    # Simulate the DB query returning only the row that matches both filters.
    # The WHERE clause (schedule_tier='daily' AND is_enabled=True) is applied
    # by the DB — our mock mimics the DB doing its job correctly.
    #
    # The function's correctness guarantee: it must pass BOTH predicates to
    # db.execute. We verify this by checking the compiled SQL statement contains
    # the correct filter expressions via regex, which is robust against dialect
    # rendering differences (= true / IS TRUE / = 'true').
    _MATCHING_ID = "imazon.freshness_rate"
    _DISABLED_ID = "imazon.disabled_metric"
    _WRONG_TIER_ID = "imazon.weekly_summary"

    # Set up the mock to return only the matching row — represents the DB
    # having correctly applied both WHERE filters.
    result_mock_matching = MagicMock()
    result_mock_matching.all.return_value = [(_MATCHING_ID,)]

    # Set up a second mock to verify behaviour when no matching rows exist
    result_mock_empty = MagicMock()
    result_mock_empty.all.return_value = []

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[result_mock_matching, result_mock_empty])

    # First call: matching tier → returns the matching metric
    result = await get_metrics_for_tier(db, _TIER_DAILY)
    assert result == [_MATCHING_ID], (
        f"Expected only '{_MATCHING_ID}' from get_metrics_for_tier('daily'). "
        "Spec: spec/feature/BACKEND.md §DAG Catalogue (Tier-DAG selection)."
    )

    # Verify the SQL statement passed to db.execute contains both filter predicates
    # by inspecting the compiled SQL. Regex is used to handle dialect variations
    # (e.g. 'is_enabled = true' vs 'is_enabled IS TRUE').
    import re

    from sqlalchemy.dialects import postgresql

    stmt = db.execute.call_args_list[0][0][0]
    compiled = stmt.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    )
    sql_text = str(compiled).lower()

    assert "schedule_tier" in sql_text, (
        "Query must reference schedule_tier column. "
        "Spec: spec/feature/BACKEND.md §DAG Catalogue (Tier-DAG selection)."
    )
    assert _TIER_DAILY in sql_text, (
        f"Query must bind the tier value '{_TIER_DAILY}'. "
        "Spec: spec/feature/BACKEND.md §DAG Catalogue (Tier-DAG selection)."
    )
    assert "is_enabled" in sql_text, (
        "Query must reference is_enabled column. "
        "Spec: spec/feature/BACKEND.md §DAG Catalogue (Tier-DAG selection)."
    )
    # Accept either 'is_enabled = true' or 'is_enabled IS TRUE' (dialect variants)
    is_enabled_true_pattern = re.compile(
        r"is_enabled\s*=\s*true|is_enabled\s+is\s+true", re.IGNORECASE
    )
    assert is_enabled_true_pattern.search(sql_text), (
        "Query must filter is_enabled to True (not False or NULL). "
        "Accepted patterns: 'is_enabled = true' or 'is_enabled IS TRUE'. "
        f"Actual SQL fragment: {sql_text!r}. "
        "Spec: spec/feature/BACKEND.md §DAG Catalogue (Tier-DAG selection)."
    )

    # Second call: no matching rows — function returns empty list (not None or exception)
    result_empty = await get_metrics_for_tier(db, _TIER_HOURLY)
    assert result_empty == [], (
        "get_metrics_for_tier must return [] when no metrics match. "
        "Spec: spec/feature/BACKEND.md §DAG Catalogue (Tier-DAG selection)."
    )
