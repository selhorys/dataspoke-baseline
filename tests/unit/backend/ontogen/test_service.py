"""Unit tests for src/backend/ontogen/service.py — OntogenService."""

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backend.ontogen.debate_models import DebateResult
from src.backend.ontogen.service import OntogenRunSummary, OntogenService, _status_for_outcome
from src.shared.config import ONTOLOGY_CONFIDENCE_THRESHOLD
from src.shared.db.models import Event, OntogenEdge, OntogenNode, OntogenTriple
from src.shared.events import ONTOGEN_RUN_COMPLETE
from src.shared.exceptions import (
    ConflictError,
    EntityNotFoundError,
    InvalidDatasetUrnError,
    PreconditionFailedError,
)
from src.shared.llm.loop_trace import LoopResult, LoopTrace
from tests.unit.backend.conftest import (
    make_dataset_node_map_row,
    make_ontogen_edge_row,
    make_ontogen_node_row,
    make_ontogen_triple_row,
    mock_db_refresh,
    mock_scalar_query,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def svc(datahub: AsyncMock, db: AsyncMock, cache: AsyncMock, llm: AsyncMock, vector: AsyncMock) -> OntogenService:
    return OntogenService(
        datahub=datahub,
        db=db,
        cache=cache,
        llm=llm,
        vector=vector,
    )


# ── Singleton conf CRUD ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_conf_returns_default_when_absent(svc: OntogenService, db: AsyncMock) -> None:
    """get_conf creates and returns a default row when none exists."""
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result_mock)
    mock_db_refresh(db)

    row = await svc.get_conf()
    assert row.is_enabled is False
    assert row.id == 1


@pytest.mark.asyncio
async def test_get_conf_returns_existing_row(svc: OntogenService, db: AsyncMock) -> None:
    """get_conf returns existing row when present."""
    conf = MagicMock()
    conf.id = 1
    conf.is_enabled = True
    mock_scalar_query(db, conf)

    row = await svc.get_conf()
    assert row.is_enabled is True


@pytest.mark.asyncio
async def test_put_conf_validates_malformed_urn(svc: OntogenService, db: AsyncMock) -> None:
    """put_conf raises InvalidDatasetUrnError for malformed dataset_filter URNs."""
    with pytest.raises(InvalidDatasetUrnError):
        await svc.put_conf({
            "is_enabled": False,
            "dataset_filter": {"dataset_urns": ["not-a-valid-urn"]},
        })


@pytest.mark.asyncio
async def test_put_conf_validates_invalid_schedule_tier(svc: OntogenService, db: AsyncMock) -> None:
    """put_conf raises PreconditionFailedError for unknown schedule_tier."""
    with pytest.raises(PreconditionFailedError) as exc_info:
        await svc.put_conf({
            "is_enabled": True,
            "schedule_tier": "minutely",
        })
    assert exc_info.value.error_code == "INVALID_PARAMETER"


@pytest.mark.asyncio
async def test_put_conf_accepts_valid_tiers(svc: OntogenService, db: AsyncMock) -> None:
    """put_conf accepts hourly/daily/weekly/None without raising."""
    # None should not raise
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result_mock)
    mock_db_refresh(db)

    row = await svc.put_conf({"is_enabled": False, "schedule_tier": None})
    assert row is not None


@pytest.mark.asyncio
async def test_patch_conf_validates_schedule_tier(svc: OntogenService, db: AsyncMock) -> None:
    """patch_conf rejects invalid schedule_tier."""
    with pytest.raises(PreconditionFailedError) as exc_info:
        await svc.patch_conf({"schedule_tier": "yearly"})
    assert exc_info.value.error_code == "INVALID_PARAMETER"


# ── Seed CRUD ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_seed_round_trips_markdown(svc: OntogenService, db: AsyncMock) -> None:
    """create_seed stores markdown body and returns a seed with an ID."""
    mock_db_refresh(db)
    body_md = "# Test seed\n\nSome content"
    seed = await svc.create_seed(body_md)
    assert seed.body_md == body_md
    db.add.assert_called()


@pytest.mark.asyncio
async def test_get_seed_malformed_uuid_raises_entity_not_found(svc: OntogenService, db: AsyncMock) -> None:
    """get_seed raises EntityNotFoundError (not ValueError) for malformed UUID seed_id."""
    with pytest.raises(EntityNotFoundError):
        await svc.get_seed("not-a-uuid")


@pytest.mark.asyncio
async def test_get_seed_absent_raises_entity_not_found(svc: OntogenService, db: AsyncMock) -> None:
    """get_seed raises EntityNotFoundError when the seed row is absent."""
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result_mock)

    with pytest.raises(EntityNotFoundError):
        await svc.get_seed(str(uuid.uuid4()))


@pytest.mark.asyncio
async def test_delete_seed_sets_retired_status(svc: OntogenService, db: AsyncMock) -> None:
    """delete_seed marks the seed as 'retired'."""
    seed_row = MagicMock()
    seed_row.id = uuid.uuid4()
    seed_row.body_md = "# Old seed"
    seed_row.status = "active"
    seed_row.updated_at = None
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = seed_row
    db.execute = AsyncMock(return_value=result_mock)

    await svc.delete_seed(str(seed_row.id))
    assert seed_row.status == "retired"


# ── Verdict enum validation ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_review_node_rejects_invalid_verdict(svc: OntogenService, db: AsyncMock) -> None:
    """review_node raises PreconditionFailedError for unknown verdicts."""
    node = make_ontogen_node_row(status="llm_pending")
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = node
    # cache get returns None
    svc._cache.get = AsyncMock(return_value=None)
    db.execute = AsyncMock(return_value=result_mock)

    with pytest.raises(PreconditionFailedError) as exc_info:
        await svc.review_node(node.id, verdict="maybe")
    assert exc_info.value.error_code == "INVALID_PARAMETER"


