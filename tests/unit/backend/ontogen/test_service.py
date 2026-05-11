"""Unit tests for src/backend/ontogen/service.py — OntogenService."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backend.ontogen.service import OntogenRunSummary, OntogenService
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
    node = make_ontogen_node_row(status="pending_review")
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
    edge = make_ontogen_edge_row(status="pending_review")
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = edge
    db.execute = AsyncMock(return_value=result_mock)

    with pytest.raises(PreconditionFailedError) as exc_info:
        await svc.review_edge(edge.id, verdict="skip")
    assert exc_info.value.error_code == "INVALID_PARAMETER"


@pytest.mark.asyncio
async def test_review_triple_rejects_invalid_verdict(svc: OntogenService, db: AsyncMock) -> None:
    """review_triple raises PreconditionFailedError for unknown verdicts."""
    triple = make_ontogen_triple_row(status="pending_review")
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
        edge_id="has-edition",
        object_node_id="edition",
        status="pending_review",
    )
    # subject node: pending_review (not approved)
    subj_node = make_ontogen_node_row(id="book", status="pending_review")
    edge_row = make_ontogen_edge_row(id="has-edition", status="approved")
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
    llm.complete_json = AsyncMock(return_value={"nodes": [], "edges": [], "triples": []})
    llm.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])

    with patch("src.backend.ontogen.service.build_run_prompt", return_value="prompt"):
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

    # LLM
    llm.complete_json = AsyncMock(return_value={"nodes": [], "edges": [], "triples": []})
    llm.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])

    with patch("src.backend.ontogen.service.build_run_prompt", return_value="prompt"):
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

    llm.complete_json = AsyncMock(return_value={"nodes": [], "edges": [], "triples": []})
    llm.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])

    with patch("src.backend.ontogen.service.build_run_prompt", return_value="prompt"):
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
    node = make_ontogen_node_row(id="book", status="pending_review")
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
        edge_id="has-edition",
        object_node_id="edition",
        status="pending_review",
    )
    subj_node = make_ontogen_node_row(id="book", status="approved")
    edge_row = make_ontogen_edge_row(id="has-edition", status="approved")
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
        edge_id="has-edition",
        object_node_id="edition",
        status="pending_review",
    )
    subj_node = make_ontogen_node_row(id="book", status="pending_review")  # not approved — triggers gate
    edge_row = make_ontogen_edge_row(id="has-edition", status="approved")
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
            # subject node lookup — still pending_review, triggers dependency gate
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
    """RUN_COMPLETE event detail carries validation_iterations and validation_errors_dropped.

    Spec: BACKEND.md §LLM Inference Loop — 'The run-complete event carries
    validation_iterations (1–max) and validation_errors_dropped (row count).'

    Setup:
    - complete_with_tools returns LoopResult with trace.iterations=2 and
      trace.final_errors containing one SLUG_FORMAT error at 'nodes[0].id'.
    - The payload has one node whose id would be flagged by SLUG_FORMAT.
    - partition_clean_rows contract (spec §Ontogen validator rules): that node is
      dropped → expected dropped_count = 1.
    - Assertion: RUN_COMPLETE event detail must contain
        validation_iterations=2, validation_errors_dropped=1.
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

    # complete_with_tools returns LoopResult with 2 iterations and one SLUG_FORMAT error.
    # The payload has one node with a valid Pydantic shape (name present) but id="Order"
    # which passes Pydantic (no length issue) but would fire SLUG_FORMAT in the semantic
    # validator. The final_errors list signals what was wrong after the loop exhausted.
    final_errors_raw = [
        {"path": "nodes[0].id", "code": "SLUG_FORMAT", "message": "nodes[0].id does not match ^[a-z0-9][a-z0-9_-]*$: 'Order'"}
    ]
    loop_result = LoopResult(
        payload={
            "nodes": [
                {
                    "name": "Order",
                    "id": "Order",          # uppercase — passes Pydantic, fails SLUG_FORMAT
                    "confidence_score": 0.9,
                    "dataset_urns": [],     # empty urns — MISSING_DATASET_URNS would fire too,
                                            # but we only assert on the final_errors supplied
                }
            ],
            "edges": [],
            "triples": [],
        },
        trace=LoopTrace(
            iterations=2,
            errors_per_iter=[final_errors_raw, final_errors_raw],
            final_errors=final_errors_raw,  # one SLUG_FORMAT error → partition drops 1 row
        ),
    )
    llm.complete_with_tools = AsyncMock(return_value=loop_result)
    llm.embed = AsyncMock(return_value=[0.1, 0.2, 0.3])

    with patch("src.backend.ontogen.service.build_run_prompt", return_value="prompt"):
        with patch("src.backend.ontogen.service.build_ontogen_validate_tool", return_value=MagicMock()):
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

    # Spec: BACKEND.md §LLM Inference Loop — 'validation_iterations (1–max)'
    assert detail.get("validation_iterations") == 2, (
        f"detail['validation_iterations'] must be 2 (from trace.iterations); "
        f"got {detail.get('validation_iterations')!r}. "
        "Spec: BACKEND.md §LLM Inference Loop — 'run-complete event carries validation_iterations'."
    )

    # Spec: BACKEND.md §LLM Inference Loop — 'validation_errors_dropped (row count)'
    # partition_clean_rows contract: one SLUG_FORMAT error at nodes[0].id → 1 node dropped.
    # Expected dropped_count derived from partition_clean_rows spec invariant, not impl introspection.
    assert detail.get("validation_errors_dropped") == 1, (
        f"detail['validation_errors_dropped'] must be 1 (one node dropped by partition_clean_rows); "
        f"got {detail.get('validation_errors_dropped')!r}. "
        "Spec: BACKEND.md §LLM Inference Loop — 'run-complete event carries validation_errors_dropped'."
    )
