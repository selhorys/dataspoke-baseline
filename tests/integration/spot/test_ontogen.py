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
- POST /spoke/common/ontogen/method/run — dry-run emits ONTOGEN.RUN_COMPLETE with
    dry_run=true in detail; counts match response body
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
- POST /spoke/common/ontogen/method/run — 409 ONTOGEN_DISABLED when is_enabled=False;
    no ONTOGEN.RUN_COMPLETE event emitted on rejected call
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
- spec/feature/BACKEND.md L661 — RUN_COMPLETE emitted for dry-run and non-dry-run;
    dry_run flag in detail
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
    """POST /spoke/common/ontogen/method/run?dry_run=true returns OntogenRunSummary body
    and emits exactly one ONTOGEN.RUN_COMPLETE event with dry_run=true in detail.

    Spec: spec/feature/BACKEND.md §Ontology Generation Service §Inference Pipeline
    — ?dry_run=true evaluates steps 2–8 without persisting; returns OntogenRunSummary
    with status (str), dry_run (bool), unresolved_urns (list), counts (dict).
    Spec: spec/feature/BACKEND.md L661 — RUN_COMPLETE recorded for both dry-run and
    non-dry-run; dry_run flag in detail.
    """
    event_url = "/api/v1/spoke/common/ontogen/event"

    # Snapshot count of existing ONTOGEN.RUN_COMPLETE events before the POST
    pre_resp = await api_client.get(
        f"{event_url}?limit=100",
        headers=admin_headers,
    )
    assert pre_resp.status_code == 200, pre_resp.text
    pre_events = pre_resp.json()["events"]
    pre_count = sum(1 for e in pre_events if e["event_type"] == "ONTOGEN.RUN_COMPLETE")

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

    # Assert exactly one new ONTOGEN.RUN_COMPLETE event was emitted
    # spec: BACKEND.md L661 — RUN_COMPLETE recorded for dry-run; dry_run flag in detail
    post_resp = await api_client.get(
        f"{event_url}?limit=100",
        headers=admin_headers,
    )
    assert post_resp.status_code == 200, post_resp.text
    post_events = post_resp.json()["events"]
    run_complete_events = [e for e in post_events if e["event_type"] == "ONTOGEN.RUN_COMPLETE"]
    post_count = len(run_complete_events)

    assert post_count == pre_count + 1, (
        f"Expected exactly one new ONTOGEN.RUN_COMPLETE event after dry-run; "
        f"pre_count={pre_count}, post_count={post_count}. "
        "spec: BACKEND.md L661 — dry-run must emit RUN_COMPLETE"
    )

    # The newest event is first (ordered by occurred_at desc)
    new_event = run_complete_events[0]
    assert new_event["detail"].get("dry_run") is True, (
        f"ONTOGEN.RUN_COMPLETE event detail must carry dry_run=true; "
        f"got detail={new_event['detail']!r}. "
        "spec: BACKEND.md L661 — dry_run flag in detail"
    )
    # counts in event detail must match the response body's counts field exactly
    # spec: BACKEND.md L661 — RUN_COMPLETE payload carries counts
    assert new_event["detail"].get("counts") == body["counts"], (
        f"Event detail counts={new_event['detail'].get('counts')!r} must match "
        f"response counts={body['counts']!r}. "
        "spec: BACKEND.md L661 — event and response must agree on counts"
    )
    # unresolved_urns in event detail must match the response body's unresolved_urns
    # spec: BACKEND.md L661 — unresolved_urns is part of the RUN_COMPLETE payload
    assert new_event["detail"].get("unresolved_urns") == body["unresolved_urns"], (
        f"Event detail unresolved_urns={new_event['detail'].get('unresolved_urns')!r} must "
        f"match response unresolved_urns={body['unresolved_urns']!r}. "
        "spec: BACKEND.md L661 — event and response must agree on unresolved_urns"
    )


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

    # status='llm_pending': LLM created this node, no human has reviewed it yet.
    session.add(
        OntogenNode(
            id=node_id,
            name=name,
            description="Spot test ontogen node.",
            confidence_score=0.85,
            status="llm_pending",
            evidence={"source": "spot-test"},
        )
    )
    await session.commit()


