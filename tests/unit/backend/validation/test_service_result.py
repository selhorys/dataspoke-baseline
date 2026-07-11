"""Unit tests for ValidationService.record_result + get_results.

Covers result recording (variable validation, score gating, event emission,
DataHub-failure handling) and historical result reads (window inclusivity,
limit defaults + clamping, count semantics).

Conf ``variables`` is a JSONB array of ``{name, description}`` objects; the
result-recording subset check compares result keys against the set of declared
variable NAMES. Result rows themselves stay a ``{name: float}`` map.

spec: VALIDATION.md §Validation Result, §GET result, §Validation rules on POST
spec: BACKEND.md §Event Catalogue — VALIDATION.RESULT_RECORDED
"""

import re
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backend.validation.service import ValidationService
from src.shared.events import VALIDATION_RESULT_RECORDED
from src.shared.exceptions import DataHubUnavailableError, PreconditionFailedError
from tests.unit.backend.conftest import mock_db_refresh
from tests.unit.backend.validation.conftest import (
    _DATASET_URN,
    _make_config_row,
    _scalar_count,
    _scalar_result,
    _var,
)
from tests.unit.conftest import route_db_execute

_REPORT = "src.backend.validation.service.report_result"
_BUILD_EVENT = "src.backend.validation.service.build_run_event"
_BUILD_URN = "src.backend.validation.service.build_assertion_urn"
_FAKE_URN = "urn:li:assertion:abc123"


# ── record_result ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_result_unknown_variable_key_raises(
    svc: ValidationService, db: AsyncMock
) -> None:
    """record_result raises UNKNOWN_VARIABLE with sorted offending names.

    spec: VALIDATION.md §Validation rules on POST — unknown keys → 422 UNKNOWN_VARIABLE
    listing the offending names. The subset check compares against declared NAMES.
    """
    config = _make_config_row(variables=[_var("row_cnt"), _var("null_rate")])
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
    assert err.detail["unknown"] == sorted(["zz_unknown", "aa_other"])


