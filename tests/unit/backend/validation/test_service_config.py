"""Unit tests for ValidationService config CRUD + listing.

Covers upsert_config (precondition, first PUT, second PUT, recreate-after-delete),
get_config (present / absent), patch_config, delete_config (hard delete + cascade),
list_configs (aggregation), get_events.

Conf ``variables`` is a JSONB array of ``{name, description}`` objects.

spec: VALIDATION.md §Rule Configuration, §API Surface
spec: BACKEND.md §Validation Service
spec: BACKEND_SCHEMA.md §validation_configs
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backend.validation.service import ValidationService
from src.shared.events import (
    VALIDATION_PREFIX,
    VALIDATION_RESULT_RECORDED,
)
from src.shared.exceptions import (
    EntityNotFoundError,
    PreconditionFailedError,
)
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
_FAKE_URN = "urn:li:assertion:abc123"


# ── get_config ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_config_present_returns_record(
    svc: ValidationService, db: AsyncMock
) -> None:
    """get_config returns the record for an existing slot.

    spec: VALIDATION.md §Rule Configuration — GET returns the conf.
    """
    existing = _make_config_row(description="row count check")
    db.execute = AsyncMock(return_value=_scalar_result(existing))

    record = await svc.get_config(dataset_urn=_DATASET_URN)

    assert record is not None
    assert record.description == "row count check"


@pytest.mark.asyncio
async def test_get_config_absent_returns_none(
    svc: ValidationService, db: AsyncMock
) -> None:
    """get_config returns None when the slot does not exist (router → CONFIG_NOT_FOUND).

    spec: VALIDATION.md §Rule Configuration — a slot that does not exist returns
    404 CONFIG_NOT_FOUND. After a DELETE the dataset reads as never-created, so
    the service returns None and the router raises CONFIG_NOT_FOUND.
    spec: API.md §DELETE attr/validation/conf — afterwards GET → 404 CONFIG_NOT_FOUND.
    """
    db.execute = AsyncMock(return_value=_scalar_result(None))

    record = await svc.get_config(dataset_urn=_DATASET_URN)

    assert record is None


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
    """First PUT creates a row, returns (record, created=True), emits assertion info.

    spec: VALIDATION.md §Rule Configuration — PUT creates or replaces the config.
    spec: VALIDATION.md §DataHub Aspect Mapping — emits assertionInfo.
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
async def test_upsert_config_recreates_after_delete(
    svc: ValidationService, db: AsyncMock, datahub: AsyncMock
) -> None:
    """PUT after a hard delete simply creates a fresh conf (created=True) — no 409.

    spec: API.md §DELETE attr/validation/conf — after delete the dataset reads as
    never-created; a fresh PUT creates a new conf (201). There is no resurrection
    concept and no VALIDATION_CONF_REMOVED conflict.
    """
    registry_row = MagicMock()
    registry_row.datahub_registered = True
    registry_result = _scalar_result(registry_row)
    # The prior conf was hard-deleted, so the slot select returns nothing.
    config_miss = _scalar_result(None)

    db.execute = AsyncMock(side_effect=[registry_result, config_miss])
    db.commit = AsyncMock()

    with (
        patch(_REGISTER, new_callable=AsyncMock) as mock_register,
        patch(_BUILD_INFO),
        patch(_BUILD_URN, return_value=_FAKE_URN),
    ):
        mock_db_refresh(db)

        _record, created = await svc.upsert_config(
            dataset_urn=_DATASET_URN,
            description="recreated after delete",
            variables=[_var("null_rate")],
        )

    assert created is True, "PUT after delete must create a new conf (201), not resurrect"
    mock_register.assert_called_once()
    db.commit.assert_awaited()


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
async def test_patch_config_absent_returns_config_not_found(
    svc: ValidationService, db: AsyncMock
) -> None:
    """PATCH on an absent slot raises EntityNotFoundError (CONFIG_NOT_FOUND).

    spec: VALIDATION.md §Rule Configuration — a slot that does not exist returns
    404 CONFIG_NOT_FOUND. There is no removed-tombstone state.
    """
    db.execute = AsyncMock(return_value=_scalar_result(None))

    with pytest.raises(EntityNotFoundError) as exc_info:
        await svc.patch_config(
            dataset_urn=_DATASET_URN,
            patch={"description": "no slot to patch"},
        )

    assert exc_info.value.error_code == "CONFIG_NOT_FOUND"


