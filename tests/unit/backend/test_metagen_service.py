"""Unit tests for src/backend/metagen/service.py — MetagenService."""

import uuid
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from src.backend.metagen.service import MetagenService, _build_initial_field_status
from src.shared.exceptions import ConflictError, EntityNotFoundError, PreconditionFailedError

from tests.unit.backend.conftest import make_metagen_result_row, mock_db_refresh, mock_scalar_query


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def svc(datahub: AsyncMock, db: AsyncMock, cache: AsyncMock, llm: AsyncMock) -> MetagenService:
    return MetagenService(datahub=datahub, db=db, llm=llm, cache=cache)


# ── Config CRUD — enum validation ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_config_rejects_invalid_target(svc: MetagenService) -> None:
    """upsert_config raises PreconditionFailedError for unknown target values."""
    with pytest.raises(PreconditionFailedError) as exc_info:
        await svc.upsert_config(
            dataset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,t,PROD)",
            targets=["dataset.description", "invalid_target"],
            code_refs=None,
            is_enabled=False,
            schedule_tier=None,
            owner="test@example.com",
        )
    assert exc_info.value.error_code == "INVALID_PARAMETER"


@pytest.mark.asyncio
async def test_upsert_config_rejects_invalid_schedule_tier(svc: MetagenService) -> None:
    """upsert_config raises PreconditionFailedError for unknown schedule_tier."""
    with pytest.raises(PreconditionFailedError) as exc_info:
        await svc.upsert_config(
            dataset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,t,PROD)",
            targets=["dataset.description"],
            code_refs=None,
            is_enabled=False,
            schedule_tier="quarterly",
            owner="test@example.com",
        )
    assert exc_info.value.error_code == "INVALID_PARAMETER"


@pytest.mark.asyncio
async def test_upsert_config_accepts_valid_targets(svc: MetagenService, db: AsyncMock) -> None:
    """upsert_config accepts all valid target values."""
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result_mock)
    mock_db_refresh(db)

    config, created = await svc.upsert_config(
        dataset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,t,PROD)",
        targets=["dataset.description", "column.description", "cross_data.md"],
        code_refs=None,
        is_enabled=False,
        schedule_tier=None,
        owner="test@example.com",
    )
    assert created is True


# ── Concurrency guard ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_raises_conflict_when_lock_held(svc: MetagenService, cache: AsyncMock, db: AsyncMock) -> None:
    """run() raises ConflictError('GENERATION_RUNNING') when SETNX returns False."""
    cache.set_nx = AsyncMock(return_value=False)

    with pytest.raises(ConflictError) as exc_info:
        await svc.run("urn:li:dataset:(urn:li:dataPlatform:postgres,t,PROD)")

    assert exc_info.value.error_code == "GENERATION_RUNNING"


# ── list_metagen — cross-dataset ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_metagen_returns_one_row_per_dataset(svc: MetagenService, db: AsyncMock) -> None:
    """list_metagen returns results (one per dataset_urn from DISTINCT ON query)."""
    urn1 = "urn:li:dataset:(urn:li:dataPlatform:postgres,a,PROD)"
    urn2 = "urn:li:dataset:(urn:li:dataPlatform:postgres,b,PROD)"

    row1 = make_metagen_result_row(dataset_urn=urn1)
    row2 = make_metagen_result_row(dataset_urn=urn2)

    count_result = MagicMock()
    count_result.scalar.return_value = 2
    rows_result = MagicMock()
    rows_result.scalars.return_value.all.return_value = [row1, row2]
    db.execute = AsyncMock(side_effect=[count_result, rows_result])

    results, total = await svc.list_metagen()
    assert total == 2
    assert len(results) == 2
    urns = {r.dataset_urn for r in results}
    assert urn1 in urns
    assert urn2 in urns


# ── get_result — entity not found ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_result_malformed_uuid_raises_entity_not_found(svc: MetagenService) -> None:
    """get_result raises EntityNotFoundError (not ValueError) for malformed UUID."""
    with pytest.raises(EntityNotFoundError):
        await svc.get_result("not-a-uuid-at-all")


@pytest.mark.asyncio
async def test_get_result_absent_raises_entity_not_found(svc: MetagenService, db: AsyncMock) -> None:
    """get_result raises EntityNotFoundError when result row is absent."""
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result_mock)

    with pytest.raises(EntityNotFoundError):
        await svc.get_result(str(uuid.uuid4()))


# ── review_result ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_review_result_approve_all_flips_all_fields(svc: MetagenService, db: AsyncMock) -> None:
    """review_result(approve, fields=None) sets all field_status entries to 'approved'."""
    row = make_metagen_result_row(
        field_status={
            "dataset.description": "pending",
            "column.description.id": "pending",
        }
    )
    row.proposals = {"dataset.description": "A dataset", "column.description": {"id": "Primary key"}}

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = row
    db.execute = AsyncMock(return_value=result_mock)
    mock_db_refresh(db)

    # Mock DataHub for apply_approved_fields
    svc._datahub.emit_aspect = AsyncMock()
    svc._datahub.get_aspect = AsyncMock(return_value=None)

    record = await svc.review_result(str(row.id), verdict="approve", fields=None)
    for status_val in record.field_status.values():
        assert status_val == "approved"


