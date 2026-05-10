"""Unit tests for IngestionService config CRUD + workflow_dag_id derivation.

Covers get_config / upsert_config / patch_config / delete_config / list_configs /
list_active_for_tier / list_passive_configs / workflow_dag_id derivation.

spec: BACKEND.md §Ingestion Service
spec: feature/BACKEND_SCHEMA.md §workflow_dag_id
spec: USE_CASE_en.md §UC1
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backend.ingestion.service import IngestionService
from src.shared.exceptions import EntityNotFoundError
from tests.unit.backend.conftest import (
    mock_db_refresh,
    mock_paginated_query,
    mock_scalar_query,
)
from tests.unit.backend.ingestion.conftest import (
    _AUTH,
    _DATASET_URN,
    _IDENTIFIER,
    _LOCATOR,
    _make_config_row,
)


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

    assert created is True
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

    assert created is False
    assert existing_row.platform == "mysql"
    assert existing_row.locator == new_locator
    assert existing_row.identifier == new_identifier
    assert existing_row.auth == new_auth
    assert existing_row.is_enabled is True
    assert existing_row.schedule_tier == "weekly"
    assert result.mode == "passive"
    assert result.is_enabled is True


# ── patch_config ─────────────────────────────────────────────────────────────


async def test_patch_config_applies_schedule_tier(service, db):
    # spec: BACKEND.md §Ingestion Service — PATCH mutates is_enabled / schedule_tier
    existing_row = _make_config_row()
    mock_scalar_query(db, existing_row)
    mock_db_refresh(db)

    result = await service.patch_config(_DATASET_URN, {"schedule_tier": "hourly"})
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


# ── list_passive_configs ──────────────────────────────────────────────────────


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