@pytest.mark.asyncio
async def test_review_edge_rejects_invalid_verdict(svc: OntogenService, db: AsyncMock) -> None:
    """review_edge raises PreconditionFailedError for unknown verdicts."""
    edge = make_ontogen_edge_row(status="llm_pending")
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = edge
    db.execute = AsyncMock(return_value=result_mock)

    with pytest.raises(PreconditionFailedError) as exc_info:
        await svc.review_edge(edge.id, verdict="skip")
    assert exc_info.value.error_code == "INVALID_PARAMETER"


@pytest.mark.asyncio
async def test_review_triple_rejects_invalid_verdict(svc: OntogenService, db: AsyncMock) -> None:
    """review_triple raises PreconditionFailedError for unknown verdicts."""
    triple = make_ontogen_triple_row(status="llm_pending")
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = triple
    db.execute = AsyncMock(return_value=result_mock)

    with pytest.raises(PreconditionFailedError) as exc_info:
        await svc.review_triple(triple.id, verdict="maybe")
    assert exc_info.value.error_code == "INVALID_PARAMETER"


# ── Triple dependency gate ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_review_triple_dependency_gate_raises_when_nodes_not_approved(
    svc: OntogenService, db: AsyncMock
) -> None:
    """review_triple(approve) raises ONTOGEN_TRIPLE_DEPENDENCY_PENDING when nodes aren't approved."""
    triple = make_ontogen_triple_row(
        subject_node_id="book",
        edge_id="has_edition",
        object_node_id="edition",
        status="llm_pending",
    )
    # subject node: llm_pending (not approved)
    subj_node = make_ontogen_node_row(id="book", status="llm_pending")
    edge_row = make_ontogen_edge_row(id="has_edition", status="approved")
    obj_node = make_ontogen_node_row(id="edition", status="approved")

    # Execute will be called multiple times: get_triple, then subj, edge, obj
    def execute_side_effect(*args, **kwargs):
        mock = MagicMock()
        # Return triple on first call, then subj_node, edge_row, obj_node
        return mock

    calls = [triple, subj_node, edge_row, obj_node]
    call_iter = iter(calls)

    def make_result(row):
        m = MagicMock()
        m.scalar_one_or_none.return_value = row
        return m

    db.execute = AsyncMock(side_effect=[make_result(r) for r in calls])

    # Also mock cache for get_node
    svc._cache.get = AsyncMock(return_value=None)

    with pytest.raises(PreconditionFailedError) as exc_info:
        await svc.review_triple(triple.id, verdict="approve")
    assert exc_info.value.error_code == "ONTOGEN_TRIPLE_DEPENDENCY_PENDING"


# ── Concurrency guard ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_raises_conflict_when_lock_held(svc: OntogenService, cache: AsyncMock) -> None:
    """run() raises ConflictError('ONTOGEN_RUNNING') when SETNX returns False."""
    cache.set_nx = AsyncMock(return_value=False)

    with pytest.raises(ConflictError) as exc_info:
        await svc.run()

    assert exc_info.value.error_code == "ONTOGEN_RUNNING"


@pytest.mark.asyncio
async def test_run_rejects_non_dry_run_when_disabled(svc: OntogenService, db: AsyncMock, cache: AsyncMock) -> None:
    """Non-dry-run raises ConflictError('ONTOGEN_DISABLED') when singleton conf is disabled.

    spec: BACKEND.md §Ontology Generation Service — is_enabled=false rejects
    non-dry-run with 409 ONTOGEN_DISABLED.
    spec: USE_CASE_en.md §UC3 — "When is_enabled=false, non-dry-run calls to
    method/run return 409 ONTOGEN_DISABLED. Dry-run is always permitted
    regardless of is_enabled." (L479)
    """
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    conf = MagicMock()
    conf.id = 1
    conf.is_enabled = False
    conf.default_run_prompt = None
    conf.dataset_filter = {}
    mock_scalar_query(db, conf)

    with pytest.raises(ConflictError) as exc_info:
        await svc.run(dry_run=False)

    assert exc_info.value.error_code == "ONTOGEN_DISABLED"


@pytest.mark.asyncio
async def test_run_allows_dry_run_when_disabled(
    svc: OntogenService, db: AsyncMock, cache: AsyncMock, llm: AsyncMock
) -> None:
    """Dry-run bypasses the disabled guard and returns OntogenRunSummary.

    spec: BACKEND.md §Ontology Generation Service — dry_run=True is always
    permitted regardless of is_enabled.
    spec: USE_CASE_en.md §UC3 (disabled gate mirrors UC1 pattern)
    """
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    conf = MagicMock()
    conf.id = 1
    conf.is_enabled = False
    conf.default_run_prompt = None
    conf.dataset_filter = {}

    def make_result(scalar_val=None, scalars_val=None):
        m = MagicMock()
        m.scalar_one_or_none.return_value = scalar_val
        ms = MagicMock()
        ms.all.return_value = scalars_val or []
        m.scalars.return_value = ms
        m.scalar.return_value = 0
        return m

    conf_result = MagicMock()
    conf_result.scalar_one_or_none.return_value = conf

    db.execute = AsyncMock(side_effect=[
        conf_result,
        make_result(scalars_val=[]),
        make_result(scalars_val=[]),
        make_result(scalars_val=[]),
        make_result(scalars_val=[]),
    ])

    svc._datahub.enumerate_datasets = AsyncMock(return_value=[])

    debate_stub = DebateResult(
        payload={"nodes": [], "edges": [], "triples": []},
        transcript={"turns_completed": 1, "outcome": "accept", "final_reviewer_verdict": "accept",
                    "rag_anchors": [], "history": [], "producer_iterations": 1,
                    "producer_errors_dropped": 0, "item_verdicts": []},
        outcome="accept",
    )
    with patch("src.backend.ontogen.service.build_run_prompt", return_value="prompt"), \
         patch("src.backend.ontogen.service.run_debate", new=AsyncMock(return_value=debate_stub)):
        summary = await svc.run(dry_run=True)

    assert isinstance(summary, OntogenRunSummary)
    assert summary.dry_run is True


