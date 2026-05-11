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
- POST /spoke/common/ontogen/method/run — dry-run with seeded documents exercises
    _fetch_documents_for_dataset evidence path (document relatedAssets filter)
- GET /spoke/common/ontogen/result/node — list nodes (envelope)
- GET /spoke/common/ontogen/result/edge — list edges (envelope)
- GET /spoke/common/ontogen/result/triple — list triples (envelope)
- POST /spoke/common/ontogen/result/node/{node_id}/method/review — approve node
- POST /spoke/common/ontogen/result/node/{node_id}/method/review — reject node
- POST /spoke/common/ontogen/result/edge/{edge_id}/method/review — approve edge
- POST /spoke/common/ontogen/result/edge/{edge_id}/method/review — reject edge
- POST /spoke/common/ontogen/result/triple/{triple_id}/method/review — approve triple
- POST /spoke/common/ontogen/method/run — 409 ONTOGEN_DISABLED when is_enabled=False
- GET /spoke/common/ontogen/result/node/{id}/attr — confidence_score (float) and evidence (dict)

NOTE: node_embeddings sync is verified at the DAG/run-level integration test, not on
per-node REST review approval.  spec/feature/BACKEND_SCHEMA.md §node_embeddings lists
DAG and on-demand inference runs as the ONLY sync triggers — the review endpoint is
not a trigger.

NOTE: AGE graph materialisation is verified at the run-level integration test.
spec/feature/BACKEND.md L397-398 mandates AGE persistence as part of the inference
pipeline (step 9); the triple-approve REST endpoint best-effort materialises, which is
not spec-mandated per BACKEND.md L411-414.

