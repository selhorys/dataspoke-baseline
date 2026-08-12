"""Unit tests for DatasetService (mocked infrastructure)."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.backend.dataset.service import DatasetService
from src.backend.ingestion.service import IngestionSourceRecord
from src.shared.exceptions import EntityNotFoundError
from tests.unit.backend.conftest import (
    make_datahub_ownership,
    make_datahub_props,
    make_datahub_schema,
    make_datahub_tags,
    make_event_row,
    mock_scalar_query,
    mock_scalars_query,
)

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.public.users,PROD)"


@pytest.fixture
def service(datahub, db, cache):
    return DatasetService(datahub=datahub, db=db, cache=cache)


# ── get_summary ───────────────────────────────────────────────────────────────


async def test_get_summary_returns_dataset(service, datahub):
    props = make_datahub_props()
    ownership = make_datahub_ownership(["urn:li:corpuser:alice@example.com"])
    tags = make_datahub_tags(["urn:li:tag:pii"])

    async def fake_get_aspect(urn, cls):
        name = cls.__name__ if hasattr(cls, "__name__") else str(cls)
        if "Properties" in name:
            return props
        if "Ownership" in name:
            return ownership
        if "GlobalTags" in name:
            return tags
        return None

    datahub.get_aspect = AsyncMock(side_effect=fake_get_aspect)

    result = await service.get_summary(_DATASET_URN)
    assert result.urn == _DATASET_URN
    assert result.name == "public.users"
    assert result.platform == "postgres"
    assert result.description == "User table"
    assert result.owners == ["urn:li:corpuser:alice@example.com"]
    assert result.tags == ["urn:li:tag:pii"]


async def test_get_summary_missing_optional_aspects(service, datahub):
    props = make_datahub_props()

    async def fake_get_aspect(urn, cls):
        name = cls.__name__ if hasattr(cls, "__name__") else str(cls)
        if "Properties" in name:
            return props
        return None

    datahub.get_aspect = AsyncMock(side_effect=fake_get_aspect)

    result = await service.get_summary(_DATASET_URN)
    assert result.owners == []
    assert result.tags == []


async def test_get_summary_not_found(service, datahub):
    datahub.get_aspect = AsyncMock(return_value=None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.get_summary(_DATASET_URN)
    assert exc_info.value.error_code == "DATASET_NOT_FOUND"


async def test_get_summary_malformed_urn_platform_fallback(service, datahub):
    """A malformed dataset URN that does not match the expected URN format yields
    platform == 'unknown' in the summary — the service uses
    platform_from_dataset_urn(...) or 'unknown' as the display fallback.

    Spec: src/shared/datahub/urn.py — platform_from_dataset_urn returns None when
    the URN does not match; DatasetService.get_summary substitutes 'unknown'.
    """
    malformed_urn = "not-a-real-urn"
    props = make_datahub_props()

    async def fake_get_aspect(urn, cls):
        name = cls.__name__ if hasattr(cls, "__name__") else str(cls)
        if "Properties" in name:
            return props
        return None

    datahub.get_aspect = AsyncMock(side_effect=fake_get_aspect)

    result = await service.get_summary(malformed_urn)
    assert result.platform == "unknown"


# ── get_attributes ────────────────────────────────────────────────────────────


async def test_get_attributes_with_schema_and_quality(service, datahub, cache):
    props = make_datahub_props()
    ownership = make_datahub_ownership(["urn:li:corpuser:bob@example.com"])
    tags = make_datahub_tags(["urn:li:tag:finance"])
    schema = make_datahub_schema(["id", "name", "email"])

    async def fake_get_aspect(urn, cls):
        name = cls.__name__ if hasattr(cls, "__name__") else str(cls)
        if "Properties" in name:
            return props
        if "Ownership" in name:
            return ownership
        if "GlobalTags" in name:
            return tags
        if "SchemaMetadata" in name:
            return schema
        return None

    datahub.get_aspect = AsyncMock(side_effect=fake_get_aspect)

    quality_json = json.dumps({"overall_score": 0.95, "dimensions": {"completeness": 0.9}})
    cache.get = AsyncMock(return_value=quality_json)

    result = await service.get_attributes(_DATASET_URN)
    assert result.column_count == 3
    assert result.fields == ["id", "name", "email"]
    assert result.quality_score is not None
    assert result.quality_score.overall_score == 0.95
    assert result.quality_score.dimensions == {"completeness": 0.9}


async def test_get_attributes_quality_cache_miss(service, datahub, cache, db):
    props = make_datahub_props()

    async def fake_get_aspect(urn, cls):
        name = cls.__name__ if hasattr(cls, "__name__") else str(cls)
        if "Properties" in name:
            return props
        return None

    datahub.get_aspect = AsyncMock(side_effect=fake_get_aspect)
    cache.get = AsyncMock(return_value=None)
    mock_scalar_query(db, None)

    result = await service.get_attributes(_DATASET_URN)
    assert result.quality_score is None


async def test_get_attributes_no_schema(service, datahub, cache, db):
    props = make_datahub_props()

    async def fake_get_aspect(urn, cls):
        name = cls.__name__ if hasattr(cls, "__name__") else str(cls)
        if "Properties" in name:
            return props
        return None

    datahub.get_aspect = AsyncMock(side_effect=fake_get_aspect)
    cache.get = AsyncMock(return_value=None)
    mock_scalar_query(db, None)

    result = await service.get_attributes(_DATASET_URN)
    assert result.column_count == 0
    assert result.fields == []


async def test_get_attributes_not_found(service, datahub):
    datahub.get_aspect = AsyncMock(return_value=None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.get_attributes(_DATASET_URN)
    assert exc_info.value.error_code == "DATASET_NOT_FOUND"


# ── get_events (unified per-dataset timeline) ──────────────────────────────────
#
# Spec: spec/feature/BACKEND.md §Dataset service / Event Catalogue + the Stage-4
# plan. ``DatasetService.get_events`` returns the UNION of (a) dataset-level events
# (``entity_type='dataset'`` — validation + metagen-candidate) and (b) the covering
# source's ingestion runs (reverse-lookup → ``get_events_for_source``, incl. the
# CLI-wrapper union). The merged stream is sorted newest-first, range/major-type
# filtered, and paginated in-memory; ``total_count`` is the post-filter length and
# the ``wrapper`` flag is carried from the source rows.


def _make_source_record(source_id: str = "src-1") -> IngestionSourceRecord:
    """Minimal owning-source value object for reverse_lookup stubs."""
    now = datetime.now(tz=UTC)
    return IngestionSourceRecord(
        id=source_id,
        mode="datahub_managed",
        name="orders-source",
        platform="postgres",
        recipe={},
        schedule=None,
        schedule_tier=None,
        datahub_source_urn=None,
        parent_source_id=None,
        status="active",
        created_at=now,
        updated_at=now,
    )


def _source_event_dict(
    *,
    event_type: str,
    status: str = "success",
    minutes_ago: int = 5,
    wrapper: bool = False,
    entity_id: str = "src-1",
) -> dict:
    """One row shaped like ``IngestionService.get_events_for_source`` output."""
    return {
        "id": str(uuid.uuid4()),
        "entity_type": "ingestion_source",
        "entity_id": entity_id,
        "event_type": event_type,
        "status": status,
        "detail": {"source": "test"},
        "occurred_at": datetime.now(tz=UTC) - timedelta(minutes=minutes_ago),
        "wrapper": wrapper,
    }


def _stub_ingestion(service, *, source, source_events):
    """Replace the composed IngestionService with reverse_lookup +
    get_events_for_source stubs (the timeline's source-side dependency)."""
    ing = MagicMock()
    ing.reverse_lookup = AsyncMock(return_value=source)
    ing.get_events_for_source = AsyncMock(return_value=(source_events, len(source_events)))
    service._ingestion = ing
    return ing


async def test_get_events_unions_dataset_and_ingestion(service, db):
    """The timeline merges dataset-level events with the covering source's
    ingestion runs in one newest-first stream."""
    # (a) dataset-level: a validation event (5m ago) + a metagen-candidate (15m ago)
    dataset_rows = [
        make_event_row(
            entity_type="dataset",
            entity_id=_DATASET_URN,
            event_type="VALIDATION.RESULT_RECORDED",
            minutes_ago=5,
        ),
        make_event_row(
            entity_type="dataset",
            entity_id=_DATASET_URN,
            event_type="METAGEN.CANDIDATE_APPROVE",
            minutes_ago=15,
        ),
    ]
    mock_scalars_query(db, dataset_rows)
    # (b) source-side: one ingestion run, 1m ago (newest overall)
    _stub_ingestion(
        service,
        source=_make_source_record(),
        source_events=[_source_event_dict(event_type="INGESTION.COMPLETE", minutes_ago=1)],
    )

    events, total = await service.get_events(_DATASET_URN, offset=0, limit=20)

    assert total == 3
    event_types = [e.event_type for e in events]
    assert "INGESTION.COMPLETE" in event_types
    assert "VALIDATION.RESULT_RECORDED" in event_types
    assert "METAGEN.CANDIDATE_APPROVE" in event_types
    # Newest-first: ingestion run (1m) > validation (5m) > metagen (15m)
    assert event_types == [
        "INGESTION.COMPLETE",
        "VALIDATION.RESULT_RECORDED",
        "METAGEN.CANDIDATE_APPROVE",
    ]


async def test_get_events_no_covering_source(service, db):
    """With no owning source, the timeline is just the dataset-level events."""
    dataset_rows = [
        make_event_row(
            entity_type="dataset",
            entity_id=_DATASET_URN,
            event_type="VALIDATION.RESULT_RECORDED",
            minutes_ago=5,
        ),
    ]
    mock_scalars_query(db, dataset_rows)
    ing = _stub_ingestion(service, source=None, source_events=[])

    events, total = await service.get_events(_DATASET_URN)

    assert total == 1
    assert events[0].event_type == "VALIDATION.RESULT_RECORDED"
    # No source → source-event aggregation is never queried.
    ing.get_events_for_source.assert_not_awaited()


async def test_get_events_empty(service, db):
    mock_scalars_query(db, [])
    _stub_ingestion(service, source=None, source_events=[])

    events, total = await service.get_events(_DATASET_URN)
    assert total == 0
    assert events == []


async def test_get_events_major_type_filter_narrows(service, db):
    """A single major-type prefix narrows the stream and total_count."""
    dataset_rows = [
        make_event_row(
            entity_type="dataset",
            entity_id=_DATASET_URN,
            event_type="VALIDATION.RESULT_RECORDED",
            minutes_ago=5,
        ),
        make_event_row(
            entity_type="dataset",
            entity_id=_DATASET_URN,
            event_type="METAGEN.CANDIDATE_REJECT",
            minutes_ago=15,
        ),
    ]
    mock_scalars_query(db, dataset_rows)
    _stub_ingestion(
        service,
        source=_make_source_record(),
        source_events=[_source_event_dict(event_type="INGESTION.COMPLETE", minutes_ago=1)],
    )

    events, total = await service.get_events(
        _DATASET_URN, event_type_prefixes={"VALIDATION."}
    )

    assert total == 1
    assert [e.event_type for e in events] == ["VALIDATION.RESULT_RECORDED"]


async def test_get_events_multi_prefix_filter(service, db):
    """Multiple major-type prefixes keep events matching any prefix; the others
    (e.g. METAGEN) are dropped and total_count reflects the narrowed set."""
    dataset_rows = [
        make_event_row(
            entity_type="dataset",
            entity_id=_DATASET_URN,
            event_type="VALIDATION.RESULT_RECORDED",
            minutes_ago=5,
        ),
        make_event_row(
            entity_type="dataset",
            entity_id=_DATASET_URN,
            event_type="METAGEN.CANDIDATE_APPROVE",
            minutes_ago=15,
        ),
    ]
    mock_scalars_query(db, dataset_rows)
    _stub_ingestion(
        service,
        source=_make_source_record(),
        source_events=[_source_event_dict(event_type="INGESTION.COMPLETE", minutes_ago=1)],
    )

    events, total = await service.get_events(
        _DATASET_URN, event_type_prefixes={"INGESTION.", "VALIDATION."}
    )

    assert total == 2
    types = {e.event_type for e in events}
    assert types == {"INGESTION.COMPLETE", "VALIDATION.RESULT_RECORDED"}
    assert "METAGEN.CANDIDATE_APPROVE" not in types


async def test_get_events_wrapper_flag_carried(service, db):
    """A run mirrored from a CLI-wrapper source carries wrapper=True; a dataset
    event carries wrapper=False."""
    dataset_rows = [
        make_event_row(
            entity_type="dataset",
            entity_id=_DATASET_URN,
            event_type="VALIDATION.RESULT_RECORDED",
            minutes_ago=5,
        ),
    ]
    mock_scalars_query(db, dataset_rows)
    _stub_ingestion(
        service,
        source=_make_source_record(),
        source_events=[
            _source_event_dict(
                event_type="INGESTION.COMPLETE",
                minutes_ago=1,
                wrapper=True,
                entity_id="wrapper-9",
            )
        ],
    )

    events, _ = await service.get_events(_DATASET_URN)

    by_type = {e.event_type: e for e in events}
    assert by_type["INGESTION.COMPLETE"].wrapper is True
    assert by_type["VALIDATION.RESULT_RECORDED"].wrapper is False


async def test_get_events_with_time_range(service, db):
    """from/to bound the merged stream by occurred_at; out-of-window rows drop."""
    now = datetime.now(tz=UTC)
    dataset_rows = [
        make_event_row(
            entity_type="dataset",
            entity_id=_DATASET_URN,
            event_type="VALIDATION.RESULT_RECORDED",
            minutes_ago=5,  # inside window
        ),
        make_event_row(
            entity_type="dataset",
            entity_id=_DATASET_URN,
            event_type="VALIDATION.CONFIG_CREATE",
            minutes_ago=600,  # 10h ago — outside window
        ),
    ]
    mock_scalars_query(db, dataset_rows)
    _stub_ingestion(service, source=None, source_events=[])

    from_dt = now - timedelta(minutes=60)
    to_dt = now

    events, total = await service.get_events(_DATASET_URN, from_dt=from_dt, to_dt=to_dt)

    assert total == 1
    assert [e.event_type for e in events] == ["VALIDATION.RESULT_RECORDED"]


async def test_get_events_pagination_total_count(service, db):
    """total_count is the full post-filter length; offset/limit page the stream."""
    # 5 dataset events at 10,20,30,40,50 min ago (newest-first ordering).
    dataset_rows = [
        make_event_row(
            entity_type="dataset",
            entity_id=_DATASET_URN,
            event_type="VALIDATION.RESULT_RECORDED",
            minutes_ago=(i + 1) * 10,
        )
        for i in range(5)
    ]
    mock_scalars_query(db, dataset_rows)
    _stub_ingestion(service, source=None, source_events=[])

    page, total = await service.get_events(_DATASET_URN, offset=0, limit=2)
    assert total == 5
    assert len(page) == 2

    page2, total2 = await service.get_events(_DATASET_URN, offset=4, limit=2)
    assert total2 == 5
    # Only one row remains after offset 4.
    assert len(page2) == 1


# ── The source feed is narrowed to THIS dataset ───────────────────────────────


async def test_get_events_narrows_the_source_feed_to_this_dataset(service, db):
    """The covering source's feed is requested narrowed to this dataset's URN.

    The source's feed is estate-wide: it carries one per-dataset observation per mapped
    dataset per observed instant, so without the narrowing every sibling dataset's
    observations would appear on this dataset's timeline (and, at a high enough volume,
    push its own run-level rows off the page the service reads).

    ``dataset_urn`` is asserted to arrive as a **keyword**, which is the contract rather
    than a style preference: positionally the parameter would sit after ``order_by: Any``,
    where a positional caller's ``order_by`` would land in it silently. This test covers
    the caller's half only — the callee's half (that the parameter is keyword-*only*, so
    such a call cannot bind at all) is pinned in
    ``tests/unit/backend/ingestion/test_service.py``
    ``::TestGetEventsForSourceRejectsAPositionalDatasetUrn``.

    spec: feature/BACKEND.md §Querying Events — the per-dataset timeline unions the
        dataset-level events with "the covering source's ingestion runs **and its
        observations for this dataset**, resolved by reverse-lookup plus the
        ``detail.dataset_urn`` predicate".
    spec: feature/BACKEND_SCHEMA.md §events — "The source's rows are narrowed to those
        whose ``detail.dataset_urn`` is this URN **or is absent**".
    """
    mock_scalars_query(db, [])
    ing = _stub_ingestion(
        service,
        source=_make_source_record(),
        source_events=[_source_event_dict(event_type="INGESTION.COMPLETE", minutes_ago=1)],
    )

    events, total = await service.get_events(_DATASET_URN, offset=0, limit=20)

    assert total == 1, (
        f"backstop: the source feed must actually have been read, or the call assertion "
        f"below inspects a call that never happened; got total={total}."
    )
    assert events[0].event_type == "INGESTION.COMPLETE"
    ing.get_events_for_source.assert_awaited_once()
    kwargs = ing.get_events_for_source.await_args.kwargs
    assert kwargs.get("dataset_urn") == _DATASET_URN, (
        f"the source feed must be requested narrowed to this dataset URN, passed by "
        f"keyword; got kwargs={kwargs!r} args={ing.get_events_for_source.await_args.args!r}. "
        "spec: feature/BACKEND.md §Querying Events."
    )
