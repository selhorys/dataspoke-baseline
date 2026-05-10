"""Unit tests for DataProcessInstance lifecycle + systemMetadata emission.

Active-custom non-dry-run emits four DPI aspects in spec-mandated order;
extractor exceptions still produce a terminal FAILURE RunEvent; dry-run skips
all DPI emission. Every DPI-targeting emit carries systemMetadata with
runId='dataspoke-{platform}-{run_id}'.

spec: BACKEND.md §Custom Ingestor Authoring Contract
spec: BACKEND.md §Active run pipeline
spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement
spec: USE_CASE_en.md §UC1 Case 1
"""

from unittest.mock import AsyncMock, patch

import pytest

from src.shared.events import INGESTION_COMPLETE, INGESTION_FAIL
from tests.unit.backend.conftest import mock_db_refresh, mock_scalar_query
from tests.unit.backend.ingestion.conftest import _DATASET_URN, _make_config_row


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