async def _insert_pending_edge(session: AsyncSession, edge_id: str, label: str) -> None:
    from src.shared.db.models import OntogenEdge

    # status='llm_pending': LLM created this edge, no human has reviewed it yet.
    session.add(
        OntogenEdge(
            id=edge_id,
            label=label,
            semantics="Spot test ontogen edge.",
            confidence_score=0.85,
            status="llm_pending",
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

    # status='llm_pending': LLM created this triple, no human has reviewed it yet.
    triple_id = f"{subject_id}__{edge_id}__{object_id}"
    session.add(
        OntogenTriple(
            id=triple_id,
            subject_node_id=subject_id,
            edge_id=edge_id,
            object_node_id=object_id,
            confidence_score=0.85,
            status="llm_pending",
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
    node_id = f"spot_node_{suffix}"
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
    edge_id = f"spot_edge_{suffix}"
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
    """Triple dep-gate strict: llm_approved deps block triple approve (422); human-approved deps allow it.

    Deps seeded as 'llm_approved' (LLM Reviewer accepted, high confidence — but no human review
    yet).  The strict dep-gate (status='approved' only) must reject the triple approve with
    422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING.  Once each dep is human-approved via REST, the gate
    passes and the triple transitions to 'approved'.

    Spec: plan §7 — strict dep-gate: human approval of a triple requires human-approved deps.
    Spec: USE_CASE_en.md §UC3 L350-L356 — triple cannot be approved unless its subject node,
    edge, and object node are all approved.
    """
    from src.shared.db.models import OntogenEdge, OntogenNode, OntogenTriple

    suffix = uuid.uuid4().hex[:8]
    subj_id = f"spot_subj_{suffix}"
    obj_id = f"spot_obj_{suffix}"
    edge_id = f"spot_tedge_{suffix}"

    # Seed deps as 'llm_approved' — LLM accepted + high confidence, but no human has reviewed yet.
    # The strict dep-gate must block triple approve until a human approves each dep.
    async_session.add(OntogenNode(
        id=subj_id,
        name=f"SpotSubject-{suffix}",
        description="Spot test subject node.",
        confidence_score=0.95,
        status="llm_approved",
        evidence={"source": "spot-test"},
    ))
    async_session.add(OntogenNode(
        id=obj_id,
        name=f"SpotObject-{suffix}",
        description="Spot test object node.",
        confidence_score=0.95,
        status="llm_approved",
        evidence={"source": "spot-test"},
    ))
    async_session.add(OntogenEdge(
        id=edge_id,
        label=f"spot_triple_edge_{suffix}",
        semantics="Spot test triple edge.",
        confidence_score=0.95,
        status="llm_approved",
        evidence={"source": "spot-test"},
    ))
    await async_session.commit()

    triple_id = await _insert_pending_triple(
        async_session, subject_id=subj_id, edge_id=edge_id, object_id=obj_id
    )

    try:
        # Step 1: triple approve must fail because deps are only llm_approved (not human-approved)
        # spec: plan §7 — strict gate: status='approved' only passes; llm_approved blocks
        deny_resp = await api_client.post(
            f"/api/v1/spoke/common/ontogen/result/triple/{triple_id}/method/review",
            headers=admin_headers,
            json={"verdict": "approve", "reason": "spot-test triple approval"},
        )
        assert deny_resp.status_code == 422, (
            f"Expected 422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING when deps are llm_approved; "
            f"got {deny_resp.status_code}: {deny_resp.text}. "
            "Spec: plan §7 — strict gate blocks llm_approved deps"
        )
        assert deny_resp.json().get("error_code") == "ONTOGEN_TRIPLE_DEPENDENCY_PENDING", (
            f"Expected error_code ONTOGEN_TRIPLE_DEPENDENCY_PENDING; got {deny_resp.json()!r}"
        )

        # Step 2: human-approve subject, object, and edge — each via REST
        # After human approval, status transitions llm_approved → approved (human sets it).
        for nid in (subj_id, obj_id):
            r = await api_client.post(
                f"/api/v1/spoke/common/ontogen/result/node/{nid}/method/review",
                headers=admin_headers,
                json={"verdict": "approve", "reason": "spot-test"},
            )
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "approved", (
                f"Node {nid!r} must be 'approved' after human review; got {r.json()['status']!r}"
            )
        r = await api_client.post(
            f"/api/v1/spoke/common/ontogen/result/edge/{edge_id}/method/review",
            headers=admin_headers,
            json={"verdict": "approve", "reason": "spot-test"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "approved"

        # Step 3: triple approve now succeeds because all deps are human-approved
        # spec: plan §7 — gate passes when status='approved' for all deps
        ok_resp = await api_client.post(
            f"/api/v1/spoke/common/ontogen/result/triple/{triple_id}/method/review",
            headers=admin_headers,
            json={"verdict": "approve", "reason": "spot-test triple approval"},
        )
        assert ok_resp.status_code == 200, (
            f"Triple approve must succeed after deps are human-approved; "
            f"got {ok_resp.status_code}: {ok_resp.text}. "
            "Spec: USE_CASE_en.md §UC3 L462-L468"
        )
        assert ok_resp.json()["status"] == "approved", (
            f"Triple status must be 'approved'; got {ok_resp.json().get('status')!r}"
        )
    finally:
        # Cleanup (triple FKs cascade — delete triple first, then deps)
        await _delete_row(async_session, OntogenTriple, triple_id)
        await _delete_row(async_session, OntogenNode, subj_id)
        await _delete_row(async_session, OntogenNode, obj_id)
        await _delete_row(async_session, OntogenEdge, edge_id)


# ── New-boundary + negative-coverage tests ─────────────────────────────────────


@pytest.mark.asyncio
async def test_ontogen_run_is_enabled_false_non_dry_run_returns_409_ONTOGEN_DISABLED(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST /method/run (no dry_run) with is_enabled=False returns 409 ONTOGEN_DISABLED;
    dry-run with the same conf returns 200; no ONTOGEN.RUN_COMPLETE event on rejected run.

    spec: USE_CASE_en.md L541 — 'When is_enabled=false, non-dry-run calls to
    method/run return 409 ONTOGEN_DISABLED. Dry-run (?dry_run=true) is always
    permitted regardless of is_enabled.'
    spec: BACKEND.md L661 — RUN_COMPLETE is emitted only when the run completes;
    a rejected (409) call must not emit it.
    """
    conf_url = "/api/v1/spoke/common/ontogen/attr/conf"
    event_url = "/api/v1/spoke/common/ontogen/event"

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

        # Snapshot ONTOGEN.RUN_COMPLETE count before the rejected call
        pre_resp = await api_client.get(f"{event_url}?limit=100", headers=admin_headers)
        assert pre_resp.status_code == 200, pre_resp.text
        pre_run_complete_count = sum(
            1 for e in pre_resp.json()["events"]
            if e["event_type"] == "ONTOGEN.RUN_COMPLETE"
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

        # Negative-parity: no ONTOGEN.RUN_COMPLETE event must have been emitted
        # spec: BACKEND.md L661 — event is for completed runs only; rejected calls are not runs
        post_resp = await api_client.get(f"{event_url}?limit=100", headers=admin_headers)
        assert post_resp.status_code == 200, post_resp.text
        post_run_complete_count = sum(
            1 for e in post_resp.json()["events"]
            if e["event_type"] == "ONTOGEN.RUN_COMPLETE"
        )
        assert post_run_complete_count == pre_run_complete_count, (
            f"No new ONTOGEN.RUN_COMPLETE event must be emitted after a 409-rejected run; "
            f"pre={pre_run_complete_count}, post={post_run_complete_count}. "
            "spec: BACKEND.md L661"
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
    node_id = f"spot_rej_node_{suffix}"
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
    edge_id = f"spot_rej_edge_{suffix}"
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
    node_id = f"spot_ev_node_{suffix}"

    session = async_session
    from src.shared.db.models import OntogenNode as _OntogenNode

    # status='llm_pending': LLM created this node, awaiting human review — sufficient for
    # evidence shape check (the endpoint returns evidence regardless of review status).
    session.add(
        _OntogenNode(
            id=node_id,
            name=f"SpotEvidenceNode-{suffix}",
            description="Spot test node for evidence shape check.",
            confidence_score=0.91,
            status="llm_pending",
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


# ── Adversarial Debate Framework tests ────────────────────────────────────────
# Spec: spec/feature/BACKEND_LLM.md §Adversarial Debate Framework §Evidence shape


@pytest.mark.asyncio
async def test_ontogen_node_detail_round_trips_evidence_debate(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """GET /result/node/{id}/attr returns the full debate sub-tree stored in evidence JSONB.

    Seeds an ontogen_nodes row with a structured evidence dict containing a 'debate'
    sub-tree and asserts the REST endpoint returns every field intact.

    Spec: spec/feature/BACKEND_LLM.md §Adversarial Debate Framework §Evidence shape —
    evidence JSONB stores: source, run_id, debate.{turns_completed, outcome,
    final_reviewer_verdict, rag_anchors, history[{turn, actor, ...}]}.
    Spec: spec/feature/BACKEND_SCHEMA.md §ontogen_nodes — evidence JSONB column.
    Route: GET /result/node/{node_id}/attr → NodeAttrResponse {node_id, confidence_score,
    evidence}.
    """
    from src.shared.db.models import OntogenNode as _OntogenNode

    suffix = uuid.uuid4().hex[:8]
    node_id = f"spot_debate_{suffix}"

    seeded_evidence = {
        "source": "spot-test",
        "run_id": "spot-test-debate",
        "debate": {
            "turns_completed": 2,
            "outcome": "accept",
            "final_reviewer_verdict": "accept",
            "rag_anchors": [],
            "history": [
                {"turn": 0, "actor": "producer", "candidate_hash": "deadbeef"},
                {"turn": 1, "actor": "reviewer", "verdict": "accept"},
            ],
        },
    }

    # status='llm_pending': LLM created this node, awaiting human review — the evidence
    # round-trip test only needs a readable row; review status doesn't affect the /attr route.
    async_session.add(
        _OntogenNode(
            id=node_id,
            name=f"SpotDebateNode-{suffix}",
            description="Spot test node for debate evidence round-trip.",
            confidence_score=0.85,
            status="llm_pending",
            evidence=seeded_evidence,
        )
    )
    await async_session.commit()

    try:
        # ── GET /result/node/{node_id}/attr ────────────────────────────────────
        # spec: BACKEND_LLM.md §Evidence shape — evidence round-trips through JSONB
        get_resp = await api_client.get(
            f"/api/v1/spoke/common/ontogen/result/node/{node_id}/attr",
            headers=admin_headers,
        )
        assert get_resp.status_code == 200, get_resp.text
        body = get_resp.json()

        evidence = body.get("evidence")
        assert isinstance(evidence, dict), (
            f"evidence must be a dict; got {type(evidence)!r}. "
            "spec: BACKEND_SCHEMA.md §ontogen_nodes — evidence JSONB"
        )

        # ── Assert top-level evidence fields ──────────────────────────────────
        # spec: BACKEND_LLM.md §Evidence shape — source and run_id fields
        assert evidence.get("source") == "spot-test", (
            f"evidence['source'] must be 'spot-test'; got {evidence.get('source')!r}. "
            "spec: BACKEND_LLM.md §Evidence shape"
        )
        assert evidence.get("run_id") == "spot-test-debate", (
            f"evidence['run_id'] must be 'spot-test-debate'; got {evidence.get('run_id')!r}. "
            "spec: BACKEND_LLM.md §Evidence shape"
        )

        # ── Assert debate sub-tree ─────────────────────────────────────────────
        # spec: BACKEND_LLM.md §Evidence shape — debate.{turns_completed, outcome,
        # final_reviewer_verdict, rag_anchors, history}
        debate = evidence.get("debate")
        assert isinstance(debate, dict), (
            f"evidence['debate'] must be a dict; got {type(debate)!r}. "
            "spec: BACKEND_LLM.md §Evidence shape"
        )
        assert debate.get("turns_completed") == 2, (
            f"debate['turns_completed'] must be 2; got {debate.get('turns_completed')!r}. "
            "spec: BACKEND_LLM.md §Evidence shape"
        )
        assert debate.get("outcome") == "accept", (
            f"debate['outcome'] must be 'accept'; got {debate.get('outcome')!r}. "
            "spec: BACKEND_LLM.md §Evidence shape"
        )
        assert debate.get("final_reviewer_verdict") == "accept", (
            f"debate['final_reviewer_verdict'] must be 'accept'; "
            f"got {debate.get('final_reviewer_verdict')!r}. "
            "spec: BACKEND_LLM.md §Evidence shape"
        )

        # ── Assert history list ────────────────────────────────────────────────
        # spec: BACKEND_LLM.md §Evidence shape — history is ordered list of turns
        history = debate.get("history")
        assert isinstance(history, list), (
            f"debate['history'] must be a list; got {type(history)!r}. "
            "spec: BACKEND_LLM.md §Evidence shape — history: list of turns"
        )
        assert len(history) == 2, (
            f"debate['history'] must have 2 entries; got {len(history)}. "
            "spec: BACKEND_LLM.md §Evidence shape"
        )
        assert history[0].get("actor") == "producer", (
            f"history[0]['actor'] must be 'producer'; got {history[0].get('actor')!r}. "
            "spec: BACKEND_LLM.md §Evidence shape — turn 0 is Producer"
        )
        assert history[1].get("actor") == "reviewer", (
            f"history[1]['actor'] must be 'reviewer'; got {history[1].get('actor')!r}. "
            "spec: BACKEND_LLM.md §Evidence shape — turn 1 is Reviewer"
        )

    finally:
        await _delete_row(async_session, _OntogenNode, node_id)


# ── New regression tests (plan §9) ────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("dep_status", ["llm_pending", "llm_approved"])
async def test_triple_dep_gate_blocks_when_deps_not_human_approved(
    dep_status: str,
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """Triple review approve returns 422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING when deps are not human-approved.

    Parametrized over dep_status in ['llm_pending', 'llm_approved'] — both LLM-only
    states must be blocked by the strict dep-gate.  This catches regressions where
    the gate is accidentally relaxed to 'status != rejected' instead of 'status = approved'.

    Spec: plan §9 — test_triple_dep_gate_blocks_when_deps_not_human_approved.
    Spec: plan §7 (strict dep-gate) — 'status=approved only; human approval of a triple
    requires human-approved dependencies.'
    """
    from src.shared.db.models import OntogenEdge, OntogenNode, OntogenTriple

    suffix = uuid.uuid4().hex[:8]
    subj_id = f"reg_subj_{suffix}"
    obj_id = f"reg_obj_{suffix}"
    edge_id = f"reg_tedge_{suffix}"

    # Seed all deps with dep_status (either 'llm_pending' or 'llm_approved').
    # Neither is sufficient for the strict dep-gate — only human 'approved' passes.
    async_session.add(OntogenNode(
        id=subj_id,
        name=f"RegSubject-{suffix}",
        description="Regression test subject node.",
        confidence_score=0.95,
        status=dep_status,
        evidence={"source": "regression-test"},
    ))
    async_session.add(OntogenNode(
        id=obj_id,
        name=f"RegObject-{suffix}",
        description="Regression test object node.",
        confidence_score=0.95,
        status=dep_status,
        evidence={"source": "regression-test"},
    ))
    async_session.add(OntogenEdge(
        id=edge_id,
        label=f"reg_edge_{suffix}",
        semantics="Regression test edge.",
        confidence_score=0.95,
        status=dep_status,
        evidence={"source": "regression-test"},
    ))
    await async_session.commit()

    triple_id = f"{subj_id}__{edge_id}__{obj_id}"
    async_session.add(OntogenTriple(
        id=triple_id,
        subject_node_id=subj_id,
        edge_id=edge_id,
        object_node_id=obj_id,
        confidence_score=0.95,
        status=dep_status,
        evidence={"source": "regression-test"},
    ))
    await async_session.commit()

    try:
        # POST triple review approve — must be blocked because deps are not human-approved
        resp = await api_client.post(
            f"/api/v1/spoke/common/ontogen/result/triple/{triple_id}/method/review",
            headers=admin_headers,
            json={"verdict": "approve", "reason": f"regression: dep-gate blocks {dep_status} deps"},
        )
        assert resp.status_code == 422, (
            f"Expected 422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING when all deps are {dep_status!r}; "
            f"got {resp.status_code}: {resp.text}. "
            "Spec: plan §9 — neither llm_pending nor llm_approved is sufficient for the strict dep-gate."
        )
        assert resp.json().get("error_code") == "ONTOGEN_TRIPLE_DEPENDENCY_PENDING", (
            f"Expected error_code ONTOGEN_TRIPLE_DEPENDENCY_PENDING; got {resp.json()!r}. "
            "Spec: plan §7 — strict dep-gate: human 'approved' required, not {dep_status!r}."
        )
    finally:
        await _delete_row(async_session, OntogenTriple, triple_id)
        await _delete_row(async_session, OntogenNode, subj_id)
        await _delete_row(async_session, OntogenNode, obj_id)
        await _delete_row(async_session, OntogenEdge, edge_id)


@pytest.mark.asyncio
async def test_triple_dep_gate_passes_when_deps_are_human_approved(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """Triple review approve returns 200 and status='approved' when all deps are human-approved.

    Regression test: seeding deps directly as 'approved' (simulating a prior human approval)
    must allow the triple's dep-gate to pass immediately.

    Spec: plan §9 — test_triple_dep_gate_passes_when_deps_are_human_approved.
    Spec: plan §7 (strict dep-gate) — 'status=approved' passes the gate.
    """
    from src.shared.db.models import OntogenEdge, OntogenNode, OntogenTriple

    suffix = uuid.uuid4().hex[:8]
    subj_id = f"appr_subj_{suffix}"
    obj_id = f"appr_obj_{suffix}"
    edge_id = f"appr_tedge_{suffix}"

    # Seed all deps as 'approved' — simulates that a human already approved them earlier.
    async_session.add(OntogenNode(
        id=subj_id,
        name=f"ApprSubject-{suffix}",
        description="Human-approved subject node.",
        confidence_score=0.95,
        status="approved",
        evidence={"source": "regression-test"},
    ))
    async_session.add(OntogenNode(
        id=obj_id,
        name=f"ApprObject-{suffix}",
        description="Human-approved object node.",
        confidence_score=0.95,
        status="approved",
        evidence={"source": "regression-test"},
    ))
    async_session.add(OntogenEdge(
        id=edge_id,
        label=f"appr_edge_{suffix}",
        semantics="Human-approved edge.",
        confidence_score=0.95,
        status="approved",
        evidence={"source": "regression-test"},
    ))
    await async_session.commit()

    triple_id = f"{subj_id}__{edge_id}__{obj_id}"
    async_session.add(OntogenTriple(
        id=triple_id,
        subject_node_id=subj_id,
        edge_id=edge_id,
        object_node_id=obj_id,
        confidence_score=0.95,
        status="llm_pending",
        evidence={"source": "regression-test"},
    ))
    await async_session.commit()

    try:
        # POST triple review approve — must succeed because all deps are human-approved
        resp = await api_client.post(
            f"/api/v1/spoke/common/ontogen/result/triple/{triple_id}/method/review",
            headers=admin_headers,
            json={"verdict": "approve", "reason": "regression: dep-gate passes human-approved deps"},
        )
        assert resp.status_code == 200, (
            f"Expected 200 when all deps are human-approved; "
            f"got {resp.status_code}: {resp.text}. "
            "Spec: plan §7 — gate passes when all deps have status='approved'."
        )
        assert resp.json().get("status") == "approved", (
            f"Triple status must be 'approved' after human review; "
            f"got {resp.json().get('status')!r}. "
            "Spec: plan §9 — human review endpoint writes 'approved' unconditionally."
        )

        # F5: DB-level confirmation that the persisted status is 'approved'.
        # The HTTP response body proves the API returned the right value; this check
        # proves the value was actually written to the DB (guards against response
        # serialisation diverging from what was committed).
        from sqlalchemy import select

        from src.shared.db.models import OntogenTriple as _OntogenTriple

        async_session.expire_all()  # force a fresh read from DB
        db_triple = (
            await async_session.execute(
                select(_OntogenTriple).where(_OntogenTriple.id == triple_id)
            )
        ).scalar_one_or_none()
        assert db_triple is not None, (
            f"Triple {triple_id!r} not found in DB after review approve."
        )
        assert db_triple.status == "approved", (
            f"Persisted triple status must be 'approved' after human review; "
            f"got {db_triple.status!r}. "
            "Spec: plan §7 — review endpoint persists 'approved' to DB."
        )
    finally:
        await _delete_row(async_session, OntogenTriple, triple_id)
        await _delete_row(async_session, OntogenNode, subj_id)
        await _delete_row(async_session, OntogenNode, obj_id)
        await _delete_row(async_session, OntogenEdge, edge_id)
