"""Unit tests for the ingestion workflow module.

spec: BACKEND.md §Ingestion Service — list_active_for_tier filters on mode='active-custom'
spec: USE_CASE_en.md §UC1 — two modes: active-custom and passive
"""

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


@pytest.mark.asyncio
async def test_get_datasets_for_tier_filter_predicates():
    """get_datasets_for_tier WHERE clause must filter on mode='active-custom',
    is_enabled=True, and schedule_tier=<tier>.

    Inspects the compiled SQLAlchemy statement captured from db.execute to verify all
    three predicates are present as binary expressions. Survives logically-equivalent
    refactors (e.g. is_(True) vs == True, mode.in_([...]) vs mode == ...) by walking
    the AST rather than matching SQL text.

    spec: BACKEND.md §Ingestion Service — list_active_for_tier filters on
        is_enabled=True, mode='active-custom', and schedule_tier matches.
    spec: USE_CASE_en.md §UC1 — two modes: active-custom (DataSpoke runs) and
        passive (external runs). Passive rows must not appear in the DAG's work list.
    """
    from sqlalchemy.sql.elements import BinaryExpression, False_, True_
    from sqlalchemy.sql.visitors import iterate

    from src.workflows.ingestion import get_datasets_for_tier

    captured: list = []

    async def capture_execute(stmt):
        captured.append(stmt)
        result = MagicMock()
        result.all.return_value = []
        return result

    db = AsyncMock()
    db.execute = capture_execute

    await get_datasets_for_tier(db, "daily")

    assert len(captured) == 1, (
        f"get_datasets_for_tier must execute exactly one query; got {len(captured)}"
    )
    stmt = captured[0]
    where = stmt.whereclause

    binaries = [n for n in iterate(where) if isinstance(n, BinaryExpression)]

    def _extract_pairs(nodes):
        pairs = set()
        for b in nodes:
            col = getattr(b.left, "key", None) or getattr(b.left, "name", None)
            if col is None:
                continue
            if isinstance(b.right, True_):
                pairs.add((col, True))
            elif isinstance(b.right, False_):
                pairs.add((col, False))
            else:
                val = getattr(b.right, "value", None)
                if val is not None:
                    pairs.add((col, val))
        return pairs

    pred_pairs = _extract_pairs(binaries)

    assert ("mode", "active-custom") in pred_pairs, (
        f"WHERE clause must filter on mode='active-custom'. "
        f"Found predicates: {pred_pairs}. "
        "spec: BACKEND.md §Ingestion Service — list_active_for_tier"
    )
    assert ("is_enabled", True) in pred_pairs, (
        f"WHERE clause must filter on is_enabled=True. "
        f"Found predicates: {pred_pairs}. "
        "spec: BACKEND.md §Ingestion Service — list_active_for_tier"
    )
    assert ("schedule_tier", "daily") in pred_pairs, (
        f"WHERE clause must filter on schedule_tier='daily'. "
        f"Found predicates: {pred_pairs}. "
        "spec: BACKEND.md §Ingestion Service — list_active_for_tier"
    )
