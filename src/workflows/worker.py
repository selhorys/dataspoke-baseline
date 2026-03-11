"""Temporal worker — registers all workflows and activities, then polls the task queue."""

import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from src.api.config import settings
from src.workflows._common import TASK_QUEUE
from src.workflows.embedding_sync import (
    EmbeddingSyncWorkflow,
    enumerate_datasets_activity,
    reindex_batch_activity,
)
from src.workflows.generation import GenerationWorkflow, run_generation_activity
from src.workflows.ingestion import IngestionWorkflow, run_ingestion_activity
from src.workflows.metrics import (
    MetricsCollectionWorkflow,
    aggregate_health_activity,
    publish_metric_update_activity,
    run_metric_activity,
)
from src.workflows.ontology import (
    OntologyRebuildWorkflow,
    build_hierarchy_activity,
    classify_datasets_activity,
    detect_drift_activity,
    infer_relationships_activity,
)
from src.workflows.sla_monitor import (
    SLAMonitorWorkflow,
    check_sla_activity,
    send_sla_alerts_activity,
)
from src.workflows.validation import ValidationWorkflow, run_validation_activity


async def main() -> None:
    client = await Client.connect(
        f"{settings.temporal_host}:{settings.temporal_port}",
        namespace=settings.temporal_namespace,
    )

    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[
            IngestionWorkflow,
            ValidationWorkflow,
            SLAMonitorWorkflow,
            GenerationWorkflow,
            EmbeddingSyncWorkflow,
            MetricsCollectionWorkflow,
            OntologyRebuildWorkflow,
        ],
        activities=[
            run_ingestion_activity,
            run_validation_activity,
            check_sla_activity,
            send_sla_alerts_activity,
            run_generation_activity,
            enumerate_datasets_activity,
            reindex_batch_activity,
            run_metric_activity,
            aggregate_health_activity,
            publish_metric_update_activity,
            classify_datasets_activity,
            build_hierarchy_activity,
            infer_relationships_activity,
            detect_drift_activity,
        ],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
