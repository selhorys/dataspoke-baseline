"""Unit tests for the pct_fresh measurer (ingestion_freshness.py).

Tests the spec-mandated calculation:
- Empty datasets list returns (0.0, breakdown with dataset_count=0, empty datasets list).
- Dataset with INGESTION.COMPLETE within freshness_days window → category="fresh".
- Dataset with no INGESTION.COMPLETE event → category="stale".
- Dataset with INGESTION.COMPLETE older than freshness_days → category="stale".
- freshness_days default is 1; custom values are respected.
- Returned breakdown shape: dataset_count, datasets[]{urn, category, detail}.
- Per-dataset detail includes last_event_at.

spec: feature/BACKEND.md §Metrics Service — pct_fresh measurer:
      a dataset is fresh iff its latest INGESTION.COMPLETE event occurred within
      freshness_days; stale otherwise; no event → stale; returns stale_count.
      Breakdown format: {"dataset_count": <int>, "datasets": [{"urn": ..., "category": ...,
      "detail": {...}}]} (BACKEND.md lines 610-622).
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.backend.metrics.measurers.ingestion_freshness import measure


@pytest.mark.asyncio
async def test_empty_datasets_returns_zero_stale() -> None:
    """measure([]) must return (0.0, breakdown with all zeros).

    spec: feature/BACKEND.md §Metrics Service — empty list produces zero counts.
    """
    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.all.return_value = []
    db.execute = AsyncMock(return_value=execute_result)

    value, breakdown = await measure(
        datasets=[],
        datahub=MagicMock(),
        db=db,
    )
    assert value == 0.0
    assert breakdown["dataset_count"] == 0
    assert breakdown["datasets"] == []


@pytest.mark.asyncio
async def test_dataset_with_recent_ingestion_is_fresh() -> None:
    """Dataset with INGESTION.COMPLETE within freshness_days=1 is categorised as fresh.

    spec: feature/BACKEND.md §Metrics Service — dataset is fresh iff last event is within window.
    """
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.catalog.title_master,DEV)"
    recent = datetime.now(tz=UTC) - timedelta(hours=1)

    row = MagicMock()
    row.entity_id = urn
    row.occurred_at = recent

    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.all.return_value = [row]
    db.execute = AsyncMock(return_value=execute_result)

    value, breakdown = await measure(
        datasets=[urn],
        datahub=MagicMock(),
        db=db,
        freshness_days=1,
    )

    assert value == 0.0, "No stale datasets expected"
    assert breakdown["datasets"][0]["category"] == "fresh"


@pytest.mark.asyncio
async def test_dataset_with_no_ingestion_event_is_stale() -> None:
    """Dataset with no INGESTION.COMPLETE event is categorised as stale.

    spec: feature/BACKEND.md §Metrics Service — no event → stale.
    """
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.catalog.editions,DEV)"

    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.all.return_value = []  # no event rows
    db.execute = AsyncMock(return_value=execute_result)

    value, breakdown = await measure(
        datasets=[urn],
        datahub=MagicMock(),
        db=db,
        freshness_days=1,
    )

    assert value == 1.0, "One stale dataset expected"
    ds = breakdown["datasets"][0]
    assert ds["category"] == "stale"
    assert ds["detail"]["last_event_at"] is None


@pytest.mark.asyncio
async def test_dataset_with_old_ingestion_event_is_stale() -> None:
    """Dataset whose last INGESTION.COMPLETE is older than freshness_days is stale.

    spec: feature/BACKEND.md §Metrics Service — event outside freshness window → stale.
    """
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders.raw_events,DEV)"
    stale_ts = datetime.now(tz=UTC) - timedelta(days=3)

    row = MagicMock()
    row.entity_id = urn
    row.occurred_at = stale_ts

    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.all.return_value = [row]
    db.execute = AsyncMock(return_value=execute_result)

    value, breakdown = await measure(
        datasets=[urn],
        datahub=MagicMock(),
        db=db,
        freshness_days=1,
    )

    assert value == 1.0, "Old event is outside window → stale"
    assert breakdown["datasets"][0]["category"] == "stale"


@pytest.mark.asyncio
async def test_custom_freshness_days_respected() -> None:
    """freshness_days parameter controls the staleness cutoff.

    spec: feature/BACKEND.md §Metrics Service — freshness_days taken from measurement_query.
    An event 2 days ago is stale with freshness_days=1 but fresh with freshness_days=7.
    """
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.shipping.carrier_status,DEV)"
    two_days_ago = datetime.now(tz=UTC) - timedelta(days=2)

    row = MagicMock()
    row.entity_id = urn
    row.occurred_at = two_days_ago

    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.all.return_value = [row]
    db.execute = AsyncMock(return_value=execute_result)

    # With freshness_days=1: stale
    value_1, breakdown_1 = await measure(
        datasets=[urn], datahub=MagicMock(), db=db, freshness_days=1
    )
    assert value_1 == 1.0
    assert breakdown_1["datasets"][0]["category"] == "stale"

    # Reset mock, same rows
    db.execute = AsyncMock(return_value=execute_result)

    # With freshness_days=7: fresh
    value_7, breakdown_7 = await measure(
        datasets=[urn], datahub=MagicMock(), db=db, freshness_days=7
    )
    assert value_7 == 0.0
    assert breakdown_7["datasets"][0]["category"] == "fresh"


@pytest.mark.asyncio
async def test_breakdown_contains_required_keys() -> None:
    """Returned breakdown must contain dataset_count and datasets with urn/category/detail.

    spec: feature/BACKEND.md §Metrics Service lines 610-622 — breakdown shape:
          {"dataset_count": <int>, "datasets": [{"urn": ..., "category": ..., "detail": {...}}]}.
          detail for pct_fresh includes last_event_at.
    """
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.test,DEV)"

    db = AsyncMock()
    execute_result = MagicMock()
    execute_result.all.return_value = []
    db.execute = AsyncMock(return_value=execute_result)

    _, breakdown = await measure(datasets=[urn], datahub=MagicMock(), db=db)

    for key in ("dataset_count", "datasets"):
        assert key in breakdown, (
            f"breakdown must contain '{key}'; spec: feature/BACKEND.md §Metrics Service."
        )

    ds = breakdown["datasets"][0]
    assert "urn" in ds
    assert "category" in ds
    assert "detail" in ds
    assert "last_event_at" in ds["detail"]
