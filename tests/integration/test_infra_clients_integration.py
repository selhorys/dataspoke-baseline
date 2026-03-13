"""Integration tests for infrastructure clients against dev-env services.

Test-specific data extensions (created and cleaned up by fixtures/tests):
- 1 DataHub dataset entity (imazon.test.infra_clients_<uuid>, env=DEV)
  with StatusClass, DatasetPropertiesClass, and SchemaMetadataClass aspects.
  Unique URN per run; soft-deleted on teardown.
- Transient Redis keys under ``integration_test:infra_clients:*`` prefix.
- Transient Qdrant collection ``integration_test_infra_clients`` with 2 points.
- LLM tests require DATASPOKE_LLM_API_KEY env var (skipped otherwise).

Prerequisites:
- DataHub GMS port-forwarded to localhost:9004
- Redis port-forwarded to localhost:9202
- Qdrant port-forwarded to localhost:9203 (HTTP) / 9204 (gRPC)
"""

import asyncio
import os
import uuid

import pytest
import pytest_asyncio

from .conftest import emit_test_dataset, make_test_urn, soft_delete_test_dataset

_llm_provider = os.environ.get("DATASPOKE_LLM_PROVIDER", "openai")
_llm_api_key = os.environ.get("DATASPOKE_LLM_API_KEY", "")
_llm_model = os.environ.get("DATASPOKE_LLM_MODEL", "gpt-4o-mini")


# --- DataHub ---


@pytest_asyncio.fixture
async def datahub_test_dataset(datahub_client):
    """Emit a self-contained Imazon test dataset, clean up after.

    Uses a unique URN per run to avoid stale soft-delete state in the ES index.
    """
    suffix = f"infra_clients_{uuid.uuid4().hex[:8]}"
    urn = make_test_urn("infra", suffix)
    name = urn.split(",")[1]  # matches assertion in test_datahub_get_aspect_existing
    await emit_test_dataset(
        datahub_client,
        urn=urn,
        name=name,
        description="Self-contained integration test fixture",
        wait_seconds=1.0,
    )
    yield urn
    await soft_delete_test_dataset(datahub_client, urn)


async def test_datahub_connectivity(datahub_client) -> None:
    assert await datahub_client.check_connectivity() is True


async def test_datahub_enumerate_datasets(datahub_client) -> None:
    datasets = await datahub_client.enumerate_datasets()
    assert isinstance(datasets, list)
    assert len(datasets) > 0
    assert all(d.startswith("urn:li:dataset:") for d in datasets)


async def test_datahub_get_aspect_existing(datahub_client, datahub_test_dataset) -> None:
    from datahub.metadata.schema_classes import DatasetPropertiesClass

    result = await datahub_client.get_aspect(datahub_test_dataset, DatasetPropertiesClass)
    assert result is not None
    assert result.name == datahub_test_dataset.split(",")[1]


async def test_datahub_get_aspect_nonexistent(datahub_client) -> None:
    from datahub.metadata.schema_classes import DatasetPropertiesClass

    result = await datahub_client.get_aspect(
        "urn:li:dataset:(urn:li:dataPlatform:nonexistent,nonexistent,PROD)",
        DatasetPropertiesClass,
    )
    assert result is None


# --- Redis ---


async def test_redis_set_get_delete(redis_client) -> None:
    key = "integration_test:infra_clients:roundtrip"
    await redis_client.set(key, "hello", ttl_seconds=30)
    assert await redis_client.get(key) == "hello"
    await redis_client.delete(key)
    assert await redis_client.get(key) is None


async def test_redis_ttl_expiry(redis_client) -> None:
    key = "integration_test:infra_clients:ttl"
    await redis_client.set(key, "ephemeral", ttl_seconds=1)
    await asyncio.sleep(2)
    assert await redis_client.get(key) is None


async def test_redis_pubsub(redis_client) -> None:
    channel = "integration_test:infra_clients:pubsub"
    received = []

    async def subscriber():
        async for msg in redis_client.subscribe(channel):
            received.append(msg)
            if len(received) >= 1:
                break

    sub_task = asyncio.create_task(subscriber())
    await asyncio.sleep(0.5)
    await redis_client.publish(channel, "test_message")
    await asyncio.wait_for(sub_task, timeout=5)
    assert "test_message" in received


# --- Qdrant (qdrant_manager fixture provided by conftest.py) ---


async def test_qdrant_connectivity(qdrant_manager) -> None:
    assert await qdrant_manager.check_connectivity() is True


async def test_qdrant_collection_lifecycle(qdrant_manager) -> None:
    from qdrant_client.models import PointStruct

    col_name = "integration_test_infra_clients"

    await qdrant_manager.ensure_collection(col_name, vector_size=4)
    await qdrant_manager.upsert(
        col_name,
        [
            PointStruct(id=1, vector=[0.1, 0.2, 0.3, 0.4], payload={"label": "a"}),
            PointStruct(id=2, vector=[0.5, 0.6, 0.7, 0.8], payload={"label": "b"}),
        ],
    )

    results = await qdrant_manager.search(col_name, [0.1, 0.2, 0.3, 0.4], limit=2)
    assert len(results) > 0

    await qdrant_manager.delete(col_name, [1, 2])


# --- LLM ---


@pytest.mark.skipif(not _llm_api_key, reason="DATASPOKE_LLM_API_KEY not set")
async def test_llm_complete() -> None:
    from src.shared.llm.client import LLMClient

    client = LLMClient(provider=_llm_provider, api_key=_llm_api_key, model=_llm_model)
    result = await client.complete("Say hello in one word.")
    assert len(result) > 0
