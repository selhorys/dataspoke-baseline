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
from tests.unit.conftest import route_db_execute

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

    route_db_execute(db, [("dataset_registry", registry_result)], default=config_miss)
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

    route_db_execute(db, [("dataset_registry", registry_result)], default=config_result)
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

    route_db_execute(db, [("dataset_registry", registry_result)], default=config_miss)
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


# ── attribute + parameter lifecycle ──────────────────────────────────────────
#
# spec: VALIDATION.md §Rule Configuration — `attribute` is written wholesale, per-field
# defaulted, on both PUT and PATCH; `parameter` is the one optional-by-absence section,
# whose PUT/PATCH/GET lifecycle that section states in full.


def _patched_registry(db: AsyncMock, config_row) -> None:
    """Route the upsert's two reads: the registry precondition, then the conf slot."""
    registry_row = MagicMock()
    registry_row.datahub_registered = True
    route_db_execute(
        db,
        [("dataset_registry", _scalar_result(registry_row))],
        default=_scalar_result(config_row),
    )
    db.commit = AsyncMock()


@pytest.mark.asyncio
async def test_put_without_attribute_stores_the_all_defaults_cadence(
    svc: ValidationService, db: AsyncMock, datahub: AsyncMock
) -> None:
    """A PUT that names no cadence still stores a complete object.

    The column is NOT NULL and the `validation-score` measurer reads it without
    branching on absence, so "omitted" has to become the all-defaults object at the
    write rather than a null the reader has to repair.

    spec: VALIDATION.md §Rule Configuration — "Omitting it on `PUT` stores the
    all-defaults object; it is never absent from a stored conf or from a response."
    """
    _patched_registry(db, None)

    with (
        patch(_REGISTER, new_callable=AsyncMock),
        patch(_BUILD_INFO),
        patch(_BUILD_URN, return_value=_FAKE_URN),
    ):
        mock_db_refresh(db)
        record, _created = await svc.upsert_config(
            dataset_urn=_DATASET_URN,
            description="Daily row count check",
            variables=[_var("row_cnt")],
        )

    assert record.attribute == {"cadence_unit": 86400, "cadence_offset": 0}


@pytest.mark.asyncio
async def test_put_with_a_partial_attribute_fills_the_unnamed_field_with_its_default(
    svc: ValidationService, db: AsyncMock, datahub: AsyncMock
) -> None:
    """A partially-named cadence is completed field-by-field, not stored partial.

    spec: VALIDATION.md §Rule Configuration — "Supplying `attribute` on `PUT` or
    `PATCH` writes the **complete** per-field-defaulted object".
    """
    _patched_registry(db, None)

    with (
        patch(_REGISTER, new_callable=AsyncMock),
        patch(_BUILD_INFO),
        patch(_BUILD_URN, return_value=_FAKE_URN),
    ):
        mock_db_refresh(db)
        record, _created = await svc.upsert_config(
            dataset_urn=_DATASET_URN,
            description="D-8 partition check",
            variables=[_var("row_cnt")],
            attribute={"cadence_offset": 7},
        )

    assert record.attribute == {"cadence_unit": 86400, "cadence_offset": 7}


@pytest.mark.asyncio
async def test_put_replaces_a_stored_attribute_wholesale_rather_than_merging(
    svc: ValidationService, db: AsyncMock, datahub: AsyncMock
) -> None:
    """A PUT over a non-default cadence resets the field it does not name.

    The stored row carries an hourly D-2 cadence; the PUT names only `cadence_offset`,
    so `cadence_unit` must come back to `86400` — the *default*, never the stored
    `3600`. A deep-merge would leave 3600 in place and silently keep measuring against
    a cadence the operator just replaced.

    spec: VALIDATION.md §Rule Configuration — the complete object "replac[es] the
    previous value outright rather than deep-merging into it — the same
    wholesale-replacement rule `variables` follows."
    """
    existing = _make_config_row(attribute={"cadence_unit": 3600, "cadence_offset": 2})
    _patched_registry(db, existing)

    with (
        patch(_REGISTER, new_callable=AsyncMock),
        patch(_BUILD_INFO),
        patch(_BUILD_URN, return_value=_FAKE_URN),
    ):
        mock_db_refresh(db)
        record, created = await svc.upsert_config(
            dataset_urn=_DATASET_URN,
            description="replaced",
            variables=[_var("row_cnt")],
            attribute={"cadence_offset": 7},
        )

    assert created is False, "backstop: this PUT must have replaced an existing row"
    assert existing.attribute == {"cadence_unit": 86400, "cadence_offset": 7}
    assert record.attribute == {"cadence_unit": 86400, "cadence_offset": 7}


