"""UC3 — Ontology Generation: end-to-end through public REST API.

Maps `spec/USE_CASE_en.md §UC3` paragraphs to executable steps. REST-only
per `spec/TESTING.md §Api-Wired Integration Tests`.

Tests in this module:
  - test_uc3_run_and_list: Conf PUT, seed POST, dry-run inference, list node/edge/triple
    envelopes, seed DELETE.
  - test_uc3_review_in_dependency_order: Seed pending rows via raw SQL, attempt triple
    approve before deps (expect 422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING), then approve
    nodes → edge → triple in dependency order.
  - test_uc3_run_dry_run_with_seeded_documents: Seed two NATIVE document entities whose
    relatedAssets reference an in-scope dataset, POST dry-run, assert evidence-gathering
    succeeds (dataset URN absent from unresolved_urns).
  - test_uc3_run_disabled_returns_409: PUT is_enabled=False, POST run (no dry_run),
    assert 409 ONTOGEN_DISABLED.
  - test_uc3_review_reject: Seed pending node, POST review verdict=reject, assert
    status='rejected'.
"""
# spec: USE_CASE_en.md §UC3

import json
import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: TCH002

# Declare fixture dependencies so module_dummy_data seeds all schemas + topics for
# cross-dataset ontology inference. spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset(
    {"catalog", "customers", "reviews", "orders", "shipping"}
)
DUMMY_DATA_DATAHUB_TOPICS: frozenset[str] = frozenset(
    {"imazon.orders.events", "imazon.shipping.updates"}
)

# ── Raw-SQL helpers for DB seeding (setup/teardown only — not in test bodies) ──
# These helpers use raw SQL so the test bodies remain import-free of src.shared.db.models,
# satisfying spec/TESTING.md §Api-Wired Integration Tests — "REST only in test body."


async def _seed_pending_node(session: AsyncSession, node_id: str, name: str) -> None:
    """Insert a pending_review node row via raw SQL."""
    from sqlalchemy import text

    await session.execute(
        text(
            "INSERT INTO dataspoke.ontogen_nodes"
            " (id, name, description, confidence_score, status, evidence)"
            " VALUES (:id, :name, :desc, :conf, 'pending_review', CAST(:ev AS jsonb))"
        ),
        {
            "id": node_id,
            "name": name,
            "desc": "api-wired UC3 seed",
            "conf": 0.85,
            "ev": json.dumps({"source": "api-wired-uc3"}),
        },
    )
    await session.commit()


async def _seed_pending_edge(session: AsyncSession, edge_id: str, label: str) -> None:
    """Insert a pending_review edge row via raw SQL."""
    from sqlalchemy import text

    await session.execute(
        text(
            "INSERT INTO dataspoke.ontogen_edges"
            " (id, label, semantics, confidence_score, status, evidence)"
            " VALUES (:id, :label, :semantics, :conf, 'pending_review', CAST(:ev AS jsonb))"
        ),
        {
            "id": edge_id,
            "label": label,
            "semantics": "api-wired UC3 edge semantics",
            "conf": 0.85,
            "ev": json.dumps({"source": "api-wired-uc3"}),
        },
    )
    await session.commit()


async def _seed_pending_triple(
    session: AsyncSession,
    *,
    subject_node_id: str,
    edge_id: str,
    object_node_id: str,
) -> str:
    """Insert a pending_review triple row via raw SQL; return composite triple ID."""
    from sqlalchemy import text

    triple_id = f"{subject_node_id}__{edge_id}__{object_node_id}"
    await session.execute(
        text(
            "INSERT INTO dataspoke.ontogen_triples"
            " (id, subject_node_id, edge_id, object_node_id,"
            "  confidence_score, status, evidence)"
            " VALUES (:id, :subj, :edge, :obj, :conf, 'pending_review', CAST(:ev AS jsonb))"
        ),
        {
            "id": triple_id,
            "subj": subject_node_id,
            "edge": edge_id,
            "obj": object_node_id,
            "conf": 0.85,
            "ev": json.dumps({"source": "api-wired-uc3"}),
        },
    )
    await session.commit()
    return triple_id