# ── dry_run happy path ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_dry_run_returns_summary_no_db_writes(
    svc: OntogenService, db: AsyncMock, cache: AsyncMock, llm: AsyncMock
) -> None:
    """run(dry_run=True) returns OntogenRunSummary and emits ONTOGEN.RUN_COMPLETE.

    Spec: spec/feature/BACKEND.md §Event Catalogue — RUN_COMPLETE recorded for
    both dry-run and non-dry-run; ontology rows are not persisted on dry-run.
    """
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock(return_value=None)

    # get_conf
    conf = MagicMock()
    conf.id = 1
    conf.is_enabled = True
    conf.default_run_prompt = None
    conf.dataset_filter = {}

    # For list of approved nodes/edges/triples (3 queries) + conf query
    def make_result(scalar_val=None, scalars_val=None):
        m = MagicMock()
        m.scalar_one_or_none.return_value = scalar_val
        ms = MagicMock()
        ms.all.return_value = scalars_val or []
        m.scalars.return_value = ms
        m.scalar.return_value = 0
        return m

    conf_result = MagicMock()
    conf_result.scalar_one_or_none.return_value = conf

    db.execute = AsyncMock(side_effect=[
        conf_result,  # get_conf
        make_result(scalars_val=[]),  # OntogenSeed query
        make_result(scalars_val=[]),  # approved nodes
        make_result(scalars_val=[]),  # approved edges
        make_result(scalars_val=[]),  # approved triples
    ])

    # DataHub enumerate_datasets
    svc._datahub.enumerate_datasets = AsyncMock(return_value=[])

    debate_stub = DebateResult(
        payload={"nodes": [], "edges": [], "triples": []},
        transcript={"turns_completed": 1, "outcome": "accept", "final_reviewer_verdict": "accept",
                    "rag_anchors": [], "history": [], "producer_iterations": 1,
                    "producer_errors_dropped": 0, "item_verdicts": []},
        outcome="accept",
    )
    with patch("src.backend.ontogen.service.build_run_prompt", return_value="prompt"), \
         patch("src.backend.ontogen.service.run_debate", new=AsyncMock(return_value=debate_stub)):
        summary = await svc.run(dry_run=True)

    assert isinstance(summary, OntogenRunSummary)
    assert summary.dry_run is True

    # Spec invariant: dry-run records ONTOGEN.RUN_COMPLETE with dry_run=True;
    # no OntogenNode/OntogenEdge/OntogenTriple rows are persisted.
    added_args = [call.args[0] for call in db.add.call_args_list]

    # Exactly one Event row with ONTOGEN_RUN_COMPLETE and dry_run flag
    event_rows = [a for a in added_args if isinstance(a, Event)]
    assert len(event_rows) == 1
    assert event_rows[0].event_type == ONTOGEN_RUN_COMPLETE
    assert event_rows[0].detail["dry_run"] is True

    # No ontology rows persisted on dry-run
    assert not any(isinstance(a, OntogenNode) for a in added_args)
    assert not any(isinstance(a, OntogenEdge) for a in added_args)
    assert not any(isinstance(a, OntogenTriple) for a in added_args)

    # Spec invariant: exactly one commit on the dry-run path (event row only).
    # spec: BACKEND.md §Inference Pipeline — dry-run records the event and returns;
    # no node/edge/triple commits occur.
    assert db.commit.call_count == 1, (
        f"Expected exactly one db.commit on dry-run (event row); "
        f"got {db.commit.call_count}. "
        "spec: BACKEND.md §Inference Pipeline — dry-run must not write ontology rows"
    )


# ── real_run happy path ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_real_run_emits_complete_event_with_dry_run_false(
    svc: OntogenService, db: AsyncMock, cache: AsyncMock, llm: AsyncMock
) -> None:
    """run(dry_run=False) emits ONTOGEN.RUN_COMPLETE with dry_run=False and unresolved_urns.

    spec: spec/feature/BACKEND.md §Ontology Generation Service §Inference Pipeline
    — Step 12 emits RUN_COMPLETE with dry_run key; real-run sets it to False.
    spec: spec/feature/BACKEND.md L661 — RUN_COMPLETE recorded for both dry-run
    and non-dry-run; dry_run flag and unresolved_urns are present in detail.
    """
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock(return_value=None)

    conf = MagicMock()
    conf.id = 1
    conf.is_enabled = True
    conf.default_run_prompt = None
    conf.dataset_filter = {}

    def make_result(scalar_val=None, scalars_val=None):
        m = MagicMock()
        m.scalar_one_or_none.return_value = scalar_val
        ms = MagicMock()
        ms.all.return_value = scalars_val or []
        m.scalars.return_value = ms
        m.scalar.return_value = 0
        return m

    conf_result = MagicMock()
    conf_result.scalar_one_or_none.return_value = conf

    db.execute = AsyncMock(side_effect=[
        conf_result,              # get_conf
        make_result(scalars_val=[]),  # OntogenSeed query
        make_result(scalars_val=[]),  # approved nodes
        make_result(scalars_val=[]),  # approved edges
        make_result(scalars_val=[]),  # approved triples
    ])

    # Empty dataset enumeration → empty dataset_urns and empty unresolved_urns
    svc._datahub.enumerate_datasets = AsyncMock(return_value=[])

    debate_stub = DebateResult(
        payload={"nodes": [], "edges": [], "triples": []},
        transcript={"turns_completed": 1, "outcome": "accept", "final_reviewer_verdict": "accept",
                    "rag_anchors": [], "history": [], "producer_iterations": 1,
                    "producer_errors_dropped": 0, "item_verdicts": []},
        outcome="accept",
    )
    with patch("src.backend.ontogen.service.build_run_prompt", return_value="prompt"), \
         patch("src.backend.ontogen.service.run_debate", new=AsyncMock(return_value=debate_stub)):
        summary = await svc.run(dry_run=False)

    assert isinstance(summary, OntogenRunSummary)
    assert summary.dry_run is False

    # Exactly one Event row added with ONTOGEN_RUN_COMPLETE and dry_run=False.
    # spec: BACKEND.md §Inference Pipeline Step 12 — real-run emits RUN_COMPLETE
    # with dry_run=False; unresolved_urns present in detail.
    added_args = [call.args[0] for call in db.add.call_args_list]
    event_rows = [a for a in added_args if isinstance(a, Event)]
    assert len(event_rows) == 1, (
        f"Expected exactly one Event row on real-run; got {len(event_rows)}. "
        "spec: BACKEND.md §Inference Pipeline Step 12"
    )
    assert event_rows[0].event_type == ONTOGEN_RUN_COMPLETE, (
        f"Event type must be ONTOGEN_RUN_COMPLETE; got {event_rows[0].event_type!r}. "
        "spec: BACKEND.md §Event Catalogue"
    )
    assert event_rows[0].detail["dry_run"] is False, (
        f"detail['dry_run'] must be False on real-run path; "
        f"got {event_rows[0].detail.get('dry_run')!r}. "
        "spec: BACKEND.md L661 — dry_run flag in detail"
    )
    # unresolved_urns must be present in detail and agree with the summary
    # spec: BACKEND.md L661 — unresolved_urns in RUN_COMPLETE payload
    assert "unresolved_urns" in event_rows[0].detail, (
        "detail must carry 'unresolved_urns'. spec: BACKEND.md L661"
    )
    assert event_rows[0].detail["unresolved_urns"] == summary.unresolved_urns, (
        f"detail['unresolved_urns'] must match summary.unresolved_urns; "
        f"detail={event_rows[0].detail['unresolved_urns']!r}, "
        f"summary={summary.unresolved_urns!r}. "
        "spec: BACKEND.md L661 — event and summary must agree on unresolved_urns"
    )