Spec traceability:
- spec/feature/BACKEND.md §Ontology Generation Service §Inference Pipeline
- spec/feature/BACKEND.md §Approval flow (node/edge/triple review)
- spec/feature/BACKEND_SCHEMA.md §Graph (ontogen_triples → AGE)
- spec/feature/BACKEND_SCHEMA.md §node_embeddings (pgvector)
- spec/USE_CASE_en.md §UC3 §Inputs — document evidence read path
- spec/USE_CASE_en.md L541 — ONTOGEN_DISABLED on non-dry run with is_enabled=False
- spec/DATAHUB_INTEGRATION.md §Document Aspects — relatedAssets discovery filter
- spec/DATAHUB_INTEGRATION.md L114 — UC3 direction is Read-only
"""

import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

# Declare DataHub fixture dependency so module_dummy_data ingests catalog.title_master
# into DataHub before tests that seed NATIVE documents (evidence path tests).
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})


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
    # spec: USE_CASE_en.md §UC3 L389 — OntogenConfResponse fields: is_enabled, schedule_tier,
    # dataset_filter, default_run_prompt, updated_at (max_manual/system_queries removed)
    assert "is_enabled" in body
    assert "schedule_tier" in body
    assert "dataset_filter" in body
    assert "default_run_prompt" in body
    assert "updated_at" in body


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
        },
    )

    patch_resp = await api_client.patch(
        "/api/v1/spoke/common/ontogen/attr/conf",
        headers=admin_headers,
        json={"schedule_tier": "weekly"},
    )

    assert patch_resp.status_code == 200
    body = patch_resp.json()
    assert body["schedule_tier"] == "weekly"


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
    """POST /spoke/common/ontogen/method/run?dry_run=true returns OntogenRunSummary body.

    Spec: spec/feature/BACKEND.md §Ontology Generation Service §Inference Pipeline
    — ?dry_run=true evaluates steps 2–8 without persisting; returns OntogenRunSummary
    with status (str), dry_run (bool), unresolved_urns (list), counts (dict).
    """
    resp = await api_client.post(
        "/api/v1/spoke/common/ontogen/method/run?dry_run=true",
        headers=admin_headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    # Assert all OntogenRunSummary keys are present with correct types
    assert "status" in body and isinstance(body["status"], str)
    assert "dry_run" in body and isinstance(body["dry_run"], bool)
    assert "unresolved_urns" in body and isinstance(body["unresolved_urns"], list)
    assert "counts" in body and isinstance(body["counts"], dict)
    assert body["dry_run"] is True


@pytest.mark.asyncio
async def test_ontogen_run_dry_run_includes_seeded_documents_in_evidence(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """A dry-run inference reads documents whose relatedAssets reference an in-scope dataset.

    Spec: USE_CASE_en.md §UC3 §Inputs — 'documentInfo.contents.text on document entities
    whose relatedAssets reference an in-scope dataset (Markdown body by convention)'
    Spec: DATAHUB_INTEGRATION.md §Document Aspects — Discovery via searchAcrossEntities
    filtering on relatedAssets; cap at DOCUMENT_EVIDENCE_CAP_PER_DATASET.

    Steps:
      1. Seed two NATIVE documents whose relatedAssets include a known dev-env dataset URN.
      2. PUT ontogen conf with dataset_filter narrowed to that dataset URN.
      3. POST ?dry_run=true — assert the run completes without error (200, dry_run=True).
      4. Assert our dataset URN does NOT appear in unresolved_urns (evidence-gathering
         succeeded for that dataset, i.e. _fetch_documents_for_dataset did not raise).
      5. Cleanup: hard-delete both documents, restore conf to disabled.

    The stub LLM (DATASPOKE_TEST_MODE=true) returns no nodes/edges/triples, so we cannot
    assert ontology output.  The absence of our dataset URN from unresolved_urns is the
    proxy proof that the evidence path reached the document-fetch step without error.
    """
    from tests.integration.util.datahub import (
        get_datahub_token,
        hard_delete_document,
        seed_native_document,
    )

    # ── Dataset URN — catalog.title_master is the canonical UC3 test dataset ──
    # spec: TESTING.md §Imazon Dummy-Data Reference — catalog.title_master is UC1, UC3
    dataset_urn = (
        "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
    )

    suffix = uuid.uuid4().hex[:12]
    doc1_id = f"spot-doc1-{suffix}"
    doc2_id = f"spot-doc2-{suffix}"
    doc1_urn: str | None = None
    doc2_urn: str | None = None

    conf_url = "/api/v1/spoke/common/ontogen/attr/conf"

    try:
        # ── Step 1: Seed two NATIVE documents whose relatedAssets include dataset_urn ──
        # spec: DATAHUB_INTEGRATION.md §Document Aspects — NATIVE source, relatedAssets shape
        token = get_datahub_token()
        doc1_urn = seed_native_document(
            document_id=doc1_id,
            title="Spot test doc 1 — catalog context",
            body_markdown="# Catalog Context\n\nDataspoke spot test seed document 1.",
            related_dataset_urns=[dataset_urn],
            token=token,
        )
        doc2_urn = seed_native_document(
            document_id=doc2_id,
            title="Spot test doc 2 — title master notes",
            body_markdown="# Title Master Notes\n\nDataspoke spot test seed document 2.",
            related_dataset_urns=[dataset_urn],
            token=token,
        )

        # ── Step 2: PUT ontogen conf narrowed to our dataset URN ─────────────────
        # spec: USE_CASE_en.md §UC3 L392-L398 — dataset_filter.dataset_urns narrows scope
        conf_resp = await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "is_enabled": True,
                "schedule_tier": "daily",
                "dataset_filter": {"dataset_urns": [dataset_urn]},
            },
        )
        assert conf_resp.status_code in (200, 201), (
            f"PUT ontogen conf failed: {conf_resp.status_code} {conf_resp.text}"
        )

        # ── Step 3: POST dry-run — must complete without error ─────────────────────
        # spec: USE_CASE_en.md §UC3 L415-L416 — dry_run=true evaluates without persisting
        # spec: DATAHUB_INTEGRATION.md §Document Aspects — relatedAssets discovery path
        dry_run_resp = await api_client.post(
            "/api/v1/spoke/common/ontogen/method/run?dry_run=true",
            headers=admin_headers,
        )
        assert dry_run_resp.status_code == 200, (
            f"POST dry-run failed: {dry_run_resp.status_code} {dry_run_resp.text}. "
            "spec: USE_CASE_en.md §UC3 §Run semantics — dry-run must succeed when document "
            "evidence is present (regression guard for _fetch_documents_for_dataset)"
        )
        body = dry_run_resp.json()

        # ── Step 4: Assert OntogenRunSummary shape and evidence success ───────────
        # spec: USE_CASE_en.md §UC3 — OntogenRunSummary: status, dry_run, unresolved_urns, counts
        assert "status" in body and isinstance(body["status"], str), (
            "OntogenRunSummary missing 'status'. spec: USE_CASE_en.md §UC3 §Run semantics"
        )
        assert body.get("dry_run") is True, (
            "OntogenRunSummary dry_run must be True. spec: USE_CASE_en.md §UC3 §Run semantics"
        )
        assert isinstance(body.get("unresolved_urns"), list), (
            "OntogenRunSummary missing 'unresolved_urns'. spec: USE_CASE_en.md §UC3"
        )
        assert isinstance(body.get("counts"), dict), (
            "OntogenRunSummary missing 'counts'. spec: USE_CASE_en.md §UC3"
        )
        # The dataset_filter pins to our dataset_urn; if evidence-gathering succeeded it
        # must NOT appear in unresolved_urns (which lists URNs that were skipped).
        # spec: USE_CASE_en.md §UC3 L396 — "entries that don't resolve … are skipped and
        # reported in the run-complete event's unresolved_urns field"
        assert dataset_urn not in body["unresolved_urns"], (
            f"dataset_urn {dataset_urn!r} found in unresolved_urns — evidence-gathering failed. "
            "spec: USE_CASE_en.md §UC3 §dataset_filter — seeded documents with matching "
            "relatedAssets must not cause the dataset to be unresolvable. "
            "Regression: _fetch_documents_for_dataset may have raised or returned wrong filter."
        )

    finally:
        # ── Step 5: Cleanup — hard-delete documents, restore conf ─────────────────
        if doc1_urn is not None:
            try:
                hard_delete_document(document_urn=doc1_urn, token=token)
            except Exception:
                pass
        if doc2_urn is not None:
            try:
                hard_delete_document(document_urn=doc2_urn, token=token)
            except Exception:
                pass
        await api_client.patch(conf_url, headers=admin_headers, json={"is_enabled": False})


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


async def _insert_pending_node(session: AsyncSession, node_id: str, name: str) -> None:
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


async def _insert_pending_edge(session: AsyncSession, edge_id: str, label: str) -> None:
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


# ── New-boundary + negative-coverage tests ─────────────────────────────────────


@pytest.mark.asyncio
async def test_ontogen_run_disabled_returns_409(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST /method/run (no dry_run) with is_enabled=False returns 409 ONTOGEN_DISABLED;
    dry-run with the same conf returns 200.

    spec: USE_CASE_en.md L541 — 'When is_enabled=false, non-dry-run calls to
    method/run return 409 ONTOGEN_DISABLED. Dry-run (?dry_run=true) is always
    permitted regardless of is_enabled.'
    """
    conf_url = "/api/v1/spoke/common/ontogen/attr/conf"

    try:
        put_resp = await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "is_enabled": False,
                "schedule_tier": "daily",
                "dataset_filter": {},
            },
        )
        assert put_resp.status_code in (200, 201), (
            f"PUT ontogen conf failed: {put_resp.status_code} {put_resp.text}"
        )

        run_resp = await api_client.post(
            "/api/v1/spoke/common/ontogen/method/run",
            headers=admin_headers,
        )
        assert run_resp.status_code == 409, (
            f"Expected 409 ONTOGEN_DISABLED when is_enabled=False; "
            f"got {run_resp.status_code}: {run_resp.text}. "
            "spec: USE_CASE_en.md L541"
        )
        body = run_resp.json()
        assert body.get("error_code") == "ONTOGEN_DISABLED", (
            f"Expected error_code 'ONTOGEN_DISABLED'; got: {body}. "
            "spec: USE_CASE_en.md L541"
        )

        # Dry-run must still succeed when is_enabled=False — disabled gate is
        # scoped to non-dry-run only. spec: USE_CASE_en.md L541
        dry_resp = await api_client.post(
            "/api/v1/spoke/common/ontogen/method/run?dry_run=true",
            headers=admin_headers,
        )
        assert dry_resp.status_code == 200, (
            f"Dry-run must succeed even when is_enabled=False; "
            f"got {dry_resp.status_code}: {dry_resp.text}. "
            "spec: USE_CASE_en.md L541"
        )
    finally:
        await api_client.patch(conf_url, headers=admin_headers, json={"is_enabled": False})