# ── delete_config (hard delete + cascade) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_config_hard_deletes_conf_and_assertion(
    svc: ValidationService, db: AsyncMock, datahub: AsyncMock
) -> None:
    """delete_config hard-deletes the conf row and hard-deletes the DataHub assertion.

    spec: API.md §DELETE attr/validation/conf — removes the conf row and
    hard-deletes the DataHub assertion entity (no status.removed tombstone).
    spec: VALIDATION.md §Rule Configuration — DELETE is a hard delete.
    """
    existing = _make_config_row()
    db.execute = AsyncMock(return_value=_scalar_result(existing))
    db.commit = AsyncMock()
    db.delete = AsyncMock()
    datahub.hard_delete_entity = AsyncMock()

    with patch(_BUILD_URN, return_value=_FAKE_URN):
        await svc.delete_config(dataset_urn=_DATASET_URN)

    # The conf row is removed outright (no is_removed soft-delete flag set).
    db.delete.assert_awaited_once_with(existing)
    # The DataHub assertion is hard-deleted at the deterministic assertion URN.
    datahub.hard_delete_entity.assert_awaited_once_with(_FAKE_URN)
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_delete_config_cascades_results_and_validation_events(
    svc: ValidationService, db: AsyncMock, datahub: AsyncMock
) -> None:
    """delete_config cascades to delete the dataset's results and validation events.

    spec: API.md §DELETE attr/validation/conf — cascades to delete the dataset's
    validation results and validation events.
    spec: BACKEND.md §Validation Service — cascade scoped to VALIDATION.* events;
    other features' events for the same dataset are untouched.

    The two cascade DELETE statements (results, then validation-scoped events) plus
    the conf-row delete are issued before commit; the assertion hard-delete follows.
    """
    existing = _make_config_row()
    # 1 select (find the conf) + 2 cascade deletes (results, events) on db.execute.
    select_result = _scalar_result(existing)
    delete_results_stmt = MagicMock()
    delete_events_stmt = MagicMock()
    db.execute = AsyncMock(
        side_effect=[select_result, delete_results_stmt, delete_events_stmt]
    )
    db.commit = AsyncMock()
    db.delete = AsyncMock()
    datahub.hard_delete_entity = AsyncMock()

    from sqlalchemy.dialects import postgresql

    with patch(_BUILD_URN, return_value=_FAKE_URN):
        await svc.delete_config(dataset_urn=_DATASET_URN)

    # Two cascade DELETE statements were executed (after the initial SELECT).
    assert db.execute.await_count == 3, (
        "delete_config must issue 1 SELECT + 2 cascade DELETE statements; "
        f"got {db.execute.await_count} db.execute calls"
    )

    # Render both cascade DELETE statements and assert they target the right tables.
    cascade_stmts = [
        str(
            call.args[0].compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        for call in db.execute.await_args_list[1:]
    ]
    joined = "\n".join(cascade_stmts)
    assert "DELETE FROM" in joined and "validation_results" in joined, (
        "delete_config must DELETE the dataset's validation_results rows. "
        f"Cascade statements:\n{joined}"
    )
    assert "events" in joined, (
        "delete_config must DELETE the dataset's validation events. "
        f"Cascade statements:\n{joined}"
    )
    # The events cascade is scoped to VALIDATION.* events only.
    assert VALIDATION_PREFIX in joined, (
        "events cascade must be scoped to VALIDATION.* events (event_type prefix); "
        f"got:\n{joined}"
    )

    db.delete.assert_awaited_once_with(existing)
    datahub.hard_delete_entity.assert_awaited_once_with(_FAKE_URN)


@pytest.mark.asyncio
async def test_delete_config_absent_raises_config_not_found(
    svc: ValidationService, db: AsyncMock, datahub: AsyncMock
) -> None:
    """delete_config on an absent slot raises CONFIG_NOT_FOUND; no cascade, no DataHub call.

    spec: API.md §DELETE attr/validation/conf — DELETE on an absent slot is a 404.
    """
    db.execute = AsyncMock(return_value=_scalar_result(None))
    db.commit = AsyncMock()
    db.delete = AsyncMock()
    datahub.hard_delete_entity = AsyncMock()

    with patch(_BUILD_URN, return_value=_FAKE_URN):
        with pytest.raises(EntityNotFoundError) as exc_info:
            await svc.delete_config(dataset_urn=_DATASET_URN)

    assert exc_info.value.error_code == "CONFIG_NOT_FOUND"
    db.delete.assert_not_awaited()
    datahub.hard_delete_entity.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_config_records_no_event(
    svc: ValidationService, db: AsyncMock, datahub: AsyncMock
) -> None:
    """delete_config records no event — the cascade wipes the dataset's validation events.

    spec: API.md §DELETE attr/validation/conf — the delete records no event; the
    cascade removes the dataset's validation events so its events panel is empty.
    """
    existing = _make_config_row()
    db.execute = AsyncMock(return_value=_scalar_result(existing))
    db.commit = AsyncMock()
    db.delete = AsyncMock()
    datahub.hard_delete_entity = AsyncMock()

    with (
        patch(_BUILD_URN, return_value=_FAKE_URN),
        patch.object(svc, "_record_event", new_callable=AsyncMock) as mock_event,
    ):
        await svc.delete_config(dataset_urn=_DATASET_URN)

    mock_event.assert_not_awaited()


# ── list_configs ──────────────────────────────────────────────────────────────


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