async def _delete_node(session: AsyncSession, node_id: str) -> None:
    from sqlalchemy import text

    await session.execute(
        text("DELETE FROM dataspoke.ontogen_nodes WHERE id = :id"),
        {"id": node_id},
    )
    await session.commit()


async def _delete_edge(session: AsyncSession, edge_id: str) -> None:
    from sqlalchemy import text

    await session.execute(
        text("DELETE FROM dataspoke.ontogen_edges WHERE id = :id"),
        {"id": edge_id},
    )
    await session.commit()


async def _delete_triple(session: AsyncSession, triple_id: str) -> None:
    from sqlalchemy import text

    await session.execute(
        text("DELETE FROM dataspoke.ontogen_triples WHERE id = :id"),
        {"id": triple_id},
    )
    await session.commit()


# ── Tests ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_uc3_run_and_list(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """UC3 narrative: 'DataSpoke autonomously infer the business concepts, the
    relationship types, and the specific facts that connect them across datasets,
    so that I can navigate datasets by concept.'

    Steps mirror USE_CASE_en.md §UC3:
      1. PUT singleton ontogen conf (is_enabled, schedule_tier, dataset_filter, seed params)
      2. POST a Markdown seed to steer inference
      3. POST dry-run — returns OntogenRunSummary (status, dry_run=true, unresolved_urns, counts)
      4. GET result/node, result/edge, result/triple — assert paginated envelopes
      5. DELETE the seed
      6. Cleanup — PATCH conf to disabled
    """
    conf_url = "/api/v1/spoke/common/ontogen/attr/conf"
    seed_url = "/api/v1/spoke/common/ontogen/attr/seed"

    seed_id: str | None = None

    try:
        # ── Step 1: PUT ontogen conf ──────────────────────────────────────────
        # UC3 narrative: "The governance team enables ontology generation."
        # spec: USE_CASE_en.md §UC3 L385-L398
        put_conf_resp = await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "is_enabled": True,
                "schedule_tier": "daily",
                "dataset_filter": {"tags": ["urn:li:tag:area:catalog"]},
            },
        )
        # spec: USE_CASE_en.md §UC3 L309-L317 — PUT conf returns 200 or 201
        assert put_conf_resp.status_code in (200, 201), (
            f"PUT ontogen conf failed: {put_conf_resp.status_code} {put_conf_resp.text}"
        )
        conf_body = put_conf_resp.json()
        assert conf_body["is_enabled"] is True
        assert conf_body["schedule_tier"] == "daily"
        # spec: USE_CASE_en.md §UC3 L309-L317 — round-trip must preserve dataset_filter
        assert conf_body["dataset_filter"] == {"tags": ["urn:li:tag:area:catalog"]}, (
            f"dataset_filter not preserved: {conf_body.get('dataset_filter')!r}. "
            "spec: USE_CASE_en.md §UC3 L309-L317"
        )
        # ── Step 2: POST a Markdown seed ──────────────────────────────────────
        # UC3 narrative: "They post a domain seed (Markdown) to steer the LLM toward
        # bookstore-friendly names."
        # spec: USE_CASE_en.md §UC3 L400-L409
        seed_md = (
            "# Imazon Bookstore Domain\n\n"
            "Imazon is an online bookstore. Treat *order* as a header concept and "
            "*order line* as the per-book row. Prefer business-friendly names over "
            "table names."
        )
        create_seed_resp = await api_client.post(
            seed_url,
            headers={**admin_headers, "content-type": "text/markdown"},
            content=seed_md.encode(),
        )
        assert create_seed_resp.status_code == 201, (
            f"POST seed failed: {create_seed_resp.status_code} {create_seed_resp.text}"
        )
        seed_id = create_seed_resp.json()["seed_id"]
        assert seed_id, "server must assign a seed_id"

        # List seeds — our seed_id must appear with preview and updated_at
        # spec: USE_CASE_en.md §UC3 L362 — seed list returns [{seed_id, preview, updated_at}]
        list_seed_resp = await api_client.get(seed_url, headers=admin_headers)
        assert list_seed_resp.status_code == 200
        seeds_by_id = {s["seed_id"]: s for s in list_seed_resp.json()["seeds"]}
        assert seed_id in seeds_by_id, f"seed_id {seed_id!r} not found in seed list after POST"
        seed_entry = seeds_by_id[seed_id]
        assert "preview" in seed_entry, (
            "seed list entry missing 'preview'. spec: USE_CASE_en.md §UC3 L362"
        )
        assert "updated_at" in seed_entry, (
            "seed list entry missing 'updated_at'. spec: USE_CASE_en.md §UC3 L362"
        )

        # ── Step 3: POST dry-run ──────────────────────────────────────────────
        # UC3 narrative: "?dry_run=true evaluates the inference and returns the
        # would-be node / edge / triple set without persisting changes."
        # spec: USE_CASE_en.md §UC3 L328-L330
        dry_run_resp = await api_client.post(
            "/api/v1/spoke/common/ontogen/method/run?dry_run=true",
            headers=admin_headers,
        )
        assert dry_run_resp.status_code == 200, (
            f"POST dry-run failed: {dry_run_resp.status_code} {dry_run_resp.text}"
        )
        dry_body = dry_run_resp.json()
        # spec: USE_CASE_en.md §UC3 — OntogenRunSummary shape
        assert "status" in dry_body and isinstance(dry_body["status"], str)
        assert "dry_run" in dry_body and isinstance(dry_body["dry_run"], bool)
        assert dry_body["dry_run"] is True
        assert "unresolved_urns" in dry_body and isinstance(dry_body["unresolved_urns"], list)
        assert "counts" in dry_body and isinstance(dry_body["counts"], dict)

        # ── Step 4: List envelopes for node, edge, triple ────────────────────
        # UC3 narrative: "Three nodes, two edges, two triples — all pending_review."
        # spec: USE_CASE_en.md §UC3 L421-L437
        for result_type, list_key in [
            ("node", "nodes"),
            ("edge", "edges"),
            ("triple", "triples"),
        ]:
            list_resp = await api_client.get(
                f"/api/v1/spoke/common/ontogen/result/{result_type}?offset=0&limit=10",
                headers=admin_headers,
            )
            assert list_resp.status_code == 200, (
                f"GET result/{result_type} failed: {list_resp.status_code}"
            )
            list_body = list_resp.json()
            # spec: API.md §Standard Envelope
            assert list_key in list_body
            assert "offset" in list_body
            assert "limit" in list_body
            assert "total_count" in list_body
            assert isinstance(list_body[list_key], list)
            # spec: API.md §Standard Envelope — offset and limit echo the request params
            assert list_body["offset"] == 0, (
                f"GET result/{result_type} offset expected 0; got {list_body['offset']!r}"
            )
            assert list_body["limit"] == 10, (
                f"GET result/{result_type} limit expected 10; got {list_body['limit']!r}"
            )
            assert isinstance(list_body["total_count"], int) and list_body["total_count"] >= 0, (
                f"GET result/{result_type} total_count must be non-negative int; "
                f"got {list_body['total_count']!r}"
            )

    finally:
        # ── Step 5: DELETE the seed ───────────────────────────────────────────
        if seed_id is not None:
            await api_client.delete(f"{seed_url}/{seed_id}", headers=admin_headers)
        # ── Step 6: Patch conf back to disabled ──────────────────────────────
        await api_client.patch(conf_url, headers=admin_headers, json={"is_enabled": False})


@pytest.mark.asyncio
async def test_uc3_review_in_dependency_order(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """UC3 narrative: 'A pending triple cannot be approved until both its endpoint
    nodes and its edge are approved; attempting to do so returns
    422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING. The reviewer therefore typically processes
    nodes → edges → triples.'

    Steps mirror USE_CASE_en.md §UC3 L350-L356 (review dependency) and L439-L468:
      a. Seed 2 pending nodes + 1 pending edge + 1 pending triple
      b. POST triple review/approve → 422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING
      c. Approve subject node, then object node, then edge — each via REST
      d. Approve triple → 200 with status == 'approved'
      e. Cleanup (triple first, then nodes/edge)
    """
    suffix = uuid.uuid4().hex[:8]
    subj_id = f"uc3-subj-{suffix}"
    obj_id = f"uc3-obj-{suffix}"
    edge_id = f"uc3-edge-{suffix}"
    triple_id: str | None = None

    try:
        # ── Step a: Seed pending rows ─────────────────────────────────────────
        # spec: TESTING.md §Api-Wired Integration Tests — "fixtures may reach into
        # tests.integration.util and may execute raw SQL against async_session for
        # setup/teardown only"
        await _seed_pending_node(async_session, subj_id, f"UcSubject-{suffix}")
        await _seed_pending_node(async_session, obj_id, f"UcObject-{suffix}")
        await _seed_pending_edge(async_session, edge_id, f"uc3_edge_{suffix}")
        triple_id = await _seed_pending_triple(
            async_session,
            subject_node_id=subj_id,
            edge_id=edge_id,
            object_node_id=obj_id,
        )

        # ── Step b: Triple approve must fail — deps are still pending ─────────
        # UC3 narrative: "attempting to [approve a triple before its deps] returns
        # 422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING"
        # spec: USE_CASE_en.md §UC3 L350-L356
        deny_resp = await api_client.post(
            f"/api/v1/spoke/common/ontogen/result/triple/{triple_id}/method/review",
            headers=admin_headers,
            json={"verdict": "approve", "reason": "uc3-api-wired: should fail"},
        )
        assert deny_resp.status_code == 422, (
            f"Expected 422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING; "
            f"got {deny_resp.status_code}: {deny_resp.text}. "
            "spec: USE_CASE_en.md §UC3 L350-L356"
        )
        deny_body = deny_resp.json()
        assert deny_body.get("error_code") == "ONTOGEN_TRIPLE_DEPENDENCY_PENDING", (
            f"Expected error_code 'ONTOGEN_TRIPLE_DEPENDENCY_PENDING'; got: {deny_body}. "
            "spec: USE_CASE_en.md §UC3 L350-L356"
        )

        # ── Step c: Approve subject node, then object node, then edge ─────────
        # UC3 narrative: "ORDER_LINE has the lowest node confidence … so the reviewer
        # starts with nodes … With the nodes approved, the reviewer moves to edges."
        # spec: USE_CASE_en.md §UC3 L439-L460
        subj_approve = await api_client.post(
            f"/api/v1/spoke/common/ontogen/result/node/{subj_id}/method/review",
            headers=admin_headers,
            json={"verdict": "approve", "reason": "uc3-api-wired: subject approved"},
        )
        assert subj_approve.status_code == 200, (
            f"Subject node approve failed: {subj_approve.status_code} {subj_approve.text}"
        )
        assert subj_approve.json()["status"] == "approved"

        obj_approve = await api_client.post(
            f"/api/v1/spoke/common/ontogen/result/node/{obj_id}/method/review",
            headers=admin_headers,
            json={"verdict": "approve", "reason": "uc3-api-wired: object approved"},
        )
        assert obj_approve.status_code == 200, (
            f"Object node approve failed: {obj_approve.status_code} {obj_approve.text}"
        )
        assert obj_approve.json()["status"] == "approved"

        edge_approve = await api_client.post(
            f"/api/v1/spoke/common/ontogen/result/edge/{edge_id}/method/review",
            headers=admin_headers,
            json={"verdict": "approve", "reason": "uc3-api-wired: edge approved"},
        )
        assert edge_approve.status_code == 200, (
            f"Edge approve failed: {edge_approve.status_code} {edge_approve.text}"
        )
        assert edge_approve.json()["status"] == "approved"

        # ── Step d: Triple approve now succeeds ───────────────────────────────
        # UC3 narrative: "Once both endpoint nodes and the edge of a triple are approved,
        # the triple becomes eligible for review."
        # spec: USE_CASE_en.md §UC3 L462-L468
        triple_approve = await api_client.post(
            f"/api/v1/spoke/common/ontogen/result/triple/{triple_id}/method/review",
            headers=admin_headers,
            json={"verdict": "approve", "reason": "uc3-api-wired: triple approved"},
        )
        assert triple_approve.status_code == 200, (
            f"Triple approve failed after deps approved: "
            f"{triple_approve.status_code} {triple_approve.text}. "
            "spec: USE_CASE_en.md §UC3 L462-L468"
        )
        assert triple_approve.json()["status"] == "approved", (
            f"Expected status 'approved'; got: {triple_approve.json()['status']}. "
            "spec: USE_CASE_en.md §UC3 L462-L468"
        )

    finally:
        # ── Step e: Cleanup — triple first (FK), then nodes and edge ─────────
        if triple_id is not None:
            await _delete_triple(async_session, triple_id)
        await _delete_node(async_session, subj_id)
        await _delete_node(async_session, obj_id)
        await _delete_edge(async_session, edge_id)


@pytest.mark.asyncio
async def test_uc3_run_dry_run_with_seeded_documents(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,  # noqa: ARG001
) -> None:
    """Documents whose relatedAssets reference an in-scope dataset are visible to UC3.

    Spec: USE_CASE_en.md §UC3 §Inputs — 'documentInfo.contents.text on document entities
    whose relatedAssets reference an in-scope dataset (Markdown body by convention)'
    Spec: DATAHUB_INTEGRATION.md §Document Aspects — relatedAssets discovery via
    searchAcrossEntities; DOCUMENT_EVIDENCE_CAP_PER_DATASET=10.

    Steps mirror USE_CASE_en.md §UC3 §Inputs narrative:
      a. Seed two NATIVE document entities whose relatedAssets include the target dataset URN.
      b. PUT ontogen conf with dataset_filter narrowed to that dataset URN.
      c. POST ?dry_run=true — assert 200, dry_run=True, unresolved_urns shape.
      d. Assert the seeded dataset URN does NOT appear in unresolved_urns — evidence-gathering
         completed successfully (proxy: _fetch_documents_for_dataset did not raise or skip).
      e. Cleanup: hard-delete both documents, restore conf to disabled.

    The stub LLM (DATASPOKE_TEST_MODE=true) returns no nodes/edges/triples; we assert on
    the response shape and unresolved_urns only — not on ontology output.
    If the api-wired tier uses a real LLM, `counts` shape assertions still hold: counts
    values must be integers >= 0 regardless of LLM output.
    """
    # Setup/teardown uses tests.integration.util — test body uses only REST.
    # spec: TESTING.md §Api-Wired Integration Tests — "Setup/teardown fixtures may use
    # tests.integration.util; the test itself stays REST-only."
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
    doc1_id = f"uc3-doc1-{suffix}"
    doc2_id = f"uc3-doc2-{suffix}"
    doc1_urn: str | None = None
    doc2_urn: str | None = None
    token: str = ""

    conf_url = "/api/v1/spoke/common/ontogen/attr/conf"

    try:
        # ── Step a: Seed two NATIVE documents referencing the target dataset ──────
        # spec: DATAHUB_INTEGRATION.md §Document Aspects — NATIVE source, relatedAssets shape
        # spec: USE_CASE_en.md §UC3 §Inputs — document bodies are Markdown by convention
        token = get_datahub_token()
        doc1_urn = seed_native_document(
            document_id=doc1_id,
            title="Catalog title master — overview",
            body_markdown=(
                "# Catalog Overview\n\n"
                "Imazon title_master holds one row per book title, keyed by ISBN-13. "
                "Source of truth for title, author, publisher, and list price."
            ),
            related_dataset_urns=[dataset_urn],
            token=token,
        )
        doc2_urn = seed_native_document(
            document_id=doc2_id,
            title="Catalog editorial notes",
            body_markdown=(
                "# Editorial Notes\n\n"
                "Editorial metadata curated by the Imazon catalog team — "
                "marketing blurbs, cover artwork sourcing, and genre taxonomy decisions."
            ),
            related_dataset_urns=[dataset_urn],
            token=token,
        )

        # ── Step b: PUT ontogen conf narrowed to our dataset URN ─────────────────
        # spec: USE_CASE_en.md §UC3 L392-L398 — dataset_filter.dataset_urns narrows scope;
        # URN format validated at PUT/PATCH time; unresolvable entries skipped at run time.
        put_conf_resp = await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "is_enabled": True,
                "schedule_tier": "daily",
                "dataset_filter": {"dataset_urns": [dataset_urn]},
            },
        )
        assert put_conf_resp.status_code in (200, 201), (
            f"PUT ontogen conf failed: {put_conf_resp.status_code} {put_conf_resp.text}. "
            "spec: USE_CASE_en.md §UC3 — conf PUT returns 200/201"
        )

        # ── Step c: POST dry-run — must return 200 with correct response shape ────
        # spec: USE_CASE_en.md §UC3 L415-L416 — dry_run=true evaluates without persisting
        # spec: DATAHUB_INTEGRATION.md §Document Aspects — discovery via relatedAssets filter
        dry_run_resp = await api_client.post(
            "/api/v1/spoke/common/ontogen/method/run?dry_run=true",
            headers=admin_headers,
        )
        assert dry_run_resp.status_code == 200, (
            f"POST dry-run failed: {dry_run_resp.status_code} {dry_run_resp.text}. "
            "spec: USE_CASE_en.md §UC3 — dry-run with document evidence present must succeed "
            "(regression guard: _fetch_documents_for_dataset must not raise on real documents)"
        )
        body = dry_run_resp.json()

        # ── Step d: Assert OntogenRunSummary shape ────────────────────────────────
        # spec: USE_CASE_en.md §UC3 — OntogenRunSummary: status, dry_run, unresolved_urns, counts
        assert "status" in body and isinstance(body["status"], str), (
            "OntogenRunSummary missing 'status' (str). spec: USE_CASE_en.md §UC3"
        )
        assert body.get("dry_run") is True, (
            "OntogenRunSummary dry_run must be True. spec: USE_CASE_en.md §UC3"
        )
        assert isinstance(body.get("unresolved_urns"), list), (
            "OntogenRunSummary missing 'unresolved_urns' (list). spec: USE_CASE_en.md §UC3"
        )
        assert isinstance(body.get("counts"), dict), (
            "OntogenRunSummary missing 'counts' (dict). spec: USE_CASE_en.md §UC3"
        )
        # counts values must be non-negative integers regardless of LLM output
        # spec: USE_CASE_en.md §UC3 — counts: nodes, edges, triples (shape check only)
        for key, value in body["counts"].items():
            assert isinstance(value, int) and value >= 0, (
                f"OntogenRunSummary counts[{key!r}] must be non-negative int; got {value!r}. "
                "spec: USE_CASE_en.md §UC3"
            )

        # The dataset_filter pins to our dataset_urn. If evidence-gathering succeeded,
        # the dataset must NOT be listed in unresolved_urns.
        # spec: USE_CASE_en.md §UC3 L396 — "entries that don't resolve in DataHub at run time
        # are skipped and reported in the run-complete event's unresolved_urns field"
        assert dataset_urn not in body["unresolved_urns"], (
            f"dataset_urn {dataset_urn!r} found in unresolved_urns — evidence-gathering "
            "including document fetch failed for the seeded dataset. "
            "spec: USE_CASE_en.md §UC3 §Inputs — documents with matching relatedAssets "
            "must be discoverable by _fetch_documents_for_dataset without error."
        )

    finally:
        # ── Step e: Cleanup — hard-delete documents, restore conf ─────────────────
        # spec: DATAHUB_INTEGRATION.md §Document Aspects — DataSpoke uses Status.removed for
        # soft-delete in production; hard-delete is test-only.
        if doc1_urn is not None and token:
            try:
                hard_delete_document(document_urn=doc1_urn, token=token)
            except Exception:
                pass
        if doc2_urn is not None and token:
            try:
                hard_delete_document(document_urn=doc2_urn, token=token)
            except Exception:
                pass
        await api_client.patch(conf_url, headers=admin_headers, json={"is_enabled": False})


# ── New-boundary + negative-coverage tests ─────────────────────────────────────


@pytest.mark.asyncio
async def test_uc3_run_disabled_returns_409(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """UC3 invariant: POST /method/run with is_enabled=False returns 409 ONTOGEN_DISABLED.

    Steps mirror USE_CASE_en.md L541:
      1. PUT conf with is_enabled=False
      2. POST method/run (no dry_run query param)
      3. Assert 409 with error_code='ONTOGEN_DISABLED'
      4. Cleanup: PATCH conf back to disabled (already is, belt-and-suspenders)

    spec: USE_CASE_en.md L541 — 'When is_enabled=false, non-dry-run calls to method/run
    return 409 ONTOGEN_DISABLED.'
    """
    conf_url = "/api/v1/spoke/common/ontogen/attr/conf"

    try:
        # ── Step 1: PUT conf with is_enabled=False ────────────────────────────
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

        # ── Step 2: POST run (no dry_run) ─────────────────────────────────────
        # spec: USE_CASE_en.md L541 — non-dry-run on disabled config must be rejected
        run_resp = await api_client.post(
            "/api/v1/spoke/common/ontogen/method/run",
            headers=admin_headers,
        )

        # ── Step 3: Assert 409 ONTOGEN_DISABLED ──────────────────────────────
        assert run_resp.status_code == 409, (
            f"Expected 409 ONTOGEN_DISABLED when is_enabled=False and no dry_run; "
            f"got {run_resp.status_code}: {run_resp.text}. "
            "spec: USE_CASE_en.md L541"
        )
        body = run_resp.json()
        assert body.get("error_code") == "ONTOGEN_DISABLED", (
            f"Expected error_code 'ONTOGEN_DISABLED'; got: {body!r}. "
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
        # ── Step 4: Restore conf ─────────────────────────────────────────────
        await api_client.patch(conf_url, headers=admin_headers, json={"is_enabled": False})


@pytest.mark.asyncio
async def test_uc3_review_reject(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """UC3 invariant: POST node/{id}/method/review with verdict=reject sets status to 'rejected'.

    spec: spec/feature/BACKEND.md §Ontology Generation Service — 'verdict: reject →
    mark the result as rejected. Rejecting a node or edge does not auto-reject
    dependent triples.'
    spec: USE_CASE_en.md §UC3 — reviewer may reject candidates they disagree with.
    """
    suffix = uuid.uuid4().hex[:8]
    node_id = f"uc3-rej-{suffix}"

    try:
        # Setup: seed a pending node via raw SQL
        # spec: TESTING.md §Api-Wired Integration Tests — setup may use raw SQL
        await _seed_pending_node(async_session, node_id, f"UcRejectNode-{suffix}")

        # Test body: REST only
        review_resp = await api_client.post(
            f"/api/v1/spoke/common/ontogen/result/node/{node_id}/method/review",
            headers=admin_headers,
            json={"verdict": "reject", "reason": "uc3-api-wired: reject test"},
        )
        assert review_resp.status_code == 200, (
            f"POST review verdict=reject failed: "
            f"{review_resp.status_code} {review_resp.text}"
        )
        body = review_resp.json()
        assert body.get("status") == "rejected", (
            f"Expected status 'rejected'; got {body.get('status')!r}. "
            "spec: BACKEND.md §Ontology Generation Service §Approval flow"
        )
    finally:
        await _delete_node(async_session, node_id)


# UC3 read-only boundary is enforced structurally (no DataHub emit code paths in review
# handlers per `src/backend/ontogen/service.py`); regression coverage lives in unit tests.
