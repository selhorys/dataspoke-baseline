"""Integration tests for internal activity endpoints (Airflow-free).

Calls /internal/activities/* endpoints directly through the test-mode
server (DATASPOKE_TEST_MODE=true), bypassing Airflow DAG orchestration.
This verifies business logic end-to-end without any Airflow overhead.

All /internal/* calls use the ``internal_http_client`` fixture which
pre-configures the ``X-Internal-Token`` header from DATASPOKE_INTERNAL_TOKEN.

Prerequisites:
- In-cluster DataSpoke server running (DATASPOKE_TEST_MODE=true via dataspoke-test-mode.sh)
- DATASPOKE_INTERNAL_TOKEN set in the test environment (matches the server)
- PostgreSQL accessible via DATASPOKE_DEV_PG_HOST/PORT
- DataHub GMS accessible via DATASPOKE_DATAHUB_GMS_URL
- Dummy data ingested via conftest.py Python utilities
"""

import json

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.conftest import (
    _auth_headers,
    emit_test_dataset,
    make_test_urn,
    soft_delete_test_dataset,
)


def _urn(suffix: str) -> str:
    return make_test_urn("activity", suffix)


# ── X-Internal-Token auth enforcement ────────────────────────────────────────


@pytest.mark.asyncio
async def test_internal_endpoint_without_token_returns_401(http_client):
    """Calling /internal/activities/* without X-Internal-Token → 401 UNAUTHORIZED.

    Uses the plain ``http_client`` (no token header) to confirm that the
    require_internal_token dependency rejects unauthenticated requests.
    """
    resp = await http_client.post(
        "/internal/activities/metrics/publish-update",
        json={"run_id": "test-run-id", "status": "success", "detail": {}},
    )
    # 401 when token is configured; 503 when DATASPOKE_INTERNAL_TOKEN is blank server-side
    assert resp.status_code in (401, 503)
    if resp.status_code == 401:
        assert resp.json()["detail"]["error_code"] == "UNAUTHORIZED"
    else:
        assert resp.json()["detail"]["error_code"] == "INTERNAL_AUTH_NOT_CONFIGURED"


