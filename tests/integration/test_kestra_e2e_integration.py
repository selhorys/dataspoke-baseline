"""End-to-end integration tests for Kestra workflows with activity callbacks.

Unlike test_kestra_workflows_integration.py which tests Kestra orchestration
and accepts FAILED as a valid terminal state, these tests verify that the
full workflow (Kestra → activity callback → business logic → response)
completes successfully.

Prerequisites:
- Kestra port-forwarded to localhost:9205
- PostgreSQL port-forwarded to localhost:9201
- DataHub GMS port-forwarded to localhost:9004
- Dummy data ingested (catalog schema)

Run: uv run pytest tests/integration/test_kestra_e2e_integration.py -v --timeout=600
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from unittest.mock import AsyncMock

from src.shared.db.models import (
    ConceptCategory,
    GenerationConfig,
    GenerationResult,
    IngestionConfig,
    MetricDefinition,
    ValidationConfig,
)
from src.workflows.kestra.client import KestraClient
from src.workflows.kestra.models import ExecutionStatus

pytestmark = pytest.mark.asyncio(loop_scope="module")

# ── Per-module dummy-data declarations ─────────────────────────────────────
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset(["catalog"])

_IMAZON_DATASET_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
)

_TEST_LABEL_PREFIX = "test-e2e-"
_E2E_METRIC_ID = "test-e2e-dataset-count"


def _test_label(suffix: str) -> str:
    return f"{_TEST_LABEL_PREFIX}{suffix}-{uuid.uuid4().hex[:8]}"


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest_asyncio.fixture(scope="module", autouse=True)
async def _seed_e2e_configs(async_engine, activity_server):
    """Seed DB with configs required by activity endpoints."""
    from sqlalchemy.ext.asyncio import AsyncSession

    async with AsyncSession(async_engine) as db:
        # Clean up any existing test data
        for Model in (IngestionConfig, ValidationConfig, GenerationConfig):
            await db.execute(
                delete(Model).where(Model.dataset_urn == _IMAZON_DATASET_URN)
            )
        await db.execute(
            delete(MetricDefinition).where(MetricDefinition.id == _E2E_METRIC_ID)
        )
        await db.commit()

        # Seed configs
        db.add(IngestionConfig(
            dataset_urn=_IMAZON_DATASET_URN,
            source_type="postgres",
            location={"host": "localhost", "port": 9201, "database": "example_db", "username": "postgres", "secret_ref": "dev/example-postgres-password"},
            periodic=False,
        ))
        db.add(ValidationConfig(
            dataset_urn=_IMAZON_DATASET_URN,
            rules={"completeness": {"threshold": 0.8}},
            owner="e2e-test",
        ))
        db.add(GenerationConfig(
            dataset_urn=_IMAZON_DATASET_URN,
            target_fields={"description": True},
            owner="e2e-test",
        ))
        db.add(MetricDefinition(
            id=_E2E_METRIC_ID,
            title="E2E Dataset Count",
            description="Count all datasets for e2e test",
            theme="coverage",
            measurement_query={"type": "dataset_count"},
            alarm_enabled=False,
            active=True,
        ))
        await db.commit()

    yield

    # Teardown: clean up seeded data and any rows created during tests
    async with AsyncSession(async_engine) as db:
        for Model in (IngestionConfig, ValidationConfig, GenerationConfig):
            await db.execute(
                delete(Model).where(Model.dataset_urn == _IMAZON_DATASET_URN)
            )
        await db.execute(
            delete(MetricDefinition).where(MetricDefinition.id == _E2E_METRIC_ID)
        )
        await db.execute(
            delete(GenerationResult).where(
                GenerationResult.dataset_urn == _IMAZON_DATASET_URN
            )
        )
        await db.execute(
            delete(ConceptCategory).where(
                ConceptCategory.name == "master_data"
            )
        )
        await db.commit()


@pytest_asyncio.fixture(scope="module", autouse=True)
async def _configure_mocks(activity_server):
    """Configure mock return values for the session-scoped ActivityServer."""
    activity_server.mock_llm.complete_json.return_value = {
        "field_descriptions": {"id": "Primary key", "name": "Title name"},
        "table_summary": "Test dataset for Imazon catalog",
        "suggested_tags": ["catalog", "test"],
        "category": "master_data",
        "confidence": 0.9,
    }
    activity_server.mock_llm.embed = AsyncMock(return_value=[0.0] * 1536)
    activity_server.mock_qdrant.search = AsyncMock(return_value=[])
    activity_server.mock_qdrant.ensure_collection = AsyncMock()
    activity_server.mock_qdrant.upsert = AsyncMock()

    yield

    # Reset mocks to defaults for other test modules
    activity_server.mock_llm.reset_mock()
    activity_server.mock_llm.complete = AsyncMock(return_value="test response")
    activity_server.mock_llm.complete_json = AsyncMock(return_value={})
    activity_server.mock_qdrant.reset_mock()
    activity_server.mock_qdrant.search = AsyncMock(return_value=[])
    activity_server.mock_cache.reset_mock()


# ── E2E Tests ──────────────────────────────────────────────────────────────


async def test_validation_dry_run_e2e(
    kestra_client: KestraClient, activity_server
):
    """Validation flow (dry_run) should complete successfully via activity callbacks."""
    execution = await kestra_client.trigger_and_wait(
        "validation",
        inputs={
            "callback_base_url": activity_server.callback_url,
            "dataset_urn": _IMAZON_DATASET_URN,
            "dry_run": "true",
        },
        labels={"workflow_id": _test_label("validation")},
        timeout_seconds=120,
    )
    assert execution.status == ExecutionStatus.SUCCESS


async def test_generation_e2e(
    kestra_client: KestraClient, activity_server
):
    """Generation flow should complete successfully via activity callbacks."""
    execution = await kestra_client.trigger_and_wait(
        "generation",
        inputs={
            "callback_base_url": activity_server.callback_url,
            "dataset_urn": _IMAZON_DATASET_URN,
        },
        labels={"workflow_id": _test_label("generation")},
        timeout_seconds=120,
    )
    assert execution.status == ExecutionStatus.SUCCESS


async def test_metrics_dry_run_e2e(
    kestra_client: KestraClient, activity_server
):
    """Metrics flow (dry_run) should complete successfully via activity callbacks."""
    execution = await kestra_client.trigger_and_wait(
        "metrics",
        inputs={
            "callback_base_url": activity_server.callback_url,
            "metric_id": _E2E_METRIC_ID,
            "dry_run": "true",
            "aggregate": "false",
        },
        labels={"workflow_id": _test_label("metrics")},
        timeout_seconds=120,
    )
    assert execution.status == ExecutionStatus.SUCCESS


async def test_embedding_sync_single_e2e(
    kestra_client: KestraClient, activity_server
):
    """Embedding-sync flow (single mode) should complete successfully."""
    execution = await kestra_client.trigger_and_wait(
        "embedding-sync",
        inputs={
            "callback_base_url": activity_server.callback_url,
            "mode": "single",
            "dataset_urn": _IMAZON_DATASET_URN,
        },
        labels={"workflow_id": _test_label("embedding-sync")},
        timeout_seconds=120,
    )
    assert execution.status == ExecutionStatus.SUCCESS


async def test_ontology_rebuild_e2e(
    kestra_client: KestraClient, activity_server
):
    """Ontology-rebuild flow should complete all 4 stages successfully."""
    execution = await kestra_client.trigger_and_wait(
        "ontology-rebuild",
        inputs={
            "callback_base_url": activity_server.callback_url,
            "force": "false",
        },
        labels={"workflow_id": _test_label("ontology-rebuild")},
        timeout_seconds=180,
    )
    assert execution.status == ExecutionStatus.SUCCESS