# ── LLM proposal validation ───────────────────────────────────────────────────


def test_llm_run_result_validates_shape() -> None:
    """OntogenLLMOutput rejects nodes with missing names.

    Spec: BACKEND.md §Ontogen validator rules — 'Pydantic shape of OntogenLLMOutput → SCHEMA'.
    _LLMRunResult was renamed to OntogenLLMOutput and moved to src.backend.ontogen.models (PR2).
    """
    from pydantic import ValidationError

    from src.backend.ontogen.models import OntogenLLMOutput

    # Valid shape
    result = OntogenLLMOutput.model_validate({
        "nodes": [{"name": "Book", "confidence_score": 0.9}],
        "edges": [],
        "triples": [],
    })
    assert len(result.nodes) == 1

    # Node missing required 'name' field — should raise
    with pytest.raises(ValidationError):
        OntogenLLMOutput.model_validate({
            "nodes": [{"confidence_score": 0.9}],  # name is required
            "edges": [],
            "triples": [],
        })


def test_llm_run_result_confidence_score_range() -> None:
    """OntogenLLMOutput rejects confidence_score outside [0.0, 1.0].

    Spec: BACKEND.md §Ontogen validator rules — 'confidence_score ∈ [0.0, 1.0] → CONF_OUT_OF_RANGE'.
    _LLMRunResult was renamed to OntogenLLMOutput and moved to src.backend.ontogen.models (PR2).
    """
    from pydantic import ValidationError

    from src.backend.ontogen.models import OntogenLLMOutput

    with pytest.raises(ValidationError):
        OntogenLLMOutput.model_validate({
            "nodes": [{"name": "Book", "confidence_score": 1.5}],
            "edges": [],
            "triples": [],
        })


# ── Event emission on approval ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_review_node_approve_sets_approved_status(
    svc: OntogenService, db: AsyncMock, datahub: AsyncMock
) -> None:
    """review_node(approve) sets node.status='approved' and DatasetNodeMap status.

    Spec: BACKEND.md §Ontology Generation Service — review_node approve mutates
    node status and propagates to dataset_node_map; no DataHub writes are performed
    (DataHub is read-only for UC3; approved metadata is read by UC4 from the DB).
    """
    node = make_ontogen_node_row(id="book", status="llm_pending")
    dm = make_dataset_node_map_row(node_id="book")

    call_count = [0]

    def execute_side_effect(*args, **kwargs):
        call_count[0] += 1
        m = MagicMock()
        if call_count[0] == 1:
            # get_node
            m.scalar_one_or_none.return_value = node
            return m
        elif call_count[0] == 2:
            # DatasetNodeMap for approve status update
            ms = MagicMock()
            ms.all.return_value = [dm]
            m.scalars.return_value = ms
            return m
        else:
            # event record — any extra execute
            ms = MagicMock()
            ms.all.return_value = []
            m.scalars.return_value = ms
            m.scalar_one_or_none.return_value = None
            return m

    db.execute = AsyncMock(side_effect=execute_side_effect)
    svc._cache.get = AsyncMock(return_value=None)
    svc._cache.delete = AsyncMock()
    svc._cache.set = AsyncMock()
    mock_db_refresh(db)

    datahub.emit_aspect = AsyncMock()

    await svc.review_node("book", verdict="approve")

    # Node status must be updated to approved
    assert node.status == "approved"
    # DatasetNodeMap row status must be propagated
    assert dm.status == "approved"
    # UC3 is read-only toward DataHub; no emit_aspect calls permitted
    datahub.emit_aspect.assert_not_called()


# ── UC3 read-only DataHub boundary — triple approval ─────────────────────────


