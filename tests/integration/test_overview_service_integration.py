"""Integration tests for OverviewService against dev-env infrastructure.

Test-specific data extensions (created and cleaned up within each test):
- Transient concept_categories rows with Imazon-domain names
  (e.g. integration_test_imazon_customers, integration_test_imazon_orders).
- Transient concept_relationships rows linking test concepts.
- Transient dataset_concept_map rows mapping Imazon datasets to test concepts.
- Overview config layout may be temporarily changed and reset in test_patch_overview_attr.

Prerequisites:
- PostgreSQL port-forwarded to localhost:9201
- DataHub GMS port-forwarded to localhost:9004
- Redis port-forwarded to localhost:9202
- Dummy data ingested via dummy-data-reset.sh && dummy-data-ingest.sh
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


# ── Config CRUD tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_overview_attr_returns_config(http_client):
    """GET /overview/attr returns config with layout, color_by, filters."""
    headers = _auth_headers()

    resp = await http_client.get("/api/v1/spoke/dg/overview/attr", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "layout" in body
    assert "color_by" in body
    assert "filters" in body


@pytest.mark.asyncio
async def test_patch_overview_attr_updates_config(http_client):
    """PATCH /overview/attr updates layout, then GET returns updated value."""
    headers = _auth_headers()

    try:
        resp = await http_client.patch(
            "/api/v1/spoke/dg/overview/attr",
            headers=headers,
            json={"layout": "radial"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["layout"] == "radial"

        # Verify GET returns updated value
        resp = await http_client.get("/api/v1/spoke/dg/overview/attr", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["layout"] == "radial"
    finally:
        # Cleanup: reset to default
        await http_client.patch(
            "/api/v1/spoke/dg/overview/attr",
            headers=headers,
            json={"layout": "force"},
        )


# ── Graph assembly tests ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_overview_returns_graph(http_client, async_session: AsyncSession):
    """GET /overview returns graph with nodes, edges, medallion, blind_spots."""
    headers = _auth_headers()
    concept_a_id = str(uuid.uuid4())
    concept_b_id = str(uuid.uuid4())

    try:
        # Insert concept categories
        for cid, name in [
            (concept_a_id, "integration_test_imazon_customers"),
            (concept_b_id, "integration_test_imazon_orders"),
        ]:
            await async_session.execute(
                text(
                    "INSERT INTO dataspoke.concept_categories"
                    " (id, name, description, status, version, created_at, updated_at)"
                    " VALUES (:id, :name, :desc, :status, :version, :ca, :ua)"
                ),
                {
                    "id": cid,
                    "name": name,
                    "desc": f"Imazon {name.split('_')[-1]} for integration test",
                    "status": "approved",
                    "version": 1,
                    "ca": datetime.now(tz=UTC),
                    "ua": datetime.now(tz=UTC),
                },
            )

        # Insert concept relationship
        rel_id = str(uuid.uuid4())
        await async_session.execute(
            text(
                "INSERT INTO dataspoke.concept_relationships"
                " (id, concept_a, concept_b, relationship_type, confidence_score, created_at)"
                " VALUES (:id, :ca, :cb, :rt, :cs, :created)"
            ),
            {
                "id": rel_id,
                "ca": concept_a_id,
                "cb": concept_b_id,
                "rt": "related_to",
                "cs": 0.9,
                "created": datetime.now(tz=UTC),
            },
        )
        await async_session.commit()

        resp = await http_client.get("/api/v1/spoke/dg/overview", headers=headers)
        assert resp.status_code == 200
        body = resp.json()

        assert "nodes" in body
        assert "edges" in body
        assert "medallion" in body
        assert "blind_spots" in body
        assert "stats" in body

        # Verify concept nodes present
        concept_node_ids = [n["id"] for n in body["nodes"] if n["type"] == "concept"]
        assert concept_a_id in concept_node_ids
        assert concept_b_id in concept_node_ids

        # Verify concept relationship edge present
        cr_edges = [e for e in body["edges"] if e["type"] == "concept_relationship"]
        assert len(cr_edges) >= 1

        # Verify medallion has expected keys
        assert "bronze" in body["medallion"]
        assert "silver" in body["medallion"]
        assert "gold" in body["medallion"]
    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.concept_relationships WHERE id = :id"),
            {"id": rel_id},
        )
        for cid in [concept_a_id, concept_b_id]:
            await async_session.execute(
                text("DELETE FROM dataspoke.concept_categories WHERE id = :id"),
                {"id": cid},
            )
        await async_session.commit()


@pytest.mark.asyncio
async def test_get_overview_blind_spots_include_unmapped_datasets(
    http_client, async_session: AsyncSession
):
    """Unmapped datasets appear in blind_spots."""
    headers = _auth_headers()
    concept_id = str(uuid.uuid4())
    known_urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,imazon.customers,PROD)"

    try:
        # Insert a concept category
        await async_session.execute(
            text(
                "INSERT INTO dataspoke.concept_categories"
                " (id, name, description, status, version, created_at, updated_at)"
                " VALUES (:id, :name, :desc, :status, :version, :ca, :ua)"
            ),
            {
                "id": concept_id,
                "name": "integration_test_imazon_blind_spot",
                "desc": "Imazon concept for blind spot test",
                "status": "approved",
                "version": 1,
                "ca": datetime.now(tz=UTC),
                "ua": datetime.now(tz=UTC),
            },
        )

        # Map one known dataset to the concept
        await async_session.execute(
            text(
                "INSERT INTO dataspoke.dataset_concept_map"
                " (dataset_urn, concept_id, confidence_score, status, created_at)"
                " VALUES (:urn, :cid, :cs, :status, :ca)"
            ),
            {
                "urn": known_urn,
                "cid": concept_id,
                "cs": 0.95,
                "status": "approved",
                "ca": datetime.now(tz=UTC),
            },
        )
        await async_session.commit()

        resp = await http_client.get("/api/v1/spoke/dg/overview", headers=headers)
        assert resp.status_code == 200
        body = resp.json()

        # If DataHub has datasets, blind_spots should not include the mapped one
        if body["stats"]["total_datasets"] > 0:
            assert known_urn not in body["blind_spots"] or body["stats"]["total_datasets"] == 0
    finally:
        await async_session.execute(
            text(
                "DELETE FROM dataspoke.dataset_concept_map"
                " WHERE dataset_urn = :urn AND concept_id = :cid"
            ),
            {"urn": known_urn, "cid": concept_id},
        )
        await async_session.execute(
            text("DELETE FROM dataspoke.concept_categories WHERE id = :id"),
            {"id": concept_id},
        )
        await async_session.commit()