@pytest.mark.asyncio
async def test_put_omitting_parameter_clears_a_previously_stored_one(
    svc: ValidationService, db: AsyncMock, datahub: AsyncMock
) -> None:
    """PUT is a full replace, so an omitted `parameter` wipes the stored section.

    The seeded row carries a parameter list, which is what makes the absence
    meaningful: without it the assertion would hold trivially.

    spec: VALIDATION.md §Rule Configuration — "**`PUT`** is a full replace, like every
    other field on it: omitting `parameter` stores it as absent, clearing any previously
    stored value."
    """
    existing = _make_config_row(
        parameter=[{"name": "z_threshold", "description": "Std-dev cutoff"}]
    )
    _patched_registry(db, existing)

    with (
        patch(_REGISTER, new_callable=AsyncMock),
        patch(_BUILD_INFO),
        patch(_BUILD_URN, return_value=_FAKE_URN),
    ):
        mock_db_refresh(db)
        record, _created = await svc.upsert_config(
            dataset_urn=_DATASET_URN,
            description="replaced without parameters",
            variables=[_var("row_cnt")],
        )

    assert existing.parameter is None, (
        "the stored section must be cleared, not preserved. "
        "spec: VALIDATION.md §Rule Configuration — PUT is a full replace."
    )
    assert record.parameter is None


@pytest.mark.asyncio
async def test_put_stores_a_supplied_parameter_list_verbatim(
    svc: ValidationService, db: AsyncMock, datahub: AsyncMock
) -> None:
    """Backstop for the clearing test above: a supplied list does reach the row.

    Without this, an `upsert_config` that dropped `parameter` unconditionally would
    still pass the clearing assertion.

    spec: VALIDATION.md §Rule Configuration — `parameter` is "opaque storage for the
    pipeline's own hyperparameters"; DataSpoke "never interprets it".
    """
    _patched_registry(db, None)
    supplied = [
        {"name": "z_threshold", "description": "Std-dev cutoff for outliers"},
        {"name": "window_days", "description": ""},
    ]

    with (
        patch(_REGISTER, new_callable=AsyncMock),
        patch(_BUILD_INFO),
        patch(_BUILD_URN, return_value=_FAKE_URN),
    ):
        mock_db_refresh(db)
        record, _created = await svc.upsert_config(
            dataset_urn=_DATASET_URN,
            description="with hyperparameters",
            variables=[_var("row_cnt")],
            parameter=supplied,
        )

    assert record.parameter == supplied


@pytest.mark.asyncio
async def test_patch_attribute_replaces_wholesale_and_never_merges_the_stored_value(
    svc: ValidationService, db: AsyncMock
) -> None:
    """A PATCH naming one cadence field resets the other to its **default**.

    The stored row's `cadence_unit` is `3600`; the patch names only `cadence_offset`.
    The result must be `86400` — the default — proving the replacement is against the
    schema's defaults and not against the prior stored value. This is the one behaviour
    a deep-merge implementation would get wrong while passing every other test here.

    spec: VALIDATION.md §Rule Configuration — "A `PATCH` carrying
    `{"attribute": {"cadence_offset": 7}}` therefore also resets `cadence_unit` to
    `86400`."
    """
    existing = _make_config_row(attribute={"cadence_unit": 3600, "cadence_offset": 2})
    db.execute = AsyncMock(return_value=_scalar_result(existing))
    db.commit = AsyncMock()

    with (
        patch(_REGISTER, new_callable=AsyncMock),
        patch(_BUILD_INFO),
        patch(_BUILD_URN, return_value=_FAKE_URN),
    ):
        mock_db_refresh(db)
        # The router hands the service the request model's complete dump; here only the
        # field the caller named is present, which is the harder input — the service's
        # own defaulting is what has to complete it.
        record = await svc.patch_config(
            dataset_urn=_DATASET_URN,
            patch={"attribute": {"cadence_offset": 7}},
        )

    assert existing.attribute == {"cadence_unit": 86400, "cadence_offset": 7}, (
        "cadence_unit must reset to its default, not keep the stored 3600. "
        "spec: VALIDATION.md §Rule Configuration."
    )
    assert record.attribute == {"cadence_unit": 86400, "cadence_offset": 7}