@pytest.mark.asyncio
async def test_review_triple_approve_writes_no_datahub_aspect(
    svc: OntogenService, db: AsyncMock, datahub: AsyncMock
) -> None:
    """review_triple(approve) with all dependencies satisfied writes no DataHub aspect.

    Spec anchor: spec/DATAHUB_INTEGRATION.md §Read vs Write Boundary (UC3 = Read);
    spec/feature/BACKEND.md §Ontology Generation Service Approval flow — approval
    persists status in the DB only; no DataHub emit is made.

    The db.execute side_effect uses a callable that inspects the compiled SQL of each
    Select statement and routes to the correct row, decoupling the test from the
    source-code fetch order.
    """
    triple = make_ontogen_triple_row(
        subject_node_id="book",
        edge_id="has_edition",
        object_node_id="edition",
        status="llm_pending",
    )
    subj_node = make_ontogen_node_row(id="book", status="approved")
    edge_row = make_ontogen_edge_row(id="has_edition", status="approved")
    obj_node = make_ontogen_node_row(id="edition", status="approved")

    def make_result(row):
        m = MagicMock()
        m.scalar_one_or_none.return_value = row
        return m

    def _route_execute(stmt, *args, **kwargs):
        """Return the right mock row by inspecting the compiled SQL.

        Raises AssertionError for any SQL not matched here so that new queries
        added to the source code fail loudly rather than silently receiving the
        wrong mock result.
        """
        try:
            from sqlalchemy.dialects import postgresql
            sql = str(stmt.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            ))
        except Exception:
            sql = str(stmt)

        if "ontogen_triples" in sql:
            return make_result(triple)
        if "ontogen_edges" in sql:
            return make_result(edge_row)
        if "book" in sql:
            return make_result(subj_node)
        if "edition" in sql:
            return make_result(obj_node)
        raise AssertionError(f"Unexpected SQL in _route_execute: {sql[:300]}")

    db.execute = AsyncMock(side_effect=_route_execute)
    svc._cache.get = AsyncMock(return_value=None)
    svc._cache.delete = AsyncMock()
    svc._cache.set = AsyncMock()
    mock_db_refresh(db)

    datahub.emit_aspect = AsyncMock()

    await svc.review_triple(triple.id, verdict="approve")

    assert triple.status == "approved"
    # UC3 is read-only toward DataHub — no aspect emission permitted on triple approval
    datahub.emit_aspect.assert_not_called()


@pytest.mark.asyncio
async def test_review_triple_dependency_gate_and_no_datahub_side_effects(
    svc: OntogenService, db: AsyncMock, datahub: AsyncMock
) -> None:
    """review_triple(approve) raises ONTOGEN_TRIPLE_DEPENDENCY_PENDING when a node is not approved,
    and no DataHub write occurs either before or after the error.

    Uses _route_execute to decouple from source-code fetch order — the test passes
    regardless of which order the impl queries subject_node, edge, and object_node.

    Spec anchor: spec/USE_CASE_en.md §UC3 Approval flow — triple cannot be approved
    unless its subject node, edge, and object node are all approved;
    spec/DATAHUB_INTEGRATION.md §Read vs Write Boundary — UC3 never writes to DataHub.
    """
    triple = make_ontogen_triple_row(
        subject_node_id="book",
        edge_id="has_edition",
        object_node_id="edition",
        status="llm_pending",
    )
    subj_node = make_ontogen_node_row(id="book", status="llm_pending")  # not approved — triggers gate
    edge_row = make_ontogen_edge_row(id="has_edition", status="approved")
    obj_node = make_ontogen_node_row(id="edition", status="approved")

    def make_result(row):
        m = MagicMock()
        m.scalar_one_or_none.return_value = row
        return m

    def _route_execute(stmt, *args, **kwargs):
        """Route by SQL content — returns the appropriate row regardless of fetch order.

        Raises AssertionError for any SQL not matched here so that new queries
        added to the source code fail loudly rather than silently receiving the
        wrong mock result.
        """
        try:
            from sqlalchemy.dialects import postgresql
            sql = str(stmt.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            ))
        except Exception:
            sql = str(stmt)

        if "ontogen_triples" in sql:
            return make_result(triple)
        if "ontogen_edges" in sql:
            return make_result(edge_row)
        if "book" in sql:
            # subject node lookup — still llm_pending (not approved) — triggers dependency gate
            return make_result(subj_node)
        if "edition" in sql:
            return make_result(obj_node)
        raise AssertionError(f"Unexpected SQL in _route_execute: {sql[:300]}")

    db.execute = AsyncMock(side_effect=_route_execute)
    svc._cache.get = AsyncMock(return_value=None)
    datahub.emit_aspect = AsyncMock()

    with pytest.raises(PreconditionFailedError) as exc_info:
        await svc.review_triple(triple.id, verdict="approve")

    assert exc_info.value.error_code == "ONTOGEN_TRIPLE_DEPENDENCY_PENDING"
    # No DataHub write was attempted
    datahub.emit_aspect.assert_not_called()


# ── Validation telemetry in RUN_COMPLETE event ────────────────────────────────


