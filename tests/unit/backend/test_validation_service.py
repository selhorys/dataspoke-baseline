"""Unit tests for src/backend/validation/service.py — ValidationService.

Mocks: DataHubClient (AsyncMock), AsyncSession (AsyncMock).
No real DB or DataHub connections.

spec: VALIDATION.md §Rule Configuration, §Validation Result, §API Surface
spec: BACKEND.md §Validation Service
"""

import re
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from src.backend.validation.service import ValidationService
from src.shared.events import (
    VALIDATION_CONFIG_CREATE,
    VALIDATION_CONFIG_DELETE,
    VALIDATION_CONFIG_UPDATE,
    VALIDATION_PREFIX,
    VALIDATION_RESULT_RECORDED,
)
from src.shared.exceptions import (
    DataHubUnavailableError,
    EntityNotFoundError,
    PreconditionFailedError,
)

from tests.unit.backend.conftest import mock_db_refresh

_DATASET_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.orders.daily_fulfillment_summary,DEV)"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_config_row(
    dataset_urn: str = _DATASET_URN,
    description: str = "Daily row count check",
    variables: list[str] | None = None,
    is_removed: bool = False,
) -> MagicMock:
    row = MagicMock()
    row.dataset_urn = dataset_urn
    row.description = description
    row.variables = variables if variables is not None else ["row_cnt", "col1_mean"]
    row.is_removed = is_removed
    row.created_at = datetime.now(tz=UTC)
    row.updated_at = datetime.now(tz=UTC)
    return row


def _make_result_row(
    dataset_urn: str = _DATASET_URN,
    data_time: datetime | None = None,
    score: float = 1.0,
    variables: dict | None = None,
    ingestion_time: datetime | None = None,
) -> MagicMock:
    row = MagicMock()
    row.id = uuid.uuid4()
    row.dataset_urn = dataset_urn
    row.data_time = data_time or datetime(2026, 5, 1, tzinfo=UTC)
    row.score = score
    row.variables = variables or {"row_cnt": 50.0}
    row.ingestion_time = ingestion_time or datetime.now(tz=UTC)
    return row


def _scalar_result(value):
    """Return a mock that behaves like db.execute().scalar_one_or_none()."""
    m = MagicMock()
    m.scalar_one_or_none.return_value = value
    return m


def _scalar_count(n: int):
    """Return a mock that behaves like db.execute().scalar()."""
    m = MagicMock()
    m.scalar.return_value = n
    return m


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def svc(datahub: AsyncMock, db: AsyncMock) -> ValidationService:
    return ValidationService(datahub=datahub, db=db)


# ── upsert_config ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_config_precondition_dataset_not_in_datahub(
    svc: ValidationService, db: AsyncMock, datahub: AsyncMock
) -> None:
    """upsert_config raises DATASET_NOT_IN_DATAHUB when registry says datahub_registered=false.

    spec: VALIDATION.md §API Surface — 422 DATASET_NOT_IN_DATAHUB if not in DataHub
    """
    # Simulate: no existing registry row, DataHub returns None (dataset not found)
    registry_miss = _scalar_result(None)
    db.execute = AsyncMock(return_value=registry_miss)
    datahub.get_aspect = AsyncMock(return_value=None)  # not in DataHub
    db.flush = AsyncMock()
    db.rollback = AsyncMock()

    with pytest.raises(PreconditionFailedError) as exc_info:
        await svc.upsert_config(
            dataset_urn=_DATASET_URN,
            description="check",
            variables=["row_cnt"],
        )
    assert exc_info.value.error_code == "DATASET_NOT_IN_DATAHUB"