@pytest.mark.asyncio
async def test_patch_without_attribute_leaves_the_stored_cadence_alone(
    svc: ValidationService, db: AsyncMock
) -> None:
    """A PATCH that names no cadence does not touch the stored one.

    The seeded cadence is deliberately non-default, so a service that rewrote the
    section on every patch would visibly reset it here.

    spec: VALIDATION.md §Rule Configuration — the `attribute` replacement rule binds
    only when the section is supplied; PATCH is otherwise "a partial update".
    """
    existing = _make_config_row(attribute={"cadence_unit": 3600, "cadence_offset": 2})
    db.execute = AsyncMock(return_value=_scalar_result(existing))
    db.commit = AsyncMock()

    with (
        patch(_REGISTER, new_callable=AsyncMock),
        patch(_BUILD_INFO),
        patch(_BUILD_URN, return_value=_FAKE_URN),
    ):
        mock_db_refresh(db)
        record = await svc.patch_config(
            dataset_urn=_DATASET_URN,
            patch={"description": "renamed only"},
        )

    assert existing.attribute == {"cadence_unit": 3600, "cadence_offset": 2}
    assert record.attribute == {"cadence_unit": 3600, "cadence_offset": 2}


@pytest.mark.asyncio
async def test_patch_omitting_parameter_preserves_the_stored_value(
    svc: ValidationService, db: AsyncMock
) -> None:
    """An absent `parameter` key leaves the stored section untouched.

    The patch dict comes from `model_dump(exclude_unset=True)`, so key *presence* is
    what carries the meaning — an `is not None` test alone could not tell this case
    from the explicit-null one below.

    spec: VALIDATION.md §Rule Configuration — "Omitting `parameter` leaves the stored
    value unchanged."
    """
    stored = [{"name": "z_threshold", "description": "Std-dev cutoff"}]
    existing = _make_config_row(parameter=stored)
    db.execute = AsyncMock(return_value=_scalar_result(existing))
    db.commit = AsyncMock()

    with (
        patch(_REGISTER, new_callable=AsyncMock),
        patch(_BUILD_INFO),
        patch(_BUILD_URN, return_value=_FAKE_URN),
    ):
        mock_db_refresh(db)
        record = await svc.patch_config(
            dataset_urn=_DATASET_URN,
            patch={"description": "renamed only"},
        )

    assert existing.parameter == stored
    assert record.parameter == stored


@pytest.mark.asyncio
async def test_patch_with_an_explicit_null_clears_the_parameter_section(
    svc: ValidationService, db: AsyncMock
) -> None:
    """A present-and-null `parameter` clears the section — the one spelling of "clear".

    The contrast with the omission test above is the whole content of the rule: the two
    calls differ only in whether the key is in the patch dict at all.

    spec: VALIDATION.md §Rule Configuration — "`"parameter": null` clears it to absent —
    that is the one spelling for 'clear'."
    """
    existing = _make_config_row(
        parameter=[{"name": "z_threshold", "description": "Std-dev cutoff"}]
    )
    db.execute = AsyncMock(return_value=_scalar_result(existing))
    db.commit = AsyncMock()

    with (
        patch(_REGISTER, new_callable=AsyncMock),
        patch(_BUILD_INFO),
        patch(_BUILD_URN, return_value=_FAKE_URN),
    ):
        mock_db_refresh(db)
        record = await svc.patch_config(
            dataset_urn=_DATASET_URN,
            patch={"parameter": None},
        )

    assert existing.parameter is None
    assert record.parameter is None