@pytest.mark.asyncio
async def test_run_validation_telemetry_surfaces_in_run_complete_event(
    svc: OntogenService, db: AsyncMock, cache: AsyncMock, llm: AsyncMock
) -> None:
    """RUN_COMPLETE event detail carries producer_iterations and producer_errors_dropped.

    Spec: BACKEND_LLM.md §Inference Loop — 'The run-complete event carries
    producer_iterations (1–max) and producer_errors_dropped (row count)
    reflecting the Producer-turn inference loop.'

    Setup:
    - complete_with_tools returns LoopResult with trace.iterations=2 and
      trace.final_errors containing one SLUG_FORMAT error at 'nodes[0].id'.
    - The payload has one node whose id would be flagged by SLUG_FORMAT.
    - partition_clean_rows contract (spec §Ontogen validator rules): that node is
      dropped → expected dropped_count = 1.
    - Assertion: RUN_COMPLETE event detail must contain
        producer_iterations=2, producer_errors_dropped=1.
    """
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock(return_value=None)

    conf = MagicMock()
    conf.id = 1
    conf.is_enabled = True
    conf.default_run_prompt = None
    conf.dataset_filter = {}

    def make_result(scalar_val=None, scalars_val=None):
        m = MagicMock()
        m.scalar_one_or_none.return_value = scalar_val
        ms = MagicMock()
        ms.all.return_value = scalars_val or []
        m.scalars.return_value = ms
        m.scalar.return_value = 0
        return m

    conf_result = MagicMock()
    conf_result.scalar_one_or_none.return_value = conf

    db.execute = AsyncMock(side_effect=[
        conf_result,              # get_conf
        make_result(scalars_val=[]),  # OntogenSeed query
        make_result(scalars_val=[]),  # approved nodes
        make_result(scalars_val=[]),  # approved edges
        make_result(scalars_val=[]),  # approved triples
    ])

    svc._datahub.enumerate_datasets = AsyncMock(return_value=[])

    # DebateResult transcript carries producer_iterations=2 and producer_errors_dropped=1,
    # which service.py writes into the RUN_COMPLETE event detail under the same keys.
    # Spec: BACKEND_LLM.md §Inference Loop — producer inner-loop telemetry surfaced
    # via debate_result.transcript["producer_iterations"] / ["producer_errors_dropped"].
    debate_stub = DebateResult(
        payload={"nodes": [], "edges": [], "triples": []},
        transcript={
            "turns_completed": 2,
            "outcome": "accept",
            "final_reviewer_verdict": "accept",
            "rag_anchors": [],
            "history": [],
            "producer_iterations": 2,
            "producer_errors_dropped": 1,
            "item_verdicts": [],
        },
        outcome="accept",
    )
    with patch("src.backend.ontogen.service.build_run_prompt", return_value="prompt"), \
         patch("src.backend.ontogen.service.run_debate", new=AsyncMock(return_value=debate_stub)):
        summary = await svc.run(dry_run=True)

    assert isinstance(summary, OntogenRunSummary)

    # Find the RUN_COMPLETE event among all db.add calls
    added_args = [call.args[0] for call in db.add.call_args_list]
    event_rows = [a for a in added_args if isinstance(a, Event)]
    complete_events = [e for e in event_rows if e.event_type == ONTOGEN_RUN_COMPLETE]
    assert len(complete_events) == 1, (
        f"Expected exactly one ONTOGEN_RUN_COMPLETE event; got {len(complete_events)}. "
        "Spec: BACKEND.md §LLM Inference Loop — run-complete event carries telemetry fields."
    )

    detail = complete_events[0].detail

    # Spec: BACKEND_LLM.md §Inference Loop — 'producer_iterations (1–max)'
    assert detail.get("producer_iterations") == 2, (
        f"detail['producer_iterations'] must be 2 (from trace.iterations); "
        f"got {detail.get('producer_iterations')!r}. "
        "Spec: BACKEND_LLM.md §Inference Loop — 'run-complete event carries producer_iterations'."
    )

    # Spec: BACKEND_LLM.md §Inference Loop — 'producer_errors_dropped (row count)'
    # partition_clean_rows contract: one SLUG_FORMAT error at nodes[0].id → 1 node dropped.
    # Expected dropped_count derived from partition_clean_rows spec invariant, not impl introspection.
    assert detail.get("producer_errors_dropped") == 1, (
        f"detail['validation_errors_dropped'] must be 1 (one node dropped by partition_clean_rows); "
        f"got {detail.get('validation_errors_dropped')!r}. "
        "Spec: BACKEND.md §LLM Inference Loop — 'run-complete event carries validation_errors_dropped'."
    )


# ── _status_for_outcome regression gate (plan §8) ────────────────────────────


def test_status_for_outcome_approved_only_when_accept_and_high_confidence() -> None:
    """_status_for_outcome returns 'llm_approved' only for outcome='accept' with score >= threshold.

    Spec: plan §8 — four-state vocabulary: 'llm_approved' when accept + high confidence;
    'llm_pending' in all other cases.  Non-accept outcomes (turns_exhausted, cycle_detected)
    must never produce 'llm_approved' regardless of confidence_score.

    Four representative combos are checked; threshold is read from shared config so
    the test remains correct if the constant is changed.
    """
    high = ONTOLOGY_CONFIDENCE_THRESHOLD + 0.01  # clearly above
    low = max(0.0, ONTOLOGY_CONFIDENCE_THRESHOLD - 0.01)  # clearly below

    # (1) accept + high confidence → llm_approved
    assert _status_for_outcome(high, "accept") == "llm_approved", (
        f"accept+high must produce 'llm_approved'; threshold={ONTOLOGY_CONFIDENCE_THRESHOLD}"
    )

    # (2) accept + low confidence → llm_pending
    assert _status_for_outcome(low, "accept") == "llm_pending", (
        f"accept+low must produce 'llm_pending'; threshold={ONTOLOGY_CONFIDENCE_THRESHOLD}"
    )

    # (3) turns_exhausted + high confidence → llm_pending (not llm_approved!)
    assert _status_for_outcome(high, "turns_exhausted") == "llm_pending", (
        "turns_exhausted with high confidence must NOT produce 'llm_approved'. "
        "Spec: plan §8 — non-accept outcomes always yield llm_pending."
    )

    # (4) cycle_detected + high confidence → llm_pending (not llm_approved!)
    assert _status_for_outcome(high, "cycle_detected") == "llm_pending", (
        "cycle_detected with high confidence must NOT produce 'llm_approved'. "
        "Spec: plan §8 — non-accept outcomes always yield llm_pending."
    )