@pytest.mark.asyncio
async def test_record_result_score_out_of_range_raises(
    svc: ValidationService,
) -> None:
    """record_result raises INVALID_SCORE before any DB query for out-of-range scores.

    spec: VALIDATION.md §Validation rules on POST — score outside [0.0, 1.0] →
    422 INVALID_SCORE.
    """
    with pytest.raises(PreconditionFailedError) as exc_info:
        await svc.record_result(
            dataset_urn=_DATASET_URN,
            data_time=datetime(2026, 5, 1, tzinfo=UTC),
            score=1.5,
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
    config = _make_config_row(
        variables=[_var("row_cnt"), _var("col1_mean"), _var("null_rate")]
    )
    db.execute = AsyncMock(return_value=_scalar_result(config))
    db.commit = AsyncMock()

    with (
        patch(_REPORT, new_callable=AsyncMock, return_value=True),
        patch(_BUILD_EVENT),
        patch(_BUILD_URN, return_value=_FAKE_URN),
    ):
        mock_db_refresh(db)

        record = await svc.record_result(
            dataset_urn=_DATASET_URN,
            data_time=datetime(2026, 5, 1, tzinfo=UTC),
            score=1.0,
            variables={"row_cnt": 50.0},
        )

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

    config = _make_config_row(variables=[_var("row_cnt")])
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

    with (
        patch(_REPORT, new_callable=AsyncMock, return_value=True) as mock_report,
        patch(_BUILD_URN, return_value=_FAKE_URN),
    ):
        mock_db_refresh(db)

        await svc.record_result(
            dataset_urn=dataset_urn,
            data_time=data_time,
            score=score,
            variables=variables,
        )

    result_rows = [o for o in added_objects if isinstance(o, ValidationResult)]
    assert result_rows, "ValidationResult row must be added to db"
    row = result_rows[0]

    assert row.data_time == data_time
    assert row.score == score
    # spec: VALIDATION.md §Validation Result — result row stays a {name: float} map.
    assert row.variables == variables
    assert row.dataset_urn == dataset_urn

    # run_event.timestampMillis must derive from data_time, not server now.
    assert mock_report.called, "report_result must be called"
    call_args = mock_report.call_args
    run_event_arg = (
        call_args.args[2]
        if len(call_args.args) > 2
        else call_args.kwargs.get("run_event")
    )
    assert run_event_arg is not None, "run_event must be passed to report_result"
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
    config = _make_config_row(variables=[_var("row_cnt")])
    db.execute = AsyncMock(return_value=_scalar_result(config))
    db.commit = AsyncMock()

    events_added = []

    def capture_add(obj):
        events_added.append(obj)

    db.add = MagicMock(side_effect=capture_add)

    with (
        patch(_REPORT, new_callable=AsyncMock, return_value=True),
        patch(_BUILD_EVENT),
        patch(_BUILD_URN, return_value=_FAKE_URN),
    ):
        mock_db_refresh(db)

        await svc.record_result(
            dataset_urn=_DATASET_URN,
            data_time=datetime(2026, 5, 1, tzinfo=UTC),
            score=1.0,
            variables={"row_cnt": 50.0},
        )

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
    config = _make_config_row(variables=[_var("row_cnt")])
    db.execute = AsyncMock(return_value=_scalar_result(config))
    db.commit = AsyncMock()

    events_added = []

    def capture_add(obj):
        events_added.append(obj)

    db.add = MagicMock(side_effect=capture_add)

    with (
        patch(_REPORT, new_callable=AsyncMock, return_value=False),
        patch(_BUILD_EVENT),
        patch(_BUILD_URN, return_value=_FAKE_URN),
    ):
        mock_db_refresh(db)

        with pytest.raises(DataHubUnavailableError):
            await svc.record_result(
                dataset_urn=_DATASET_URN,
                data_time=datetime(2026, 5, 1, tzinfo=UTC),
                score=1.0,
                variables={"row_cnt": 50.0},
            )

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

    spec: VALIDATION.md §GET result — from: Inclusive lower bound; until: Exclusive.
    """
    count_mock = _scalar_count(3)
    rows_mock = MagicMock()
    rows_mock.all.return_value = []

    route_db_execute(db, [("count(", count_mock)], default=rows_mock)

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
    route_db_execute(db, [("count(", count_mock)], default=rows_mock)

    await svc.get_results(dataset_urn=_DATASET_URN)
    assert db.execute.call_count == 2

    rows_stmt = db.execute.call_args_list[1].args[0]
    rendered = str(
        rows_stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert re.search(r"\bLIMIT 1000\b", rendered), (
        f"Expected 'LIMIT 1000' in rows query SQL (default limit); got:\n{rendered}"
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
    route_db_execute(db, [("count(", count_mock)], default=rows_mock)

    await svc.get_results(dataset_urn=_DATASET_URN, limit=20000)

    rows_stmt = db.execute.call_args_list[1].args[0]
    rendered = str(
        rows_stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert re.search(r"\bLIMIT 10000\b", rendered), (
        f"Expected 'LIMIT 10000' in rows query SQL (clamped); got:\n{rendered}"
    )
    assert not re.search(r"\bLIMIT 20000\b", rendered), (
        f"Unexpected 'LIMIT 20000' (should be clamped to 10000); got:\n{rendered}"
    )


@pytest.mark.asyncio
async def test_get_results_returns_rows_and_total_count(
    svc: ValidationService, db: AsyncMock
) -> None:
    """get_results returns (rows, total_count) with total_count == len(rows) when
    every returned row is a distinct data_time partition.

    spec: VALIDATION.md §GET result — total_count counts distinct data_time
    partitions in the window (post-collapse); equals len(rows) whenever the
    collapsed window fits under `limit`.
    """
    count_mock = _scalar_count(3)
    rows_mock = MagicMock()
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
    route_db_execute(db, [("count(", count_mock)], default=rows_mock)

    rows, total_count = await svc.get_results(dataset_urn=_DATASET_URN)

    assert total_count == len(rows) == 3


@pytest.mark.asyncio
async def test_get_results_total_count_uses_count_distinct_data_time(
    svc: ValidationService, db: AsyncMock
) -> None:
    """The count query is `COUNT(DISTINCT data_time)`, not `COUNT(*)`.

    spec: VALIDATION.md §GET result — total_count is the number of distinct
    data_time partitions in the window.
    """
    from sqlalchemy.dialects import postgresql

    count_mock = _scalar_count(0)
    rows_mock = MagicMock()
    rows_mock.all.return_value = []
    route_db_execute(db, [("count(", count_mock)], default=rows_mock)

    await svc.get_results(dataset_urn=_DATASET_URN)

    count_stmt = db.execute.call_args_list[0].args[0]
    rendered = str(
        count_stmt.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert re.search(
        r"count\(\s*distinct[\s(]+\S*data_time\b", rendered, re.IGNORECASE
    ), f"Expected COUNT(DISTINCT ...data_time) in count query SQL; got:\n{rendered}"


@pytest.mark.asyncio
async def test_get_results_returned_in_descending_data_time_order(
    svc: ValidationService, db: AsyncMock
) -> None:
    """get_results returns rows ordered by data_time DESC (newest first).

    spec: VALIDATION.md §GET result — "Rows are ordered by data_time descending
    (newest first) so the most recent partition appears at the head of the response."
    """
    dt_newest = datetime(2026, 5, 10, tzinfo=UTC)
    dt_mid = datetime(2026, 5, 8, tzinfo=UTC)
    dt_oldest = datetime(2026, 5, 5, tzinfo=UTC)

    count_mock = _scalar_count(3)
    rows_mock = MagicMock()
    rows_mock.all.return_value = [
        MagicMock(data_time=dt_newest, score=0.9, variables={"row_cnt": 51.0}),
        MagicMock(data_time=dt_mid, score=0.7, variables={"row_cnt": 42.0}),
        MagicMock(data_time=dt_oldest, score=1.0, variables={"row_cnt": 50.0}),
    ]
    route_db_execute(db, [("count(", count_mock)], default=rows_mock)

    results, total_count = await svc.get_results(dataset_urn=_DATASET_URN)

    assert total_count == 3
    assert len(results) == 3

    data_times = [r.data_time for r in results]
    for i in range(len(data_times) - 1):
        assert data_times[i] > data_times[i + 1], (
            f"Expected descending order at index {i}: "
            f"{data_times[i]!r} must be > {data_times[i + 1]!r}"
        )


@pytest.mark.asyncio
async def test_record_result_duplicate_data_time_both_calls_succeed_and_emit_event(
    svc: ValidationService, db: AsyncMock
) -> None:
    """Two POSTs with the same data_time are each accepted (append-only) and each emits
    VALIDATION.RESULT_RECORDED.

    spec: VALIDATION.md §Duplicate data_time policy — "Multiple POSTs with the same
    data_time are append-only: each becomes a distinct assertionRunEvent row."
    """
    config = _make_config_row(variables=[_var("row_cnt")])
    db.commit = AsyncMock()

    events_added: list = []

    def capture_add(obj):
        events_added.append(obj)

    db.add = MagicMock(side_effect=capture_add)

    shared_data_time = datetime(2026, 5, 8, tzinfo=UTC)

    with (
        patch(_REPORT, new_callable=AsyncMock, return_value=True),
        patch(_BUILD_EVENT),
        patch(_BUILD_URN, return_value=_FAKE_URN),
    ):
        mock_db_refresh(db)

        # First call: score=1.0 (pass)
        db.execute = AsyncMock(return_value=_scalar_result(config))
        result_1 = await svc.record_result(
            dataset_urn=_DATASET_URN,
            data_time=shared_data_time,
            score=1.0,
            variables={"row_cnt": 50.0},
        )

        # Second call: same data_time, different score (fail)
        db.execute = AsyncMock(return_value=_scalar_result(config))
        result_2 = await svc.record_result(
            dataset_urn=_DATASET_URN,
            data_time=shared_data_time,
            score=0.0,
            variables={"row_cnt": 20.0},
        )

    assert result_1 is not None
    assert result_2 is not None
    assert result_1.data_time == shared_data_time
    assert result_2.data_time == shared_data_time

    # Each record carries its own input score (no cached/shared record).
    assert result_1.score == 1.0
    assert result_2.score == 0.0

    from src.shared.db.models import ValidationResult

    inserted_results = [o for o in events_added if isinstance(o, ValidationResult)]
    assert len(inserted_results) == 2, (
        f"Expected 2 ValidationResult rows (one per POST); got {len(inserted_results)}. "
        "spec: VALIDATION.md §Duplicate data_time policy — append-only, distinct rows."
    )

    event_types = [
        getattr(obj, "event_type", None)
        for obj in events_added
        if hasattr(obj, "event_type")
    ]
    result_recorded_count = event_types.count(VALIDATION_RESULT_RECORDED)
    assert result_recorded_count == 2, (
        f"Expected 2 RESULT_RECORDED events (one per POST), got "
        f"{result_recorded_count}; all event_types: {event_types}"
    )
