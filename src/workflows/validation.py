"""Validation workflow — delegates to ValidationService.run()."""

from dataclasses import dataclass

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from temporalio.exceptions import ApplicationError

    from src.backend.validation.service import ValidationService
    from src.shared.db.session import SessionLocal
    from src.shared.exceptions import DataSpokeError
    from src.workflows._common import (
        DEFAULT_ACTIVITY_TIMEOUT,
        HEARTBEAT_TIMEOUT,
        default_retry_policy,
        make_cache,
        make_datahub,
    )


@dataclass
class ValidationParams:
    dataset_urn: str
    config_id: str | None = None
    dry_run: bool = False


@activity.defn
async def run_validation_activity(dataset_urn: str, config_id: str | None, dry_run: bool) -> dict:
    """Run the full validation pipeline for a dataset."""
    datahub = make_datahub()
    cache = make_cache()
    try:
        async with SessionLocal() as db:
            service = ValidationService(datahub=datahub, db=db, cache=cache)
            result = await service.run(dataset_urn, config_id=config_id, dry_run=dry_run)
            return {"run_id": result.run_id, "status": result.status, "detail": result.detail}
    except DataSpokeError as exc:
        raise ApplicationError(str(exc), type=exc.error_code, non_retryable=True) from exc


@workflow.defn
class ValidationWorkflow:
    """Orchestrate data-quality validation via Temporal.

    Workflow ID convention: ``validation-{dataset_urn}``
    """

    @workflow.run
    async def run(self, params: ValidationParams) -> dict:
        return await workflow.execute_activity(
            run_validation_activity,
            args=[params.dataset_urn, params.config_id, params.dry_run],
            start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
            retry_policy=default_retry_policy(),
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
        )
