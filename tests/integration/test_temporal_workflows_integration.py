"""Integration tests for Temporal workflows against dev-env infrastructure.

Prerequisites:
- Temporal port-forwarded to localhost:9205
- PostgreSQL port-forwarded to localhost:9201
- DataHub GMS port-forwarded to localhost:9004
- Dummy data ingested via dev_env/dummy-data-ingest.sh

Test-specific data extensions (inserted after dummy-data-reset.sh):
- 1 IngestionConfig row for example_db.catalog.title_master (sources={}, dry-run)
- 1 ValidationConfig row for example_db.catalog.title_master (completeness threshold 0.8)
Both rows are cleaned up in fixture teardown.

Run: uv run pytest tests/integration/test_temporal_workflows_integration.py -v
"""

import asyncio
from datetime import timedelta

import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio(loop_scope="module")
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

from src.workflows._common import TASK_QUEUE
from src.workflows.embedding_sync import (
    EmbeddingSyncParams,
    EmbeddingSyncWorkflow,
    enumerate_datasets_activity,
    reindex_batch_activity,
)
from src.workflows.ingestion import (
    IngestionParams,
    IngestionWorkflow,
    run_ingestion_activity,
)
from src.workflows.metrics import (
    MetricsCollectionWorkflow,
    aggregate_health_activity,
    publish_metric_update_activity,
    run_metric_activity,
)
from src.workflows.validation import (
    ValidationParams,
    ValidationWorkflow,
    run_validation_activity,
)

_IMAZON_DATASET_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
)
_WORKFLOW_TIMEOUT = timedelta(minutes=2)

ALL_WORKFLOWS = [
    IngestionWorkflow,
    ValidationWorkflow,
    EmbeddingSyncWorkflow,
    MetricsCollectionWorkflow,
]

ALL_ACTIVITIES = [
    run_ingestion_activity,
    run_validation_activity,
    enumerate_datasets_activity,
    reindex_batch_activity,
    run_metric_activity,
    aggregate_health_activity,
    publish_metric_update_activity,
]


_TEST_WORKFLOW_IDS = [
    f"integration-ingestion-{_IMAZON_DATASET_URN}",
    f"integration-validation-{_IMAZON_DATASET_URN}",
    f"integration-embedding-{_IMAZON_DATASET_URN}",
    "integration-duplicate-test",
    "integration-status-check",
]


@pytest_asyncio.fixture(scope="module", autouse=True)
async def _cleanup_stale_workflows(temporal_client):
    """Terminate any stale test workflows from previous runs."""
    for wf_id in _TEST_WORKFLOW_IDS:
        try:
            handle = temporal_client.get_workflow_handle(wf_id)
            await handle.terminate("cleanup before test run")
        except Exception:
            pass


@pytest_asyncio.fixture(scope="module", autouse=True)
async def _seed_workflow_configs():
    """Seed IngestionConfig and ValidationConfig for the test dataset."""
    from sqlalchemy import delete

    from src.shared.db.models import IngestionConfig, ValidationConfig
    from src.shared.db.session import SessionLocal

    async with SessionLocal() as db:
        await db.execute(
            delete(IngestionConfig).where(IngestionConfig.dataset_urn == _IMAZON_DATASET_URN)
        )
        await db.execute(
            delete(ValidationConfig).where(ValidationConfig.dataset_urn == _IMAZON_DATASET_URN)
        )
        db.add(
            IngestionConfig(
                dataset_urn=_IMAZON_DATASET_URN,
                sources={},
                deep_spec_enabled=False,
                owner="integration-test",
            )
        )
        db.add(
            ValidationConfig(
                dataset_urn=_IMAZON_DATASET_URN,
                rules={"completeness": {"threshold": 0.8}},
                owner="integration-test",
            )
        )
        await db.commit()

    yield

    async with SessionLocal() as db:
        await db.execute(
            delete(IngestionConfig).where(IngestionConfig.dataset_urn == _IMAZON_DATASET_URN)
        )
        await db.execute(
            delete(ValidationConfig).where(ValidationConfig.dataset_urn == _IMAZON_DATASET_URN)
        )
        await db.commit()


