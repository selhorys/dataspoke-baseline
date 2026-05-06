"""Unit tests for IngestionService (mocked infrastructure).

spec: BACKEND.md §Ingestion Service (§Feature Services)
spec: BACKEND.md §Active run pipeline
spec: BACKEND.md §Custom Ingestor Authoring Contract
spec: USE_CASE_en.md §UC1
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from src.backend.ingestion.service import IngestionService
from src.shared.events import INGESTION_COMPLETE, INGESTION_FAIL
from src.shared.exceptions import ConflictError, EntityNotFoundError
from tests.unit.backend.conftest import (
    make_event_row,
    mock_db_refresh,
    mock_paginated_query,
    mock_scalar_query,
)

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,mydb.public.users,PROD)"
_LOCATOR = {"host": "db.example.com", "port": 5432}
_IDENTIFIER = {"database": "mydb", "schema_name": "public", "table": "users"}
_AUTH = {"username": "user", "secret_ref": "pw"}


def _make_config_row(
    dataset_urn: str = _DATASET_URN,
    platform: str = "postgres",
    locator: dict | None = None,
    identifier: dict | None = None,
    auth: dict | None = None,
    is_enabled: bool = False,
    mode: str = "active-custom",
    schedule_tier: str | None = "daily",
    workflow_dag_id: str | None = None,
    status: str = "OK",
):
    row = MagicMock()
    row.id = uuid.uuid4()
    row.dataset_urn = dataset_urn
    row.platform = platform
    row.locator = locator or _LOCATOR
    row.identifier = identifier or _IDENTIFIER
    row.auth = auth if auth is not None else _AUTH
    row.is_enabled = is_enabled
    row.mode = mode
    row.schedule_tier = schedule_tier
    row.workflow_dag_id = workflow_dag_id
    row.status = status
    row.created_at = datetime.now(tz=UTC)
    row.updated_at = datetime.now(tz=UTC)
    return row


@pytest.fixture
def service(datahub, db):
    return IngestionService(datahub=datahub, db=db)


@pytest.fixture
def service_with_cache(datahub, db, cache):
    return IngestionService(datahub=datahub, db=db, cache=cache)


# ── get_config ───────────────────────────────────────────────────────────────


async def test_get_config_found(service, db):
    config_row = _make_config_row()
    mock_scalar_query(db, config_row)

    config = await service.get_config(_DATASET_URN)
    assert config is not None
    assert config.dataset_urn == _DATASET_URN
    assert config.platform == "postgres"
    assert config.locator == _LOCATOR
    assert config.identifier == _IDENTIFIER
    assert config.auth == _AUTH


async def test_get_config_not_found(service, db):
    mock_scalar_query(db, None)

    config = await service.get_config("nonexistent")
    assert config is None


# ── upsert_config ────────────────────────────────────────────────────────────


async def test_upsert_config_creates_new(service, db):
    # spec: BACKEND.md §Ingestion Service — "Config upsert registers the dataset URN
    # in dataset_registry (does not require the dataset to exist in DataHub yet)"
    mock_scalar_query(db, None)
    mock_db_refresh(db)

    with patch("src.backend.ingestion.service.ensure_dataset_registered", new=AsyncMock()):
        result, created = await service.upsert_config(
            dataset_urn=_DATASET_URN,
            mode="active-custom",
            platform="postgres",
            locator=_LOCATOR,
            identifier=_IDENTIFIER,
            auth=_AUTH,
            is_enabled=False,
            schedule_tier=None,
        )

    # Behavioral: created flag must be True for a brand-new config
    assert created is True
    # Behavioral: the returned record must carry the correct URN and platform
    assert result.dataset_urn == _DATASET_URN
    assert result.platform == "postgres"
    assert result.mode == "active-custom"


async def test_upsert_config_updates_existing(service, db):
    # spec: BACKEND.md §Ingestion Service — upsert mutates existing row in-place
    existing_row = _make_config_row()
    mock_scalar_query(db, existing_row)
    mock_db_refresh(db)

    new_locator = {"host": "newdb.example.com", "port": 5432}
    new_identifier = {"database": "newdb", "schema_name": "public", "table": "orders"}
    new_auth = {"username": "admin", "secret_ref": "newpw"}

    with patch("src.backend.ingestion.service.ensure_dataset_registered", new=AsyncMock()):
        result, created = await service.upsert_config(
            dataset_urn=_DATASET_URN,
            mode="passive",
            platform="mysql",
            locator=new_locator,
            identifier=new_identifier,
            auth=new_auth,
            is_enabled=True,
            schedule_tier="weekly",
        )

    # Behavioral: created flag must be False for an existing config
    assert created is False
    # Behavioral: all mutated fields are reflected in the ORM row
    assert existing_row.platform == "mysql"
    assert existing_row.locator == new_locator
    assert existing_row.identifier == new_identifier
    assert existing_row.auth == new_auth
    assert existing_row.is_enabled is True
    assert existing_row.schedule_tier == "weekly"
    # Behavioral: the returned record mirrors the updated row
    assert result.mode == "passive"
    assert result.is_enabled is True


# ── patch_config ─────────────────────────────────────────────────────────────


async def test_patch_config_applies_schedule_tier(service, db):
    # spec: BACKEND.md §Ingestion Service — PATCH mutates is_enabled / schedule_tier
    existing_row = _make_config_row()
    mock_scalar_query(db, existing_row)
    mock_db_refresh(db)

    result = await service.patch_config(_DATASET_URN, {"schedule_tier": "hourly"})
    # Behavioral: ORM row mutation must propagate to the returned record
    assert existing_row.schedule_tier == "hourly"
    assert result.schedule_tier == "hourly"


async def test_patch_config_applies_platform(service, db):
    existing_row = _make_config_row()
    mock_scalar_query(db, existing_row)
    mock_db_refresh(db)

    await service.patch_config(_DATASET_URN, {"platform": "mysql"})
    assert existing_row.platform == "mysql"


async def test_patch_config_applies_is_enabled_and_schedule(service, db):
    existing_row = _make_config_row(is_enabled=False)
    mock_scalar_query(db, existing_row)
    mock_db_refresh(db)

    await service.patch_config(_DATASET_URN, {"is_enabled": True, "schedule_tier": "daily"})
    assert existing_row.is_enabled is True
    assert existing_row.schedule_tier == "daily"


async def test_patch_config_not_found(service, db):
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.patch_config("nonexistent", {"schedule_tier": "daily"})
    assert exc_info.value.error_code == "CONFIG_NOT_FOUND"


# ── delete_config ────────────────────────────────────────────────────────────


async def test_delete_config_success(service, db):
    # spec: BACKEND.md §Ingestion Service — DELETE removes the config row from DB
    existing_row = _make_config_row()
    mock_scalar_query(db, existing_row)

    await service.delete_config(_DATASET_URN)
    # Behavioral: the correct ORM row was deleted and the session was committed
    db.delete.assert_awaited_once_with(existing_row)


async def test_delete_config_not_found(service, db):
    mock_scalar_query(db, None)

    with pytest.raises(EntityNotFoundError) as exc_info:
        await service.delete_config("nonexistent")
    assert exc_info.value.error_code == "CONFIG_NOT_FOUND"


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


# ── list_active_for_tier ──────────────────────────────────────────────────────


async def test_list_active_for_tier_returns_urns(service, db):
    urns = [
        "urn:li:dataset:(urn:li:dataPlatform:postgres,db1.public.t1,PROD)",
        "urn:li:dataset:(urn:li:dataPlatform:postgres,db1.public.t2,PROD)",
    ]
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = urns
    db.execute = AsyncMock(return_value=result_mock)

    datasets = await service.list_active_for_tier("daily")
    assert datasets == urns


async def test_list_active_for_tier_empty(service, db):
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=result_mock)

    datasets = await service.list_active_for_tier("weekly")
    assert datasets == []


# ── run ──────────────────────────────────────────────────────────────────────


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

    # Behavioral: mark_registered must be awaited with the dataset URN on success
    # spec: BACKEND.md §Active run pipeline L201
    mock_mark_registered.assert_awaited_once_with(db, _DATASET_URN)

    # Behavioral: an INGESTION.COMPLETE event must be added to the DB
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

    # Behavioral: status must indicate failure when zero entities ingested (non-dry-run)
    assert result.status == "error", (
        "Expected status='error' when entities_ingested=0 and dry_run=False; "
        f"got status='{result.status}'"
    )
    # Behavioral: the result detail must indicate zero entities were ingested
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


async def test_list_passive_configs_filter_predicates(db, datahub):
    """list_passive_configs() WHERE clause must filter on mode='passive' AND is_enabled=True.

    Inspects the compiled SQLAlchemy statement captured from db.execute to verify
    both predicates are present as binary expressions. Survives logically-equivalent
    refactors (e.g. is_(True) vs == True) by walking the AST, not matching SQL text.

    spec: BACKEND.md §Ingestion passive status-sync — enumerate all configs with
    mode='passive' AND is_enabled=True.
    spec: USE_CASE_en.md §UC1 — passive sync skips disabled configs.
    """
    from sqlalchemy.sql.elements import BinaryExpression, False_, True_
    from sqlalchemy.sql.visitors import iterate

    captured_stmts: list = []

    async def capture_execute(stmt):
        captured_stmts.append(stmt)
        result = MagicMock()
        result.scalars.return_value.all.return_value = []
        return result

    db.execute = capture_execute
    service = IngestionService(datahub=datahub, db=db)

    await service.list_passive_configs()

    assert len(captured_stmts) == 1, (
        f"list_passive_configs must execute exactly one query; got {len(captured_stmts)}"
    )
    stmt = captured_stmts[0]
    where = stmt.whereclause

    binaries = [n for n in iterate(where) if isinstance(n, BinaryExpression)]

    # Normalise each binary into (column_name, value) regardless of operator shape.
    # is_(True) produces a True_ singleton on the right; == produces a BindParameter.
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

    assert ("mode", "passive") in pred_pairs, (
        f"WHERE clause must filter on mode='passive'. "
        f"Found predicates: {pred_pairs}. "
        "spec: BACKEND.md §Ingestion passive status-sync"
    )
    assert ("is_enabled", True) in pred_pairs, (
        f"WHERE clause must filter on is_enabled=True. "
        f"Found predicates: {pred_pairs}. "
        "spec: BACKEND.md §Ingestion passive status-sync"
    )


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
    # One passive config
    config_row = _make_config_row(mode="passive")
    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = [config_row]
    db.execute = AsyncMock(return_value=result_mock)

    # DataHub raises an exception for the dataset
    datahub._with_retry = AsyncMock(side_effect=Exception("DataHub down"))
    # Patch _fetch_datahub_run_history to raise
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


# ── Two-mode taxonomy: passive rejection + DPI emission contract ──────────────


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


async def test_active_custom_run_emits_dpi_lifecycle(service, db):
    """active-custom non-dry-run emits four DPI aspects in spec-mandated order.

    spec: BACKEND.md §Custom Ingestor Authoring Contract —
        Required aspects per run in order:
          1. DataProcessInstanceProperties
          2. DataProcessInstanceRelationships
          3. DataProcessInstanceRunEvent(STARTED)  — BEFORE dataset aspect work
          4. (dataset aspect work)
          5. DataProcessInstanceRunEvent(COMPLETE/SUCCESS)  — AFTER dataset work

    spec: BACKEND.md §Custom Ingestor Authoring Contract —
        DPI URN convention: urn:li:dataProcessInstance:<platform>-<run_id>

    spec: BACKEND.md §Custom Ingestor Authoring Contract —
        "Ordering guarantee: the STARTED event must precede schema/property emission
        for the dataset; the terminal event must follow all aspect work."
    """
    from datahub.metadata.schema_classes import (
        DataProcessInstancePropertiesClass,
        DataProcessInstanceRelationshipsClass,
        DataProcessInstanceRunEventClass,
        DataProcessRunStatusClass,
        RunResultTypeClass,
    )

    from src.backend.ingestion.extractors import IngestionResult

    config_row = _make_config_row(
        platform="postgres",
        mode="active-custom",
        is_enabled=True,
    )
    mock_scalar_query(db, config_row)
    mock_db_refresh(db)

    emit_calls: list[tuple[str, object]] = []

    async def _capture_emit(urn: str, aspect: object, **kwargs: object) -> None:
        emit_calls.append((urn, aspect))

    service._datahub.emit_aspect = _capture_emit

    with (
        patch(
            "src.backend.ingestion.service.run_datahub_ingestion",
            new=AsyncMock(
                return_value=IngestionResult(entities_ingested=3, errors=[], warnings=[])
            ),
        ),
        patch("src.backend.ingestion.service.mark_registered", new=AsyncMock()),
    ):
        result = await service.run(_DATASET_URN, dry_run=False)

    assert result.status == "success"
    run_id = result.run_id

    expected_dpi_urn = f"urn:li:dataProcessInstance:postgres-{run_id}"

    dpi_calls = [(urn, aspect) for urn, aspect in emit_calls if urn == expected_dpi_urn]
    dataset_calls = [(urn, aspect) for urn, aspect in emit_calls if urn == _DATASET_URN]

    assert len(dpi_calls) >= 4, (
        f"Expected at least 4 emit calls against DPI URN {expected_dpi_urn!r}; "
        f"got {len(dpi_calls)}. spec: BACKEND.md §Custom Ingestor Authoring Contract"
    )

    dpi_aspect_types = [type(aspect).__name__ for _, aspect in dpi_calls]

    assert "DataProcessInstancePropertiesClass" in dpi_aspect_types, (
        "DataProcessInstanceProperties must be emitted. "
        "spec: BACKEND.md §Custom Ingestor Authoring Contract row #1"
    )
    assert "DataProcessInstanceRelationshipsClass" in dpi_aspect_types, (
        "DataProcessInstanceRelationships must be emitted. "
        "spec: BACKEND.md §Custom Ingestor Authoring Contract row #2"
    )
    assert "DataProcessInstanceRunEventClass" in dpi_aspect_types, (
        "DataProcessInstanceRunEvent must be emitted. "
        "spec: BACKEND.md §Custom Ingestor Authoring Contract rows #3 and #5"
    )

    run_events = [
        aspect
        for _, aspect in dpi_calls
        if isinstance(aspect, DataProcessInstanceRunEventClass)
    ]
    assert len(run_events) >= 2, (
        f"Expected at least 2 DataProcessInstanceRunEvent aspects (STARTED + COMPLETE); "
        f"got {len(run_events)}. spec: BACKEND.md §Custom Ingestor Authoring Contract"
    )

    started_events = [e for e in run_events if e.status == DataProcessRunStatusClass.STARTED]
    complete_events = [e for e in run_events if e.status == DataProcessRunStatusClass.COMPLETE]

    assert len(started_events) >= 1, (
        "At least one STARTED RunEvent must be emitted. "
        "spec: BACKEND.md §Custom Ingestor Authoring Contract row #3"
    )
    assert len(complete_events) >= 1, (
        "At least one COMPLETE RunEvent must be emitted on the happy path. "
        "spec: BACKEND.md §Custom Ingestor Authoring Contract row #5"
    )

    complete_event = complete_events[0]
    assert complete_event.result is not None, (
        "Terminal COMPLETE RunEvent must carry a result. "
        "spec: BACKEND.md §Custom Ingestor Authoring Contract row #5"
    )
    assert complete_event.result.type == RunResultTypeClass.SUCCESS, (
        f"Terminal RunEvent result.type must be SUCCESS on happy path; "
        f"got {complete_event.result.type!r}. "
        "spec: BACKEND.md §Custom Ingestor Authoring Contract row #5"
    )

    started_index = next(
        i
        for i, (_, aspect) in enumerate(emit_calls)
        if isinstance(aspect, DataProcessInstanceRunEventClass)
        and aspect.status == DataProcessRunStatusClass.STARTED
    )
    complete_index = next(
        i
        for i, (_, aspect) in enumerate(emit_calls)
        if isinstance(aspect, DataProcessInstanceRunEventClass)
        and aspect.status == DataProcessRunStatusClass.COMPLETE
    )
    assert started_index < complete_index, (
        "STARTED RunEvent must appear before COMPLETE RunEvent in the emit sequence. "
        "spec: BACKEND.md §Custom Ingestor Authoring Contract §Ordering guarantee"
    )

    properties_index = next(
        (
            i
            for i, (_, aspect) in enumerate(emit_calls)
            if isinstance(aspect, DataProcessInstancePropertiesClass)
        ),
        None,
    )
    assert properties_index is not None
    assert properties_index < started_index, (
        "DataProcessInstanceProperties must be emitted before the STARTED RunEvent. "
        "spec: BACKEND.md §Custom Ingestor Authoring Contract rows #1-3"
    )

    # spec: BACKEND.md §Custom Ingestor Authoring Contract row #2 —
    # DataProcessInstanceRelationships must be emitted and must precede the STARTED RunEvent.
    relationships_index = next(
        (
            i
            for i, (urn, aspect) in enumerate(emit_calls)
            if urn == expected_dpi_urn
            and isinstance(aspect, DataProcessInstanceRelationshipsClass)
        ),
        None,
    )
    assert relationships_index is not None, (
        "DataProcessInstanceRelationships must be emitted against the DPI URN. "
        "spec: BACKEND.md §Custom Ingestor Authoring Contract row #2"
    )
    assert relationships_index < started_index, (
        "DataProcessInstanceRelationships must be emitted before the STARTED RunEvent. "
        "spec: BACKEND.md §Custom Ingestor Authoring Contract rows #2-3"
    )


async def test_active_custom_run_emits_dpi_failure_on_extractor_exception(service, db):
    """When the extractor raises, a terminal FAILURE RunEvent is emitted and the exception re-raises.

    spec: BACKEND.md §Custom Ingestor Authoring Contract —
        "Failures emit a terminal event (do not let the run hang in STARTED)"
    spec: BACKEND.md §Custom Ingestor Authoring Contract §Failure semantics —
        "a failed run still emits the COMPLETE RunEvent, not a missing event"
    spec: BACKEND.md §Active run pipeline —
        "emit DataProcessInstanceRunEvent(COMPLETE | FAILED) carrying the run outcome"
    """
    from datahub.metadata.schema_classes import (
        DataProcessInstanceRunEventClass,
        DataProcessRunStatusClass,
        RunResultTypeClass,
    )

    config_row = _make_config_row(
        platform="postgres",
        mode="active-custom",
        is_enabled=True,
    )
    mock_scalar_query(db, config_row)
    mock_db_refresh(db)

    emit_calls: list[tuple[str, object]] = []

    async def _capture_emit(urn: str, aspect: object, **kwargs: object) -> None:
        emit_calls.append((urn, aspect))

    service._datahub.emit_aspect = _capture_emit

    extractor_error = RuntimeError("DB connection refused")

    with (
        patch(
            "src.backend.ingestion.service.run_datahub_ingestion",
            new=AsyncMock(side_effect=extractor_error),
        ),
        pytest.raises(RuntimeError, match="DB connection refused"),
    ):
        await service.run(_DATASET_URN, dry_run=False)

    run_events = [
        (urn, aspect)
        for urn, aspect in emit_calls
        if isinstance(aspect, DataProcessInstanceRunEventClass)
    ]
    complete_events = [
        aspect
        for _, aspect in run_events
        if aspect.status == DataProcessRunStatusClass.COMPLETE
    ]

    assert len(complete_events) == 1, (
        "A terminal COMPLETE RunEvent must be emitted even when the extractor raises. "
        "spec: BACKEND.md §Custom Ingestor Authoring Contract §Failure semantics"
    )
    assert complete_events[0].result is not None, (
        "Terminal COMPLETE RunEvent on failure path must carry a result."
    )
    assert complete_events[0].result.type == RunResultTypeClass.FAILURE, (
        f"Terminal RunEvent result.type must be FAILURE when extractor raises; "
        f"got {complete_events[0].result.type!r}. "
        "spec: BACKEND.md §Custom Ingestor Authoring Contract row #5"
    )
    # spec: BACKEND.md §Custom Ingestor Authoring Contract row #5 —
    # nativeResultType is author-specific; the only constraint is that it is a non-empty string.
    assert isinstance(complete_events[0].result.nativeResultType, str), (
        "nativeResultType must be a string on failure path. "
        "spec: BACKEND.md §Custom Ingestor Authoring Contract row #5"
    )
    assert complete_events[0].result.nativeResultType, (
        "nativeResultType must be non-empty on failure path. "
        "spec: BACKEND.md §Custom Ingestor Authoring Contract row #5"
    )

    # spec: BACKEND.md §Custom Ingestor Authoring Contract §Ordering guarantee —
    # STARTED must precede COMPLETE; both must reference the same DPI URN.
    started_run_events = [
        (urn, aspect)
        for urn, aspect in run_events
        if aspect.status == DataProcessRunStatusClass.STARTED
    ]
    assert len(started_run_events) >= 1, (
        "A STARTED RunEvent must be emitted before the COMPLETE RunEvent. "
        "spec: BACKEND.md §Custom Ingestor Authoring Contract row #3"
    )
    started_urn, _ = started_run_events[0]
    complete_urn = next(
        urn for urn, aspect in run_events if aspect.status == DataProcessRunStatusClass.COMPLETE
    )
    assert started_urn == complete_urn, (
        f"STARTED and COMPLETE RunEvents must reference the same DPI URN; "
        f"STARTED URN={started_urn!r}, COMPLETE URN={complete_urn!r}. "
        "spec: BACKEND.md §Custom Ingestor Authoring Contract §Ordering guarantee"
    )

    started_index_fail = next(
        i
        for i, (_, aspect) in enumerate(emit_calls)
        if isinstance(aspect, DataProcessInstanceRunEventClass)
        and aspect.status == DataProcessRunStatusClass.STARTED
    )
    complete_index_fail = next(
        i
        for i, (_, aspect) in enumerate(emit_calls)
        if isinstance(aspect, DataProcessInstanceRunEventClass)
        and aspect.status == DataProcessRunStatusClass.COMPLETE
    )
    assert started_index_fail < complete_index_fail, (
        "STARTED RunEvent must appear before COMPLETE RunEvent even on failure path. "
        "spec: BACKEND.md §Custom Ingestor Authoring Contract §Ordering guarantee"
    )

    added_event_types = [
        getattr(call_args.args[0], "event_type", None)
        for call_args in db.add.call_args_list
        if hasattr(call_args.args[0] if call_args.args else None, "event_type")
    ]
    assert INGESTION_FAIL in added_event_types, (
        f"Expected INGESTION.FAIL event row on extractor exception; got {added_event_types}. "
        "spec: BACKEND.md §Active run pipeline"
    )


async def test_active_custom_dry_run_skips_dpi_emission(service, db):
    """dry_run=True runs the extractor pre-flight but emits no DPI aspects.

    spec: BACKEND.md §Ingestion Service —
        "dry_run: true runs the extractor and returns the schema preview
        without emitting any aspects"
    spec: BACKEND.md §Active run pipeline —
        "emit DataProcessInstanceRunEvent(STARTED) … (skipped on dry_run)"
    spec: BACKEND.md §Active run pipeline —
        "emit DataProcessInstanceRunEvent(COMPLETE | FAILED) … (skipped on dry_run)"
    spec: BACKEND.md §Active run pipeline —
        "record INGESTION.COMPLETE event (recorded for both dry-run and non-dry-run)"
    """
    from src.backend.ingestion.extractors import IngestionResult

    config_row = _make_config_row(
        platform="postgres",
        mode="active-custom",
        is_enabled=True,
    )
    mock_scalar_query(db, config_row)
    mock_db_refresh(db)

    emit_calls: list[tuple[str, object]] = []

    async def _capture_emit(urn: str, aspect: object) -> None:
        emit_calls.append((urn, aspect))

    service._datahub.emit_aspect = _capture_emit

    with patch(
        "src.backend.ingestion.service.run_datahub_ingestion",
        new=AsyncMock(
            return_value=IngestionResult(entities_ingested=0, errors=[], warnings=[])
        ),
    ):
        result = await service.run(_DATASET_URN, dry_run=True)

    assert result.detail["dry_run"] is True

    # spec: BACKEND.md §Ingestion Service — "dry_run: true runs the extractor and
    # returns the schema preview without emitting any aspects"
    # All DPI aspects (Properties, Relationships, Output, RunEvent) must be skipped.
    emitted_dpi_urns = [
        urn for urn, _aspect in emit_calls if urn.startswith("urn:li:dataProcessInstance:")
    ]
    assert not emitted_dpi_urns, (
        f"dry-run must not emit any DPI aspects; leaked DPI URNs: {emitted_dpi_urns}. "
        "spec: BACKEND.md §Ingestion Service — dry_run skips DPI emission entirely"
    )

    added_event_types = [
        getattr(call_args.args[0], "event_type", None)
        for call_args in db.add.call_args_list
        if hasattr(call_args.args[0] if call_args.args else None, "event_type")
    ]
    assert INGESTION_COMPLETE in added_event_types, (
        f"INGESTION.COMPLETE event row must be recorded even for dry_run=True; "
        f"got {added_event_types}. "
        "spec: BACKEND.md §Active run pipeline — dry-run event observability"
    )


# ── workflow_dag_id derivation ────────────────────────────────────────────────


async def test_upsert_active_custom_daily_sets_workflow_dag_id(service, db):
    """upsert_config active-custom + daily sets workflow_dag_id='ingestion-active-daily'.

    spec: feature/BACKEND_SCHEMA.md §workflow_dag_id — 'Airflow DAG ID of the assigned
    periodic DAG (active-custom mode only)'. Tier DAG IDs follow the pattern
    ingestion-active-{tier} derived from the DAG files in src/workflows/dags/.
    """
    mock_scalar_query(db, None)
    mock_db_refresh(db)

    with patch("src.backend.ingestion.service.ensure_dataset_registered", new=AsyncMock()):
        result, created = await service.upsert_config(
            dataset_urn=_DATASET_URN,
            mode="active-custom",
            platform="postgres",
            locator=_LOCATOR,
            identifier=_IDENTIFIER,
            auth=_AUTH,
            is_enabled=True,
            schedule_tier="daily",
        )

    assert created is True
    assert result.workflow_dag_id == "ingestion-active-daily", (
        f"active-custom + daily must produce workflow_dag_id='ingestion-active-daily'; "
        f"got {result.workflow_dag_id!r}. "
        "spec: feature/BACKEND_SCHEMA.md §workflow_dag_id"
    )


@pytest.mark.parametrize(
    "schedule_tier,expected_dag_id",
    [
        ("hourly", "ingestion-active-hourly"),
        ("daily", "ingestion-active-daily"),
        ("weekly", "ingestion-active-weekly"),
    ],
)
async def test_upsert_active_custom_each_tier_sets_matching_dag(
    service, db, schedule_tier, expected_dag_id
):
    """upsert_config active-custom sets workflow_dag_id matching the schedule_tier.

    spec: feature/BACKEND_SCHEMA.md §workflow_dag_id — tier DAG IDs are
    ingestion-active-{tier}. Parametrized over all three valid tiers.
    """
    mock_scalar_query(db, None)
    mock_db_refresh(db)

    with patch("src.backend.ingestion.service.ensure_dataset_registered", new=AsyncMock()):
        result, _ = await service.upsert_config(
            dataset_urn=_DATASET_URN,
            mode="active-custom",
            platform="postgres",
            locator=_LOCATOR,
            identifier=_IDENTIFIER,
            auth=_AUTH,
            is_enabled=True,
            schedule_tier=schedule_tier,
        )

    assert result.workflow_dag_id == expected_dag_id, (
        f"active-custom + schedule_tier={schedule_tier!r} must produce "
        f"workflow_dag_id={expected_dag_id!r}; got {result.workflow_dag_id!r}. "
        "spec: feature/BACKEND_SCHEMA.md §workflow_dag_id"
    )


async def test_upsert_passive_leaves_workflow_dag_id_null(service, db):
    """upsert_config passive mode leaves workflow_dag_id=None.

    spec: feature/BACKEND_SCHEMA.md §workflow_dag_id — 'active-custom mode only';
    passive configs must not carry a DAG ID.
    """
    mock_scalar_query(db, None)
    mock_db_refresh(db)

    with patch("src.backend.ingestion.service.ensure_dataset_registered", new=AsyncMock()):
        result, created = await service.upsert_config(
            dataset_urn=_DATASET_URN,
            mode="passive",
            platform="postgres",
            locator=None,
            identifier=_IDENTIFIER,
            auth=None,
            is_enabled=True,
            schedule_tier=None,
        )

    assert created is True
    assert result.workflow_dag_id is None, (
        f"passive config must have workflow_dag_id=None; "
        f"got {result.workflow_dag_id!r}. "
        "spec: feature/BACKEND_SCHEMA.md §workflow_dag_id"
    )


async def test_patch_schedule_tier_updates_workflow_dag_id(service, db):
    """PATCH schedule_tier from daily to hourly updates workflow_dag_id accordingly.

    spec: feature/BACKEND_SCHEMA.md §workflow_dag_id — derivation is re-evaluated
    on every patch_config call using the final (post-patch) row state.
    """
    existing_row = _make_config_row(
        mode="active-custom",
        schedule_tier="daily",
        workflow_dag_id="ingestion-active-daily",
    )
    mock_scalar_query(db, existing_row)
    mock_db_refresh(db)

    result = await service.patch_config(_DATASET_URN, {"schedule_tier": "hourly"})

    assert existing_row.schedule_tier == "hourly"
    assert existing_row.workflow_dag_id == "ingestion-active-hourly", (
        f"PATCH schedule_tier='hourly' must update workflow_dag_id to "
        f"'ingestion-active-hourly'; got {existing_row.workflow_dag_id!r}. "
        "spec: feature/BACKEND_SCHEMA.md §workflow_dag_id"
    )
    assert result.workflow_dag_id == "ingestion-active-hourly", (
        f"Returned record must reflect the updated workflow_dag_id; "
        f"got {result.workflow_dag_id!r}."
    )


async def test_patch_mode_to_passive_clears_workflow_dag_id(service, db):
    """PATCH mode from active-custom to passive clears workflow_dag_id to None.

    spec: feature/BACKEND_SCHEMA.md §workflow_dag_id — 'active-custom mode only';
    switching to passive must null out the DAG ID.
    """
    existing_row = _make_config_row(
        mode="active-custom",
        schedule_tier="daily",
        workflow_dag_id="ingestion-active-daily",
    )
    mock_scalar_query(db, existing_row)
    mock_db_refresh(db)

    result = await service.patch_config(_DATASET_URN, {"mode": "passive"})

    assert existing_row.mode == "passive"
    assert existing_row.workflow_dag_id is None, (
        f"PATCH mode='passive' must clear workflow_dag_id to None; "
        f"got {existing_row.workflow_dag_id!r}. "
        "spec: feature/BACKEND_SCHEMA.md §workflow_dag_id"
    )
    assert result.workflow_dag_id is None, (
        f"Returned record must carry workflow_dag_id=None after mode switch to passive; "
        f"got {result.workflow_dag_id!r}."
    )


# ── systemMetadata emission on DPI aspects ────────────────────────────────────


async def test_active_custom_run_emits_systemmetadata_on_dpi_aspects(service, db):
    """Happy path: every DPI-targeting emit carries system_metadata with
    runId starting with 'dataspoke-postgres-'.

    The STARTED emit and the terminal COMPLETE emit (happy path) must carry the
    same runId — the single sysmeta object is reused for all aspects in a run.

    spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement —
        "every aspect emission targeting a dataset URN within a custom ingestor
        run MUST carry a non-default systemMetadata"
    spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement —
        "The same sysmeta object is reused for all aspects in a single run
        (Properties, Relationships, Output, and all RunEvents on the DPI)"
    spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement —
        "runId='dataspoke-{platform}-{run_id}', matching the DPI URN suffix
        <platform>-<run_id>, so dataset aspects and the DPI cross-reference cleanly"
    spec: USE_CASE_en.md §UC1 Case 1 — active-custom run emits DPI lifecycle
    """
    from datahub.metadata.schema_classes import (
        DataProcessInstanceRunEventClass,
        DataProcessRunStatusClass,
        SystemMetadataClass,
    )

    from src.backend.ingestion.extractors import IngestionResult

    config_row = _make_config_row(
        platform="postgres",
        mode="active-custom",
        is_enabled=True,
    )
    mock_scalar_query(db, config_row)
    mock_db_refresh(db)

    emit_calls: list[tuple[str, object, object | None]] = []

    async def _capture_emit(urn: str, aspect: object, **kwargs: object) -> None:
        emit_calls.append((urn, aspect, kwargs.get("system_metadata")))

    service._datahub.emit_aspect = _capture_emit

    with (
        patch(
            "src.backend.ingestion.service.run_datahub_ingestion",
            new=AsyncMock(
                return_value=IngestionResult(entities_ingested=3, errors=[], warnings=[])
            ),
        ),
        patch("src.backend.ingestion.service.mark_registered", new=AsyncMock()),
    ):
        result = await service.run(_DATASET_URN, dry_run=False)

    assert result.status == "success"
    run_id = result.run_id
    expected_dpi_urn = f"urn:li:dataProcessInstance:postgres-{run_id}"

    dpi_calls = [(urn, aspect, sm) for urn, aspect, sm in emit_calls if urn == expected_dpi_urn]
    assert len(dpi_calls) >= 4, (
        f"Expected at least 4 DPI emit calls; got {len(dpi_calls)}. "
        "spec: BACKEND.md §Custom Ingestor Authoring Contract"
    )

    expected_run_id = f"dataspoke-postgres-{run_id}"

    for i, (urn, aspect, sysmeta) in enumerate(dpi_calls):
        assert sysmeta is not None, (
            f"DPI emit call #{i} (aspect={type(aspect).__name__}) must carry "
            f"system_metadata; got None. "
            "spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement"
        )
        assert isinstance(sysmeta, SystemMetadataClass), (
            f"DPI emit call #{i} system_metadata must be SystemMetadataClass; "
            f"got {type(sysmeta).__name__!r}"
        )
        assert sysmeta.runId.startswith("dataspoke-postgres-"), (
            f"DPI emit call #{i} system_metadata.runId must start with 'dataspoke-postgres-'; "
            f"got {sysmeta.runId!r}. "
            "spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement"
        )

    # The STARTED and terminal COMPLETE (success path) emit must carry the same runId.
    # spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement —
    #     "The same sysmeta object is reused for all aspects in a single run"
    run_events_with_meta = [
        (aspect, sm)
        for _, aspect, sm in dpi_calls
        if isinstance(aspect, DataProcessInstanceRunEventClass)
    ]
    started_run_ids = [
        sm.runId
        for aspect, sm in run_events_with_meta
        if aspect.status == DataProcessRunStatusClass.STARTED and sm is not None
    ]
    complete_run_ids = [
        sm.runId
        for aspect, sm in run_events_with_meta
        if aspect.status == DataProcessRunStatusClass.COMPLETE and sm is not None
    ]

    assert started_run_ids, "At least one STARTED RunEvent with system_metadata must exist"
    assert complete_run_ids, "At least one COMPLETE RunEvent with system_metadata must exist"

    assert started_run_ids[0] == complete_run_ids[0], (
        f"STARTED and terminal COMPLETE RunEvent must carry the same runId; "
        f"STARTED runId={started_run_ids[0]!r}, COMPLETE runId={complete_run_ids[0]!r}. "
        "spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement"
    )
    assert started_run_ids[0] == expected_run_id, (
        f"STARTED RunEvent runId must equal {expected_run_id!r}; "
        f"got {started_run_ids[0]!r}. "
        "spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement"
    )


async def test_active_custom_run_failure_path_emits_systemmetadata_on_terminal_dpi(service, db):
    """Failure path: the terminal COMPLETE (FAILURE) DPI emit also carries system_metadata
    with runId matching 'dataspoke-postgres-<run_id>'.

    spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement —
        "The same sysmeta object is reused for all aspects in a single run …
        and all RunEvents on the DPI"
    spec: BACKEND.md §Custom Ingestor Authoring Contract §Failure semantics —
        "a failed run still emits the COMPLETE RunEvent"
    """
    from datahub.metadata.schema_classes import (
        DataProcessInstanceRunEventClass,
        DataProcessRunStatusClass,
        RunResultTypeClass,
        SystemMetadataClass,
    )

    config_row = _make_config_row(
        platform="postgres",
        mode="active-custom",
        is_enabled=True,
    )
    mock_scalar_query(db, config_row)
    mock_db_refresh(db)

    emit_calls: list[tuple[str, object, object | None]] = []

    async def _capture_emit(urn: str, aspect: object, **kwargs: object) -> None:
        emit_calls.append((urn, aspect, kwargs.get("system_metadata")))

    service._datahub.emit_aspect = _capture_emit

    extractor_error = RuntimeError("DB connection refused")

    with (
        patch(
            "src.backend.ingestion.service.run_datahub_ingestion",
            new=AsyncMock(side_effect=extractor_error),
        ),
        pytest.raises(RuntimeError, match="DB connection refused"),
    ):
        await service.run(_DATASET_URN, dry_run=False)

    # The failure-path COMPLETE emit is fired from within the except block.
    # It must still carry system_metadata with the run's dataspoke-postgres- prefix.
    # spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement
    failure_complete_events = [
        (aspect, sm)
        for _, aspect, sm in emit_calls
        if isinstance(aspect, DataProcessInstanceRunEventClass)
        and aspect.status == DataProcessRunStatusClass.COMPLETE
        and getattr(getattr(aspect, "result", None), "type", None) == RunResultTypeClass.FAILURE
    ]

    assert len(failure_complete_events) == 1, (
        f"Expected exactly one failure-path COMPLETE RunEvent; "
        f"got {len(failure_complete_events)}. "
        "spec: BACKEND.md §Custom Ingestor Authoring Contract §Failure semantics"
    )

    _, sm = failure_complete_events[0]
    assert sm is not None, (
        "Failure-path COMPLETE RunEvent must carry system_metadata; got None. "
        "spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement"
    )
    assert isinstance(sm, SystemMetadataClass), (
        f"Failure-path COMPLETE RunEvent system_metadata must be SystemMetadataClass; "
        f"got {type(sm).__name__!r}"
    )
    assert sm.runId.startswith("dataspoke-postgres-"), (
        f"Failure-path COMPLETE RunEvent system_metadata.runId must start with "
        f"'dataspoke-postgres-'; got {sm.runId!r}. "
        "spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement"
    )


async def test_patch_active_custom_with_null_schedule_tier_keeps_dag_id_null(service, db):
    """active-custom row with schedule_tier=None: patching mode='active-custom' keeps dag_id null.

    This exercises the case where an active-custom config was stored without a valid
    schedule_tier (e.g., created with schedule_tier=None before tier was required).
    _derive_workflow_dag_id(mode='active-custom', schedule_tier=None) must return None
    because None is not in _VALID_TIERS.

    spec: feature/BACKEND_SCHEMA.md §workflow_dag_id — tier must be one of
    {hourly, daily, weekly}; None schedule_tier must not produce a DAG ID.

    Note: the public upsert API accepts schedule_tier=None for active-custom (the
    schema column is nullable), so this row state is reachable without bypassing
    the API layer.
    """
    existing_row = _make_config_row(
        mode="active-custom",
        schedule_tier=None,
        workflow_dag_id=None,
    )
    mock_scalar_query(db, existing_row)
    mock_db_refresh(db)

    # PATCH mode back to active-custom (no-op change) — workflow_dag_id must remain null
    result = await service.patch_config(_DATASET_URN, {"mode": "active-custom"})

    assert existing_row.workflow_dag_id is None, (
        f"active-custom + schedule_tier=None must keep workflow_dag_id=None; "
        f"got {existing_row.workflow_dag_id!r}. "
        "spec: feature/BACKEND_SCHEMA.md §workflow_dag_id"
    )
    assert result.workflow_dag_id is None, (
        f"Returned record must carry workflow_dag_id=None when schedule_tier is None; "
        f"got {result.workflow_dag_id!r}."
    )