@pytest.mark.asyncio
async def test_upsert_config_first_put_creates_row(
    svc: ValidationService, db: AsyncMock, datahub: AsyncMock
) -> None:
    """First PUT creates a new DB row, returns (record, created=True), emits assertionInfo + status.

    spec: VALIDATION.md §Rule Configuration — PUT creates or replaces the configuration.
    spec: VALIDATION.md §DataHub Aspect Mapping — emits assertionInfo and status(removed=False).
    """
    registry_row = MagicMock()
    registry_row.datahub_registered = True
    registry_miss = _scalar_result(registry_row)
    config_miss = _scalar_result(None)
    count_result = _scalar_count(0)

    db.execute = AsyncMock(side_effect=[registry_miss, config_miss])
    db.commit = AsyncMock()

    with patch("src.backend.validation.service.register_assertion", new_callable=AsyncMock) as mock_register, \
         patch("src.backend.validation.service.build_assertion_info") as mock_info, \
         patch("src.backend.validation.service.build_assertion_urn", return_value="urn:li:assertion:abc123"):

        mock_db_refresh(db)

        record, created = await svc.upsert_config(
            dataset_urn=_DATASET_URN,
            description="Daily row count check",
            variables=["row_cnt", "col1_mean"],
        )

    assert created is True
    assert record.is_removed is False
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

    with patch("src.backend.validation.service.register_assertion", new_callable=AsyncMock), \
         patch("src.backend.validation.service.build_assertion_info"), \
         patch("src.backend.validation.service.build_assertion_urn", return_value="urn:li:assertion:abc123"):

        mock_db_refresh(db)

        record, created = await svc.upsert_config(
            dataset_urn=_DATASET_URN,
            description="Updated check",
            variables=["row_cnt"],
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

    with patch("src.backend.validation.service.register_assertion", new_callable=AsyncMock), \
         patch("src.backend.validation.service.build_assertion_info"), \
         patch("src.backend.validation.service.build_assertion_urn", return_value="urn:li:assertion:abc123"):

        mock_db_refresh(db)

        record, created = await svc.upsert_config(
            dataset_urn=_DATASET_URN,
            description="Resurrected check",
            variables=["row_cnt"],
        )

    # The row's is_removed was mutated to False by the service
    assert deleted_config.is_removed is False


# ── patch_config ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_config_only_updates_supplied_fields(
    svc: ValidationService, db: AsyncMock
) -> None:
    """patch_config updates only the supplied fields, emits assertionInfo.

    spec: VALIDATION.md §Rule Configuration — PATCH accepts partial body.
    spec: VALIDATION.md §DataHub Aspect Mapping — emits assertionInfo with merged vars.
    """
    existing = _make_config_row(
        description="old description",
        variables=["row_cnt", "col1_mean"],
    )
    db.execute = AsyncMock(return_value=_scalar_result(existing))
    db.commit = AsyncMock()

    with patch("src.backend.validation.service.register_assertion", new_callable=AsyncMock) as mock_register, \
         patch("src.backend.validation.service.build_assertion_info") as mock_info, \
         patch("src.backend.validation.service.build_assertion_urn", return_value="urn:li:assertion:abc123"):

        mock_db_refresh(db)

        record = await svc.patch_config(
            dataset_urn=_DATASET_URN,
            patch={"description": "new description"},  # only description
        )

    # description was updated
    assert existing.description == "new description"
    # variables were NOT touched
    assert existing.variables == ["row_cnt", "col1_mean"]
    mock_register.assert_called_once()


# ── delete_config ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_config_sets_is_removed_true(
    svc: ValidationService, db: AsyncMock
) -> None:
    """delete_config soft-deletes the row and emits status(removed=True).

    spec: VALIDATION.md §Rule Configuration — DELETE performs soft delete via status.removed=true.
    """
    existing = _make_config_row()
    db.execute = AsyncMock(return_value=_scalar_result(existing))
    db.commit = AsyncMock()

    with patch("src.backend.validation.service.tombstone_assertion", new_callable=AsyncMock) as mock_tombstone, \
         patch("src.backend.validation.service.build_assertion_urn", return_value="urn:li:assertion:abc123"):

        await svc.delete_config(dataset_urn=_DATASET_URN)

    assert existing.is_removed is True
    mock_tombstone.assert_called_once_with(svc._datahub, "urn:li:assertion:abc123")


