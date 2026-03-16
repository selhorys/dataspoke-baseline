"""Ingestion workflow — three fine-grained activities matching pipeline phases."""

from dataclasses import dataclass

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from temporalio.exceptions import ApplicationError

    from src.backend.ingestion.service import IngestionService
    from src.shared.exceptions import DataSpokeError
    from src.workflows._common import (
        DEFAULT_ACTIVITY_TIMEOUT,
        default_retry_policy,
        make_datahub,
        make_db_session,
        make_llm,
    )


@dataclass
class IngestionParams:
    dataset_urn: str
    dry_run: bool = False


@activity.defn
async def extract_metadata_activity(dataset_urn: str, run_id: str) -> dict:
    """Phase 1 — extract metadata from all configured sources for a dataset."""
    datahub = make_datahub()
    llm = make_llm()
    try:
        async with make_db_session() as db:
            service = IngestionService(datahub=datahub, db=db, llm=llm)
            return await service.extract_metadata(dataset_urn, run_id)
    except DataSpokeError as exc:
        raise ApplicationError(str(exc), type=exc.error_code, non_retryable=True) from exc


@activity.defn
async def emit_to_datahub_activity(dataset_urn: str, extract_result: dict) -> dict:
    """Phase 2 — transform and emit extracted metadata to DataHub (with optional LLM enrichment)."""
    datahub = make_datahub()
    llm = make_llm()
    try:
        async with make_db_session() as db:
            service = IngestionService(datahub=datahub, db=db, llm=llm)
            return await service.emit_metadata_to_datahub(dataset_urn, extract_result)
    except DataSpokeError as exc:
        raise ApplicationError(str(exc), type=exc.error_code, non_retryable=True) from exc


@activity.defn
async def record_ingestion_event_activity(
    dataset_urn: str, run_id: str, status: str, detail: dict
) -> dict:
    """Phase 3 — record the ingestion event and return the final run result."""
    datahub = make_datahub()
    llm = make_llm()
    try:
        async with make_db_session() as db:
            service = IngestionService(datahub=datahub, db=db, llm=llm)
            return await service.record_ingestion_event(dataset_urn, run_id, status, detail)
    except DataSpokeError as exc:
        raise ApplicationError(str(exc), type=exc.error_code, non_retryable=True) from exc


@workflow.defn
class IngestionWorkflow:
    """Orchestrate dataset metadata ingestion via Temporal.

    Workflow ID convention: ``ingestion-{md5(dataset_urn)[:12]}``
    ID reuse policy: REJECT_DUPLICATE (prevents concurrent runs for the same dataset).

    Pipeline:
      1. extract_metadata_activity  — load config, run extractors
      2. emit_to_datahub_activity   — transform + emit (skipped when dry_run=True)
      3. record_ingestion_event_activity — persist event, return result
    """

    @workflow.run
    async def run(self, params: IngestionParams) -> dict:
        run_id = str(workflow.uuid4())

        extract_result: dict = await workflow.execute_activity(
            extract_metadata_activity,
            args=[params.dataset_urn, run_id],
            start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
            retry_policy=default_retry_policy(),
        )

        if params.dry_run:
            # Build a dry-run status/detail without emitting anything
            errors: list = extract_result.get("errors", [])
            metadata_count = len(extract_result.get("metadata", []))
            if errors and metadata_count == 0:
                status = "error"
            elif errors:
                status = "partial"
            else:
                status = "success"
            detail: dict = {
                "run_id": run_id,
                "sources_processed": extract_result.get("sources_processed", 0),
                "metadata_extracted": metadata_count,
                "dry_run": True,
            }
            if errors:
                detail["extractor_errors"] = errors
        else:
            emit_result: dict = await workflow.execute_activity(
                emit_to_datahub_activity,
                args=[params.dataset_urn, extract_result],
                start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
                retry_policy=default_retry_policy(),
            )
            status = emit_result["status"]
            detail = emit_result["detail"]

        return await workflow.execute_activity(
            record_ingestion_event_activity,
            args=[params.dataset_urn, run_id, status, detail],
            start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
            retry_policy=default_retry_policy(),
        )
