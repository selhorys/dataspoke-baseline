"""Embedding sync workflow — reindex dataset vectors in Qdrant."""

from dataclasses import dataclass
from datetime import timedelta

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from src.backend.search.service import SearchService
    from src.shared.config import BULK_BATCH_SIZE
    from src.workflows._common import (
        DEFAULT_ACTIVITY_TIMEOUT,
        HEARTBEAT_TIMEOUT,
        default_retry_policy,
        make_cache,
        make_datahub,
        make_llm,
        make_qdrant,
    )


@dataclass
class EmbeddingSyncParams:
    mode: str = "full"  # "full" or "single"
    dataset_urn: str | None = None


@activity.defn
async def enumerate_datasets_activity() -> list[str]:
    """Enumerate all dataset URNs from DataHub."""
    datahub = make_datahub()
    urns = await datahub.enumerate_datasets()
    return urns


@activity.defn
async def reindex_batch_activity(dataset_urns: list[str]) -> dict:
    """Reindex a batch of datasets in Qdrant."""
    datahub = make_datahub()
    cache = make_cache()
    llm = make_llm()
    qdrant = make_qdrant()
    service = SearchService(datahub=datahub, cache=cache, llm=llm, qdrant=qdrant)

    indexed = 0
    errors = []
    for urn in dataset_urns:
        try:
            activity.heartbeat(f"reindexing {urn}")
            await service.reindex(urn)
            indexed += 1
        except Exception as exc:
            errors.append(f"{urn}: {exc}")

    return {"indexed": indexed, "errors": errors}


def _batched(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


@workflow.defn
class EmbeddingSyncWorkflow:
    """Orchestrate dataset embedding synchronisation via Temporal.

    Workflow ID convention: ``embedding-sync`` (full) or ``embedding-sync-{dataset_urn}`` (single).
    """

    @workflow.run
    async def run(self, params: EmbeddingSyncParams) -> dict:
        if params.mode == "full":
            urns = await workflow.execute_activity(
                enumerate_datasets_activity,
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=default_retry_policy(),
            )
            batches = _batched(urns, BULK_BATCH_SIZE)
            total_indexed = 0
            all_errors: list[str] = []
            for batch in batches:
                result = await workflow.execute_activity(
                    reindex_batch_activity,
                    args=[batch],
                    start_to_close_timeout=timedelta(minutes=10),
                    retry_policy=default_retry_policy(),
                    heartbeat_timeout=HEARTBEAT_TIMEOUT,
                )
                total_indexed += result["indexed"]
                all_errors.extend(result["errors"])
            return {"status": "ok", "mode": "full", "indexed": total_indexed, "errors": all_errors}
        else:
            result = await workflow.execute_activity(
                reindex_batch_activity,
                args=[[params.dataset_urn]],
                start_to_close_timeout=DEFAULT_ACTIVITY_TIMEOUT,
                retry_policy=default_retry_policy(),
                heartbeat_timeout=HEARTBEAT_TIMEOUT,
            )
            return {"status": "ok", "mode": "single", **result}