# ── /validation ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_validation_activity_dry_run(
    http_client, internal_http_client, async_session: AsyncSession, datahub_client,
):
    """POST /internal/activities/validation/run (dry_run=true) → 200."""
    dataset_urn = _urn("val_dry")
    headers = _auth_headers()

    await emit_test_dataset(
        datahub_client, urn=dataset_urn, name="val_dry", wait_seconds=5.0,
    )

    try:
        # Seed config via public API (handles all required columns)
        resp = await http_client.put(
            f"/api/v1/spoke/common/data/{dataset_urn}/attr/validation/conf",
            headers=headers,
            json={
                "dataset_urn": dataset_urn,
                "rules": [{"rule_id": "freshness_01", "type": "freshness", "max_age_hours": 24}],
                "owner": "test@imazon.com",
            },
        )
        assert resp.status_code in (200, 201)

        # Call activity endpoint directly (bypasses Airflow DAG)
        resp = await internal_http_client.post(
            "/internal/activities/validation/run",
            json={
                "dataset_urn": dataset_urn,
                "dry_run": True,
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"].lower() == "success"
        assert "run_id" in body
    finally:
        await soft_delete_test_dataset(datahub_client, dataset_urn)
        await async_session.execute(
            text("DELETE FROM dataspoke.validation_configs WHERE dataset_urn = :urn"),
            {"urn": dataset_urn},
        )
        await async_session.commit()


# ── /generation (skipped — needs overhaul) ───────────────────────────────────


@pytest.mark.skip(reason="generation flow removed from startup flows — needs overhaul")
@pytest.mark.asyncio
async def test_run_generation_activity(
    http_client, async_session: AsyncSession, datahub_client,
):
    """POST /internal/activities/generation/run → 200."""
    dataset_urn = _urn("gen")
    headers = _auth_headers()

    await emit_test_dataset(
        datahub_client, urn=dataset_urn, name="gen", wait_seconds=1.0,
    )

    try:
        # Seed config via public API
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

        # Call activity endpoint directly
        resp = await http_client.post(
            "/internal/activities/generation/run",
            json={"dataset_urn": dataset_urn},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"].lower() == "success"
        assert "run_id" in body
    finally:
        await soft_delete_test_dataset(datahub_client, dataset_urn)
        await async_session.execute(
            text("DELETE FROM dataspoke.generation_results WHERE dataset_urn = :urn"),
            {"urn": dataset_urn},
        )
        await async_session.execute(
            text("DELETE FROM dataspoke.generation_configs WHERE dataset_urn = :urn"),
            {"urn": dataset_urn},
        )
        await async_session.commit()


# ── /metrics ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_metric_activity_dry_run(
    http_client, internal_http_client, async_session: AsyncSession,
):
    """POST /internal/activities/metrics/run (dry_run=true) → 200."""
    metric_id = "imazon.test.activity.metric_dry"
    headers = _auth_headers()

    try:
        # Seed config via public API
        resp = await http_client.put(
            f"/api/v1/spoke/dg/metric/{metric_id}/attr/conf",
            headers=headers,
            json={
                "title": "Activity Test Metric",
                "description": "Direct activity endpoint test",
                "theme": "quality",
                "measurement_query": {"type": "poorly_documented"},
            },
        )
        assert resp.status_code in (200, 201)

        # Call activity endpoint directly with internal auth
        resp = await internal_http_client.post(
            "/internal/activities/metrics/run",
            json={"metric_id": metric_id, "dry_run": True},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"].lower() == "success"
        assert "run_id" in body
    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.metric_definitions WHERE id = :id"),
            {"id": metric_id},
        )
        await async_session.commit()


# ── /search (skipped — embedding-sync flow needs overhaul) ──────────────────


@pytest.mark.skip(reason="embedding-sync flow removed from startup flows — needs overhaul")
@pytest.mark.asyncio
async def test_enumerate_datasets_single_mode(http_client):
    """search/enumerate (single mode) returns only the requested URN."""
    dataset_urn = _urn("enumerate_single")
    resp = await http_client.post(
        "/internal/activities/search/enumerate",
        json={"mode": "single", "dataset_urn": dataset_urn},
    )
    assert resp.status_code == 200
    assert resp.json() == [dataset_urn]


@pytest.mark.skip(reason="embedding-sync flow removed from startup flows — needs overhaul")
@pytest.mark.asyncio
async def test_reindex_batch(
    http_client, datahub_client,
):
    """reindex-batch should process a dataset (errors acceptable with stubs)."""
    dataset_urn = _urn("reindex")

    await emit_test_dataset(
        datahub_client, urn=dataset_urn, name="reindex", wait_seconds=1.0,
    )

    try:
        resp = await http_client.post(
            "/internal/activities/search/reindex-batch",
            json={"dataset_urns": [dataset_urn]},
        )
        assert resp.status_code == 200
        body = resp.json()
        # With stubs, reindex may succeed or error on individual datasets
        assert "indexed" in body
        assert "errors" in body
        assert body["indexed"] + len(body["errors"]) == 1
    finally:
        await soft_delete_test_dataset(datahub_client, dataset_urn)


@pytest.mark.skip(reason="embedding-sync flow removed from startup flows — needs overhaul")
@pytest.mark.asyncio
async def test_embedding_sync_chain(
    http_client, datahub_client,
):
    """search/enumerate → search/reindex-batch chain (mimics embedding-sync flow)."""
    dataset_urn = _urn("embed_chain")

    await emit_test_dataset(
        datahub_client, urn=dataset_urn, name="embed_chain", wait_seconds=1.0,
    )

    try:
        # Step 1: enumerate
        resp = await http_client.post(
            "/internal/activities/search/enumerate",
            json={"mode": "single", "dataset_urn": dataset_urn},
        )
        assert resp.status_code == 200
        urns = resp.json()
        assert dataset_urn in urns

        # Step 2: reindex using output from step 1
        resp = await http_client.post(
            "/internal/activities/search/reindex-batch",
            json={"dataset_urns": urns},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "indexed" in body
    finally:
        await soft_delete_test_dataset(datahub_client, dataset_urn)


# ── /ontology (skipped — needs overhaul) ─────────────────────────────────────


@pytest.mark.skip(reason="ontology-rebuild flow removed from startup flows — needs overhaul")
@pytest.mark.asyncio
async def test_classify_datasets(http_client):
    """ontology/classify endpoint should return a list (may be empty with stubs)."""
    resp = await http_client.post(
        "/internal/activities/ontology/classify",
        json={"force": False},
    )
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


@pytest.mark.skip(reason="ontology-rebuild flow removed from startup flows — needs overhaul")
@pytest.mark.asyncio
async def test_build_hierarchy(
    http_client, async_session: AsyncSession,
):
    """build-hierarchy should create ConceptCategory rows from classifications."""
    category_name = "test_activity_master_data"

    try:
        resp = await http_client.post(
            "/internal/activities/ontology/build-hierarchy",
            json={
                "classifications": [
                    {
                        "category": category_name,
                        "dataset_urn": "urn:test:ds1",
                        "confidence": 0.9,
                        "field_count": 5,
                    },
                    {
                        "category": category_name,
                        "dataset_urn": "urn:test:ds2",
                        "confidence": 0.85,
                        "field_count": 3,
                    },
                ],
            },
        )
        assert resp.status_code == 200
        hierarchy = resp.json()
        assert len(hierarchy) == 1
        assert hierarchy[0]["name"] == category_name
        assert hierarchy[0]["dataset_count"] == 2
        assert set(hierarchy[0]["dataset_urns"]) == {"urn:test:ds1", "urn:test:ds2"}
    finally:
        await async_session.execute(
            text("DELETE FROM dataspoke.concept_categories WHERE name = :name"),
            {"name": category_name},
        )
        await async_session.commit()


@pytest.mark.skip(reason="ontology-rebuild flow removed from startup flows — needs overhaul")
@pytest.mark.asyncio
async def test_infer_relationships(http_client):
    """infer-relationships should find shared datasets between categories."""
    resp = await http_client.post(
        "/internal/activities/ontology/infer-relationships",
        json={
            "hierarchy": [
                {"name": "cat_a", "dataset_urns": ["urn:1", "urn:2", "urn:3"]},
                {"name": "cat_b", "dataset_urns": ["urn:2", "urn:3", "urn:4"]},
                {"name": "cat_c", "dataset_urns": ["urn:5"]},
            ],
        },
    )
    assert resp.status_code == 200
    rels = resp.json()
    # Only cat_a ↔ cat_b share datasets (urn:2, urn:3)
    assert len(rels) == 1
    assert rels[0]["source"] == "cat_a"
    assert rels[0]["target"] == "cat_b"
    assert rels[0]["shared_count"] == 2


@pytest.mark.skip(reason="ontology-rebuild flow removed from startup flows — needs overhaul")
@pytest.mark.asyncio
async def test_detect_drift(http_client):
    """detect-drift should identify new categories not in DB."""
    resp = await http_client.post(
        "/internal/activities/ontology/detect-drift",
        json={
            "current_hierarchy": [
                {"name": "novel_category_xyz_test"},
            ],
        },
    )
    assert resp.status_code == 200
    drift = resp.json()
    new_names = [d["name"] for d in drift if d["type"] == "new_category"]
    assert "novel_category_xyz_test" in new_names


@pytest.mark.skip(reason="ontology-rebuild flow removed from startup flows — needs overhaul")
@pytest.mark.asyncio
async def test_ontology_rebuild_chain(
    http_client, async_session: AsyncSession,
):
    """classify → hierarchy → relationships → drift chain (mimics ontology-rebuild flow).

    Uses pre-built classifications (bypassing LLM) to verify the chain.
    """
    cat_a = "test_chain_ref_data"
    cat_b = "test_chain_master_data"
    shared_urn = "urn:test:chain:shared"

    try:
        # Step 1: classify (skip — use pre-built classifications instead,
        # since stub LLM returns empty confidence)
        classifications = [
            {"category": cat_a, "dataset_urn": shared_urn, "confidence": 0.9, "field_count": 3},
            {"category": cat_a, "dataset_urn": "urn:test:chain:a_only", "confidence": 0.9, "field_count": 2},
            {"category": cat_b, "dataset_urn": shared_urn, "confidence": 0.85, "field_count": 3},
        ]

        # Step 2: build-hierarchy
        resp = await http_client.post(
            "/internal/activities/ontology/build-hierarchy",
            json={"classifications": classifications},
        )
        assert resp.status_code == 200
        hierarchy = resp.json()
        assert len(hierarchy) == 2

        # Step 3: infer-relationships
        resp = await http_client.post(
            "/internal/activities/ontology/infer-relationships",
            json={"hierarchy": hierarchy},
        )
        assert resp.status_code == 200
        rels = resp.json()
        assert len(rels) == 1
        assert rels[0]["shared_count"] == 1

        # Step 4: detect-drift
        resp = await http_client.post(
            "/internal/activities/ontology/detect-drift",
            json={"current_hierarchy": hierarchy},
        )
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
    finally:
        for name in (cat_a, cat_b):
            await async_session.execute(
                text("DELETE FROM dataspoke.concept_categories WHERE name = :name"),
                {"name": name},
            )
        await async_session.commit()


# ── /metrics (publish) ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_publish_metric_update(internal_http_client):
    """metrics/publish-update should succeed (stub cache is no-op)."""
    resp = await internal_http_client.post(
        "/internal/activities/metrics/publish-update",
        json={"run_id": "test-run-id", "status": "success", "detail": {}},
    )
    assert resp.status_code == 200
    assert resp.json()["published"] is True