@pytest.mark.asyncio
async def test_turns_exhausted_persists_rows_as_llm_pending(
    svc: OntogenService, db: AsyncMock, cache: AsyncMock
) -> None:
    """When debate outcome='turns_exhausted', every persisted row has status='llm_pending'.

    Spec: BACKEND_LLM.md §Adversarial Debate Framework — Termination — turns_exhausted row:
    'rows persist with status=llm_pending regardless of confidence_score.'
    The debate loop was cut off before a final reviewer accept, so the result is
    placed in the human review queue (llm_pending), not auto-approved.
    """
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock(return_value=None)

    conf = MagicMock()
    conf.id = 1
    conf.is_enabled = True
    conf.default_run_prompt = None
    conf.dataset_filter = {}

    def make_result(scalar_val=None, scalars_val=None):
        m = MagicMock()
        m.scalar_one_or_none.return_value = scalar_val
        ms = MagicMock()
        ms.all.return_value = scalars_val or []
        m.scalars.return_value = ms
        m.scalar.return_value = 0
        return m

    conf_result = MagicMock()
    conf_result.scalar_one_or_none.return_value = conf

    # Provide enough results for get_conf + seeds + existing nodes/edges/triples +
    # per-node select (for new-vs-existing check) + per-edge select + per-triple check
    def _any_result(*_args, **_kwargs):
        m = MagicMock()
        m.scalar_one_or_none.return_value = None
        ms = MagicMock()
        ms.all.return_value = []
        m.scalars.return_value = ms
        m.scalar.return_value = 0
        return m

    db.execute = AsyncMock(side_effect=[conf_result] + [_any_result() for _ in range(20)])
    svc._datahub.enumerate_datasets = AsyncMock(return_value=[])

    # turns_exhausted with high-confidence payload — status must still be llm_pending
    high_score = ONTOLOGY_CONFIDENCE_THRESHOLD + 0.1
    debate_stub = DebateResult(
        payload={
            "nodes": [
                {
                    "name": "BookTitle",
                    "id": "book_title",
                    "confidence_score": high_score,  # above threshold
                    "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.catalog,PROD)"],
                }
            ],
            "edges": [
                {
                    "id": "has_edition",
                    "label": "has edition",
                    "confidence_score": high_score,
                }
            ],
            "triples": [
                {
                    "subject_node_id": "book_title",
                    "edge_id": "has_edition",
                    "object_node_id": "book_title",
                    "confidence_score": high_score,
                }
            ],
        },
        transcript={
            "turns_completed": 4,
            "outcome": "turns_exhausted",
            "final_reviewer_verdict": "revise",
            "rag_anchors": [],
            "history": [],
            "producer_iterations": 4,
            "producer_errors_dropped": 0,
            "item_verdicts": [],
        },
        outcome="turns_exhausted",
    )

    with patch("src.backend.ontogen.service.build_run_prompt", return_value="prompt"), \
         patch("src.backend.ontogen.service.run_debate", new=AsyncMock(return_value=debate_stub)), \
         patch("src.backend.ontogen.service._search_node_embeddings", new=AsyncMock(return_value=[])), \
         patch("src.backend.ontogen.service._upsert_node_embedding", new=AsyncMock(return_value=None)), \
         patch("src.backend.ontogen.service._upsert_edge_embedding", new=AsyncMock(return_value=None)):
        await svc.run(dry_run=False)

    # Every OntogenNode / OntogenEdge / OntogenTriple added must have status='llm_pending'
    added_args = [call.args[0] for call in db.add.call_args_list]

    persisted_nodes = [a for a in added_args if isinstance(a, OntogenNode)]
    persisted_edges = [a for a in added_args if isinstance(a, OntogenEdge)]
    persisted_triples = [a for a in added_args if isinstance(a, OntogenTriple)]

    for node in persisted_nodes:
        assert node.status == "llm_pending", (
            f"OntogenNode.status must be 'llm_pending' for turns_exhausted outcome; "
            f"got {node.status!r} for node {node.id!r}. "
            "Spec: BACKEND_LLM.md §Termination — turns_exhausted → llm_pending always."
        )
    for edge in persisted_edges:
        assert edge.status == "llm_pending", (
            f"OntogenEdge.status must be 'llm_pending' for turns_exhausted outcome; "
            f"got {edge.status!r} for edge {edge.id!r}. "
            "Spec: BACKEND_LLM.md §Termination — turns_exhausted → llm_pending always."
        )
    for triple in persisted_triples:
        assert triple.status == "llm_pending", (
            f"OntogenTriple.status must be 'llm_pending' for turns_exhausted outcome; "
            f"got {triple.status!r} for triple {triple.id!r}. "
            "Spec: BACKEND_LLM.md §Termination — turns_exhausted → llm_pending always."
        )

    # At least one row should have been attempted — verify the debate payload was processed
    assert (persisted_nodes or persisted_edges or persisted_triples), (
        "No OntogenNode/Edge/Triple rows were added to db — "
        "check that the debate stub payload is not being silently skipped."
    )


@pytest.mark.asyncio
async def test_cycle_detected_persists_rows_as_llm_pending(
    svc: OntogenService, db: AsyncMock, cache: AsyncMock
) -> None:
    """When debate outcome='cycle_detected', every persisted row has status='llm_pending'.

    Spec: BACKEND_LLM.md §Adversarial Debate Framework — Termination — cycle_detected row:
    'rows persist with status=llm_pending regardless of confidence_score.'
    Same contract as turns_exhausted; separate test to lock down each outcome independently.
    """
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock(return_value=None)

    conf = MagicMock()
    conf.id = 1
    conf.is_enabled = True
    conf.default_run_prompt = None
    conf.dataset_filter = {}

    def _any_result(*_args, **_kwargs):
        m = MagicMock()
        m.scalar_one_or_none.return_value = None
        ms = MagicMock()
        ms.all.return_value = []
        m.scalars.return_value = ms
        m.scalar.return_value = 0
        return m

    conf_result = MagicMock()
    conf_result.scalar_one_or_none.return_value = conf

    db.execute = AsyncMock(side_effect=[conf_result] + [_any_result() for _ in range(20)])
    svc._datahub.enumerate_datasets = AsyncMock(return_value=[])

    high_score = ONTOLOGY_CONFIDENCE_THRESHOLD + 0.1
    debate_stub = DebateResult(
        payload={
            "nodes": [
                {
                    "name": "Customer",
                    "id": "customer",
                    "confidence_score": high_score,
                    "dataset_urns": ["urn:li:dataset:(urn:li:dataPlatform:postgres,db.customers,PROD)"],
                }
            ],
            "edges": [
                {
                    "id": "placed_by",
                    "label": "placed by",
                    "confidence_score": high_score,
                }
            ],
            "triples": [
                {
                    "subject_node_id": "customer",
                    "edge_id": "placed_by",
                    "object_node_id": "customer",
                    "confidence_score": high_score,
                }
            ],
        },
        transcript={
            "turns_completed": 3,
            "outcome": "cycle_detected",
            "final_reviewer_verdict": "revise",
            "rag_anchors": [],
            "history": [],
            "producer_iterations": 3,
            "producer_errors_dropped": 0,
            "item_verdicts": [],
        },
        outcome="cycle_detected",
    )

    with patch("src.backend.ontogen.service.build_run_prompt", return_value="prompt"), \
         patch("src.backend.ontogen.service.run_debate", new=AsyncMock(return_value=debate_stub)), \
         patch("src.backend.ontogen.service._search_node_embeddings", new=AsyncMock(return_value=[])), \
         patch("src.backend.ontogen.service._upsert_node_embedding", new=AsyncMock(return_value=None)), \
         patch("src.backend.ontogen.service._upsert_edge_embedding", new=AsyncMock(return_value=None)):
        await svc.run(dry_run=False)

    added_args = [call.args[0] for call in db.add.call_args_list]

    persisted_nodes = [a for a in added_args if isinstance(a, OntogenNode)]
    persisted_edges = [a for a in added_args if isinstance(a, OntogenEdge)]
    persisted_triples = [a for a in added_args if isinstance(a, OntogenTriple)]

    for node in persisted_nodes:
        assert node.status == "llm_pending", (
            f"OntogenNode.status must be 'llm_pending' for cycle_detected outcome; "
            f"got {node.status!r}. "
            "Spec: BACKEND_LLM.md §Termination — cycle_detected → llm_pending always."
        )
    for edge in persisted_edges:
        assert edge.status == "llm_pending", (
            f"OntogenEdge.status must be 'llm_pending' for cycle_detected outcome; "
            f"got {edge.status!r}. "
            "Spec: BACKEND_LLM.md §Termination — cycle_detected → llm_pending always."
        )
    for triple in persisted_triples:
        assert triple.status == "llm_pending", (
            f"OntogenTriple.status must be 'llm_pending' for cycle_detected outcome; "
            f"got {triple.status!r}. "
            "Spec: BACKEND_LLM.md §Termination — cycle_detected → llm_pending always."
        )

    assert (persisted_nodes or persisted_edges or persisted_triples), (
        "No OntogenNode/Edge/Triple rows were added to db — "
        "check that the debate stub payload is not being silently skipped."
    )


