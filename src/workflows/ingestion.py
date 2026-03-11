"""Ingestion workflow — delegates to IngestionService.run()."""

from dataclasses import dataclass

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from temporalio.exceptions import ApplicationError

    from src.backend.ingestion.service import IngestionService
    from src.shared.db.session import SessionLocal
    from src.shared.exceptions import DataSpokeError
    from src.workflows._common import (
        DEFAULT_ACTIVITY_TIMEOUT,
        HEARTBEAT_TIMEOUT,
        default_retry_policy,
        make_datahub,
        make_llm,
    )


@dataclass
class IngestionParams:
    dataset_urn: str
    dry_run: bool = False


@activity.defn
async def run_ingestion_activity(dataset_urn: str, dry_run: bool) -> dict:
    """Run the full ingestion pipeline for a dataset."""
    datahub = make_datahub()
    llm = make_llm()
    try:
        async with SessionLocal() as db:
            service = IngestionService(datahub=datahub, db=db, llm=llm)
            result = await service.run(dataset_urn, dry_run=dry_run)
            return {"run_id": result.run_id, "status": result.status, "detail": result.detail}
    except DataSpokeError as exc:
        raise ApplicationError(str(exc), type=exc.error_code, non_retryable=True) from exc


@workflow.defn
class IngestionWorkflow:
    """Orchestrate dataset metadata ingestion via Temporal.

    Workflow ID convention: ``ingestion-{dataset_urn}``
    ID reuse policy: REJECT_DUPLICATE (prevents concurrent runs for the same dataset).
    """

    # TODO: split into fine-grained activities when service exposes step-level methods

    @workflow.run
    async def run(self, params: IngestionParams) -> dict:
        return await workflow.execute_activity(
            run_ingestion_activity,
            args=[params.dataset_urn, params.dry_run],
            start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
            retry_policy=default_retry_policy(),
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
        )
