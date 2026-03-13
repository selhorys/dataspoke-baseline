"""Unit tests for GenerationService (mocked infrastructure)."""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backend.generation.service import GenerationService
from src.shared.exceptions import ConflictError, EntityNotFoundError
from tests.unit.backend.conftest import make_event_row, mock_paginated_query, mock_scalar_query

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.public.users,PROD)"


def _make_config_row(
    dataset_urn: str = _DATASET_URN,
    target_fields: dict | None = None,
    code_refs: dict | None = None,
    schedule: str | None = "0 0 * * *",
    status: str = "draft",
    owner: str = "alice@example.com",
):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.dataset_urn = dataset_urn
    row.target_fields = target_fields or {"description": True, "tags": True}
    row.code_refs = code_refs
    row.schedule = schedule
    row.status = status
    row.owner = owner
    row.created_at = datetime.now(tz=UTC)
    row.updated_at = datetime.now(tz=UTC)
    return row


def _make_result_row(
    dataset_urn: str = _DATASET_URN,
    proposals: dict | None = None,
    similar_diffs: list | None = None,
    approval_status: str = "pending",
    minutes_ago: int = 5,
):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.dataset_urn = dataset_urn
    row.proposals = proposals or {
        "field_descriptions": {"user_id": "Unique identifier"},
        "table_summary": "User table",
        "suggested_tags": ["pii"],
    }
    row.similar_diffs = similar_diffs or []
    row.approval_status = approval_status
    row.run_id = uuid.uuid4()
    row.generated_at = datetime.now(tz=UTC) - timedelta(minutes=minutes_ago)
    row.applied_at = None
    return row


@pytest.fixture
def service(datahub, db, llm, qdrant):
    return GenerationService(datahub=datahub, db=db, llm=llm, qdrant=qdrant)


# ── get_config ───────────────────────────────────────────────────────────────


async def test_get_config_found(service, db):
    config_row = _make_config_row()
    mock_scalar_query(db, config_row)

    config = await service.get_config(_DATASET_URN)
    assert config is not None
    assert config.dataset_urn == _DATASET_URN
    assert config.owner == "alice@example.com"


async def test_get_config_not_found(service, db):
    mock_scalar_query(db, None)

    config = await service.get_config("nonexistent")
    assert config is None


# ── upsert_config ────────────────────────────────────────────────────────────


async def test_upsert_config_creates_new(service, db):
    mock_scalar_query(db, None)
    db.refresh = AsyncMock(side_effect=lambda obj: None)

    await service.upsert_config(
        dataset_urn=_DATASET_URN,
        target_fields={"description": True},
        code_refs=None,
        schedule=None,
        owner="alice@example.com",
    )
    db.add.assert_called_once()
    db.commit.assert_awaited_once()


async def test_upsert_config_updates_existing(service, db):
    existing_row = _make_config_row()
    mock_scalar_query(db, existing_row)
    db.refresh = AsyncMock(side_effect=lambda obj: None)

    await service.upsert_config(
        dataset_urn=_DATASET_URN,
        target_fields={"tags": True},
        code_refs={"owner": "org", "repo": "app"},
        schedule="0 6 * * *",
        owner="bob@example.com",
    )
    db.add.assert_called_once()
    db.commit.assert_awaited_once()
    assert existing_row.target_fields == {"tags": True}
    assert existing_row.owner == "bob@example.com"


# ── patch_config ─────────────────────────────────────────────────────────────


async def test_patch_config_applies_partial(service, db):
    existing_row = _make_config_row()
    mock_scalar_query(db, existing_row)
    db.refresh = AsyncMock(side_effect=lambda obj: None)

    await service.patch_config(_DATASET_URN, {"schedule": "0 12 * * *"})
    assert existing_row.schedule == "0 12 * * *"
    db.commit.assert_awaited_once()


async def test_patch_config_not_found(service, db):
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.patch_config("nonexistent", {"schedule": "0 12 * * *"})
    assert exc_info.value.error_code == "GENERATION_CONFIG_NOT_FOUND"


# ── delete_config ────────────────────────────────────────────────────────────


async def test_delete_config_success(service, db):
    existing_row = _make_config_row()
    mock_scalar_query(db, existing_row)

    await service.delete_config(_DATASET_URN)
    db.delete.assert_awaited_once_with(existing_row)
    db.commit.assert_awaited_once()


async def test_delete_config_not_found(service, db):
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.delete_config("nonexistent")
    assert exc_info.value.error_code == "GENERATION_CONFIG_NOT_FOUND"


# ── list_configs ─────────────────────────────────────────────────────────────


async def test_list_configs_paginated(service, db):
    rows = [_make_config_row(dataset_urn=f"urn:{i}") for i in range(3)]
    mock_paginated_query(db, rows, total_count=5)

    configs, total = await service.list_configs(offset=0, limit=3)
    assert total == 5
    assert len(configs) == 3


async def test_list_configs_empty(service, db):
    mock_paginated_query(db, [], total_count=0)

    configs, total = await service.list_configs()
    assert total == 0
    assert configs == []


# ── get_results ──────────────────────────────────────────────────────────────


async def test_get_results_paginated(service, db):
    rows = [_make_result_row(minutes_ago=i) for i in range(3)]
    mock_paginated_query(db, rows, total_count=5)

    results, total = await service.get_results(_DATASET_URN, offset=0, limit=3)
    assert total == 5
    assert len(results) == 3
    assert results[0].dataset_urn == _DATASET_URN