@pytest.mark.asyncio
async def test_patch_with_a_non_empty_list_replaces_the_parameter_section_wholesale(
    svc: ValidationService, db: AsyncMock
) -> None:
    """A supplied list replaces the stored one outright — no element-wise merge.

    The stored and patched lists share a name with a different description and the
    patched list drops one of the stored entries, so a merge would leave the dropped
    entry behind or keep the stale description.

    spec: VALIDATION.md §Rule Configuration — "A non-empty list (1–200 entries,
    validated exactly as `variables` is) replaces the stored value wholesale."
    """
    existing = _make_config_row(
        parameter=[
            {"name": "z_threshold", "description": "old cutoff"},
            {"name": "window_days", "description": "lookback"},
        ]
    )
    db.execute = AsyncMock(return_value=_scalar_result(existing))
    db.commit = AsyncMock()
    replacement = [{"name": "z_threshold", "description": "new cutoff"}]

    with (
        patch(_REGISTER, new_callable=AsyncMock),
        patch(_BUILD_INFO),
        patch(_BUILD_URN, return_value=_FAKE_URN),
    ):
        mock_db_refresh(db)
        record = await svc.patch_config(
            dataset_urn=_DATASET_URN,
            patch={"parameter": replacement},
        )

    assert existing.parameter == replacement, (
        "the dropped `window_days` entry must not survive the replacement"
    )
    assert record.parameter == replacement