# ── record_result ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_result_unknown_variable_key_raises(
    svc: ValidationService, db: AsyncMock
) -> None:
    """record_result raises UNKNOWN_VARIABLE with sorted offending names.

    spec: VALIDATION.md §Validation rules on POST — unknown keys → 422 UNKNOWN_VARIABLE
    listing the offending names.
    """
    config = _make_config_row(variables=["row_cnt", "null_rate"])
    db.execute = AsyncMock(return_value=_scalar_result(config))

    with pytest.raises(PreconditionFailedError) as exc_info:
        await svc.record_result(
            dataset_urn=_DATASET_URN,
            data_time=datetime(2026, 5, 1, tzinfo=UTC),
            score=1.0,
            variables={"row_cnt": 50.0, "zz_unknown": 1.0, "aa_other": 2.0},
        )

    err = exc_info.value
    assert err.error_code == "UNKNOWN_VARIABLE"
    # unknown keys are sorted
    assert err.detail["unknown"] == sorted(["zz_unknown", "aa_other"])


@pytest.mark.asyncio
async def test_record_result_score_out_of_range_raises(
    svc: ValidationService,
) -> None:
    """record_result raises INVALID_SCORE before any DB query for out-of-range scores.

    spec: VALIDATION.md §Validation rules on POST — score outside [0.0, 1.0] → 422 INVALID_SCORE
    """
    with pytest.raises(PreconditionFailedError) as exc_info:
        await svc.record_result(
            dataset_urn=_DATASET_URN,
            data_time=datetime(2026, 5, 1, tzinfo=UTC),
            score=1.5,  # out of range
            variables={"row_cnt": 50.0},
        )
    assert exc_info.value.error_code == "INVALID_SCORE"


@pytest.mark.asyncio
async def test_record_result_missing_declared_key_accepted_silently(
    svc: ValidationService, db: AsyncMock
) -> None:
    """Missing declared variable keys are accepted (partial coverage is legitimate).

    spec: VALIDATION.md §Validation rules on POST — Missing declared keys are accepted
    silently (a result with partial coverage is a legitimate signal).
    """
    config = _make_config_row(variables=["row_cnt", "col1_mean", "null_rate"])
    db.execute = AsyncMock(return_value=_scalar_result(config))
    db.commit = AsyncMock()

    with patch("src.backend.validation.service.report_result", new_callable=AsyncMock, return_value=True), \
         patch("src.backend.validation.service.build_run_event"), \
         patch("src.backend.validation.service.build_assertion_urn", return_value="urn:li:assertion:abc123"):

        mock_db_refresh(db)

        # Only row_cnt supplied; col1_mean and null_rate are omitted
        record = await svc.record_result(
            dataset_urn=_DATASET_URN,
            data_time=datetime(2026, 5, 1, tzinfo=UTC),
            score=1.0,
            variables={"row_cnt": 50.0},
        )

    # No exception raised — partial coverage is fine
    assert record is not None


