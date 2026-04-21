"""Integration tests for infrastructure clients against dev-env services.

Test-specific data extensions (created and cleaned up by fixtures/tests):
- 1 DataHub dataset entity (imazon.test.infra_clients_<uuid>, env=DEV)
  with StatusClass, DatasetPropertiesClass, and SchemaMetadataClass aspects.
  Unique URN per run; soft-deleted on teardown.
- Transient Redis keys under ``integration_test:infra_clients:*`` prefix.
- Pgvector connectivity probe against PostgreSQL (no row writes).
- LLM tests require DATASPOKE_LLM_API_KEY env var (skipped otherwise).

Prerequisites:
- DataHub GMS port-forwarded to localhost:9004
- Redis port-forwarded to localhost:9202
- PostgreSQL (dataspoke, with pgvector) port-forwarded to localhost:9201
"""

import asyncio
import os
import uuid

import pytest
import pytest_asyncio

from .conftest import emit_test_dataset, make_test_urn, soft_delete_test_dataset

DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset(["catalog"])

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


# --- pgvector (vector_manager fixture provided by conftest.py) ---


async def test_pgvector_connectivity(vector_manager) -> None:
    assert await vector_manager.check_connectivity() is True


# --- LLM ---


async def test_llm_complete() -> None:
    from src.shared.llm.client import LLMClient

    api_key = os.environ.get("DATASPOKE_LLM_API_KEY", "")
    if not api_key:
        pytest.skip("DATASPOKE_LLM_API_KEY not set")

    provider = os.environ.get("DATASPOKE_LLM_PROVIDER", "openai")
    model = os.environ.get("DATASPOKE_LLM_MODEL", "gpt-4o-mini")
    client = LLMClient(provider=provider, api_key=api_key, model=model)
    result = await client.complete("Say hello in one word.")
    assert len(result) > 0
