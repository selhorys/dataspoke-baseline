"""Integration tests for IngestionService against dev-env infrastructure.

Test-specific data extensions (created and cleaned up within each test):
- Transient ingestion_configs rows via PUT API (Imazon-prefixed test URNs).
- Transient dataspoke.events rows for event pagination tests.
- LLM calls are mocked (deterministic fixture responses).

Prerequisites:
- PostgreSQL port-forwarded to localhost:9201
- DataHub GMS port-forwarded to localhost:9004
- Dummy data ingested via dummy-data-reset.sh && dummy-data-ingest.sh
"""

import json
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .conftest import _auth_headers

_TEST_URN_PREFIX = "urn:li:dataset:(urn:li:dataPlatform:postgres,imazon.test.ingestion"


@pytest_asyncio.fixture
async def mock_llm():
    llm = AsyncMock()
    llm.complete_json = AsyncMock(
        return_value={"description": "Enriched description", "tags": ["test-tag"]}
    )
    return llm


@pytest_asyncio.fixture
async def http_client(datahub_client, mock_llm, async_session):
    """HTTP client with real DI providers pointing to dev-env infra."""
    from src.api.dependencies import get_datahub, get_db, get_llm
    from src.api.main import app

    app.dependency_overrides[get_datahub] = lambda: datahub_client
    app.dependency_overrides[get_llm] = lambda: mock_llm

    async def _override_db():
        yield async_session

    app.dependency_overrides[get_db] = _override_db

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        yield client

    app.dependency_overrides.clear()


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ingestion_config_crud_via_http(http_client, async_session: AsyncSession):
    """PUT → GET → PATCH → GET → DELETE → GET (404)."""
    dataset_urn = f"{_TEST_URN_PREFIX}.crud_test,DEV)"
    headers = _auth_headers()

    try:
        # PUT - create config
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/conf",
            headers=headers,
            json={
                "dataset_urn": dataset_urn,
                "sources": {"sql_log": {"queries": ["SELECT 1 FROM test_table"]}},
                "deep_spec_enabled": False,
                "schedule": "0 0 * * *",
                "owner": "test@imazon.com",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["dataset_urn"] == dataset_urn
        assert body["owner"] == "test@imazon.com"
        assert body["schedule"] == "0 0 * * *"
        config_id = body["id"]

        # GET - read config
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/conf",
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == config_id

        # PATCH - update schedule
        resp = await http_client.patch(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/conf",
            headers=headers,
            json={"schedule": "0 6 * * *"},
        )
        assert resp.status_code == 200
        assert resp.json()["schedule"] == "0 6 * * *"

        # GET via ingestion router
        resp = await http_client.get(
            f"/api/v1/spoke/common/ingestion/{dataset_urn}",
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["schedule"] == "0 6 * * *"

        # DELETE
        resp = await http_client.delete(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/conf",
            headers=headers,
        )
        assert resp.status_code == 204

        # GET after delete → 404
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/conf",
            headers=headers,
        )
        assert resp.status_code == 404
    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.ingestion_configs WHERE dataset_urn = :urn"),
            {"urn": dataset_urn},
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_list_ingestion_configs(http_client, async_session: AsyncSession):
    """PUT 2 configs → GET list → verify pagination."""
    urn1 = f"{_TEST_URN_PREFIX}.list_test_1,DEV)"
    urn2 = f"{_TEST_URN_PREFIX}.list_test_2,DEV)"
    headers = _auth_headers()

    try:
        for urn in (urn1, urn2):
            resp = await http_client.put(
                f"/api/v1/spoke/common/data/{urn}/attr/ingestion/conf",
                headers=headers,
                json={
                    "dataset_urn": urn,
                    "sources": {"sql_log": {"queries": ["SELECT 1"]}},
                    "owner": "test@imazon.com",
                },
            )
            assert resp.status_code == 200

        resp = await http_client.get(
            "/api/v1/spoke/common/ingestion",
            headers=headers,
            params={"limit": 100},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_count"] >= 2
        urns = [c["dataset_urn"] for c in body["configs"]]
        assert urn1 in urns
        assert urn2 in urns
    finally:
        for urn in (urn1, urn2):
            await async_session.execute(
                text("DELETE FROM dataspoke.ingestion_configs WHERE dataset_urn = :urn"),
                {"urn": urn},
            )
        await async_session.commit()


@pytest.mark.asyncio
async def test_run_ingestion_dry_run(http_client, async_session: AsyncSession):
    """PUT config with sql_log → POST run dry_run=true → verify events."""
    dataset_urn = f"{_TEST_URN_PREFIX}.run_test,DEV)"
    headers = _auth_headers()

    try:
        # Create config
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/conf",
            headers=headers,
            json={
                "dataset_urn": dataset_urn,
                "sources": {
                    "sql_log": {
                        "queries": [
                            "SELECT * FROM catalog.title_master "
                            "JOIN orders.order_header "
                            "ON catalog.title_master.id = orders.order_header.title_id"
                        ]
                    }
                },
                "owner": "test@imazon.com",
            },
        )
        assert resp.status_code == 200

        # Run with dry_run=true
        resp = await http_client.post(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/method/run",
            headers=headers,
            json={"dry_run": True},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "success"
        assert body["detail"]["dry_run"] is True
        assert body["detail"]["metadata_extracted"] >= 1

        # Check events were recorded
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/event",
            headers=headers,
        )
        assert resp.status_code == 200
        events_body = resp.json()
        assert events_body["total_count"] >= 1
    finally:
        await async_session.execute(
            text(
                "DELETE FROM dataspoke.events WHERE entity_id = :urn AND entity_type = 'ingestion'"
            ),
            {"urn": dataset_urn},
        )
        await async_session.execute(
            text("DELETE FROM dataspoke.ingestion_configs WHERE dataset_urn = :urn"),
            {"urn": dataset_urn},
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_run_ingestion_not_found(http_client):
    """POST run for unconfigured URN → 404."""
    fake_urn = f"{_TEST_URN_PREFIX}.nonexistent,DEV)"
    resp = await http_client.post(
        f"/api/v1/spoke/common/data/{fake_urn}/attr/ingestion/method/run",
        headers=_auth_headers(),
        json={"dry_run": False},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_ingestion_events_pagination(http_client, async_session: AsyncSession):
    """Seed 3 events → GET with limit=2 → verify pagination."""
    dataset_urn = f"{_TEST_URN_PREFIX}.events_test,DEV)"
    headers = _auth_headers()
    event_ids = []

    try:
        for i in range(3):
            eid = uuid.uuid4()
            event_ids.append(eid)
            await async_session.execute(
                text(
                    "INSERT INTO dataspoke.events"
                    " (id, entity_type, entity_id, event_type, status, detail, occurred_at)"
                    " VALUES (:id, :entity_type, :entity_id, :event_type,"
                    " :status, :detail, :occurred_at)"
                ),
                {
                    "id": str(eid),
                    "entity_type": "ingestion",
                    "entity_id": dataset_urn,
                    "event_type": "ingestion.completed",
                    "status": "success",
                    "detail": json.dumps({"run_id": str(uuid.uuid4()), "index": i}),
                    "occurred_at": datetime.now(tz=UTC),
                },
            )
        await async_session.commit()

        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/ingestion/event",
            headers=headers,
            params={"limit": 2, "offset": 0},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_count"] == 3
        assert len(body["events"]) == 2

        # Also test via ingestion router
        resp = await http_client.get(
            f"/api/v1/spoke/common/ingestion/{dataset_urn}/event",
            headers=headers,
            params={"limit": 2, "offset": 0},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_count"] == 3
    finally:
        for eid in event_ids:
            await async_session.execute(
                text("DELETE FROM dataspoke.events WHERE id = :id"),
                {"id": str(eid)},
            )
        await async_session.commit()