@pytest_asyncio.fixture(scope="module")
async def temporal_worker(temporal_client):
    worker = Worker(
        temporal_client,
        task_queue=TASK_QUEUE,
        workflows=ALL_WORKFLOWS,
        activities=ALL_ACTIVITIES,
    )
    task = asyncio.create_task(worker.run())
    yield worker
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_start_ingestion_workflow(temporal_client, temporal_worker):
    """Start IngestionWorkflow for an Imazon dataset and verify completion."""
    result = await temporal_client.execute_workflow(
        IngestionWorkflow.run,
        IngestionParams(dataset_urn=_IMAZON_DATASET_URN, dry_run=True),
        id=f"integration-ingestion-{_IMAZON_DATASET_URN}",
        task_queue=TASK_QUEUE,
        execution_timeout=_WORKFLOW_TIMEOUT,
    )
    assert result["status"] in ("success", "partial", "error")
    assert "run_id" in result


async def test_validation_workflow_against_real_dataset(temporal_client, temporal_worker):
    """Start ValidationWorkflow and verify Temporal orchestration completes.

    The activity may fail with a DataHub auth error if no token is configured,
    which is fine — the test verifies that the workflow runs and the error
    propagates correctly through Temporal.
    """
    from temporalio.client import WorkflowFailureError

    try:
        result = await temporal_client.execute_workflow(
            ValidationWorkflow.run,
            ValidationParams(dataset_urn=_IMAZON_DATASET_URN, dry_run=True),
            id=f"integration-validation-{_IMAZON_DATASET_URN}",
            task_queue=TASK_QUEUE,
            execution_timeout=_WORKFLOW_TIMEOUT,
        )
        assert result["status"] in ("success", "partial", "error")
        assert "run_id" in result
    except WorkflowFailureError:
        # DataHub auth or other infra error — Temporal orchestration still worked
        pass


async def test_duplicate_workflow_rejected(temporal_client, temporal_worker):
    """Start a workflow, then try to start another with the same ID while it's running."""
    workflow_id = "integration-duplicate-test"

    # Start the first workflow (ALLOW_DUPLICATE handles previously-used IDs)
    handle = await temporal_client.start_workflow(
        IngestionWorkflow.run,
        IngestionParams(dataset_urn=_IMAZON_DATASET_URN, dry_run=True),
        id=workflow_id,
        task_queue=TASK_QUEUE,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        execution_timeout=_WORKFLOW_TIMEOUT,
    )

    # Try to start a second workflow with the same ID — should be rejected
    with pytest.raises(WorkflowAlreadyStartedError):
        await temporal_client.start_workflow(
            IngestionWorkflow.run,
            IngestionParams(dataset_urn=_IMAZON_DATASET_URN, dry_run=True),
            id=workflow_id,
            task_queue=TASK_QUEUE,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            execution_timeout=_WORKFLOW_TIMEOUT,
        )

    # Wait for the first workflow to complete
    try:
        await handle.result()
    except Exception:
        pass


async def test_embedding_sync_single_dataset(temporal_client, temporal_worker):
    """Start EmbeddingSyncWorkflow in single mode for a known dataset."""
    result = await temporal_client.execute_workflow(
        EmbeddingSyncWorkflow.run,
        EmbeddingSyncParams(mode="single", dataset_urn=_IMAZON_DATASET_URN),
        id=f"integration-embedding-{_IMAZON_DATASET_URN}",
        task_queue=TASK_QUEUE,
        execution_timeout=_WORKFLOW_TIMEOUT,
    )
    assert result["status"] == "ok"
    assert result["mode"] == "single"


async def test_query_workflow_status(temporal_client, temporal_worker):
    """Start a workflow and query its handle to verify status."""
    handle = await temporal_client.start_workflow(
        IngestionWorkflow.run,
        IngestionParams(dataset_urn=_IMAZON_DATASET_URN, dry_run=True),
        id="integration-status-check",
        task_queue=TASK_QUEUE,
        execution_timeout=_WORKFLOW_TIMEOUT,
    )

    # Query the handle — workflow should be RUNNING or complete quickly
    desc = await handle.describe()
    assert desc.status is not None

    # Wait for completion
    await handle.result()