@pytest.mark.asyncio
async def test_get_config_carries_a_stored_parameter_and_none_when_absent(
    svc: ValidationService, db: AsyncMock
) -> None:
    """The record distinguishes a stored `parameter` from an absent one.

    `None` is the absent state the route renders by omitting the key; an empty list is
    not an admissible value, so it must never be what absence reads as.

    spec: VALIDATION.md §Rule Configuration — "**`GET`** omits the `parameter` key
    entirely from the response body when the section is absent; it is never serialized
    as `null`."
    """
    stored = [{"name": "z_threshold", "description": "Std-dev cutoff"}]
    db.execute = AsyncMock(return_value=_scalar_result(_make_config_row(parameter=stored)))
    present = await svc.get_config(dataset_urn=_DATASET_URN)
    assert present is not None
    assert present.parameter == stored

    db.execute = AsyncMock(return_value=_scalar_result(_make_config_row(parameter=None)))
    absent = await svc.get_config(dataset_urn=_DATASET_URN)
    assert absent is not None
    assert absent.parameter is None, (
        "absence must read as None, never as an empty list — `[]` is rejected at the "
        "write boundary, so it is not a state the store can hold"
    )


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
    # Route by statement: the results-cascade DELETE, the events-cascade DELETE, and
    # the initial conf SELECT are dispatched by SQL rather than call order.
    route_db_execute(
        db,
        [
            (lambda s: "delete" in s and "validation_results" in s, delete_results_stmt),
            (lambda s: "delete" in s and "events" in s, delete_events_stmt),
        ],
        default=select_result,
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

    route_db_execute(
        db,
        [("count(", count_mock), ("validation_results", latest_mock)],
        default=rows_mock,
    )

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

    route_db_execute(
        db,
        [("count(", count_mock), ("validation_results", latest_mock)],
        default=rows_mock,
    )

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

    route_db_execute(
        db,
        [("count(", count_mock), ("validation_results", latest_mock)],
        default=rows_mock,
    )

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


# ── _latest_results_by_urn tiebreak (regression: GH #194) ──────────────────────


@pytest.mark.asyncio
async def test_latest_results_by_urn_orders_by_data_time_then_ingestion_time(
    svc: ValidationService, db: AsyncMock
) -> None:
    """The latest-result row_number() window partitions by dataset_urn and
    orders by data_time DESC, then ingestion_time DESC.

    ``validation_results`` is append-only, so a re-posted result for the same
    ``data_time`` leaves two rows; without the ``ingestion_time`` tiebreaker
    ``row_number()`` can resolve the tie to the stale row instead of the
    most-recently-ingested one. A canned ``db.execute`` return value cannot
    exercise that — Postgres decides the tie inside the window function, before
    Python ever sees a row — so this compiles the *actual* statement
    ``_latest_results_by_urn`` builds and asserts on the ``OVER (...)`` window
    clause itself, pinning both ``PARTITION BY dataset_urn`` and the
    ``ORDER BY`` column order within that one clause — not just anywhere in
    the statement — so a mutation that also swapped the partition key (e.g.
    accidentally copying the sibling ``get_results`` site's
    ``partition_by=data_time`` shape) is caught too.

    **Scope**: this proves the *statement DataSpoke builds* carries the right
    partition and tiebreak columns; it cannot prove PostgreSQL's `row_number()`
    actually resolves a real tie the way the SQL text says it will — that needs
    two real rows sharing a `data_time` and a live database. That proof
    belongs in ``tests/integration/spot/test_validation_list_view.py`` (a
    second POST at the same `data_time` with a different score, asserted
    against the list view's `latest_score`).

    Spec: spec/feature/VALIDATION.md §Duplicate `data_time` policy — "This
          last-write-wins resolution — newest `ingestion_time` breaks a
          `data_time` tie — is the tiebreak rule for **every** DataSpoke read
          that selects one `validation_results` row per group... It governs:
          ... the cross-dataset list view's per-dataset latest result
          (`ValidationService._latest_results_by_urn`, backing
          `GET /spoke/validation`)."
    """
    from sqlalchemy.dialects import postgresql

    latest_mock = MagicMock()
    latest_mock.all.return_value = []
    db.execute = AsyncMock(return_value=latest_mock)

    await svc._latest_results_by_urn([_DATASET_URN])

    assert db.execute.await_count == 1, (
        "_latest_results_by_urn issues exactly one query for a non-empty urns list"
    )
    stmt = db.execute.call_args_list[0].args[0]
    rendered = str(stmt.compile(dialect=postgresql.dialect()))

    # Isolate the row_number() window clause itself — `OVER (... ) AS rn` — so
    # the PARTITION BY / ORDER BY assertions below can't be satisfied by text
    # sitting elsewhere in the statement (e.g. an unrelated WHERE clause).
    over_start = rendered.index("OVER (") + len("OVER (")
    over_end = rendered.index(") AS rn")
    window_clause = rendered[over_start:over_end]

    partition_idx = window_clause.index("PARTITION BY")
    order_idx = window_clause.index("ORDER BY")
    partition_segment = window_clause[partition_idx:order_idx]
    assert "validation_results.dataset_urn" in partition_segment, (
        "expected the window to partition by dataset_urn (per-dataset latest "
        f"row); got PARTITION BY segment:\n{partition_segment}"
    )

    order_segment = window_clause[order_idx:]
    data_time_idx = order_segment.find("data_time DESC")
    ingestion_time_idx = order_segment.find("ingestion_time DESC")
    assert data_time_idx != -1 and ingestion_time_idx != -1, (
        "expected both 'data_time DESC' and 'ingestion_time DESC' in the "
        f"row_number() window's ORDER BY; got:\n{order_segment}"
    )
    assert data_time_idx < ingestion_time_idx, (
        "data_time must be the primary sort key and ingestion_time the "
        f"tiebreaker, in that order; got ORDER BY clause:\n{order_segment}"
    )


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

    route_db_execute(db, [("count(", count_mock)], default=rows_mock)

    events, total_count = await svc.get_events(dataset_urn=_DATASET_URN)

    assert total_count == 1
    assert len(events) == 1
    assert events[0]["event_type"].startswith(VALIDATION_PREFIX)
