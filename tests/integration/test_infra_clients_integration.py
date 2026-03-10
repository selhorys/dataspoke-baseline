"""Integration tests for infrastructure clients against dev-env services.

Prerequisites:
- DataHub GMS port-forwarded to localhost:9004
- Redis port-forwarded to localhost:9202
- Qdrant port-forwarded to localhost:9203
- LLM tests require DATASPOKE_LLM_API_KEY env var
"""

import asyncio
import base64
import json
import os

import pytest
import pytest_asyncio
import requests

_datahub_gms_url = os.environ.get("DATASPOKE_DATAHUB_GMS_URL", "http://localhost:9004")
_datahub_frontend_url = os.environ.get("DATASPOKE_DATAHUB_FRONTEND_URL", "http://localhost:9002")
_datahub_token = os.environ.get("DATASPOKE_DATAHUB_TOKEN", "")


def _get_datahub_session_token() -> str:
    """Get a DataHub session token via frontend login for dev-env testing."""
    resp = requests.post(
        f"{_datahub_frontend_url}/logIn",
        json={"username": "datahub", "password": "datahub"},
        timeout=5,
    )
    resp.raise_for_status()
    cookie = resp.headers.get("Set-Cookie", "")
    if "PLAY_SESSION=" not in cookie:
        return ""
    play_session = cookie.split("PLAY_SESSION=")[1].split(";")[0]
    payload = play_session.split(".")[1]
    payload += "=" * (4 - len(payload) % 4)
    data = json.loads(base64.b64decode(payload))
    return data.get("data", {}).get("token", "")


_redis_host = os.environ.get("DATASPOKE_REDIS_HOST", "localhost")
_redis_port = int(os.environ.get("DATASPOKE_REDIS_PORT", "9202"))
_redis_password = os.environ.get("DATASPOKE_REDIS_PASSWORD", "")

_qdrant_host = os.environ.get("DATASPOKE_QDRANT_HOST", "localhost")
_qdrant_http_port = int(os.environ.get("DATASPOKE_QDRANT_HTTP_PORT", "9203"))
_qdrant_grpc_port = int(os.environ.get("DATASPOKE_QDRANT_GRPC_PORT", "9204"))
_qdrant_api_key = os.environ.get("DATASPOKE_QDRANT_API_KEY", "")

_llm_provider = os.environ.get("DATASPOKE_LLM_PROVIDER", "openai")
_llm_api_key = os.environ.get("DATASPOKE_LLM_API_KEY", "")
_llm_model = os.environ.get("DATASPOKE_LLM_MODEL", "gpt-4o-mini")

_TEST_DATASET_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,integration_test.test_schema.test_table,DEV)"
)


# --- DataHub ---


@pytest_asyncio.fixture
async def datahub_client():
    from src.shared.datahub.client import DataHubClient

    token = _datahub_token
    if not token:
        try:
            token = _get_datahub_session_token()
        except Exception:
            pytest.skip("Cannot obtain DataHub token (frontend unreachable)")
    return DataHubClient(gms_url=_datahub_gms_url, token=token)


@pytest_asyncio.fixture
async def datahub_test_dataset(datahub_client):
    """Emit a self-contained test dataset, clean up after."""
    from datahub.metadata.schema_classes import (
        DatasetPropertiesClass,
        OtherSchemaClass,
        SchemaFieldClass,
        SchemaMetadataClass,
        StatusClass,
    )

    urn = _TEST_DATASET_URN
    await datahub_client.emit_aspect(urn, StatusClass(removed=False))
    await datahub_client.emit_aspect(
        urn,
        DatasetPropertiesClass(
            name="test_schema.test_table",
            qualifiedName="integration_test.test_schema.test_table",
            description="Self-contained integration test fixture",
            customProperties={"source": "integration-test"},
        ),
    )
    await datahub_client.emit_aspect(
        urn,
        SchemaMetadataClass(
            schemaName="test_schema.test_table",
            platform="urn:li:dataPlatform:postgres",
            version=0,
            hash="",
            platformSchema=OtherSchemaClass(rawSchema=""),
            fields=[
                SchemaFieldClass(
                    fieldPath="id",
                    nativeDataType="integer",
                    type={"type": {"type": "NUMBER"}},
                    nullable=False,
                ),
                SchemaFieldClass(
                    fieldPath="name",
                    nativeDataType="text",
                    type={"type": {"type": "STRING"}},
                    nullable=True,
                ),
            ],
        ),
    )
    # Wait for DataHub's ES index to propagate the new entity
    await asyncio.sleep(8)
    yield urn
    await datahub_client.emit_aspect(urn, StatusClass(removed=True))


async def test_datahub_connectivity(datahub_client) -> None:
    assert await datahub_client.check_connectivity() is True


async def test_datahub_enumerate_datasets(datahub_client, datahub_test_dataset) -> None:
    datasets = await datahub_client.enumerate_datasets()
    assert isinstance(datasets, list)
    assert len(datasets) > 0
    assert datahub_test_dataset in datasets


async def test_datahub_get_aspect_existing(datahub_client, datahub_test_dataset) -> None:
    from datahub.metadata.schema_classes import DatasetPropertiesClass

    result = await datahub_client.get_aspect(datahub_test_dataset, DatasetPropertiesClass)
    assert result is not None
    assert result.name == "test_schema.test_table"


async def test_datahub_get_aspect_nonexistent(datahub_client) -> None:
    from datahub.metadata.schema_classes import DatasetPropertiesClass

    result = await datahub_client.get_aspect(
        "urn:li:dataset:(urn:li:dataPlatform:nonexistent,nonexistent,PROD)",
        DatasetPropertiesClass,
    )
    assert result is None


# --- Redis ---


@pytest_asyncio.fixture
async def redis_client():
    from src.shared.cache.client import RedisClient

    client = RedisClient(host=_redis_host, port=_redis_port, password=_redis_password)
    yield client
    await client.close()


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


# --- Qdrant ---


@pytest_asyncio.fixture
async def qdrant_manager():
    from src.shared.vector.client import QdrantManager

    return QdrantManager(
        host=_qdrant_host,
        port=_qdrant_http_port,
        api_key=_qdrant_api_key,
        grpc_port=_qdrant_grpc_port,
    )


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
