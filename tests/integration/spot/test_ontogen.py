"""Spot tests for Ontology Generation endpoints.

Concerns covered:
- GET /spoke/common/ontogen/attr/conf — get singleton conf (200 with defaults)
- PUT /spoke/common/ontogen/attr/conf — create/replace conf
- PATCH /spoke/common/ontogen/attr/conf — partial update
- DELETE /spoke/common/ontogen/attr/conf — reset (204)
- POST /spoke/common/ontogen/attr/seed — create from raw Markdown (201)
- GET /spoke/common/ontogen/attr/seed — list seeds (envelope)
- GET /spoke/common/ontogen/attr/seed/{seed_id} — get seed body
- PATCH /spoke/common/ontogen/attr/seed/{seed_id} — replace seed body
- DELETE /spoke/common/ontogen/attr/seed/{seed_id} — retire seed (204)
- POST /spoke/common/ontogen/method/run — trigger run (dry_run query param)
- GET /spoke/common/ontogen/result/node — list nodes (envelope)
- GET /spoke/common/ontogen/result/edge — list edges (envelope)
- GET /spoke/common/ontogen/result/triple — list triples (envelope)
- POST /spoke/common/ontogen/result/node/{node_id}/method/review — approve node
- POST /spoke/common/ontogen/result/edge/{edge_id}/method/review — approve edge
- POST /spoke/common/ontogen/result/triple/{triple_id}/method/review — approve triple
"""

