"""Spot tests for Ontology Generation — review approve/reject, detail/evidence, triple dep gate.

Concerns covered (11 test functions, including 1 parametrized over 2 values = 12 test cases):

Review approve/reject:
  test_ontogen_node_review_approve
  test_ontogen_edge_review_approve
  test_ontogen_triple_review_dependency_order
  test_ontogen_node_review_reject
  test_ontogen_edge_review_reject

Detail/evidence:
  test_ontogen_node_detail_carries_evidence
  test_ontogen_node_detail_round_trips_evidence_debate

Triple dep gate:
  test_triple_dep_gate_blocks_when_deps_not_human_approved (parametrized: llm_pending, llm_approved)
  test_triple_dep_gate_passes_when_deps_are_human_approved

The stub LLM (stub_llm_client=true) returns no nodes/edges/triples, so review tests
seed one pending row per test directly into PostgreSQL via the async_session fixture,
then exercise the review endpoint over REST.  Each test uses a uuid-suffixed
name/label so unique constraints don't clash with rows left behind by prior sessions.

NOTE: UC3 read-only boundary is enforced structurally (no DataHub emit code paths in
review handlers per src/backend/ontogen/service.py); regression coverage lives in unit tests.

Spec traceability:
- spec/feature/BACKEND.md §Ontology Generation Service §Approval flow
- spec/feature/BACKEND_SCHEMA.md §Graph (ontogen_triples → AGE)
- spec/feature/BACKEND_SCHEMA.md §node_embeddings (pgvector)
- spec/feature/BACKEND_LLM.md §Adversarial Debate Framework §Evidence shape
- spec/USE_CASE_en.md §UC3 L350-L356 — triple cannot be approved unless deps are approved
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


# ── Raw-SQL seed helpers ───────────────────────────────────────────────────────
# Helpers live at the top of this file because they are used only by review/dep-gate
# tests; no DRY benefit from extracting to a shared util module.


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


# ── Review approve/reject tests ───────────────────────────────────────────────


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
            f"/api/v1/spoke/ontogen/result/node/{node_id}/method/review",
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
            f"/api/v1/spoke/ontogen/result/edge/{edge_id}/method/review",
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
            f"/api/v1/spoke/ontogen/result/triple/{triple_id}/method/review",
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
                f"/api/v1/spoke/ontogen/result/node/{nid}/method/review",
                headers=admin_headers,
                json={"verdict": "approve", "reason": "spot-test"},
            )
            assert r.status_code == 200, r.text
            assert r.json()["status"] == "approved", (
                f"Node {nid!r} must be 'approved' after human review; got {r.json()['status']!r}"
            )
        r = await api_client.post(
            f"/api/v1/spoke/ontogen/result/edge/{edge_id}/method/review",
            headers=admin_headers,
            json={"verdict": "approve", "reason": "spot-test"},
        )
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "approved"

        # Step 3: triple approve now succeeds because all deps are human-approved
        # spec: plan §7 — gate passes when status='approved' for all deps
        ok_resp = await api_client.post(
            f"/api/v1/spoke/ontogen/result/triple/{triple_id}/method/review",
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
            f"/api/v1/spoke/ontogen/result/node/{node_id}/method/review",
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
            f"/api/v1/spoke/ontogen/result/edge/{edge_id}/method/review",
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


# ── Detail / evidence tests ───────────────────────────────────────────────────


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
    Route: src/api/routers/spoke/ontogen.py:373-384
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
            f"/api/v1/spoke/ontogen/result/node/{node_id}/attr",
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
            f"/api/v1/spoke/ontogen/result/node/{node_id}/attr",
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


# ── Triple dep gate regression tests ─────────────────────────────────────────


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
            f"/api/v1/spoke/ontogen/result/triple/{triple_id}/method/review",
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
            f"/api/v1/spoke/ontogen/result/triple/{triple_id}/method/review",
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