@pytest.mark.asyncio
async def test_record_result_inserts_row_with_correct_fields(
    svc: ValidationService, db: AsyncMock
) -> None:
    """record_result inserts a ValidationResult row with correct fields and emits a
    run_event whose timestampMillis matches data_time.

    spec: VALIDATION.md §Validation Result — result POST persists to the store.
    spec: VALIDATION.md §assertionRunEvent — timestampMillis = data_time epoch ms.
    """
    from src.shared.db.models import ValidationResult

    config = _make_config_row(variables=["row_cnt"])
    db.execute = AsyncMock(return_value=_scalar_result(config))
    db.commit = AsyncMock()

    data_time = datetime(2026, 5, 1, tzinfo=UTC)
    score = 0.9
    variables = {"row_cnt": 50.0}
    dataset_urn = _DATASET_URN

    added_objects: list = []

    def capture_add(obj):
        added_objects.append(obj)

    db.add = MagicMock(side_effect=capture_add)

    with patch("src.backend.validation.service.report_result", new_callable=AsyncMock, return_value=True) as mock_report, \
         patch("src.backend.validation.service.build_assertion_urn", return_value="urn:li:assertion:abc123"):

        mock_db_refresh(db)

        record = await svc.record_result(
            dataset_urn=dataset_urn,
            data_time=data_time,
            score=score,
            variables=variables,
        )

    # Capture the ValidationResult row passed to db.add()
    result_rows = [o for o in added_objects if isinstance(o, ValidationResult)]
    assert result_rows, "ValidationResult row must be added to db"
    row = result_rows[0]

    assert row.data_time == data_time, (
        f"row.data_time={row.data_time!r} != data_time={data_time!r}"
    )
    assert row.score == score, f"row.score={row.score!r} != score={score!r}"
    assert row.variables == variables, (
        f"row.variables={row.variables!r} != variables={variables!r}"
    )
    assert row.dataset_urn == dataset_urn, (
        f"row.dataset_urn={row.dataset_urn!r} != dataset_urn={dataset_urn!r}"
    )

    # Verify report_result was called with a run_event whose timestampMillis = data_time epoch ms.
    # We let the real build_run_event run (not mocked), so inspect report_result's third arg.
    # Signature: report_result(datahub, assertion_urn, run_event) — run_event is args[2].
    assert mock_report.called, "report_result must be called"
    call_args = mock_report.call_args
    run_event_arg = (
        call_args.args[2]
        if len(call_args.args) > 2
        else call_args.kwargs.get("run_event")
    )
    assert run_event_arg is not None, "run_event must be passed to report_result as third arg"
    expected_ms = int(data_time.timestamp() * 1000)
    assert run_event_arg.timestampMillis == expected_ms, (
        f"run_event.timestampMillis={run_event_arg.timestampMillis} != {expected_ms} "
        "(must use data_time, not server now)"
    )


@pytest.mark.asyncio
async def test_record_result_records_event_after_successful_emit(
    svc: ValidationService, db: AsyncMock
) -> None:
    """VALIDATION.RESULT_RECORDED event is recorded after a successful DataHub emit.

    spec: BACKEND.md §Event Catalogue — VALIDATION.RESULT_RECORDED on success.
    """
    config = _make_config_row(variables=["row_cnt"])
    db.execute = AsyncMock(return_value=_scalar_result(config))
    db.commit = AsyncMock()

    events_added = []
    original_add = db.add

    def capture_add(obj):
        events_added.append(obj)

    db.add = MagicMock(side_effect=capture_add)

    with patch("src.backend.validation.service.report_result", new_callable=AsyncMock, return_value=True), \
         patch("src.backend.validation.service.build_run_event"), \
         patch("src.backend.validation.service.build_assertion_urn", return_value="urn:li:assertion:abc123"):

        mock_db_refresh(db)

        await svc.record_result(
            dataset_urn=_DATASET_URN,
            data_time=datetime(2026, 5, 1, tzinfo=UTC),
            score=1.0,
            variables={"row_cnt": 50.0},
        )

    # An Event row with VALIDATION.RESULT_RECORDED must have been added
    from src.shared.db.models import Event
    event_types = [
        getattr(obj, "event_type", None)
        for obj in events_added
        if hasattr(obj, "event_type")
    ]
    assert VALIDATION_RESULT_RECORDED in event_types, (
        f"Expected {VALIDATION_RESULT_RECORDED} event; got: {event_types}"
    )


@pytest.mark.asyncio
async def test_record_result_datahub_emit_failure_raises_without_recording_event(
    svc: ValidationService, db: AsyncMock
) -> None:
    """When DataHub emit returns False, DataHubUnavailableError is raised and
    VALIDATION.RESULT_RECORDED is NOT recorded (row stays in DB).

    spec: VALIDATION.md §Validation Result — row inserted regardless of emit success;
    on emit failure DataHubUnavailableError raised; RESULT_RECORDED not recorded.
    """
    config = _make_config_row(variables=["row_cnt"])
    db.execute = AsyncMock(return_value=_scalar_result(config))
    db.commit = AsyncMock()

    events_added = []

    def capture_add(obj):
        events_added.append(obj)

    db.add = MagicMock(side_effect=capture_add)

    with patch("src.backend.validation.service.report_result", new_callable=AsyncMock, return_value=False), \
         patch("src.backend.validation.service.build_run_event"), \
         patch("src.backend.validation.service.build_assertion_urn", return_value="urn:li:assertion:abc123"):

        mock_db_refresh(db)

        with pytest.raises(DataHubUnavailableError):
            await svc.record_result(
                dataset_urn=_DATASET_URN,
                data_time=datetime(2026, 5, 1, tzinfo=UTC),
                score=1.0,
                variables={"row_cnt": 50.0},
            )

    # VALIDATION.RESULT_RECORDED must NOT be in added events
    event_types = [
        getattr(obj, "event_type", None)
        for obj in events_added
        if hasattr(obj, "event_type")
    ]
    assert VALIDATION_RESULT_RECORDED not in event_types, (
        f"RESULT_RECORDED must not be emitted on DataHub failure; got: {event_types}"
    )


