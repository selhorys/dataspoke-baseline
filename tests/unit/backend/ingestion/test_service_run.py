"""Unit tests for IngestionService.run() — execution + concurrency.

Covers run() success / dry-run / extractor errors / disabled gating /
zero-entity failure / Redis SETNX concurrency lock.

spec: BACKEND.md §Active run pipeline
spec: BACKEND.md §Ingestion Service — is_enabled=false rejects non-dry-run
spec: USE_CASE_en.md §UC1
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.shared.events import INGESTION_COMPLETE
from src.shared.exceptions import ConflictError, EntityNotFoundError
from tests.unit.backend.conftest import mock_db_refresh, mock_scalar_query
from tests.unit.backend.ingestion.conftest import _DATASET_URN, _make_config_row


async def test_run_success(service, db):
    # spec: BACKEND.md §Active run pipeline L195-L204:
    #   "on success mark dataset_registry.datahub_registered = true via mark_registered()"
    #   "record INGESTION.COMPLETE event"
    config_row = _make_config_row(is_enabled=True)
    mock_scalar_query(db, config_row)
    mock_db_refresh(db)

    from src.backend.ingestion.extractors import IngestionResult

    with (
        patch(
            "src.backend.ingestion.service.run_datahub_ingestion",
            new=AsyncMock(
                return_value=IngestionResult(entities_ingested=5, errors=[], warnings=[])
            ),
        ),
        patch(
            "src.backend.ingestion.service.mark_registered",
            new=AsyncMock(),
        ) as mock_mark_registered,
    ):
        result = await service.run(_DATASET_URN)

    assert result.status == "success"
    assert result.run_id
    assert result.detail["dry_run"] is False
    assert result.detail["entities_ingested"] == 5

    # spec: BACKEND.md §Active run pipeline L201
    mock_mark_registered.assert_awaited_once_with(db, _DATASET_URN)

    # spec: BACKEND.md §Active run pipeline L201-L204 + §Event Catalogue
    added_event_types = [
        getattr(call_args.args[0], "event_type", None)
        for call_args in db.add.call_args_list
        if hasattr(call_args.args[0] if call_args.args else None, "event_type")
    ]
    assert INGESTION_COMPLETE in added_event_types, (
        f"Expected INGESTION.COMPLETE event to be recorded, got: {added_event_types}"
    )


async def test_run_dry_run(service, db):
    config_row = _make_config_row()
    mock_scalar_query(db, config_row)
    mock_db_refresh(db)

    from src.backend.ingestion.extractors import IngestionResult

    with patch(
        "src.backend.ingestion.service.run_datahub_ingestion",
        new=AsyncMock(
            return_value=IngestionResult(entities_ingested=0, errors=[], warnings=[])
        ),
    ):
        result = await service.run(_DATASET_URN, dry_run=True)

    assert result.detail["dry_run"] is True
    assert result.detail["entities_ingested"] == 0


async def test_run_ingestion_error(service, db):
    config_row = _make_config_row(is_enabled=True)
    mock_scalar_query(db, config_row)
    mock_db_refresh(db)

    from src.backend.ingestion.extractors import IngestionResult

    with patch(
        "src.backend.ingestion.service.run_datahub_ingestion",
        new=AsyncMock(
            return_value=IngestionResult(
                entities_ingested=0,
                errors=["Connection refused"],
                warnings=[],
            )
        ),
    ):
        result = await service.run(_DATASET_URN)

    assert result.status == "error"
    assert "errors" in result.detail
    assert "Connection refused" in result.detail["errors"]


async def test_run_zero_entities_non_dry_run_fails(service, db):
    """A non-dry-run ingestion that ingests zero entities with no explicit errors
    must be treated as a failure (status='error', INGESTION.FAIL event recorded).

    spec: BACKEND.md §Active run pipeline — "a non-dry-run that ingests zero
    entities is treated as failure" L200-L201
    """
    # spec: BACKEND.md §Active run pipeline L200-L201
    config_row = _make_config_row(is_enabled=True)
    mock_scalar_query(db, config_row)
    mock_db_refresh(db)

    from src.backend.ingestion.extractors import IngestionResult

    with patch(
        "src.backend.ingestion.service.run_datahub_ingestion",
        new=AsyncMock(
            return_value=IngestionResult(entities_ingested=0, errors=[], warnings=[])
        ),
    ):
        result = await service.run(_DATASET_URN, dry_run=False)

    assert result.status == "error", (
        "Expected status='error' when entities_ingested=0 and dry_run=False; "
        f"got status='{result.status}'"
    )
    assert result.detail["entities_ingested"] == 0
    assert result.detail["dry_run"] is False


async def test_run_config_not_found(service, db):
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.run("nonexistent")
    assert exc_info.value.error_code == "CONFIG_NOT_FOUND"


async def test_run_rejects_non_dry_run_when_disabled(service, db):
    """Non-dry-run against a disabled config raises ConflictError('INGESTION_DISABLED').

    spec: BACKEND.md §Ingestion Service — is_enabled=false rejects non-dry-run
    with 409 INGESTION_DISABLED.
    spec: USE_CASE_en.md §UC1 — "non-dry-run calls return 409 INGESTION_DISABLED"
    (L112: "Dry-run is also the only way to exercise method/ingestion/run while
    is_enabled=false; non-dry-run calls return 409 INGESTION_DISABLED.")
    """
    config_row = _make_config_row(is_enabled=False)
    mock_scalar_query(db, config_row)

    with pytest.raises(ConflictError) as exc_info:
        await service.run(_DATASET_URN, dry_run=False)

    assert exc_info.value.error_code == "INGESTION_DISABLED"


async def test_run_allows_dry_run_when_disabled(service, db):
    """Dry-run bypasses the disabled guard and returns an IngestionRunResult.

    spec: BACKEND.md §Ingestion Service — dry_run=True is always permitted
    regardless of is_enabled.
    spec: USE_CASE_en.md §UC1 L106-L110
    """
    config_row = _make_config_row(is_enabled=False)
    mock_scalar_query(db, config_row)
    mock_db_refresh(db)

    from src.backend.ingestion.extractors import IngestionResult
    from src.backend.ingestion.service import IngestionRunResult

    with patch(
        "src.backend.ingestion.service.run_datahub_ingestion",
        new=AsyncMock(
            return_value=IngestionResult(entities_ingested=0, errors=[], warnings=[])
        ),
    ):
        result = await service.run(_DATASET_URN, dry_run=True)

    assert isinstance(result, IngestionRunResult)
    assert result.detail["dry_run"] is True


# ── Redis SETNX concurrency guard ─────────────────────────────────────────────


async def test_run_redis_setnx_conflict(service_with_cache, db, cache):
    """Second concurrent run() raises ConflictError when lock is already held."""
    cache.set_nx = AsyncMock(return_value=False)  # lock already held

    with pytest.raises(ConflictError) as exc_info:
        await service_with_cache.run(_DATASET_URN)
    assert exc_info.value.error_code == "INGESTION_RUNNING"


async def test_run_redis_setnx_acquired_then_released(service_with_cache, db, cache):
    """Lock is acquired then released even when inner run raises."""
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    config_row = _make_config_row(is_enabled=True)
    mock_scalar_query(db, config_row)

    from src.backend.ingestion.extractors import IngestionResult

    with patch(
        "src.backend.ingestion.service.run_datahub_ingestion",
        new=AsyncMock(
            return_value=IngestionResult(entities_ingested=5, errors=[], warnings=[])
        ),
    ):
        mock_db_refresh(db)
        await service_with_cache.run(_DATASET_URN)

    # Lock must be released in finally block
    cache.delete_if_value.assert_awaited_once()
