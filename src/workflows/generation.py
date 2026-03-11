"""Generation workflow — delegates to GenerationService.generate()."""

from dataclasses import dataclass

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from temporalio.exceptions import ApplicationError

    from src.backend.generation.service import GenerationService
    from src.shared.db.session import SessionLocal
    from src.shared.exceptions import DataSpokeError
    from src.workflows._common import (
        DEFAULT_ACTIVITY_TIMEOUT,
        HEARTBEAT_TIMEOUT,
        default_retry_policy,
        make_datahub,
        make_llm,
        make_qdrant,
    )


@dataclass
class GenerationParams:
    dataset_urn: str


@activity.defn
async def run_generation_activity(dataset_urn: str) -> dict:
    """Run the full generation pipeline for a dataset."""
    datahub = make_datahub()
    llm = make_llm()
    qdrant = make_qdrant()
    try:
        async with SessionLocal() as db:
            service = GenerationService(datahub=datahub, db=db, llm=llm, qdrant=qdrant)
            result = await service.generate(dataset_urn)
            return {"run_id": result.run_id, "status": result.status, "detail": result.detail}
    except DataSpokeError as exc:
        raise ApplicationError(str(exc), type=exc.error_code, non_retryable=True) from exc


@workflow.defn
class GenerationWorkflow:
    """Orchestrate LLM-powered metadata generation via Temporal.

    Workflow ID convention: ``generation-{dataset_urn}``
    """

    @workflow.run
    async def run(self, params: GenerationParams) -> dict:
        return await workflow.execute_activity(
            run_generation_activity,
            args=[params.dataset_urn],
            start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
            retry_policy=default_retry_policy(),
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
        )