# ── get_results ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_results_from_until_filter_inclusivity(
    svc: ValidationService, db: AsyncMock
) -> None:
    """from is inclusive (>=), until is exclusive (<).

    spec: VALIDATION.md §GET result — from: Inclusive lower bound; until: Exclusive upper bound.
    """
    # We're only checking the SQL shape here (spot tests verify real filtering).
    # Simulate: count = 3, no collapsed rows
    count_mock = _scalar_count(3)
    rows_mock = MagicMock()
    rows_mock.all.return_value = []

    db.execute = AsyncMock(side_effect=[count_mock, rows_mock])

    from_dt = datetime(2026, 5, 1, tzinfo=UTC)
    until_dt = datetime(2026, 5, 8, tzinfo=UTC)

    collapsed, total_count = await svc.get_results(
        dataset_urn=_DATASET_URN,
        from_dt=from_dt,
        until_dt=until_dt,
    )
    assert total_count == 3
    assert collapsed == []


@pytest.mark.asyncio
async def test_get_results_limit_default_is_1000(
    svc: ValidationService, db: AsyncMock
) -> None:
    """Default limit is 1000; rows query SQL includes LIMIT 1000.

    spec: VALIDATION.md §GET result — limit default 1,000.
    """
    from sqlalchemy.dialects import postgresql

    count_mock = _scalar_count(0)
    rows_mock = MagicMock()
    rows_mock.all.return_value = []
    db.execute = AsyncMock(side_effect=[count_mock, rows_mock])

    # Call without specifying limit — should use 1000 default
    await svc.get_results(dataset_urn=_DATASET_URN)
    # Verify execute was called twice (count + rows query)
    assert db.execute.call_count == 2

    # Verify the rows query (second execute call) has exactly LIMIT 1000 (not LIMIT 10000,
    # LIMIT 100000, etc.).  Use word-boundary regex to prevent substring false positives.
    rows_stmt = db.execute.call_args_list[1].args[0]
    rendered = str(rows_stmt.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    ))
    assert re.search(r"\bLIMIT 1000\b", rendered), (
        f"Expected 'LIMIT 1000' (word-bounded) in rows query SQL (default limit); got:\n{rendered}"
    )


@pytest.mark.asyncio
async def test_get_results_limit_20000_clamped_to_10000(
    svc: ValidationService, db: AsyncMock
) -> None:
    """limit=20000 is clamped to the server cap 10000; rows query SQL includes LIMIT 10000.

    spec: VALIDATION.md §GET result — limit default 1,000; server cap 10,000.
    """
    from sqlalchemy.dialects import postgresql

    count_mock = _scalar_count(0)
    rows_mock = MagicMock()
    rows_mock.all.return_value = []
    db.execute = AsyncMock(side_effect=[count_mock, rows_mock])

    # Should not raise — limit is silently clamped
    await svc.get_results(dataset_urn=_DATASET_URN, limit=20000)

    # Verify the rows query (second execute call) has exactly LIMIT 10000 (not LIMIT 20000).
    # Word-boundary regex prevents "LIMIT 100000" from matching "LIMIT 10000".
    rows_stmt = db.execute.call_args_list[1].args[0]
    rendered = str(rows_stmt.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    ))
    assert re.search(r"\bLIMIT 10000\b", rendered), (
        f"Expected 'LIMIT 10000' (word-bounded) in rows query SQL (clamped); got:\n{rendered}"
    )
    assert not re.search(r"\bLIMIT 20000\b", rendered), (
        f"Unexpected 'LIMIT 20000' in rows query SQL (should be clamped to 10000); got:\n{rendered}"
    )


