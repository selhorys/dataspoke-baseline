"""Unit tests for ValidationService config CRUD + listing.

Covers upsert_config (precondition, first PUT, second PUT, resurrection),
patch_config, delete_config, list_configs (filters + aggregation), get_events.

Conf ``variables`` is a JSONB array of ``{name, description}`` objects.

spec: VALIDATION.md §Rule Configuration, §API Surface
spec: BACKEND.md §Validation Service
spec: BACKEND_SCHEMA.md §validation_configs
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backend.validation.service import ValidationService
from src.shared.events import VALIDATION_PREFIX, VALIDATION_RESULT_RECORDED
from src.shared.exceptions import EntityNotFoundError, PreconditionFailedError
from tests.unit.backend.conftest import mock_db_refresh
from tests.unit.backend.validation.conftest import (
    _DATASET_URN,
    _make_config_row,
    _scalar_count,
    _scalar_result,
    _var,
)

_REGISTER = "src.backend.validation.service.register_assertion"
_BUILD_INFO = "src.backend.validation.service.build_assertion_info"
_BUILD_URN = "src.backend.validation.service.build_assertion_urn"
_TOMBSTONE = "src.backend.validation.service.tombstone_assertion"
_FAKE_URN = "urn:li:assertion:abc123"


# ── upsert_config ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_config_precondition_dataset_not_in_datahub(
    svc: ValidationService, db: AsyncMock, datahub: AsyncMock
) -> None:
    """upsert_config raises DATASET_NOT_IN_DATAHUB when datahub_registered=false.

    spec: VALIDATION.md §API Surface — 422 DATASET_NOT_IN_DATAHUB if not in DataHub
    """
    registry_miss = _scalar_result(None)
    db.execute = AsyncMock(return_value=registry_miss)
    datahub.get_aspect = AsyncMock(return_value=None)
    db.flush = AsyncMock()
    db.rollback = AsyncMock()

    with pytest.raises(PreconditionFailedError) as exc_info:
        await svc.upsert_config(
            dataset_urn=_DATASET_URN,
            description="check",
            variables=[_var("row_cnt")],
        )
    assert exc_info.value.error_code == "DATASET_NOT_IN_DATAHUB"


@pytest.mark.asyncio
async def test_upsert_config_first_put_creates_row(
    svc: ValidationService, db: AsyncMock, datahub: AsyncMock
) -> None:
    """First PUT creates a row, returns (record, created=True), emits info + status.

    spec: VALIDATION.md §Rule Configuration — PUT creates or replaces the config.
    spec: VALIDATION.md §DataHub Aspect Mapping — emits assertionInfo + status.
    """
    registry_row = MagicMock()
    registry_row.datahub_registered = True
    registry_result = _scalar_result(registry_row)
    config_miss = _scalar_result(None)

    db.execute = AsyncMock(side_effect=[registry_result, config_miss])
    db.commit = AsyncMock()

    with (
        patch(_REGISTER, new_callable=AsyncMock) as mock_register,
        patch(_BUILD_INFO),
        patch(_BUILD_URN, return_value=_FAKE_URN),
    ):
        mock_db_refresh(db)

        record, created = await svc.upsert_config(
            dataset_urn=_DATASET_URN,
            description="Daily row count check",
            variables=[_var("row_cnt", "Daily row count"), _var("col1_mean")],
        )

    assert created is True
    assert record.is_removed is False
    # spec: BACKEND_SCHEMA.md — variables persisted as {name, description} objects.
    assert record.variables == [
        {"name": "row_cnt", "description": "Daily row count"},
        {"name": "col1_mean", "description": ""},
    ]
    mock_register.assert_called_once()


@pytest.mark.asyncio
async def test_upsert_config_second_put_updates_row(
    svc: ValidationService, db: AsyncMock, datahub: AsyncMock
) -> None:
    """Second PUT updates existing row, returns (record, created=False).

    spec: VALIDATION.md §Rule Configuration — PUT is create-or-replace.
    """
    registry_row = MagicMock()
    registry_row.datahub_registered = True
    registry_result = _scalar_result(registry_row)
    existing_config = _make_config_row()
    config_result = _scalar_result(existing_config)

    db.execute = AsyncMock(side_effect=[registry_result, config_result])
    db.commit = AsyncMock()

    with (
        patch(_REGISTER, new_callable=AsyncMock),
        patch(_BUILD_INFO),
        patch(_BUILD_URN, return_value=_FAKE_URN),
    ):
        mock_db_refresh(db)

        _record, created = await svc.upsert_config(
            dataset_urn=_DATASET_URN,
            description="Updated check",
            variables=[_var("row_cnt")],
        )

    assert created is False


@pytest.mark.asyncio
async def test_upsert_config_put_after_delete_resurrects_row(
    svc: ValidationService, db: AsyncMock, datahub: AsyncMock
) -> None:
    """PUT on a soft-deleted row flips is_removed back to False.

    spec: VALIDATION.md §Rule Configuration — DELETE soft-deletes; subsequent PUT
    resurrects the assertion (clears removed).
    """
    registry_row = MagicMock()
    registry_row.datahub_registered = True
    registry_result = _scalar_result(registry_row)
    deleted_config = _make_config_row(is_removed=True)
    config_result = _scalar_result(deleted_config)

    db.execute = AsyncMock(side_effect=[registry_result, config_result])
    db.commit = AsyncMock()

    with (
        patch(_REGISTER, new_callable=AsyncMock),
        patch(_BUILD_INFO),
        patch(_BUILD_URN, return_value=_FAKE_URN),
    ):
        mock_db_refresh(db)

        await svc.upsert_config(
            dataset_urn=_DATASET_URN,
            description="Resurrected check",
            variables=[_var("row_cnt")],
        )

    assert deleted_config.is_removed is False


# ── patch_config ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_config_only_updates_supplied_fields(
    svc: ValidationService, db: AsyncMock
) -> None:
    """patch_config updates only the supplied fields, emits assertionInfo.

    spec: VALIDATION.md §Rule Configuration — PATCH accepts partial body.
    spec: VALIDATION.md §DataHub Aspect Mapping — emits assertionInfo.
    """
    existing = _make_config_row(
        description="old description",
        variables=[_var("row_cnt"), _var("col1_mean")],
    )
    db.execute = AsyncMock(return_value=_scalar_result(existing))
    db.commit = AsyncMock()

    with (
        patch(_REGISTER, new_callable=AsyncMock) as mock_register,
        patch(_BUILD_INFO),
        patch(_BUILD_URN, return_value=_FAKE_URN),
    ):
        mock_db_refresh(db)

        await svc.patch_config(
            dataset_urn=_DATASET_URN,
            patch={"description": "new description"},
        )

    assert existing.description == "new description"
    # variables unchanged because the patch did not include them.
    assert existing.variables == [_var("row_cnt"), _var("col1_mean")]
    mock_register.assert_called_once()


@pytest.mark.asyncio
async def test_patch_config_replaces_variables_with_objects(
    svc: ValidationService, db: AsyncMock
) -> None:
    """patch_config stores the replacement variable objects verbatim.

    spec: VALIDATION.md §Rule Configuration — replacing variables is allowed.
    spec: BACKEND_SCHEMA.md — variables stored as {name, description} objects.
    """
    existing = _make_config_row(variables=[_var("row_cnt")])
    db.execute = AsyncMock(return_value=_scalar_result(existing))
    db.commit = AsyncMock()

    new_vars = [_var("row_cnt", "Daily row count"), _var("null_rate", "Null rate")]

    with (
        patch(_REGISTER, new_callable=AsyncMock),
        patch(_BUILD_INFO),
        patch(_BUILD_URN, return_value=_FAKE_URN),
    ):
        mock_db_refresh(db)

        await svc.patch_config(
            dataset_urn=_DATASET_URN,
            patch={"variables": new_vars},
        )

    assert existing.variables == new_vars


@pytest.mark.asyncio
async def test_patch_config_on_soft_deleted_returns_not_found(
    svc: ValidationService, db: AsyncMock
) -> None:
    """PATCH on a soft-deleted slot is invisible and raises EntityNotFoundError.

    spec: VALIDATION.md §Rule Configuration — after DELETE, GET conf returns 404;
    the resource view treats a soft-deleted rule as absent. PATCH targets the same
    resource view, so a tombstoned row must not be silently mutated.
    """
    from sqlalchemy.dialects import postgresql

    db.execute = AsyncMock(return_value=_scalar_result(None))

    with pytest.raises(EntityNotFoundError):
        await svc.patch_config(
            dataset_urn=_DATASET_URN,
            patch={"description": "should not apply to a tombstoned slot"},
        )

    select_stmt = db.execute.call_args_list[0].args[0]
    rendered = str(
        select_stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "is_removed" in rendered, (
        f"Expected is_removed filter in patch_config SELECT to honor the "
        f"resource view of GET; got:\n{rendered}"
    )


# ── delete_config ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_config_sets_is_removed_true(
    svc: ValidationService, db: AsyncMock
) -> None:
    """delete_config soft-deletes the row and emits status(removed=True).

    spec: VALIDATION.md §Rule Configuration — DELETE performs soft delete.
    """
    existing = _make_config_row()
    db.execute = AsyncMock(return_value=_scalar_result(existing))
    db.commit = AsyncMock()

    with (
        patch(_TOMBSTONE, new_callable=AsyncMock) as mock_tombstone,
        patch(_BUILD_URN, return_value=_FAKE_URN),
    ):
        await svc.delete_config(dataset_urn=_DATASET_URN)

    assert existing.is_removed is True
    mock_tombstone.assert_called_once_with(svc._datahub, _FAKE_URN)


# ── list_configs ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_configs_removed_true_filter(
    svc: ValidationService, db: AsyncMock
) -> None:
    """removed_filter=True shows only soft-deleted configs; SQL has is_removed=true.

    spec: VALIDATION.md §API Surface — cross-dataset list filterable by removed status.
    """
    from sqlalchemy.dialects import postgresql

    count_mock = _scalar_count(1)
    config_row = _make_config_row(is_removed=True)
    rows_mock = MagicMock()
    rows_mock.scalars.return_value.all.return_value = [config_row]

    latest_mock = MagicMock()
    latest_mock.all.return_value = []

    db.execute = AsyncMock(side_effect=[count_mock, rows_mock, latest_mock])

    items, total_count = await svc.list_configs(removed_filter=True)
    assert total_count == 1
    assert all(item.is_removed for item in items)

    first_stmt = db.execute.call_args_list[0].args[0]
    rendered = str(
        first_stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "is_removed" in rendered, (
        f"Expected 'is_removed' in count query SQL; got:\n{rendered}"
    )


@pytest.mark.asyncio
async def test_list_configs_removed_false_filter(
    svc: ValidationService, db: AsyncMock
) -> None:
    """removed_filter=False shows only active configs; SQL targets the is_removed column.

    spec: VALIDATION.md §API Surface — cross-dataset list filterable by removed status.
    """
    from sqlalchemy.dialects import postgresql

    count_mock = _scalar_count(2)
    active_row = _make_config_row(is_removed=False)
    rows_mock = MagicMock()
    rows_mock.scalars.return_value.all.return_value = [active_row]

    latest_mock = MagicMock()
    latest_mock.all.return_value = []

    db.execute = AsyncMock(side_effect=[count_mock, rows_mock, latest_mock])

    items, total_count = await svc.list_configs(removed_filter=False)
    assert total_count == 2
    assert all(not item.is_removed for item in items)

    first_stmt = db.execute.call_args_list[0].args[0]
    rendered = str(
        first_stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "is_removed" in rendered, (
        f"Expected 'is_removed' in count query SQL; got:\n{rendered}"
    )


@pytest.mark.asyncio
async def test_list_configs_variable_count_is_len_of_array(
    svc: ValidationService, db: AsyncMock
) -> None:
    """list_configs reports variable_count = number of declared variable objects.

    spec: VALIDATION.md §API Surface — row.variable_count == len(conf.variables).
    The count is over the {name, description} object array, not over names only.
    """
    count_mock = _scalar_count(1)
    config_row = _make_config_row(
        variables=[_var("row_cnt"), _var("fill_rate"), _var("anomaly_score")]
    )
    rows_mock = MagicMock()
    rows_mock.scalars.return_value.all.return_value = [config_row]

    latest_mock = MagicMock()
    latest_mock.all.return_value = []

    db.execute = AsyncMock(side_effect=[count_mock, rows_mock, latest_mock])

    items, _total = await svc.list_configs()
    assert items[0].variable_count == 3


@pytest.mark.asyncio
async def test_list_configs_aggregates_latest_result_per_dataset(
    svc: ValidationService, db: AsyncMock
) -> None:
    """list_configs joins latest result (latest_data_time, latest_score) per dataset.

    spec: VALIDATION.md §API Surface — cross-dataset list aggregates conf + latest result.
    """
    count_mock = _scalar_count(1)
    config_row = _make_config_row()
    rows_mock = MagicMock()
    rows_mock.scalars.return_value.all.return_value = [config_row]

    latest_data_time = datetime(2026, 5, 8, tzinfo=UTC)
    latest_result_row = MagicMock()
    latest_result_row.dataset_urn = _DATASET_URN
    latest_result_row.data_time = latest_data_time
    latest_result_row.score = 0.95

    latest_mock = MagicMock()
    latest_mock.all.return_value = [latest_result_row]

    db.execute = AsyncMock(side_effect=[count_mock, rows_mock, latest_mock])

    items, total_count = await svc.list_configs()
    assert len(items) == 1
    assert items[0].latest_data_time == latest_data_time
    assert items[0].latest_score == 0.95


_DATASET_URN_B = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,"
    "example_db.reviews.user_ratings,DEV)"
)


@pytest.mark.asyncio
async def test_list_configs_aggregates_correct_latest_per_dataset_across_multiple_datasets(
    svc: ValidationService, db: AsyncMock
) -> None:
    """list_configs attributes each latest result to the correct dataset when configs
    for ≥2 datasets are returned in the same query.

    spec: VALIDATION.md §API Surface — cross-dataset list aggregates conf + latest result.
    The join must partition by dataset_urn so dataset A's latest result is not
    attributed to dataset B (no row crossover).
    """
    count_mock = _scalar_count(2)

    config_a = _make_config_row(dataset_urn=_DATASET_URN)
    config_b = _make_config_row(
        dataset_urn=_DATASET_URN_B,
        description="Rating score null-rate check",
    )
    rows_mock = MagicMock()
    rows_mock.scalars.return_value.all.return_value = [config_a, config_b]

    dt_a = datetime(2026, 5, 10, tzinfo=UTC)
    score_a = 0.8
    latest_a = MagicMock()
    latest_a.dataset_urn = _DATASET_URN
    latest_a.data_time = dt_a
    latest_a.score = score_a

    dt_b = datetime(2026, 5, 7, tzinfo=UTC)
    score_b = 0.5
    latest_b = MagicMock()
    latest_b.dataset_urn = _DATASET_URN_B
    latest_b.data_time = dt_b
    latest_b.score = score_b

    latest_mock = MagicMock()
    latest_mock.all.return_value = [latest_a, latest_b]

    db.execute = AsyncMock(side_effect=[count_mock, rows_mock, latest_mock])

    items, total_count = await svc.list_configs()

    assert total_count == 2
    assert len(items) == 2

    by_urn = {item.dataset_urn: item for item in items}

    assert _DATASET_URN in by_urn, "Dataset A must appear in the list"
    assert _DATASET_URN_B in by_urn, "Dataset B must appear in the list"

    item_a = by_urn[_DATASET_URN]
    assert item_a.latest_data_time == dt_a, (
        f"Dataset A latest_data_time={item_a.latest_data_time!r} "
        f"must equal its own result dt_a={dt_a!r} (no row crossover)"
    )
    assert item_a.latest_score == score_a

    item_b = by_urn[_DATASET_URN_B]
    assert item_b.latest_data_time == dt_b, (
        f"Dataset B latest_data_time={item_b.latest_data_time!r} "
        f"must equal its own result dt_b={dt_b!r} (no row crossover)"
    )
    assert item_b.latest_score == score_b


# ── get_events ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_events_returns_only_validation_events(
    svc: ValidationService, db: AsyncMock
) -> None:
    """get_events returns only events where event_type starts with 'VALIDATION.'.

    spec: BACKEND.md §Event Catalogue — VALIDATION.* event namespace.
    """
    import uuid

    count_mock = _scalar_count(1)

    event_row = MagicMock()
    event_row.id = uuid.uuid4()
    event_row.entity_type = "dataset"
    event_row.entity_id = _DATASET_URN
    event_row.event_type = VALIDATION_RESULT_RECORDED
    event_row.status = "success"
    event_row.detail = {"score": 1.0}
    event_row.occurred_at = datetime.now(tz=UTC)

    rows_mock = MagicMock()
    rows_mock.scalars.return_value.all.return_value = [event_row]

    db.execute = AsyncMock(side_effect=[count_mock, rows_mock])

    events, total_count = await svc.get_events(dataset_urn=_DATASET_URN)

    assert total_count == 1
    assert len(events) == 1
    assert events[0]["event_type"].startswith(VALIDATION_PREFIX)
