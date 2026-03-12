"""Integration tests for OntologyService against dev-env infrastructure.

Test-specific data extensions (created and cleaned up within each test):
- Transient concept_categories rows with Imazon-domain names
  (e.g. integration_test_imazon_customer, integration_test_imazon_order).
- Transient dataspoke.events rows for approve/reject lifecycle tests.

Prerequisites:
- PostgreSQL port-forwarded to localhost:9201
- Dummy data ingested via conftest.py Python utilities
"""

import uuid
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .conftest import _auth_headers


@pytest_asyncio.fixture
async def http_client(async_session):
    """HTTP client with DI overrides pointing to dev-env PostgreSQL."""
    from src.api.dependencies import get_db
    from src.api.main import app

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
async def test_concept_list(http_client):
    """GET /ontology returns valid paginated response."""
    headers = _auth_headers()

    resp = await http_client.get(
        "/api/v1/spoke/common/ontology",
        headers=headers,
        params={"limit": 10, "offset": 0},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "total_count" in body
    assert "concepts" in body
    assert isinstance(body["concepts"], list)


@pytest.mark.asyncio
async def test_concept_approve_reject_lifecycle(http_client, async_session: AsyncSession):
    """Insert concept -> GET -> approve -> verify status & version -> GET events."""
    headers = _auth_headers()
    concept_id = str(uuid.uuid4())

    try:
        # Insert a pending concept directly
        await async_session.execute(
            text(
                "INSERT INTO dataspoke.concept_categories"
                " (id, name, description, status, version, created_at, updated_at)"
                " VALUES (:id, :name, :desc, :status, :version, :created_at, :updated_at)"
            ),
            {
                "id": concept_id,
                "name": "integration_test_imazon_customer",
                "desc": "Imazon customer concept for integration test",
                "status": "pending",
                "version": 1,
                "created_at": datetime.now(tz=UTC),
                "updated_at": datetime.now(tz=UTC),
            },
        )
        await async_session.commit()

        # GET concept
        resp = await http_client.get(
            f"/api/v1/spoke/common/ontology/{concept_id}",
            headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "integration_test_imazon_customer"
        assert body["status"] == "pending"
        assert body["version"] == 1

        # POST approve
        resp = await http_client.post(
            f"/api/v1/spoke/common/ontology/{concept_id}/method/approve",
            headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "approved"
        assert body["version"] == 2

        # GET events -> should have approve event
        resp = await http_client.get(
            f"/api/v1/spoke/common/ontology/{concept_id}/event",
            headers=headers,
        )
        assert resp.status_code == 200
        events_body = resp.json()
        assert events_body["total_count"] >= 1
        event_types = [e["event_type"] for e in events_body["events"]]
        assert "concept.approved" in event_types
    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.events WHERE entity_id = :id AND entity_type = 'ontology'"),
            {"id": concept_id},
        )
        await async_session.execute(
            text("DELETE FROM dataspoke.concept_categories WHERE id = :id"),
            {"id": concept_id},
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_concept_reject_and_conflict(http_client, async_session: AsyncSession):
    """Insert concept -> reject -> reject again -> 409 conflict."""
    headers = _auth_headers()
    concept_id = str(uuid.uuid4())

    try:
        await async_session.execute(
            text(
                "INSERT INTO dataspoke.concept_categories"
                " (id, name, description, status, version, created_at, updated_at)"
                " VALUES (:id, :name, :desc, :status, :version, :created_at, :updated_at)"
            ),
            {
                "id": concept_id,
                "name": "integration_test_imazon_order",
                "desc": "Imazon order concept for integration test",
                "status": "pending",
                "version": 1,
                "created_at": datetime.now(tz=UTC),
                "updated_at": datetime.now(tz=UTC),
            },
        )
        await async_session.commit()

        # POST reject
        resp = await http_client.post(
            f"/api/v1/spoke/common/ontology/{concept_id}/method/reject",
            headers=headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "rejected"
        # Version should NOT be bumped on rejection
        assert body["version"] == 1

        # POST reject again -> 409
        resp = await http_client.post(
            f"/api/v1/spoke/common/ontology/{concept_id}/method/reject",
            headers=headers,
        )
        assert resp.status_code == 409
    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.events WHERE entity_id = :id AND entity_type = 'ontology'"),
            {"id": concept_id},
        )
        await async_session.execute(
            text("DELETE FROM dataspoke.concept_categories WHERE id = :id"),
            {"id": concept_id},
        )
        await async_session.commit()