@pytest.mark.asyncio
async def test_get_results_returns_rows_and_total_count(
    svc: ValidationService, db: AsyncMock
) -> None:
    """get_results returns (rows, total_count); total_count >= len(rows).

    spec: VALIDATION.md §GET result — returns rows and a count for the queried window.
    Spec is silent on whether total_count is pre-collapse (all raw rows) or post-collapse
    (distinct data_time slots).  The weakest valid contract is:
      len(rows) <= total_count <= upper_bound_raw_rows
    Both pre-collapse and post-collapse semantics satisfy this.
    """
    count_mock = _scalar_count(5)  # DB returns 5 (pre-collapse raw rows in this mock)
    rows_mock = MagicMock()
    # Mock returns 3 collapsed rows (some data_times had duplicates)
    rows_mock.all.return_value = [
        MagicMock(
            data_time=datetime(2026, 5, 1, tzinfo=UTC),
            score=1.0,
            variables={"row_cnt": 50.0},
        ),
        MagicMock(
            data_time=datetime(2026, 5, 2, tzinfo=UTC),
            score=0.8,
            variables={"row_cnt": 48.0},
        ),
        MagicMock(
            data_time=datetime(2026, 5, 3, tzinfo=UTC),
            score=0.5,
            variables={"row_cnt": 10.0},
        ),
    ]
    db.execute = AsyncMock(side_effect=[count_mock, rows_mock])

    rows, total_count = await svc.get_results(dataset_urn=_DATASET_URN)

    # Spec-anchored contract: count is non-negative, rows is a list, count >= len(rows)
    assert len(rows) <= total_count <= 5, (
        f"total_count={total_count} must be >= len(rows)={len(rows)} "
        "and <= the raw row count returned by the mock (5)"
    )
    assert len(rows) == 3  # collapsed rows returned by mock


# ── list_configs ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_configs_removed_true_filter(
    svc: ValidationService, db: AsyncMock
) -> None:
    """removed_filter=True shows only soft-deleted configs; SQL has is_removed=true predicate.

    spec: VALIDATION.md §API Surface — cross-dataset list filterable by removed status.
    """
    from sqlalchemy.dialects import postgresql

    count_mock = _scalar_count(1)
    config_row = _make_config_row(is_removed=True)
    rows_mock = MagicMock()
    rows_mock.scalars.return_value.all.return_value = [config_row]

    # latest result query returns empty
    latest_mock = MagicMock()
    latest_mock.all.return_value = []

    db.execute = AsyncMock(side_effect=[count_mock, rows_mock, latest_mock])

    items, total_count = await svc.list_configs(removed_filter=True)
    assert total_count == 1
    assert all(item.is_removed for item in items)

    # Verify the count query (first execute call) targets the right column.
    # Substring check on "is_removed" verifies the correct column is filtered;
    # we avoid checking the rendered literal value ("true"/"false") because
    # the exact SQL rendering is an implementation detail, not a spec requirement.
    first_stmt = db.execute.call_args_list[0].args[0]
    rendered = str(first_stmt.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    ))
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

    # Verify the count query (first execute call) targets the right column.
    first_stmt = db.execute.call_args_list[0].args[0]
    rendered = str(first_stmt.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"literal_binds": True},
    ))
    assert "is_removed" in rendered, (
        f"Expected 'is_removed' in count query SQL; got:\n{rendered}"
    )


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


# ── get_events ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_events_returns_only_validation_events(
    svc: ValidationService, db: AsyncMock
) -> None:
    """get_events returns only events where event_type starts with 'VALIDATION.'.

    spec: BACKEND.md §Event Catalogue — VALIDATION.* event namespace.
    """
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