@pytest.mark.asyncio
async def test_review_result_approve_partial_flips_only_listed_fields(
    svc: MetagenService, db: AsyncMock
) -> None:
    """review_result(approve, fields=[...]) flips only the listed fields."""
    row = make_metagen_result_row(
        field_status={
            "dataset.description": "pending",
            "column.description.id": "pending",
        }
    )
    row.proposals = {"dataset.description": "Desc", "column.description": {"id": "PK"}}

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = row
    db.execute = AsyncMock(return_value=result_mock)
    mock_db_refresh(db)

    svc._datahub.emit_aspect = AsyncMock()
    svc._datahub.get_aspect = AsyncMock(return_value=None)

    record = await svc.review_result(
        str(row.id), verdict="approve", fields=["dataset.description"]
    )
    assert record.field_status["dataset.description"] == "approved"
    assert record.field_status["column.description.id"] == "pending"


@pytest.mark.asyncio
async def test_review_result_reject_flips_to_rejected(svc: MetagenService, db: AsyncMock) -> None:
    """review_result(reject, fields=None) sets all field_status entries to 'rejected'."""
    row = make_metagen_result_row(
        field_status={
            "dataset.description": "pending",
            "column.description.id": "pending",
        }
    )
    row.proposals = {}

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = row
    db.execute = AsyncMock(return_value=result_mock)
    mock_db_refresh(db)

    record = await svc.review_result(str(row.id), verdict="reject", fields=None)
    for status_val in record.field_status.values():
        assert status_val == "rejected"


@pytest.mark.asyncio
async def test_review_result_rejects_invalid_verdict(svc: MetagenService) -> None:
    """review_result raises PreconditionFailedError for unknown verdicts."""
    with pytest.raises(PreconditionFailedError) as exc_info:
        await svc.review_result(str(uuid.uuid4()), verdict="maybe")
    assert exc_info.value.error_code == "INVALID_PARAMETER"


# ── read-modify-write for EditableSchemaMetadata ──────────────────────────────


@pytest.mark.asyncio
async def test_apply_approved_fields_reads_existing_schema_before_emit(
    svc: MetagenService, db: AsyncMock, datahub: AsyncMock
) -> None:
    """_apply_approved_fields fetches existing EditableSchemaMetadata before emitting (Fix #10)."""
    from unittest.mock import call as mock_call

    dataset_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,t,PROD)"
    proposals = {
        "column.description": {"id": "Primary key", "name": "Name column"},
    }
    approved_fields = ["column.description.id"]

    # Simulate existing editable schema with one prior field
    prior_field = MagicMock()
    prior_field.fieldPath = "name"
    prior_field.description = "Prior approved description"
    existing_schema = MagicMock()
    existing_schema.editableSchemaFieldInfo = [prior_field]

    datahub.get_aspect = AsyncMock(return_value=existing_schema)
    datahub.emit_aspect = AsyncMock()

    await svc._apply_approved_fields(dataset_urn, proposals, approved_fields)

    # get_aspect must be called BEFORE emit_aspect (read-modify-write)
    datahub.get_aspect.assert_called()
    datahub.emit_aspect.assert_called()

    # Verify call order: get_aspect before emit_aspect
    get_idx = None
    emit_idx = None
    for i, c in enumerate(datahub.method_calls):
        if c[0] == "get_aspect" and get_idx is None:
            get_idx = i
        if c[0] == "emit_aspect" and emit_idx is None:
            emit_idx = i
    if get_idx is not None and emit_idx is not None:
        assert get_idx < emit_idx


# ── _build_initial_field_status helper ───────────────────────────────────────


def test_build_initial_field_status_dataset_description() -> None:
    """dataset.description maps to a single 'pending' key."""
    status = _build_initial_field_status({"dataset.description": "some text"})
    assert status == {"dataset.description": "pending"}


def test_build_initial_field_status_column_description() -> None:
    """column.description maps to column.description.{fieldPath} per field."""
    status = _build_initial_field_status({
        "column.description": {"id": "PK", "name": "Name"},
    })
    assert status["column.description.id"] == "pending"
    assert status["column.description.name"] == "pending"


def test_build_initial_field_status_cross_data_md() -> None:
    """cross_data.md maps to cross_data.md.{action_id} per action."""
    status = _build_initial_field_status({
        "cross_data.md": [
            {"action_id": "action-001", "action": "create"},
            {"action_id": "action-002", "action": "modify"},
        ]
    })
    assert status["cross_data.md.action-001"] == "pending"
    assert status["cross_data.md.action-002"] == "pending"


# ── last_reviewed_at updated on review ───────────────────────────────────────


@pytest.mark.asyncio
async def test_review_result_updates_last_reviewed_at(svc: MetagenService, db: AsyncMock) -> None:
    """review_result updates last_reviewed_at timestamp on the result row."""
    row = make_metagen_result_row()
    row.last_reviewed_at = None
    row.proposals = {}

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = row
    db.execute = AsyncMock(return_value=result_mock)
    mock_db_refresh(db)

    record = await svc.review_result(str(row.id), verdict="reject")
    # After review, last_reviewed_at should have been set on the row
    assert row.last_reviewed_at is not None
