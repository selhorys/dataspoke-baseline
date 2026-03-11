"""Integration tests for GenerationService against dev-env infrastructure.

Test-specific data extensions (created and cleaned up within each test):
- Transient generation_configs rows via PUT API (Imazon-prefixed test URNs).
- Transient generation_results rows from POST generate runs.
- Transient dataspoke.events rows for event pagination tests.
- LLM and Qdrant calls are mocked (deterministic fixture responses).

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

_TEST_URN_PREFIX = "urn:li:dataset:(urn:li:dataPlatform:postgres,imazon.test.generation"


@pytest_asyncio.fixture
async def mock_llm():
    llm = AsyncMock()
    llm.complete = AsyncMock(return_value="test response")
    llm.complete_json = AsyncMock(
        return_value={
            "field_descriptions": {"id": "Primary key identifier"},
            "table_summary": "Integration test dataset",
            "suggested_tags": ["test"],
        }
    )
    return llm


@pytest_asyncio.fixture
async def mock_qdrant():
    qdrant = AsyncMock()
    qdrant.search = AsyncMock(return_value=[])
    qdrant.ensure_collection = AsyncMock()
    qdrant.check_connectivity = AsyncMock(return_value=True)
    return qdrant


@pytest_asyncio.fixture
async def http_client(datahub_client, mock_llm, mock_qdrant, async_session):
    """HTTP client with real DI providers pointing to dev-env infra."""
    from src.api.dependencies import get_datahub, get_db, get_llm, get_qdrant
    from src.api.main import app

    app.dependency_overrides[get_datahub] = lambda: datahub_client
    app.dependency_overrides[get_llm] = lambda: mock_llm
    app.dependency_overrides[get_qdrant] = lambda: mock_qdrant

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
async def test_generation_config_crud_via_http(http_client, async_session: AsyncSession):
    """PUT -> GET -> PATCH -> GET -> DELETE -> GET (404)."""
    dataset_urn = f"{_TEST_URN_PREFIX}.crud_test,DEV)"
    headers = _auth_headers()

    try:
        # PUT - create config
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/gen/conf",
            headers=headers,
            json={
                "dataset_urn": dataset_urn,
                "target_fields": {"description": True, "tags": True},
                "code_refs": None,
                "schedule": "0 0 * * *",
                "owner": "test@imazon.com",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["dataset_urn"] == dataset_urn
        assert body["owner"] == "test@imazon.com"
        assert body["target_fields"] == {"description": True, "tags": True}
        config_id = body["id"]

        # GET - read config
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/gen/conf",
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == config_id

        # PATCH - update schedule
        resp = await http_client.patch(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/gen/conf",
            headers=headers,
            json={"schedule": "0 6 * * *"},
        )
        assert resp.status_code == 200
        assert resp.json()["schedule"] == "0 6 * * *"

        # GET via gen router
        resp = await http_client.get(
            f"/api/v1/spoke/common/gen/{dataset_urn}",
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["schedule"] == "0 6 * * *"

        # DELETE
        resp = await http_client.delete(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/gen/conf",
            headers=headers,
        )
        assert resp.status_code == 204

        # GET after delete -> 404
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/gen/conf",
            headers=headers,
        )
        assert resp.status_code == 404
    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.generation_configs WHERE dataset_urn = :urn"),
            {"urn": dataset_urn},
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_list_generation_configs(http_client, async_session: AsyncSession):
    """PUT 2 configs -> GET list -> verify pagination."""
    urn1 = f"{_TEST_URN_PREFIX}.list_test_1,DEV)"
    urn2 = f"{_TEST_URN_PREFIX}.list_test_2,DEV)"
    headers = _auth_headers()

    try:
        for urn in (urn1, urn2):
            resp = await http_client.put(
                f"/api/v1/spoke/common/data/{urn}/attr/gen/conf",
                headers=headers,
                json={
                    "dataset_urn": urn,
                    "target_fields": {"description": True},
                    "owner": "test@imazon.com",
                },
            )
            assert resp.status_code == 200

        resp = await http_client.get(
            "/api/v1/spoke/common/gen",
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
                text("DELETE FROM dataspoke.generation_configs WHERE dataset_urn = :urn"),
                {"urn": urn},
            )
        await async_session.commit()


@pytest.mark.asyncio
async def test_generate_produces_result(http_client, async_session: AsyncSession):
    """PUT config -> POST generate -> GET results -> verify result."""
    dataset_urn = f"{_TEST_URN_PREFIX}.generate_test,DEV)"
    headers = _auth_headers()

    try:
        # Create config
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/gen/conf",
            headers=headers,
            json={
                "dataset_urn": dataset_urn,
                "target_fields": {"description": True},
                "owner": "test@imazon.com",
            },
        )
        assert resp.status_code == 200

        # Run generate
        resp = await http_client.post(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/gen/method/generate",
            headers=headers,
        )
        assert resp.status_code == 200
        run_body = resp.json()
        assert run_body["status"] == "success"
        assert "run_id" in run_body

        # Get results
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/gen/result",
            headers=headers,
        )
        assert resp.status_code == 200
        results_body = resp.json()
        assert results_body["total_count"] >= 1
        result = results_body["results"][0]
        assert "proposals" in result
        assert result["approval_status"] == "pending"
    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.generation_results WHERE dataset_urn = :urn"),
            {"urn": dataset_urn},
        )
        await async_session.execute(
            text(
                "DELETE FROM dataspoke.events WHERE entity_id = :urn AND entity_type = 'generation'"
            ),
            {"urn": dataset_urn},
        )
        await async_session.execute(
            text("DELETE FROM dataspoke.generation_configs WHERE dataset_urn = :urn"),
            {"urn": dataset_urn},
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_apply_after_approval(http_client, async_session: AsyncSession):
    """PUT config -> POST generate -> approve in DB -> POST apply -> verify applied_at."""
    dataset_urn = f"{_TEST_URN_PREFIX}.apply_test,DEV)"
    headers = _auth_headers()

    try:
        # Create config
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/gen/conf",
            headers=headers,
            json={
                "dataset_urn": dataset_urn,
                "target_fields": {"description": True},
                "owner": "test@imazon.com",
            },
        )
        assert resp.status_code == 200

        # Run generate
        resp = await http_client.post(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/gen/method/generate",
            headers=headers,
        )
        assert resp.status_code == 200

        # Get the result ID
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/gen/result",
            headers=headers,
        )
        assert resp.status_code == 200
        result_id = resp.json()["results"][0]["id"]

        # Manually approve in DB
        await async_session.execute(
            text(
                "UPDATE dataspoke.generation_results SET approval_status = 'approved' WHERE id = :id"
            ),
            {"id": result_id},
        )
        await async_session.commit()

        # Apply
        resp = await http_client.post(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/gen/method/apply",
            headers=headers,
            json={"result_id": result_id},
        )
        assert resp.status_code == 200
        apply_body = resp.json()
        assert apply_body["status"] == "applied"

        # Verify applied_at is set
        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/gen/result",
            headers=headers,
        )
        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert result["applied_at"] is not None
    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.generation_results WHERE dataset_urn = :urn"),
            {"urn": dataset_urn},
        )
        await async_session.execute(
            text(
                "DELETE FROM dataspoke.events WHERE entity_id = :urn AND entity_type = 'generation'"
            ),
            {"urn": dataset_urn},
        )
        await async_session.execute(
            text("DELETE FROM dataspoke.generation_configs WHERE dataset_urn = :urn"),
            {"urn": dataset_urn},
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_generate_config_not_found(http_client):
    """POST generate for unconfigured URN -> 404."""
    fake_urn = f"{_TEST_URN_PREFIX}.nonexistent,DEV)"
    resp = await http_client.post(
        f"/api/v1/spoke/common/data/{fake_urn}/attr/gen/method/generate",
        headers=_auth_headers(),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_generation_events(http_client, async_session: AsyncSession):
    """Seed events -> GET events with limit -> verify pagination."""
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
                    "entity_type": "generation",
                    "entity_id": dataset_urn,
                    "event_type": "generation.completed",
                    "status": "success",
                    "detail": json.dumps({"run_id": str(uuid.uuid4()), "index": i}),
                    "occurred_at": datetime.now(tz=UTC),
                },
            )
        await async_session.commit()

        resp = await http_client.get(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/gen/event",
            headers=headers,
            params={"limit": 2, "offset": 0},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_count"] == 3
        assert len(body["events"]) == 2

        # Also test via gen router
        resp = await http_client.get(
            f"/api/v1/spoke/common/gen/{dataset_urn}/event",
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
