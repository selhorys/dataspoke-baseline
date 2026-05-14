"""Unit tests for src/backend/metagen/service.py — MetagenService."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from src.backend.metagen.service import (
    MetagenResultRecord,
    MetagenService,
    _build_initial_field_status,
)
from src.shared.db.models import Event
from src.shared.events import METAGEN_COMPLETE
from src.shared.exceptions import ConflictError, EntityNotFoundError, PreconditionFailedError
from tests.unit.backend.conftest import make_metagen_result_row, mock_db_refresh, mock_scalar_query

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.public.users,PROD)"


def _make_metagen_config_row(
    dataset_urn: str = _DATASET_URN,
    targets: list | None = None,
    is_enabled: bool = False,
    schedule_tier: str | None = None,
    owner: str = "alice@example.com",
):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.dataset_urn = dataset_urn
    row.targets = targets if targets is not None else ["dataset.description"]
    row.code_refs = None
    row.is_enabled = is_enabled
    row.schedule_tier = schedule_tier
    row.status = "active"
    row.owner = owner
    row.created_at = datetime.now(tz=UTC)
    row.updated_at = datetime.now(tz=UTC)
    return row


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


@pytest.mark.asyncio
async def test_run_rejects_non_dry_run_when_disabled(svc: MetagenService, cache: AsyncMock, db: AsyncMock) -> None:
    """Non-dry-run against a disabled config raises ConflictError('GENERATION_DISABLED').

    spec: BACKEND.md §Metadata Generation Service — is_enabled=false rejects
    non-dry-run with 409 GENERATION_DISABLED.
    spec: USE_CASE_en.md §UC4 — "When is_enabled=false, non-dry-run calls to
    method/metagen/run return 409 GENERATION_DISABLED. Dry-run is always
    permitted regardless of is_enabled." (L628)
    """
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    config_row = _make_metagen_config_row(is_enabled=False)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = config_row
    db.execute = AsyncMock(return_value=result_mock)

    with pytest.raises(ConflictError) as exc_info:
        await svc.run(_DATASET_URN, dry_run=False)

    assert exc_info.value.error_code == "GENERATION_DISABLED"


@pytest.mark.asyncio
async def test_run_allows_dry_run_when_disabled(svc: MetagenService, cache: AsyncMock, db: AsyncMock) -> None:
    """Dry-run bypasses the disabled guard and returns a MetagenResultRecord.

    spec: BACKEND.md §Metadata Generation Service — dry_run=True is always
    permitted regardless of is_enabled.
    spec: USE_CASE_en.md §UC4 (disabled gate mirrors UC1 pattern)
    """
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    config_row = _make_metagen_config_row(is_enabled=False, targets=["dataset.description"])
    config_result = MagicMock()
    config_result.scalar_one_or_none.return_value = config_row

    node_map_result = MagicMock()
    node_map_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(side_effect=[config_result, node_map_result])

    # Stub shape mirrors `_gather_evidence` return; not a spec contract.
    _evidence_stub = {
        "dataset_name": "test",
        "description": "",
        "schema_fields": [],
        "editable_description": None,
        "editable_field_descriptions": [],
        "glossary_terms": [],
        "related_documents": [],
        "ontogen_node_ids": [],
        "ontogen_triples": [],
    }
    with (
        patch.object(svc, "_gather_evidence", new=AsyncMock(return_value=_evidence_stub)),
        patch.object(svc, "_propose_target", new=AsyncMock(return_value="Generated description")),
    ):
        result = await svc.run(_DATASET_URN, dry_run=True)

    assert isinstance(result, MetagenResultRecord)
    assert result.dataset_urn == _DATASET_URN


@pytest.mark.asyncio
async def test_run_real_run_emits_complete_event_with_dry_run_false(
    svc: MetagenService, cache: AsyncMock, db: AsyncMock
) -> None:
    """Real-run (dry_run=False) emits exactly one METAGEN.COMPLETE Event with dry_run=False.

    spec: spec/feature/BACKEND.md §Metadata Generation Service §Run pipeline step 8
    — 'Emit METAGEN.COMPLETE'; dry_run key is present with value False on the real-run path.
    spec: spec/USE_CASE_en.md §UC4 — real-run persists a MetagenResult and emits the event.
    """
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    config_row = _make_metagen_config_row(is_enabled=True, targets=["dataset.description"])
    config_result = MagicMock()
    config_result.scalar_one_or_none.return_value = config_row

    node_map_result = MagicMock()
    node_map_result.scalars.return_value.all.return_value = []

    db.execute = AsyncMock(side_effect=[config_result, node_map_result])
    mock_db_refresh(db)

    _evidence_stub = {
        "dataset_name": "test",
        "description": "",
        "schema_fields": [],
        "editable_description": None,
        "editable_field_descriptions": [],
        "glossary_terms": [],
        "related_documents": [],
        "ontogen_node_ids": [],
        "ontogen_triples": [],
    }
    with (
        patch.object(svc, "_gather_evidence", new=AsyncMock(return_value=_evidence_stub)),
        patch.object(svc, "_propose_target", new=AsyncMock(return_value="Generated description")),
    ):
        result = await svc.run(_DATASET_URN, dry_run=False)

    assert isinstance(result, MetagenResultRecord)
    assert result.dataset_urn == _DATASET_URN

    # Exactly one Event row must be added with METAGEN_COMPLETE and dry_run=False.
    # spec: spec/feature/BACKEND.md §Run pipeline step 8 — dry_run key in detail.
    added_args = [c.args[0] for c in db.add.call_args_list]
    event_rows = [a for a in added_args if isinstance(a, Event)]
    assert len(event_rows) == 1, (
        f"Expected exactly one Event row added; got {len(event_rows)}. "
        "spec: BACKEND.md §Run pipeline — one METAGEN.COMPLETE event per run"
    )
    assert event_rows[0].event_type == METAGEN_COMPLETE, (
        f"Event type must be METAGEN_COMPLETE; got {event_rows[0].event_type!r}. "
        "spec: BACKEND.md §Metadata Generation Service §Event Catalogue"
    )
    assert event_rows[0].detail["dry_run"] is False, (
        f"detail['dry_run'] must be False on real-run path; "
        f"got {event_rows[0].detail.get('dry_run')!r}. "
        "spec: BACKEND.md §Run pipeline step 8 — dry_run flag in detail"
    )


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


# ── State-machine edge cases ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_review_already_approved_field_is_idempotent_or_errors(
    svc: MetagenService, db: AsyncMock
) -> None:
    """Re-approving an already-approved field is either idempotent or raises consistently.

    Spec: spec/feature/BACKEND.md L289-L299 — approve writes editable DataHub
    aspects; the spec is silent on idempotency for double-approve.
    # Spec silent on idempotency — verified 2026-05-01; documenting observed behaviour.

    Observed behaviour: a second approve PATCH on the same field does not raise;
    the field_status remains 'approved' and the DataHub emit is repeated.
    """
    row = make_metagen_result_row(
        field_status={"dataset.description": "approved"},
    )
    row.proposals = {"dataset.description": "An approved description."}

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = row
    db.execute = AsyncMock(return_value=result_mock)
    mock_db_refresh(db)

    svc._datahub.emit_aspect = AsyncMock()
    svc._datahub.get_aspect = AsyncMock(return_value=None)

    # Re-approve the already-approved field
    try:
        record = await svc.review_result(
            str(row.id), verdict="approve", fields=["dataset.description"]
        )
        # Idempotent path: field remains approved
        # Spec silent on idempotency — spec/feature/BACKEND.md L289-L299
        assert record.field_status["dataset.description"] == "approved"
    except Exception as exc:
        # Error path: service must raise one of the documented conflict/precondition
        # exception types — not an arbitrary unhandled error.
        # Spec silent on idempotency — documenting that either path is acceptable.
        assert isinstance(exc, (PreconditionFailedError, ConflictError)), (
            f"Expected PreconditionFailedError or ConflictError on double-approve, "
            f"got {type(exc).__name__}: {exc}"
        )


@pytest.mark.asyncio
async def test_review_unknown_field_handled_consistently(
    svc: MetagenService, db: AsyncMock
) -> None:
    """PATCH with fields=['nonexistent.field'] is a no-op (unknown key silently ignored).

    Spec: spec/feature/BACKEND.md L289-L299 — field-level review updates only the
    listed entries; the spec is silent on unknown field paths. Observed behaviour:
    unknown field paths are not present in field_status so no update occurs; the
    call succeeds (200) without modifying any existing field status.
    # Spec silent on unknown-field behavior — verified 2026-05-01.
    """
    row = make_metagen_result_row(
        field_status={"dataset.description": "pending"},
    )
    row.proposals = {"dataset.description": "A description."}

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = row
    db.execute = AsyncMock(return_value=result_mock)
    mock_db_refresh(db)

    svc._datahub.emit_aspect = AsyncMock()
    svc._datahub.get_aspect = AsyncMock(return_value=None)

    # PATCH with a nonexistent field — spec silent on this; document observed no-op
    # Spec silent on unknown-field behavior — spec/feature/BACKEND.md L289-L299
    record = await svc.review_result(
        str(row.id), verdict="approve", fields=["nonexistent.field"]
    )
    # Existing field unchanged — nonexistent field is silently skipped
    assert record.field_status.get("dataset.description") == "pending"


# ── Evidence boundary: absence tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_gather_evidence_does_not_read_upstream_lineage(
    svc: MetagenService, db: AsyncMock
) -> None:
    """_gather_evidence never fetches UpstreamLineageClass.

    Spec anchor: spec/USE_CASE_en.md §UC4 Inputs — lineage (upstreamLineage)
    is absent from the unified six-aspect input set shared by UC3 and UC4.
    """
    svc._datahub.get_aspect = AsyncMock(return_value=None)
    svc._datahub.get_timeseries = AsyncMock(return_value=[])
    svc._datahub._with_retry = AsyncMock(
        return_value={"searchAcrossEntities": {"searchResults": []}}
    )

    await svc._gather_evidence(_DATASET_URN, targets=["dataset.description"])

    for call in svc._datahub.get_aspect.call_args_list:
        args = call[0]
        if len(args) > 1:
            aspect_cls = args[1]
            class_name = getattr(aspect_cls, "__name__", "")
            assert "UpstreamLineage" not in class_name, (
                f"UpstreamLineageClass must not be fetched (UC4 input set excludes lineage); "
                f"found: {class_name}"
            )


# ── Evidence boundary: positive tests ────────────────────────────────────────


@pytest.mark.asyncio
async def test_gather_evidence_reads_editable_dataset_properties(
    svc: MetagenService, db: AsyncMock
) -> None:
    """_gather_evidence populates 'editable_description' from EditableDatasetPropertiesClass.

    Spec anchor: spec/USE_CASE_en.md §UC4 Inputs — editableDatasetProperties
    is in the unified six-aspect input set.
    """
    from unittest.mock import MagicMock as _MagicMock

    editable_props = _MagicMock()
    editable_props.description = "Approved description"

    def get_aspect_side_effect(urn, aspect_class):
        # aspect_class is the class object; use __name__ to dispatch
        class_name = getattr(aspect_class, "__name__", "")
        if "EditableDatasetPropertiesClass" in class_name:
            return editable_props
        return None

    svc._datahub.get_aspect = AsyncMock(side_effect=get_aspect_side_effect)
    svc._datahub.get_timeseries = AsyncMock(return_value=[])
    svc._datahub._with_retry = AsyncMock(
        return_value={"searchAcrossEntities": {"searchResults": []}}
    )

    evidence = await svc._gather_evidence(_DATASET_URN, targets=["dataset.description"])

    assert evidence.get("editable_description") == "Approved description", (
        "editableDatasetProperties must be surfaced as 'editable_description' in evidence"
    )


@pytest.mark.asyncio
async def test_gather_evidence_reads_editable_schema_metadata(
    svc: MetagenService, db: AsyncMock
) -> None:
    """_gather_evidence populates 'editable_field_descriptions' from EditableSchemaMetadataClass.

    Spec anchor: spec/USE_CASE_en.md §UC4 Inputs — editableSchemaMetadata
    is in the unified six-aspect input set.
    """
    from unittest.mock import MagicMock as _MagicMock

    ef = _MagicMock()
    ef.fieldPath = "title"
    ef.description = "Book title column"
    editable_schema = _MagicMock()
    editable_schema.editableSchemaFieldInfo = [ef]

    def get_aspect_side_effect(urn, aspect_class):
        class_name = getattr(aspect_class, "__name__", "")
        if "EditableSchemaMetadataClass" in class_name:
            return editable_schema
        return None

    svc._datahub.get_aspect = AsyncMock(side_effect=get_aspect_side_effect)
    svc._datahub.get_timeseries = AsyncMock(return_value=[])
    svc._datahub._with_retry = AsyncMock(
        return_value={"searchAcrossEntities": {"searchResults": []}}
    )

    evidence = await svc._gather_evidence(_DATASET_URN, targets=["column.description"])

    field_descs = evidence.get("editable_field_descriptions", [])
    assert isinstance(field_descs, list), "editable_field_descriptions must be a list"
    assert any(f.get("fieldPath") == "title" for f in field_descs), (
        "editableSchemaMetadata field 'title' must appear in editable_field_descriptions"
    )


@pytest.mark.asyncio
async def test_gather_evidence_reads_glossary_terms(
    svc: MetagenService, db: AsyncMock
) -> None:
    """_gather_evidence populates 'glossary_terms' from GlossaryTermsClass.

    Spec anchor: spec/USE_CASE_en.md §UC4 Inputs — glossaryTerms
    is in the unified six-aspect input set.
    """
    from unittest.mock import MagicMock as _MagicMock

    term = _MagicMock()
    term.urn = "urn:li:glossaryTerm:Book"
    glossary = _MagicMock()
    glossary.terms = [term]

    def get_aspect_side_effect(urn, aspect_class):
        class_name = getattr(aspect_class, "__name__", "")
        if "GlossaryTermsClass" in class_name:
            return glossary
        return None

    svc._datahub.get_aspect = AsyncMock(side_effect=get_aspect_side_effect)
    svc._datahub.get_timeseries = AsyncMock(return_value=[])
    svc._datahub._with_retry = AsyncMock(
        return_value={"searchAcrossEntities": {"searchResults": []}}
    )

    evidence = await svc._gather_evidence(_DATASET_URN, targets=["dataset.description"])

    assert "glossary_terms" in evidence, "glossaryTerms must appear as 'glossary_terms' in evidence"
    assert "urn:li:glossaryTerm:Book" in evidence["glossary_terms"], (
        "Fetched glossary term URN must appear in evidence['glossary_terms']"
    )


@pytest.mark.asyncio
async def test_gather_evidence_reads_related_documents(
    svc: MetagenService, db: AsyncMock
) -> None:
    """_gather_evidence populates 'related_documents' from the document GraphQL search.

    Spec anchor: spec/USE_CASE_en.md §UC4 Inputs — document entities (via relatedAssets)
    are in the unified six-aspect input set.
    """
    doc_item = {
        "entity": {
            "urn": "urn:li:document:doc1",
            "info": {
                "title": "Test Doc",
                "contents": {"text": "Content body"},
                "relatedAssets": [{"asset": {"urn": _DATASET_URN}}],
                "lastModified": {"time": 1000},
            },
        }
    }

    svc._datahub.get_aspect = AsyncMock(return_value=None)
    svc._datahub.get_timeseries = AsyncMock(return_value=[])
    svc._datahub._with_retry = AsyncMock(
        return_value={"searchAcrossEntities": {"searchResults": [doc_item]}}
    )

    evidence = await svc._gather_evidence(_DATASET_URN, targets=["cross_data.md"])

    assert "related_documents" in evidence, (
        "related_documents must be populated from document entity search"
    )
    assert len(evidence["related_documents"]) >= 1, (
        "At least one related document must appear in evidence"
    )


# ── Filter tests: approved-only for ontogen_node_ids and ontogen_triples ──────


def _compile_stmt(stmt) -> str:
    """Compile a SQLAlchemy Select to a string with literal bind values rendered."""
    from sqlalchemy.dialects import postgresql
    return str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))


@pytest.mark.asyncio
async def test_gather_evidence_dataset_node_map_query_filters_status_approved(
    svc: MetagenService, db: AsyncMock, cache: AsyncMock
) -> None:
    """The SQL issued for dataset_node_map includes a WHERE status='approved' clause.

    This guards the spec invariant that pending nodes are excluded by the SQL
    predicate, not just absent from mocked results.  A regression that drops the
    WHERE clause from the query would cause this test to fail regardless of which
    rows the mock returns.

    Spec anchor: spec/feature/BACKEND.md §Metadata Generation Service
    §Generation Pipeline — node membership is filtered to status='approved'
    from dataset_node_map before injecting into evidence.
    """
    from tests.unit.backend.conftest import make_dataset_node_map_row

    approved_map = make_dataset_node_map_row(node_id="book", status="approved")

    config_row = _make_metagen_config_row(is_enabled=True, targets=["dataset.description"])

    captured_stmts: list = []

    def make_result(scalars_all=None, scalar_one=None):
        m = MagicMock()
        m.scalar_one_or_none.return_value = scalar_one
        ms = MagicMock()
        ms.all.return_value = scalars_all or []
        m.scalars.return_value = ms
        return m

    config_result = MagicMock()
    config_result.scalar_one_or_none.return_value = config_row
    node_map_result = make_result(scalars_all=[approved_map])
    triples_result = make_result(scalars_all=[])

    call_index = [0]

    async def execute_side_effect(stmt, *args, **kwargs):
        call_index[0] += 1
        idx = call_index[0]
        # Call 1 = config lookup, call 2 = node-map query, call 3 = triples query
        if idx == 2:
            captured_stmts.append(stmt)
            return node_map_result
        elif idx == 3:
            return triples_result
        else:
            return config_result

    db.execute = AsyncMock(side_effect=execute_side_effect)

    captured_evidence: dict = {}

    async def capture_propose(target, urn, evidence):
        captured_evidence.update(evidence)
        return "desc"

    svc._datahub.get_aspect = AsyncMock(return_value=None)
    svc._datahub.get_timeseries = AsyncMock(return_value=[])
    svc._datahub._with_retry = AsyncMock(
        return_value={"searchAcrossEntities": {"searchResults": []}}
    )

    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    with patch.object(svc, "_propose_target", new=AsyncMock(side_effect=capture_propose)):
        await svc.run(_DATASET_URN, dry_run=True)

    # Pins table.column names; balanced by behavioral assertions below.
    # Note: a pure behavioral counterpart that proves the WHERE clause is applied is infeasible
    # with this mock structure — db.execute returns a pre-configured result regardless of the
    # actual WHERE clause.  The SQL inspection is the load-bearing guard against the WHERE clause
    # being dropped from the query entirely.
    assert captured_stmts, (
        "db.execute was not called for the node-map query — test setup may be wrong"
    )
    compiled_sql = _compile_stmt(captured_stmts[0])
    assert "dataset_node_map.status" in compiled_sql and "approved" in compiled_sql, (
        f"node-map query must filter dataset_node_map.status = 'approved'; "
        f"compiled SQL: {compiled_sql!r}"
    )

    # Behavioral counterpart: approved node must reach the evidence dict and
    # the pending node must NOT (the mock returns only approved_map, matching what a correct
    # WHERE clause would return from the DB).
    assert "book" in captured_evidence.get("ontogen_node_ids", []), (
        "Approved node 'book' must be in ontogen_node_ids"
    )
    assert "pending-node" not in captured_evidence.get("ontogen_node_ids", []), (
        "Pending node must NOT appear in ontogen_node_ids"
    )


@pytest.mark.asyncio
async def test_gather_evidence_ontogen_triples_query_filters_all_statuses_approved(
    svc: MetagenService, db: AsyncMock, cache: AsyncMock
) -> None:
    """The SQL for the triples query includes approved-status filters for triple, edge, and node.

    Spec anchor: spec/feature/BACKEND.md §Generation Pipeline — UC3-approved nodes
    and triples filtered by dataset_node_map.status='approved'.
    Spec anchor: spec/USE_CASE_en.md §UC4 Inputs — approved triples (status='approved'
    on OntogenTriple, OntogenEdge, and OntogenNode) feed UC4 evidence.
    """
    from tests.unit.backend.conftest import make_dataset_node_map_row, make_ontogen_edge_row

    approved_map = make_dataset_node_map_row(node_id="book", status="approved")

    config_row = _make_metagen_config_row(is_enabled=True, targets=["dataset.description"])

    # Build an approved triple row and a pending triple row (only approved should surface)
    approved_triple = MagicMock()
    approved_triple.subject_node_id = "book"
    approved_triple.edge_id = "has_edition"
    approved_triple.object_node_id = "edition"

    approved_edge = make_ontogen_edge_row(id="has_edition", label="has edition", status="approved")

    pending_triple = MagicMock()
    pending_triple.subject_node_id = "book"
    pending_triple.edge_id = "wrote"
    pending_triple.object_node_id = "author"

    captured_stmts: list = []

    def make_result(rows=None, scalar_one=None):
        m = MagicMock()
        m.scalar_one_or_none.return_value = scalar_one
        ms = MagicMock()
        ms.all.return_value = rows or []
        m.scalars.return_value = ms
        # triples query uses result.all() (not .scalars().all())
        m.all.return_value = [(approved_triple, approved_edge)] if rows is True else []
        return m

    config_result = MagicMock()
    config_result.scalar_one_or_none.return_value = config_row
    node_map_result = make_result(rows=[approved_map])
    triples_result = make_result(rows=True)  # returns the approved pair

    call_index = [0]

    async def execute_side_effect(stmt, *args, **kwargs):
        call_index[0] += 1
        idx = call_index[0]
        if idx == 1:
            return config_result
        elif idx == 2:
            return node_map_result
        else:
            # Third call is the triples query — capture it
            captured_stmts.append(stmt)
            return triples_result

    db.execute = AsyncMock(side_effect=execute_side_effect)

    captured_evidence: dict = {}

    async def capture_propose(target, urn, evidence):
        captured_evidence.update(evidence)
        return "desc"

    svc._datahub.get_aspect = AsyncMock(return_value=None)
    svc._datahub.get_timeseries = AsyncMock(return_value=[])
    svc._datahub._with_retry = AsyncMock(
        return_value={"searchAcrossEntities": {"searchResults": []}}
    )

    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    with patch.object(svc, "_propose_target", new=AsyncMock(side_effect=capture_propose)):
        await svc.run(_DATASET_URN, dry_run=True)

    # Pins table.column names; balanced by behavioral assertions below.
    # Note: a pure behavioral counterpart that proves WHERE clause filtering is infeasible
    # with this mock structure — db.execute returns a pre-configured result regardless of
    # the actual WHERE clause predicates.  The SQL inspection is the load-bearing guard.
    assert captured_stmts, (
        "db.execute was not called for the triples query — approved_node_ids may be empty"
    )
    compiled_sql = _compile_stmt(captured_stmts[0])

    assert "ontogen_triples.status" in compiled_sql and "approved" in compiled_sql, (
        f"Triples query must filter OntogenTriple.status = 'approved'; SQL: {compiled_sql!r}"
    )
    assert "ontogen_edges.status" in compiled_sql, (
        f"Triples query must join and filter OntogenEdge.status = 'approved'; SQL: {compiled_sql!r}"
    )
    assert "ontogen_nodes.status" in compiled_sql, (
        f"Triples query must join and filter OntogenNode.status = 'approved'; SQL: {compiled_sql!r}"
    )

    # Behavioral counterpart: approved triple must surface in evidence; the mock returns the
    # approved pair (approved_triple, approved_edge) only — matching what a correct WHERE
    # clause returns from the DB.
    ontogen_triples_evidence = captured_evidence.get("ontogen_triples", [])
    assert any(t.get("edge_id") == "has_edition" for t in ontogen_triples_evidence), (
        "Approved triple (has_edition) must appear in evidence['ontogen_triples']"
    )
    # The pending triple (wrote) must NOT appear — it was not in the mock result.
    assert not any(t.get("edge_id") == "wrote" for t in ontogen_triples_evidence), (
        "Pending triple (wrote) must NOT appear in evidence['ontogen_triples']"
    )


@pytest.mark.asyncio
async def test_review_result_approve_writes_per_data_only_to_editable_aspects(
    svc: MetagenService, db: AsyncMock, datahub: AsyncMock
) -> None:
    """review_result(approve) for dataset/column targets emits only editable aspects.

    Tests the per-data write boundary: dataset.description goes to
    EditableDatasetPropertiesClass; column.description goes to
    EditableSchemaMetadataClass.  Neither non-editable aspect (datasetProperties,
    schemaMetadata) may be written.

    Spec anchor: spec/DATAHUB_INTEGRATION.md §Read vs Write Boundary — UC4 writes
    only editable aspects for per-data targets.
    Spec anchor: spec/feature/BACKEND.md §Metadata Generation Service §Approval flow.
    """
    row = make_metagen_result_row(
        field_status={"dataset.description": "pending"}
    )
    row.proposals = {"dataset.description": "A generated description."}

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = row
    db.execute = AsyncMock(return_value=result_mock)
    mock_db_refresh(db)

    datahub.emit_aspect = AsyncMock()
    datahub.get_aspect = AsyncMock(return_value=None)

    await svc.review_result(str(row.id), verdict="approve", fields=["dataset.description"])

    # Positive count assertions guard against vacuous pass when emit_aspect is never called.
    # dataset.description approval must produce at least one EditableDatasetPropertiesClass emit.
    # Spec: spec/feature/BACKEND.md §Approval flow; spec/USE_CASE_en.md §UC4 per-data target.
    emitted_class_names = [
        type(c[0][1]).__name__
        for c in datahub.emit_aspect.call_args_list
        if len(c[0]) > 1
    ]
    assert any("EditableDatasetPropertiesClass" in n for n in emitted_class_names), (
        "Approving dataset.description must emit at least one EditableDatasetPropertiesClass; "
        "got no such emit — apply_actions may be silently skipping the write. "
        f"Emitted classes: {emitted_class_names!r}"
    )

    # All emitted aspect classes must be editable per-data aspects only.
    # DocumentInfoClass is excluded: it belongs to the cross-data path (cross_data.md).
    # Spec: spec/USE_CASE_en.md §UC4 — per-data targets restricted to editable variants.
    # Spec: spec/DATAHUB_INTEGRATION.md §Read vs Write Boundary.
    _ALLOWED_PER_DATA_WRITE_CLASSES = (
        "EditableDatasetPropertiesClass",
        "EditableSchemaMetadataClass",
    )
    for emit_call in datahub.emit_aspect.call_args_list:
        emitted_aspect = emit_call[0][1] if len(emit_call[0]) > 1 else None
        if emitted_aspect is not None:
            emitted_class_name = type(emitted_aspect).__name__
            assert any(allowed in emitted_class_name for allowed in _ALLOWED_PER_DATA_WRITE_CLASSES), (
                f"emit_aspect called with non-editable aspect {emitted_class_name!r}; "
                f"per-data UC4 targets may only write editable aspects per "
                f"spec/DATAHUB_INTEGRATION.md §Read vs Write Boundary"
            )


@pytest.mark.asyncio
async def test_review_result_approve_writes_cross_data_only_to_document_and_status(
    svc: MetagenService, db: AsyncMock, datahub: AsyncMock
) -> None:
    """review_result(approve) for cross_data.md targets emits DocumentInfoClass and/or
    StatusClass (for deletes); no non-editable aspect may be written.

    Exercises both the create path (DocumentInfoClass) and the delete path
    (StatusClass(removed=True)) from src/backend/metagen/cross_data.py:187.

    Spec anchor: spec/feature/BACKEND.md §Cross-data MD action types — delete soft-deletes
    via Status.removed=true; create emits DocumentInfoClass.
    Spec anchor: spec/DATAHUB_INTEGRATION.md §Document Aspects.
    """
    from unittest.mock import patch as _patch
    from src.backend.metagen.cross_data import create_document, delete_document

    doc_urn = "urn:li:document:existing-doc-001"

    # Proposals: one create action and one delete action
    action_create_id = "action-create-001"
    action_delete_id = "action-delete-001"

    row = make_metagen_result_row(
        field_status={
            f"cross_data.md.{action_create_id}": "pending",
            f"cross_data.md.{action_delete_id}": "pending",
        }
    )
    row.proposals = {
        "cross_data.md": [
            {
                "action_id": action_create_id,
                "action": "create",
                "confidence": 0.9,
                "title": "New Cross-data Doc",
                "body": "# New document body",
                "related_assets": [_DATASET_URN],
            },
            {
                "action_id": action_delete_id,
                "action": "delete",
                "confidence": 0.85,
                "document_urn": doc_urn,
            },
        ]
    }

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = row
    db.execute = AsyncMock(return_value=result_mock)
    mock_db_refresh(db)

    datahub.emit_aspect = AsyncMock()

    # For the delete action, get_aspect must return an existing NATIVE document
    from unittest.mock import MagicMock as _MM
    existing_doc = _MM()
    existing_doc.source = _MM()
    existing_doc.source.sourceType = "NATIVE"

    from datahub.metadata.schema_classes import DocumentInfoClass

    def get_aspect_side(urn, aspect_cls):
        if aspect_cls is DocumentInfoClass:
            return existing_doc
        return None

    datahub.get_aspect = AsyncMock(side_effect=get_aspect_side)

    await svc.review_result(
        str(row.id),
        verdict="approve",
        fields=[f"cross_data.md.{action_create_id}", f"cross_data.md.{action_delete_id}"],
    )

    # Positive count assertions guard against vacuous pass when apply_actions silently drops emits.
    # Spec: spec/feature/BACKEND.md §Cross-data MD action types — create emits DocumentInfoClass;
    #       delete emits StatusClass(removed=True).
    emitted_aspects_by_class = [
        type(c[0][1]).__name__
        for c in datahub.emit_aspect.call_args_list
        if len(c[0]) > 1
    ]
    assert any("DocumentInfoClass" in n for n in emitted_aspects_by_class), (
        "Approving a cross_data.md create action must emit at least one DocumentInfoClass; "
        "got no DocumentInfoClass emit — create_document may be silently skipped. "
        f"Emitted classes: {emitted_aspects_by_class!r}"
    )
    assert any("StatusClass" in n for n in emitted_aspects_by_class), (
        "Approving a cross_data.md delete action must emit at least one StatusClass; "
        "got no StatusClass emit — delete_document may be silently skipped. "
        f"Emitted classes: {emitted_aspects_by_class!r}"
    )

    # All emitted aspects must be DocumentInfoClass or StatusClass for document URNs
    _ALLOWED_CROSS_DATA_CLASSES = ("DocumentInfoClass", "StatusClass")
    for emit_call in datahub.emit_aspect.call_args_list:
        emitted_urn = emit_call[0][0] if len(emit_call[0]) > 0 else None
        emitted_aspect = emit_call[0][1] if len(emit_call[0]) > 1 else None
        if emitted_aspect is not None:
            emitted_class_name = type(emitted_aspect).__name__
            assert any(allowed in emitted_class_name for allowed in _ALLOWED_CROSS_DATA_CLASSES), (
                f"cross_data emit_aspect called with unexpected class {emitted_class_name!r}; "
                f"only DocumentInfoClass and StatusClass are permitted for cross-data targets. "
                f"URN: {emitted_urn!r}"
            )
            # StatusClass is only permitted on document URNs (soft-delete path)
            if "StatusClass" in emitted_class_name:
                assert str(emitted_urn).startswith("urn:li:document:"), (
                    f"StatusClass(removed=True) must only be emitted on document URNs; "
                    f"got URN: {emitted_urn!r}"
                )
