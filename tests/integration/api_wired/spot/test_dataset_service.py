"""Integration tests for DatasetService against dev-env infrastructure.

Test-specific data extensions (created and cleaned up by fixtures):
- 1 DataHub dataset entity (imazon.test.dataset_svc.users, env=DEV) with
  StatusClass, DatasetPropertiesClass, SchemaMetadataClass (2 fields),
  OwnershipClass, and GlobalTagsClass aspects. Soft-deleted on teardown.
- Transient rows in dataspoke.events for event-list tests.

Prerequisites:
- DataHub GMS port-forwarded to localhost:9004
- PostgreSQL port-forwarded to localhost:9201
- Redis port-forwarded to localhost:9202
- Dummy data ingested via conftest.py Python utilities
"""

import json
import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.conftest import (
    _auth_headers,
    emit_test_dataset,
    override_app,
    soft_delete_test_dataset,
)


@pytest_asyncio.fixture
async def test_dataset_urn(datahub_client):
    """Emit a self-contained Imazon test dataset for service-level testing."""
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,imazon.test.dataset_svc.users,DEV)"
    await emit_test_dataset(
        datahub_client,
        urn=urn,
        name="dataset_svc.users",
        description="Integration test dataset for DatasetService",
        fields=[("id", "integer", False), ("email", "text", False)],
        with_ownership=True,
        with_tags=True,
    )
    yield urn
    await soft_delete_test_dataset(datahub_client, urn)


@pytest_asyncio.fixture
async def http_client(datahub_client, redis_client, async_session):
    """HTTP client with real DI providers pointing to dev-env infra."""
    async with override_app(datahub=datahub_client, redis=redis_client, db=async_session) as client:
        yield client


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
        text(
            "INSERT INTO dataspoke.events"
            " (id, entity_type, entity_id, event_type, status, detail, occurred_at)"
            " VALUES (:id, :entity_type, :entity_id, :event_type,"
            " :status, :detail, :occurred_at)"
        ),
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
