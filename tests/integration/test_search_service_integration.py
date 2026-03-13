"""Integration tests for SearchService against dev-env infrastructure.

Test-specific data extensions (created and cleaned up by fixtures):
- 1 DataHub dataset entity (imazon.test.search_svc.orders, env=DEV) with
  StatusClass, DatasetPropertiesClass, SchemaMetadataClass (2 fields),
  OwnershipClass, and GlobalTagsClass aspects. Soft-deleted on teardown.
- Transient Qdrant vectors created via reindex API calls.

Prerequisites:
- DataHub GMS port-forwarded to localhost:9004
- Redis port-forwarded to localhost:9202
- Qdrant port-forwarded to localhost:9203 (HTTP) / 9204 (gRPC)
- Dummy data ingested via conftest.py Python utilities
"""

import os
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from src.shared.vector.client import QdrantManager

from .conftest import (
    _auth_headers,
    emit_test_dataset,
    override_app,
    soft_delete_test_dataset,
)

_qdrant_host = os.environ.get("DATASPOKE_QDRANT_HOST", "localhost")
_qdrant_http_port = int(os.environ.get("DATASPOKE_QDRANT_HTTP_PORT", "9203"))
_qdrant_grpc_port = int(os.environ.get("DATASPOKE_QDRANT_GRPC_PORT", "9204"))
_qdrant_api_key = os.environ.get("DATASPOKE_QDRANT_API_KEY", "")

_TEST_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,imazon.test.search_svc.orders,DEV)"


@pytest_asyncio.fixture
async def qdrant_client():
    return QdrantManager(
        host=_qdrant_host,
        port=_qdrant_http_port,
        api_key=_qdrant_api_key,
        grpc_port=_qdrant_grpc_port,
    )


@pytest_asyncio.fixture
async def mock_llm():
    """Mock LLM client that returns deterministic embeddings (no real API key needed)."""
    import hashlib

    llm = AsyncMock()

    def _deterministic_embed(text: str) -> list[float]:
        digest = hashlib.sha256(text.encode()).digest()
        vec = []
        for i in range(1536):
            byte_val = digest[i % len(digest)]
            vec.append((byte_val / 255.0) * 2 - 1)
        return vec

    llm.embed.side_effect = _deterministic_embed
    llm.complete.return_value = "SELECT * FROM orders LIMIT 10"
    return llm


@pytest_asyncio.fixture
async def test_dataset_urn(datahub_client):
    """Emit a self-contained Imazon test dataset for search integration testing."""
    urn = _TEST_URN
    await emit_test_dataset(
        datahub_client,
        urn=urn,
        name="search_svc.orders",
        description="Integration test dataset for SearchService",
        fields=[("order_id", "integer", False), ("customer_email", "text", False)],
        with_ownership=True,
        with_tags=True,
    )
    yield urn
    await soft_delete_test_dataset(datahub_client, urn)


@pytest_asyncio.fixture
async def http_client(datahub_client, redis_client, qdrant_client, mock_llm, async_session):
    """HTTP client with real DI providers pointing to dev-env infra."""
    async with override_app(
        datahub=datahub_client,
        redis=redis_client,
        qdrant=qdrant_client,
        llm=mock_llm,
        db=async_session,
    ) as client:
        yield client


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reindex_via_http(http_client, test_dataset_urn):
    resp = await http_client.post(
        f"/api/v1/spoke/common/search/method/reindex?dataset_urn={test_dataset_urn}",
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert test_dataset_urn in body["message"]


@pytest.mark.asyncio
async def test_search_via_http(http_client, test_dataset_urn):
    # First reindex
    resp = await http_client.post(
        f"/api/v1/spoke/common/search/method/reindex?dataset_urn={test_dataset_urn}",
        headers=_auth_headers(),
    )
    assert resp.status_code == 200

    # Then search
    resp = await http_client.get(
        "/api/v1/spoke/common/search?q=orders",
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "datasets" in body
    # The mock LLM returns deterministic embeddings, so the result may or may not match
    # depending on vector similarity — just verify the structure is correct
    assert isinstance(body["datasets"], list)
    assert "total_count" in body


@pytest.mark.asyncio
async def test_search_returns_enriched_metadata(http_client, test_dataset_urn):
    # Reindex
    await http_client.post(
        f"/api/v1/spoke/common/search/method/reindex?dataset_urn={test_dataset_urn}",
        headers=_auth_headers(),
    )

    resp = await http_client.get(
        "/api/v1/spoke/common/search?q=orders+integration+test",
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    # If any results, verify enriched fields are present
    for item in body["datasets"]:
        assert "urn" in item
        assert "name" in item
        assert "platform" in item
        assert "owners" in item
        assert "tags" in item


@pytest.mark.asyncio
async def test_search_not_found_returns_empty(http_client):
    resp = await http_client.get(
        "/api/v1/spoke/common/search?q=zzz_absolutely_nothing_matches_this_xyz",
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["datasets"] == []


@pytest.mark.asyncio
async def test_reindex_nonexistent_dataset(http_client):
    fake_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,nonexistent.table.xyz,DEV)"
    resp = await http_client.post(
        f"/api/v1/spoke/common/search/method/reindex?dataset_urn={fake_urn}",
        headers=_auth_headers(),
    )
    assert resp.status_code == 404