@pytest.mark.asyncio
async def test_ontogen_node_review_reject(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """POST node/{id}/method/review with 'reject' transitions status to rejected.

    spec: spec/feature/BACKEND.md §Ontology Generation Service — 'verdict: reject →
    mark the result as rejected.'
    """
    from src.shared.db.models import OntogenNode

    suffix = uuid.uuid4().hex[:8]
    node_id = f"spot-rej-node-{suffix}"
    await _insert_pending_node(async_session, node_id, f"SpotRejectNode-{suffix}")

    try:
        review_resp = await api_client.post(
            f"/api/v1/spoke/common/ontogen/result/node/{node_id}/method/review",
            headers=admin_headers,
            json={"verdict": "reject", "reason": "spot-test rejection"},
        )
        assert review_resp.status_code == 200, review_resp.text
        body = review_resp.json()
        assert body["status"] == "rejected", (
            f"Expected status 'rejected'; got {body['status']!r}. "
            "spec: BACKEND.md §Ontology Generation Service §Approval flow"
        )
        assert body["id"] == node_id
    finally:
        await _delete_row(async_session, OntogenNode, node_id)


@pytest.mark.asyncio
async def test_ontogen_edge_review_reject(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """POST edge/{id}/method/review with 'reject' transitions status to rejected.

    spec: spec/feature/BACKEND.md §Ontology Generation Service — verdict=reject flow.
    """
    from src.shared.db.models import OntogenEdge

    suffix = uuid.uuid4().hex[:8]
    edge_id = f"spot-rej-edge-{suffix}"
    await _insert_pending_edge(async_session, edge_id, f"spot_reject_edge_{suffix}")

    try:
        review_resp = await api_client.post(
            f"/api/v1/spoke/common/ontogen/result/edge/{edge_id}/method/review",
            headers=admin_headers,
            json={"verdict": "reject", "reason": "spot-test edge rejection"},
        )
        assert review_resp.status_code == 200, review_resp.text
        body = review_resp.json()
        assert body["status"] == "rejected", (
            f"Expected status 'rejected'; got {body['status']!r}. "
            "spec: BACKEND.md §Ontology Generation Service §Approval flow"
        )
    finally:
        await _delete_row(async_session, OntogenEdge, edge_id)


@pytest.mark.asyncio
async def test_ontogen_node_detail_carries_evidence(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """GET /result/node/{id}/attr returns confidence_score (float >= 0) and evidence (dict).

    Shape-only assertion — does not pin specific values because evidence content is
    LLM-generated and varies.
    spec: spec/feature/BACKEND_SCHEMA.md §ontogen_nodes — confidence_score FLOAT NOT NULL,
    evidence JSONB.
    Route: src/api/routers/spoke/common/ontogen.py:373-384
      GET /result/node/{node_id}/attr → NodeAttrResponse {node_id, confidence_score, evidence}
    """
    from src.shared.db.models import OntogenNode

    suffix = uuid.uuid4().hex[:8]
    node_id = f"spot-ev-node-{suffix}"

    session = async_session
    from src.shared.db.models import OntogenNode as _OntogenNode

    session.add(
        _OntogenNode(
            id=node_id,
            name=f"SpotEvidenceNode-{suffix}",
            description="Spot test node for evidence shape check.",
            confidence_score=0.91,
            status="pending_review",
            evidence={"source": "spot-test", "datasets": ["urn:li:dataset:test"]},
        )
    )
    await session.commit()

    try:
        get_resp = await api_client.get(
            f"/api/v1/spoke/common/ontogen/result/node/{node_id}/attr",
            headers=admin_headers,
        )
        assert get_resp.status_code == 200, get_resp.text
        body = get_resp.json()
        # spec: BACKEND_SCHEMA.md §ontogen_nodes — confidence_score is FLOAT NOT NULL
        assert isinstance(body.get("confidence_score"), float), (
            f"confidence_score must be a float; got {type(body.get('confidence_score'))!r}. "
            "spec: BACKEND_SCHEMA.md §ontogen_nodes"
        )
        assert body["confidence_score"] >= 0, (
            f"confidence_score must be >= 0; got {body['confidence_score']!r}. "
            "spec: BACKEND_SCHEMA.md §ontogen_nodes"
        )
        # spec: BACKEND_SCHEMA.md §ontogen_nodes — evidence JSONB (shape check only)
        assert isinstance(body.get("evidence"), dict), (
            f"evidence must be a dict; got {type(body.get('evidence'))!r}. "
            "spec: BACKEND_SCHEMA.md §ontogen_nodes"
        )
    finally:
        await _delete_row(async_session, OntogenNode, node_id)


# UC3 read-only boundary is enforced structurally (no DataHub emit code paths in review
# handlers per `src/backend/ontogen/service.py`); regression coverage lives in unit tests.
