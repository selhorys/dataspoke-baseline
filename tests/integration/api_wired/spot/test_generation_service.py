"""Integration tests for GenerationService against dev-env infrastructure.

Test-specific data extensions (created and cleaned up within each test):
- Transient generation_configs rows via PUT API (Imazon-prefixed test URNs).
- Transient generation_results rows from POST generate runs.
- Transient dataspoke.events rows for event pagination tests.
- LLM and Qdrant calls use stubs via DATASPOKE_TEST_MODE on the host-mode server.

Prerequisites:
- Host-mode DataSpoke server running (DATASPOKE_TEST_MODE=true uv run -m src.cli --backend-only)
- PostgreSQL port-forwarded to localhost:9201
- DataHub GMS port-forwarded to localhost:9004
- Kestra port-forwarded to localhost:9205
- Dummy data ingested via conftest.py Python utilities
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.conftest import (
    _auth_headers,
    cleanup_events,
    emit_test_dataset,
    make_test_urn,
    seed_events,
    soft_delete_test_dataset,
)


def _urn(suffix: str) -> str:
    return make_test_urn("generation", suffix)


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generation_config_crud_via_http(
    http_client, async_session: AsyncSession,
):
    """PUT -> GET -> PATCH -> GET -> DELETE -> GET (404)."""
    dataset_urn = _urn("crud_test")
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
        assert resp.status_code in (200, 201)
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
            text(
                "DELETE FROM dataspoke.generation_configs"
                " WHERE dataset_urn = :urn"
            ),
            {"urn": dataset_urn},
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_list_generation_configs(
    http_client, async_session: AsyncSession,
):
    """PUT 2 configs -> GET list -> verify pagination."""
    urn1 = _urn("list_test_1")
    urn2 = _urn("list_test_2")
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
            assert resp.status_code in (200, 201)

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
                text(
                    "DELETE FROM dataspoke.generation_configs"
                    " WHERE dataset_urn = :urn"
                ),
                {"urn": urn},
            )
        await async_session.commit()


@pytest.mark.asyncio
async def test_generate_produces_result(
    http_client, async_session: AsyncSession,
    datahub_client,
):
    """PUT config -> POST generate -> GET results -> verify result."""
    dataset_urn = _urn("generate_test")
    headers = _auth_headers()

    # Emit test dataset to DataHub so the activity can read schema
    await emit_test_dataset(
        datahub_client, urn=dataset_urn, name="generate_test",
        wait_seconds=1.0,
    )

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
        assert resp.status_code in (200, 201)

        # Run generate (goes through real Kestra)
        resp = await http_client.post(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/gen/method/generate",
            headers=headers,
        )
        assert resp.status_code == 200
        run_body = resp.json()
        assert run_body["status"].lower() == "success"
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
        await soft_delete_test_dataset(datahub_client, dataset_urn)
        await async_session.execute(
            text(
                "DELETE FROM dataspoke.generation_results"
                " WHERE dataset_urn = :urn"
            ),
            {"urn": dataset_urn},
        )
        await async_session.execute(
            text(
                "DELETE FROM dataspoke.events"
                " WHERE entity_id = :urn"
                " AND entity_type = 'dataset'"
                " AND event_type LIKE 'GENERATION.%'"
            ),
            {"urn": dataset_urn},
        )
        await async_session.execute(
            text(
                "DELETE FROM dataspoke.generation_configs"
                " WHERE dataset_urn = :urn"
            ),
            {"urn": dataset_urn},
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_apply_after_approval(
    http_client, async_session: AsyncSession,
    datahub_client,
):
    """PUT config -> POST generate -> approve -> POST apply -> verify."""
    dataset_urn = _urn("apply_test")
    headers = _auth_headers()

    await emit_test_dataset(
        datahub_client, urn=dataset_urn, name="apply_test",
        wait_seconds=1.0,
    )

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
        assert resp.status_code in (200, 201)

        # Run generate (through real Kestra)
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
                "UPDATE dataspoke.generation_results"
                " SET approval_status = 'approved' WHERE id = :id"
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
        await soft_delete_test_dataset(datahub_client, dataset_urn)
        await async_session.execute(
            text(
                "DELETE FROM dataspoke.generation_results"
                " WHERE dataset_urn = :urn"
            ),
            {"urn": dataset_urn},
        )
        await async_session.execute(
            text(
                "DELETE FROM dataspoke.events"
                " WHERE entity_id = :urn"
                " AND entity_type = 'dataset'"
                " AND event_type LIKE 'GENERATION.%'"
            ),
            {"urn": dataset_urn},
        )
        await async_session.execute(
            text(
                "DELETE FROM dataspoke.generation_configs"
                " WHERE dataset_urn = :urn"
            ),
            {"urn": dataset_urn},
        )
        await async_session.commit()


@pytest.mark.asyncio
async def test_generate_config_not_found(http_client):
    """POST generate for unconfigured URN -> error.

    The data router triggers Kestra without checking config. The flow
    fails because the activity can't find the config. Returns 500.
    """
    fake_urn = _urn("nonexistent")
    resp = await http_client.post(
        f"/api/v1/spoke/common/data/{fake_urn}/attr/gen/method/generate",
        headers=_auth_headers(),
    )
    assert resp.status_code in (404, 500)


@pytest.mark.asyncio
async def test_generation_events(
    http_client, async_session: AsyncSession,
):
    """Seed events -> GET events with limit -> verify pagination."""
    dataset_urn = _urn("events_test")
    headers = _auth_headers()

    event_ids = await seed_events(
        async_session,
        entity_type="dataset",
        entity_id=dataset_urn,
        event_type="GENERATION.COMPLETE",
    )

    try:
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
        await cleanup_events(async_session, event_ids)