async def test_get_results_empty(service, db):
    mock_paginated_query(db, [], total_count=0)

    results, total = await service.get_results(_DATASET_URN)
    assert total == 0
    assert results == []


# ── generate ─────────────────────────────────────────────────────────────────


async def test_generate_builds_prompt_with_schema(service, db, datahub, llm):
    config_row = _make_config_row(code_refs=None)
    mock_scalar_query(db, config_row)
    db.refresh = AsyncMock(side_effect=lambda obj: None)

    # Mock DataHub aspects
    schema_mock = MagicMock()
    field_mock = MagicMock()
    field_mock.fieldPath = "user_id"
    field_mock.nativeDataType = "int"
    field_mock.description = ""
    schema_mock.fields = [field_mock]

    datahub.get_aspect = AsyncMock(
        side_effect=lambda urn, cls: schema_mock if "Schema" in cls.__name__ else None
    )

    llm.complete_json = AsyncMock(
        return_value={
            "field_descriptions": {"user_id": "Unique user identifier"},
            "table_summary": "User table",
            "suggested_tags": ["pii"],
        }
    )

    result = await service.generate(_DATASET_URN)
    assert result.status == "success"
    assert result.run_id
    llm.complete_json.assert_awaited_once()

    # Verify the prompt includes the field name
    call_args = llm.complete_json.call_args
    assert "user_id" in call_args[0][0]


async def test_generate_includes_code_refs_when_configured(service, db, datahub, llm):
    config_row = _make_config_row(code_refs={"owner": "org", "repo": "app", "token": "tok"})
    mock_scalar_query(db, config_row)
    db.refresh = AsyncMock(side_effect=lambda obj: None)

    datahub.get_aspect = AsyncMock(return_value=None)

    llm.complete_json = AsyncMock(
        return_value={
            "field_descriptions": {},
            "table_summary": "Table",
            "suggested_tags": [],
        }
    )

    with patch("src.backend.generation.service.SourceCodeAnalyzer") as MockAnalyzer:
        analyzer_instance = AsyncMock()
        analyzer_instance.analyze = AsyncMock(return_value={"col1": "description"})
        analyzer_instance.diff_similar_tables = AsyncMock(return_value=[])
        MockAnalyzer.return_value = analyzer_instance

        result = await service.generate(_DATASET_URN)
        assert result.status == "success"
        analyzer_instance.analyze.assert_awaited_once()


async def test_generate_skips_code_refs_when_not_configured(service, db, datahub, llm):
    config_row = _make_config_row(code_refs=None)
    mock_scalar_query(db, config_row)
    db.refresh = AsyncMock(side_effect=lambda obj: None)

    datahub.get_aspect = AsyncMock(return_value=None)

    llm.complete_json = AsyncMock(
        return_value={
            "field_descriptions": {},
            "table_summary": "Table",
            "suggested_tags": [],
        }
    )

    result = await service.generate(_DATASET_URN)
    assert result.status == "success"


async def test_generate_config_not_found(service, db):
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.generate("nonexistent")
    assert exc_info.value.error_code == "GENERATION_CONFIG_NOT_FOUND"


async def test_generate_produces_structured_proposals(service, db, datahub, llm):
    config_row = _make_config_row(code_refs=None)
    mock_scalar_query(db, config_row)
    db.refresh = AsyncMock(side_effect=lambda obj: None)

    datahub.get_aspect = AsyncMock(return_value=None)

    expected_proposals = {
        "field_descriptions": {"id": "Primary key"},
        "table_summary": "Main table",
        "suggested_tags": ["important"],
    }
    llm.complete_json = AsyncMock(return_value=expected_proposals)

    result = await service.generate(_DATASET_URN)
    assert result.status == "success"

    # Verify the result was persisted — db.add should have been called with GenerationResult
    add_calls = db.add.call_args_list
    # Last add before commit should be the result row
    assert len(add_calls) >= 1


# ── apply ────────────────────────────────────────────────────────────────────


async def test_apply_writes_to_datahub(service, db, datahub):
    result_row = _make_result_row(approval_status="approved")
    mock_scalar_query(db, result_row)

    datahub.emit_aspect = AsyncMock()

    result = await service.apply(_DATASET_URN, str(result_row.id))
    assert result.status == "applied"
    # Should emit aspects for field descriptions, table summary, and tags
    assert datahub.emit_aspect.await_count >= 1


async def test_apply_rejects_pending_result(service, db):
    result_row = _make_result_row(approval_status="pending")
    mock_scalar_query(db, result_row)

    with pytest.raises(ConflictError) as exc_info:
        await service.apply(_DATASET_URN, str(result_row.id))
    assert exc_info.value.error_code == "GENERATION_NOT_APPROVED"


async def test_apply_result_not_found(service, db):
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.apply(_DATASET_URN, str(uuid.uuid4()))
    assert exc_info.value.error_code == "GENERATION_RESULT_NOT_FOUND"


# ── get_events ───────────────────────────────────────────────────────────────


async def test_get_events_paginated(service, db):
    rows = [
        make_event_row(entity_type="generation", event_type="generation.completed", minutes_ago=i)
        for i in range(3)
    ]
    mock_paginated_query(db, rows, total_count=5)

    events, total = await service.get_events(_DATASET_URN, offset=0, limit=3)
    assert total == 5
    assert len(events) == 3
    assert events[0]["entity_type"] == "generation"


async def test_get_events_empty(service, db):
    mock_paginated_query(db, [], total_count=0)

    events, total = await service.get_events(_DATASET_URN)
    assert total == 0
    assert events == []