import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
async def test_ontogen_conf_get_returns_defaults(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/common/ontogen/attr/conf returns 200 with singleton conf structure."""
    resp = await api_client.get(
        "/api/v1/spoke/common/ontogen/attr/conf",
        headers=admin_headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "is_enabled" in body
    assert "schedule_tier" in body
    assert "dataset_filter" in body
    assert "max_manual_queries_per_dataset" in body
    assert "max_system_queries_per_dataset" in body


@pytest.mark.asyncio
async def test_ontogen_conf_put(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT /spoke/common/ontogen/attr/conf creates or replaces the singleton conf."""
    resp = await api_client.put(
        "/api/v1/spoke/common/ontogen/attr/conf",
        headers=admin_headers,
        json={
            "is_enabled": True,
            "schedule_tier": "daily",
            "dataset_filter": {},
            "max_manual_queries_per_dataset": 20,
            "max_system_queries_per_dataset": 10,
            "default_run_prompt": None,
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_enabled"] is True
    assert body["schedule_tier"] == "daily"

    # Cleanup — reset to disabled
    await api_client.patch(
        "/api/v1/spoke/common/ontogen/attr/conf",
        headers=admin_headers,
        json={"is_enabled": False},
    )


@pytest.mark.asyncio
async def test_ontogen_conf_patch(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH /spoke/common/ontogen/attr/conf partially updates the singleton conf."""
    # Ensure a conf exists
    await api_client.put(
        "/api/v1/spoke/common/ontogen/attr/conf",
        headers=admin_headers,
        json={
            "is_enabled": False,
            "schedule_tier": "daily",
            "dataset_filter": {},
            "max_manual_queries_per_dataset": 20,
            "max_system_queries_per_dataset": 10,
        },
    )

    patch_resp = await api_client.patch(
        "/api/v1/spoke/common/ontogen/attr/conf",
        headers=admin_headers,
        json={"schedule_tier": "weekly", "max_manual_queries_per_dataset": 5},
    )

    assert patch_resp.status_code == 200
    body = patch_resp.json()
    assert body["schedule_tier"] == "weekly"
    assert body["max_manual_queries_per_dataset"] == 5


@pytest.mark.asyncio
async def test_ontogen_conf_delete_resets(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """DELETE /spoke/common/ontogen/attr/conf removes/resets the singleton conf (204)."""
    # Ensure conf exists first
    await api_client.put(
        "/api/v1/spoke/common/ontogen/attr/conf",
        headers=admin_headers,
        json={
            "is_enabled": False,
            "schedule_tier": "daily",
            "dataset_filter": {},
            "max_manual_queries_per_dataset": 20,
            "max_system_queries_per_dataset": 10,
        },
    )

    del_resp = await api_client.delete(
        "/api/v1/spoke/common/ontogen/attr/conf",
        headers=admin_headers,
    )
    assert del_resp.status_code == 204


@pytest.mark.asyncio
async def test_ontogen_seed_create_list_get_patch_delete(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Seed CRUD: create (201), list, get body, patch, delete (204)."""
    base_seed = "/api/v1/spoke/common/ontogen/attr/seed"
    seed_md = "# Imazon Ontology Seed\n\nImazon is an online bookstore."

    # Create
    create_resp = await api_client.post(
        base_seed,
        headers={**admin_headers, "content-type": "text/markdown"},
        content=seed_md.encode(),
    )
    assert create_resp.status_code == 201, create_resp.text
    seed_id = create_resp.json()["seed_id"]

    # List — seed_id must appear
    list_resp = await api_client.get(base_seed, headers=admin_headers)
    assert list_resp.status_code == 200
    list_body = list_resp.json()
    assert "seeds" in list_body
    seed_ids = [s["seed_id"] for s in list_body["seeds"]]
    assert seed_id in seed_ids

    # Get body
    get_resp = await api_client.get(f"{base_seed}/{seed_id}", headers=admin_headers)
    assert get_resp.status_code == 200
    assert "Imazon" in get_resp.text

    # Patch (replace body)
    new_md = "# Updated Seed\n\nUpdated body for spot test."
    patch_resp = await api_client.patch(
        f"{base_seed}/{seed_id}",
        headers={**admin_headers, "content-type": "text/markdown"},
        content=new_md.encode(),
    )
    assert patch_resp.status_code == 200

    # Delete (soft-delete: status -> "retired"; DELETE returns 204)
    del_resp = await api_client.delete(f"{base_seed}/{seed_id}", headers=admin_headers)
    assert del_resp.status_code == 204

    # Soft-delete semantics: GET still returns the markdown body (record stays for audit),
    # but the seed_id is removed from the active list.
    list_after = await api_client.get(base_seed, headers=admin_headers)
    assert list_after.status_code == 200
    active_ids_after = [s["seed_id"] for s in list_after.json()["seeds"]]
    assert seed_id not in active_ids_after


@pytest.mark.asyncio
async def test_ontogen_run_dry_run(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST /spoke/common/ontogen/method/run with dry_run=true returns run summary."""
    resp = await api_client.post(
        "/api/v1/spoke/common/ontogen/method/run?dry_run=true",
        headers=admin_headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "status" in body
    assert "dry_run" in body
    assert body["dry_run"] is True


@pytest.mark.asyncio
async def test_ontogen_list_nodes_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/common/ontogen/result/node returns paginated node list."""
    resp = await api_client.get(
        "/api/v1/spoke/common/ontogen/result/node?offset=0&limit=10",
        headers=admin_headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "nodes" in body
    assert "offset" in body
    assert "limit" in body
    assert "total_count" in body
    assert isinstance(body["nodes"], list)


@pytest.mark.asyncio
async def test_ontogen_list_edges_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/common/ontogen/result/edge returns paginated edge list."""
    resp = await api_client.get(
        "/api/v1/spoke/common/ontogen/result/edge?offset=0&limit=10",
        headers=admin_headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "edges" in body
    assert "offset" in body
    assert "limit" in body
    assert "total_count" in body
    assert isinstance(body["edges"], list)


@pytest.mark.asyncio
async def test_ontogen_list_triples_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/common/ontogen/result/triple returns paginated triple list."""
    resp = await api_client.get(
        "/api/v1/spoke/common/ontogen/result/triple?offset=0&limit=10",
        headers=admin_headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "triples" in body
    assert "offset" in body
    assert "limit" in body
    assert "total_count" in body
    assert isinstance(body["triples"], list)


# ── Review tests ────────────────────────────────────────────────────────────
#
# The stub LLM returns no nodes/edges/triples (deterministically empty), so the
# review tests can't rely on ontogen.run to produce candidates. We seed one
# pending row per test directly into PostgreSQL via the async_session fixture
# (root conftest), then exercise the review endpoint over REST.  Each test
# uses a uuid-suffixed name/label so unique constraints don't clash with rows
# left behind by prior sessions, and cleans up afterwards.


async def _insert_pending_node(
    session: AsyncSession, node_id: str, name: str
) -> None:
    from src.shared.db.models import OntogenNode

    session.add(
        OntogenNode(
            id=node_id,
            name=name,
            description="Spot test ontogen node.",
            confidence_score=0.85,
            status="pending_review",
            evidence={"source": "spot-test"},
        )
    )
    await session.commit()


async def _insert_pending_edge(
    session: AsyncSession, edge_id: str, label: str
) -> None:
    from src.shared.db.models import OntogenEdge

    session.add(
        OntogenEdge(
            id=edge_id,
            label=label,
            semantics="Spot test ontogen edge.",
            confidence_score=0.85,
            status="pending_review",
            evidence={"source": "spot-test"},
        )
    )
    await session.commit()


async def _insert_pending_triple(
    session: AsyncSession,
    *,
    subject_id: str,
    edge_id: str,
    object_id: str,
) -> str:
    from src.shared.db.models import OntogenTriple

    triple_id = f"{subject_id}__{edge_id}__{object_id}"
    session.add(
        OntogenTriple(
            id=triple_id,
            subject_node_id=subject_id,
            edge_id=edge_id,
            object_node_id=object_id,
            confidence_score=0.85,
            status="pending_review",
            evidence={"source": "spot-test"},
        )
    )
    await session.commit()
    return triple_id


async def _delete_row(session: AsyncSession, model: Any, pk: str) -> None:
    obj = await session.get(model, pk)
    if obj is not None:
        await session.delete(obj)
        await session.commit()


@pytest.mark.asyncio
async def test_ontogen_node_review_approve(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """POST node/{id}/method/review with 'approve' transitions status to approved."""
    from src.shared.db.models import OntogenNode

    suffix = uuid.uuid4().hex[:8]
    node_id = f"spot-node-{suffix}"
    await _insert_pending_node(async_session, node_id, f"SpotTestNode-{suffix}")

    try:
        review_resp = await api_client.post(
            f"/api/v1/spoke/common/ontogen/result/node/{node_id}/method/review",
            headers=admin_headers,
            json={"verdict": "approve", "reason": "spot-test approval"},
        )
        assert review_resp.status_code == 200, review_resp.text
        body = review_resp.json()
        assert body["status"] == "approved"
        assert body["id"] == node_id
    finally:
        await _delete_row(async_session, OntogenNode, node_id)


@pytest.mark.asyncio
async def test_ontogen_edge_review_approve(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """POST edge/{id}/method/review with 'approve' transitions status to approved."""
    from src.shared.db.models import OntogenEdge

    suffix = uuid.uuid4().hex[:8]
    edge_id = f"spot-edge-{suffix}"
    await _insert_pending_edge(async_session, edge_id, f"spot_test_edge_{suffix}")

    try:
        review_resp = await api_client.post(
            f"/api/v1/spoke/common/ontogen/result/edge/{edge_id}/method/review",
            headers=admin_headers,
            json={"verdict": "approve", "reason": "spot-test edge approval"},
        )
        assert review_resp.status_code == 200, review_resp.text
        assert review_resp.json()["status"] == "approved"
    finally:
        await _delete_row(async_session, OntogenEdge, edge_id)


@pytest.mark.asyncio
async def test_ontogen_triple_review_dependency_order(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """Triple review on un-approved deps returns 422; approving deps first then triple succeeds."""
    from src.shared.db.models import OntogenEdge, OntogenNode, OntogenTriple

    suffix = uuid.uuid4().hex[:8]
    subj_id = f"spot-subj-{suffix}"
    obj_id = f"spot-obj-{suffix}"
    edge_id = f"spot-tedge-{suffix}"

    await _insert_pending_node(async_session, subj_id, f"SpotSubject-{suffix}")
    await _insert_pending_node(async_session, obj_id, f"SpotObject-{suffix}")
    await _insert_pending_edge(async_session, edge_id, f"spot_triple_edge_{suffix}")
    triple_id = await _insert_pending_triple(
        async_session, subject_id=subj_id, edge_id=edge_id, object_id=obj_id
    )

    try:
        # Step 1: triple approve must fail because deps are pending
        deny_resp = await api_client.post(
            f"/api/v1/spoke/common/ontogen/result/triple/{triple_id}/method/review",
            headers=admin_headers,
            json={"verdict": "approve", "reason": "spot-test triple approval"},
        )
        assert deny_resp.status_code == 422, deny_resp.text
        assert "ONTOGEN_TRIPLE_DEPENDENCY_PENDING" in str(deny_resp.json())

        # Step 2: approve subject, object, and edge — each via REST
        for nid in (subj_id, obj_id):
            r = await api_client.post(
                f"/api/v1/spoke/common/ontogen/result/node/{nid}/method/review",
                headers=admin_headers,
                json={"verdict": "approve", "reason": "spot-test"},
            )
            assert r.status_code == 200, r.text
        r = await api_client.post(
            f"/api/v1/spoke/common/ontogen/result/edge/{edge_id}/method/review",
            headers=admin_headers,
            json={"verdict": "approve", "reason": "spot-test"},
        )
        assert r.status_code == 200, r.text

        # Step 3: triple approve now succeeds because all deps are approved
        ok_resp = await api_client.post(
            f"/api/v1/spoke/common/ontogen/result/triple/{triple_id}/method/review",
            headers=admin_headers,
            json={"verdict": "approve", "reason": "spot-test triple approval"},
        )
        assert ok_resp.status_code == 200, ok_resp.text
        assert ok_resp.json()["status"] == "approved"
    finally:
        # Cleanup (triple FKs cascade — delete triple first, then deps)
        await _delete_row(async_session, OntogenTriple, triple_id)
        await _delete_row(async_session, OntogenNode, subj_id)
        await _delete_row(async_session, OntogenNode, obj_id)
        await _delete_row(async_session, OntogenEdge, edge_id)
