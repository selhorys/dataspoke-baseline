"""Unit tests for src/backend/ontogen/service.py — OntogenService."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backend.ontogen.service import OntogenService, OntogenRunSummary
from src.shared.exceptions import (
    ConflictError,
    EntityNotFoundError,
    InvalidDatasetUrnError,
    PreconditionFailedError,
)
from src.shared.graph.client import AgeGraph

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
def graph() -> AsyncMock:
    """Mock AgeGraph."""
    return AsyncMock(spec=AgeGraph)


@pytest.fixture
def svc(datahub: AsyncMock, db: AsyncMock, cache: AsyncMock, llm: AsyncMock, vector: AsyncMock, graph: AsyncMock) -> OntogenService:
    return OntogenService(
        datahub=datahub,
        db=db,
        cache=cache,
        llm=llm,
        age=graph,
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


# ── dry_run happy path ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_dry_run_returns_summary_no_db_writes(
    svc: OntogenService, db: AsyncMock, cache: AsyncMock, llm: AsyncMock
) -> None:
    """run(dry_run=True) returns OntogenRunSummary without writing to DB.

    Spec: spec/feature/BACKEND.md §Ontology Generation Service §Inference Pipeline
    — step 9: ?dry_run=true evaluates steps 2-8 without persisting.
    """
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock(return_value=None)

    # get_conf
    conf = MagicMock()
    conf.id = 1
    conf.is_enabled = True
    conf.default_run_prompt = None
    conf.dataset_filter = {}
    conf.max_manual_queries_per_dataset = 20
    conf.max_system_queries_per_dataset = 10

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
    # No OntogenNode/Edge/Triple rows committed on dry_run
    # Spec: spec/feature/BACKEND.md §Ontology Generation Service §Inference Pipeline
    # — ?dry_run=true evaluates steps 2–8 without persisting (step 9 is skipped).
    assert db.commit.call_count == 0


# ── LLM proposal validation ───────────────────────────────────────────────────


def test_llm_run_result_validates_shape() -> None:
    """_LLMRunResult rejects nodes with missing names."""
    from src.backend.ontogen.service import _LLMRunResult
    from pydantic import ValidationError

    # Valid shape
    result = _LLMRunResult.model_validate({
        "nodes": [{"name": "Book", "confidence_score": 0.9}],
        "edges": [],
        "triples": [],
    })
    assert len(result.nodes) == 1

    # Node missing required 'name' field — should raise
    with pytest.raises(ValidationError):
        _LLMRunResult.model_validate({
            "nodes": [{"confidence_score": 0.9}],  # name is required
            "edges": [],
            "triples": [],
        })


def test_llm_run_result_confidence_score_range() -> None:
    """_LLMRunResult rejects confidence_score outside [0.0, 1.0]."""
    from src.backend.ontogen.service import _LLMRunResult
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _LLMRunResult.model_validate({
            "nodes": [{"name": "Book", "confidence_score": 1.5}],
            "edges": [],
            "triples": [],
        })


# ── Event emission on approval ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_review_node_approve_calls_datahub_emit_aspect(
    svc: OntogenService, db: AsyncMock, datahub: AsyncMock
) -> None:
    """review_node(approve) emits a glossary term aspect to DataHub for member datasets."""
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
        elif call_count[0] == 3:
            # DatasetNodeMap for glossary attach
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

    # Mock DataHub get_aspect and emit_aspect
    datahub.get_aspect = AsyncMock(return_value=None)
    datahub.emit_aspect = AsyncMock()

    await svc.review_node("book", verdict="approve")

    # emit_aspect should have been called (glossary term attachment)
    datahub.emit_aspect.assert_called()
