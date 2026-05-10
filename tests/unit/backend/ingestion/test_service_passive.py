"""Unit tests for IngestionService passive-mode behavior + observability.

Covers passive-mode method/run rejection (INGESTION_NOT_APPLICABLE),
sync_passive_status (zero-config and per-dataset failure paths), and
get_events read API.

spec: BACKEND.md §Ingestion Service — passive rejection
spec: BACKEND.md §Ingestion passive status-sync
spec: USE_CASE_en.md §UC1 Case 2
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.shared.exceptions import ConflictError
from tests.unit.backend.conftest import (
    make_event_row,
    mock_paginated_query,
    mock_scalar_query,
)
from tests.unit.backend.ingestion.conftest import _DATASET_URN, _make_config_row


# ── sync_passive_status ───────────────────────────────────────────────────────


async def test_sync_passive_status_no_passive_configs(service, db):
    """sync_passive_status returns zeros when no passive configs exist."""
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=result_mock)

    summary = await service.sync_passive_status()
    assert summary["synced_events"] == 0
    assert summary["errors"] == 0


async def test_sync_passive_status_skips_on_datahub_failure(service, db, datahub):
    """sync_passive_status continues past per-dataset failures, counting errors."""
    config_row = _make_config_row(mode="passive")
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [config_row]
    db.execute = AsyncMock(return_value=result_mock)

    datahub._with_retry = AsyncMock(side_effect=Exception("DataHub down"))
    with patch.object(service, "_fetch_datahub_run_history", side_effect=Exception("DH error")):
        summary = await service.sync_passive_status()

    assert summary["errors"] == 1
    assert summary["synced_events"] == 0


# ── get_events ───────────────────────────────────────────────────────────────


async def test_get_events_paginated(service, db):
    rows = [
        make_event_row(
            entity_type="dataset",
            event_type="INGESTION.COMPLETE",
            entity_id=_DATASET_URN,
            minutes_ago=i,
        )
        for i in range(3)
    ]
    mock_paginated_query(db, rows, total_count=5)

    events, total = await service.get_events(_DATASET_URN, offset=0, limit=3)
    assert total == 5
    assert len(events) == 3
    assert events[0]["event_type"] == "INGESTION.COMPLETE"


async def test_get_events_empty(service, db):
    mock_paginated_query(db, [], total_count=0)

    events, total = await service.get_events(_DATASET_URN)
    assert total == 0
    assert events == []


# ── Two-mode taxonomy: passive rejection ──────────────────────────────────────


async def test_run_passive_mode_rejected_with_not_applicable(service, db):
    """method/ingestion/run on a passive config raises ConflictError(INGESTION_NOT_APPLICABLE).

    spec: BACKEND.md §Ingestion Service —
        "method/run is rejected (409 INGESTION_NOT_APPLICABLE) for passive configs
        because passive ingestion is run externally"
    spec: USE_CASE_en.md §UC1 API Mapping —
        "POST .../method/ingestion/run … passive configs return 409 INGESTION_NOT_APPLICABLE"
    spec: USE_CASE_en.md §UC1 Case 2 —
        "POST .../method/ingestion/run returns 409 INGESTION_NOT_APPLICABLE for this URN"
    """
    passive_config = _make_config_row(mode="passive", is_enabled=True, schedule_tier=None)
    mock_scalar_query(db, passive_config)

    with pytest.raises(ConflictError) as exc_info:
        await service.run(_DATASET_URN, dry_run=False)

    assert exc_info.value.error_code == "INGESTION_NOT_APPLICABLE", (
        f"Expected error_code='INGESTION_NOT_APPLICABLE' for passive run; "
        f"got {exc_info.value.error_code!r}. "
        "spec: BACKEND.md §Ingestion Service passive rejection"
    )


async def test_run_passive_mode_rejected_with_not_applicable_even_on_dry_run(service, db):
    """Passive rejection fires regardless of dry_run flag.

    spec: BACKEND.md §Ingestion Service —
        "passive configs have no DataSpoke-side run pipeline;
        the rejection must fire before the is_enabled guard"
    spec: USE_CASE_en.md §UC1 API Mapping — INGESTION_NOT_APPLICABLE is unconditional
    for passive mode (no dry_run exemption unlike INGESTION_DISABLED).
    """
    passive_config = _make_config_row(mode="passive", is_enabled=True, schedule_tier=None)
    mock_scalar_query(db, passive_config)

    with pytest.raises(ConflictError) as exc_info:
        await service.run(_DATASET_URN, dry_run=True)

    assert exc_info.value.error_code == "INGESTION_NOT_APPLICABLE", (
        f"Expected INGESTION_NOT_APPLICABLE even for dry_run=True on passive config; "
        f"got {exc_info.value.error_code!r}."
    )