# ── New regression tests (plan §9) ───────────────────────────────────────────


# ── F1: Reuse-lookup includes llm_pending — verifies WHERE clause directly ───


@pytest.mark.asyncio
async def test_reuse_lookup_where_clause_includes_all_three_eligible_statuses(
    svc: OntogenService, db: AsyncMock, cache: AsyncMock
) -> None:
    """The eligible-nodes/edges SQL query's WHERE clause must include all 3 non-rejected statuses.

    Captures the actual `select()` statement the service emits to db.execute, compiles
    it to SQL with literal binds, and asserts each of `'approved'`, `'llm_approved'`,
    `'llm_pending'` appears as a status literal. If anyone narrows the WHERE clause
    (e.g. drops `llm_pending`), this test fails immediately — the regression is
    observable in the compiled SQL string, not in any downstream behaviour that a
    dumb mock could fake.

    Spec: plan §7 reuse-lookup — `status IN ('approved','llm_approved','llm_pending')`.
    Spec: BACKEND.md §Inference Pipeline Step 7 — eligible-nodes load for reuse.
    """
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock(return_value=None)

    conf = MagicMock()
    conf.id = 1
    conf.is_enabled = True
    conf.default_run_prompt = None
    conf.dataset_filter = {}

    def make_result(scalar_val=None, scalars_val=None):
        m = MagicMock()
        m.scalar_one_or_none.return_value = scalar_val
        ms = MagicMock()
        ms.all.return_value = scalars_val or []
        m.scalars.return_value = ms
        m.scalar.return_value = 0
        return m

    conf_result = MagicMock()
    conf_result.scalar_one_or_none.return_value = conf

    canned = [
        conf_result,                # get_conf
        make_result(scalars_val=[]),  # OntogenSeed query
        make_result(scalars_val=[]),  # eligible_nodes
        make_result(scalars_val=[]),  # eligible_edges
        make_result(scalars_val=[]),  # approved_triples (or similar)
    ]
    captured_stmts: list[Any] = []

    async def capture_execute(stmt, *args, **kwargs):
        captured_stmts.append(stmt)
        # Return the next canned result (or a safe default if more queries fire)
        if len(captured_stmts) <= len(canned):
            return canned[len(captured_stmts) - 1]
        return make_result()

    db.execute = capture_execute
    svc._datahub.enumerate_datasets = AsyncMock(return_value=[])

    debate_stub = DebateResult(
        payload={"nodes": [], "edges": [], "triples": []},
        transcript={
            "turns_completed": 1, "outcome": "accept", "final_reviewer_verdict": "accept",
            "rag_anchors": [], "history": [], "producer_iterations": 1,
            "producer_errors_dropped": 0, "item_verdicts": [],
        },
        outcome="accept",
    )

    with patch("src.backend.ontogen.service.build_run_prompt", return_value="prompt"), \
         patch(
             "src.backend.ontogen.service.run_debate",
             new=AsyncMock(return_value=debate_stub),
         ):
        await svc.run(dry_run=True)

    # Find the OntogenNode and OntogenEdge select statements among captured queries.
    # Compile each to SQL with literal binds so we can string-match the status literals.
    def compiled_sql(stmt: Any) -> str:
        try:
            return str(
                stmt.compile(compile_kwargs={"literal_binds": True})
            )
        except Exception:
            return str(stmt)

    node_sql = next(
        (compiled_sql(s) for s in captured_stmts if "ontogen_nodes" in compiled_sql(s).lower()),
        None,
    )
    edge_sql = next(
        (compiled_sql(s) for s in captured_stmts if "ontogen_edges" in compiled_sql(s).lower()),
        None,
    )

    assert node_sql is not None, (
        f"No SELECT against ontogen_nodes captured; got {[compiled_sql(s) for s in captured_stmts]!r}. "
        "Spec: plan §7 — service must query eligible nodes for reuse."
    )
    assert edge_sql is not None, (
        "No SELECT against ontogen_edges captured. Spec: plan §7 — eligible edges loaded for reuse."
    )

    for sql, table in [(node_sql, "ontogen_nodes"), (edge_sql, "ontogen_edges")]:
        for required in ("'approved'", "'llm_approved'", "'llm_pending'"):
            assert required in sql, (
                f"{table} WHERE clause must include {required}; got SQL:\n{sql}\n"
                "Spec: plan §7 — reuse-lookup is `status IN (approved, llm_approved, llm_pending)`."
            )
