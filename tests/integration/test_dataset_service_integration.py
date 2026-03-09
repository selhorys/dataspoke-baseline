"""Integration tests for DatasetService against dev-env infrastructure.

Prerequisites:
- DataHub GMS port-forwarded to localhost:9004
- PostgreSQL port-forwarded to localhost:9201
- Redis port-forwarded to localhost:9202
- Dummy data ingested via dummy-data-reset.sh && dummy-data-ingest.sh
"""

import asyncio
import base64
import json
import os
import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
import requests
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.cache.client import RedisClient
from src.shared.datahub.client import DataHubClient

_datahub_gms_url = os.environ.get("DATASPOKE_DATAHUB_GMS_URL", "http://localhost:9004")
_datahub_frontend_url = os.environ.get("DATASPOKE_DATAHUB_FRONTEND_URL", "http://localhost:9002")
_datahub_token = os.environ.get("DATASPOKE_DATAHUB_TOKEN", "")

_redis_host = os.environ.get("DATASPOKE_REDIS_HOST", "localhost")
_redis_port = int(os.environ.get("DATASPOKE_REDIS_PORT", "9202"))
_redis_password = os.environ.get("DATASPOKE_REDIS_PASSWORD", "")


def _get_datahub_session_token() -> str:
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


@pytest_asyncio.fixture
async def datahub_client():
    token = _datahub_token
    if not token:
        try:
            token = _get_datahub_session_token()
        except Exception:
            pytest.skip("Cannot obtain DataHub token (frontend unreachable)")
    return DataHubClient(gms_url=_datahub_gms_url, token=token)


@pytest_asyncio.fixture
async def redis_client():
    client = RedisClient(host=_redis_host, port=_redis_port, password=_redis_password)
    yield client
    await client.close()


@pytest_asyncio.fixture
async def test_dataset_urn(datahub_client):
    """Emit a self-contained test dataset for service-level testing."""
    from datahub.metadata.schema_classes import (
        DatasetPropertiesClass,
        GlobalTagsClass,
        OtherSchemaClass,
        OwnerClass,
        OwnershipClass,
        OwnershipTypeClass,
        SchemaFieldClass,
        SchemaMetadataClass,
        StatusClass,
        TagAssociationClass,
    )

    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,integration_test.dataset_svc.users,DEV)"

    await datahub_client.emit_aspect(urn, StatusClass(removed=False))
    await datahub_client.emit_aspect(
        urn,
        DatasetPropertiesClass(
            name="dataset_svc.users",
            description="Integration test dataset for DatasetService",
            customProperties={"source": "integration-test"},
        ),
    )
    await datahub_client.emit_aspect(
        urn,
        SchemaMetadataClass(
            schemaName="dataset_svc.users",
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
                    fieldPath="email",
                    nativeDataType="text",
                    type={"type": {"type": "STRING"}},
                    nullable=False,
                ),
            ],
        ),
    )
    await datahub_client.emit_aspect(
        urn,
        OwnershipClass(
            owners=[
                OwnerClass(
                    owner="urn:li:corpuser:testuser@example.com",
                    type=OwnershipTypeClass.DATAOWNER,
                ),
            ]
        ),
    )
    await datahub_client.emit_aspect(
        urn,
        GlobalTagsClass(tags=[TagAssociationClass(tag="urn:li:tag:integration-test")]),
    )
    # Wait for ES indexing
    await asyncio.sleep(3)
    yield urn
    await datahub_client.emit_aspect(urn, StatusClass(removed=True))


@pytest_asyncio.fixture
async def http_client(datahub_client, redis_client, async_session):
    """HTTP client with real DI providers pointing to dev-env infra."""
    from src.api.dependencies import get_datahub, get_db, get_redis
    from src.api.main import app

    app.dependency_overrides[get_datahub] = lambda: datahub_client
    app.dependency_overrides[get_redis] = lambda: redis_client

    async def _override_db():
        yield async_session

    app.dependency_overrides[get_db] = _override_db

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        yield client

    app.dependency_overrides.clear()


def _auth_headers() -> dict[str, str]:
    from src.api.auth.jwt import create_access_token

    token, _ = create_access_token(
        subject="integration-test-user", groups=["de", "da", "dg"], email="test@example.com"
    )
    return {"Authorization": f"Bearer {token}"}


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_dataset_summary_via_http(http_client, test_dataset_urn):
    resp = await http_client.get(
        f"/api/v1/spoke/common/data/{test_dataset_urn}",
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["urn"] == test_dataset_urn
    assert body["name"] == "dataset_svc.users"
    assert body["platform"] == "postgres"
    assert "urn:li:corpuser:testuser@example.com" in body["owners"]
    assert "urn:li:tag:integration-test" in body["tags"]


@pytest.mark.asyncio
async def test_get_dataset_summary_not_found(http_client):
    fake_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,nonexistent.table,DEV)"
    resp = await http_client.get(
        f"/api/v1/spoke/common/data/{fake_urn}",
        headers=_auth_headers(),
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["error_code"] == "DATASET_NOT_FOUND"


@pytest.mark.asyncio
async def test_get_dataset_attributes_via_http(http_client, test_dataset_urn):
    resp = await http_client.get(
        f"/api/v1/spoke/common/data/{test_dataset_urn}/attr",
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["urn"] == test_dataset_urn
    assert body["column_count"] == 2
    assert "id" in body["fields"]
    assert "email" in body["fields"]


@pytest.mark.asyncio
async def test_get_dataset_events_empty(http_client, test_dataset_urn):
    resp = await http_client.get(
        f"/api/v1/spoke/common/data/{test_dataset_urn}/event",
        headers=_auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["events"] == []
    assert body["total_count"] >= 0


@pytest.mark.asyncio
async def test_get_dataset_events_with_seeded_event(
    http_client, test_dataset_urn, async_session: AsyncSession
):
    event_id = uuid.uuid4()
    now = datetime.now(tz=UTC)
    await async_session.execute(
        text("""
            INSERT INTO dataspoke.events (id, entity_type, entity_id, event_type, status, detail, occurred_at)
            VALUES (:id, :entity_type, :entity_id, :event_type, :status, :detail, :occurred_at)
        """),
        {
            "id": str(event_id),
            "entity_type": "dataset",
            "entity_id": test_dataset_urn,
            "event_type": "ingestion.completed",
            "status": "success",
            "detail": json.dumps({"source": "integration-test"}),
            "occurred_at": now,
        },
    )
    await async_session.commit()

    try:
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{test_dataset_urn}/event",
            headers=_auth_headers(),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_count"] >= 1
        event_ids = [e["id"] for e in body["events"]]
        assert str(event_id) in event_ids
    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.events WHERE id = :id"),
            {"id": str(event_id)},
        )
        await async_session.commit()
