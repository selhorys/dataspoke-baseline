"""Unit tests for OverviewService — six-section snapshot shape and config CRUD.

OverviewService(datahub, db) takes two dependencies.
get_overview() calls private section helpers sequentially (AsyncSession is not
concurrent-safe); tests patch private methods to isolate each section.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backend.overview.service import (
    MedallionSummary,
    OntologyGraph,
    OverviewService,
    _classify_medallion,
)
from tests.unit.backend.conftest import (
    make_dataset_node_map_row,
    make_metric_breakdown_row,
    make_ontogen_edge_row,
    make_ontogen_node_row,
    make_ontogen_triple_row,
    mock_db_refresh,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def service(datahub, db):
    return OverviewService(datahub=datahub, db=db)


def _make_config_row(
    layout: str = "force",
    color_by: str = "quality_score",
    filters: dict | None = None,
):
    row = MagicMock()
    row.id = 1
    row.layout = layout
    row.color_by = color_by
    row.filters = filters or {}
    row.updated_at = datetime.now(tz=UTC)
    return row


# ── _classify_medallion ───────────────────────────────────────────────────────


def test_medallion_bronze_no_upstream():
    assert _classify_medallion(0) == "bronze"


def test_medallion_silver_one_upstream():
    assert _classify_medallion(1) == "silver"


def test_medallion_silver_two_upstream():
    assert _classify_medallion(2) == "silver"


def test_medallion_gold_three_plus_upstream():
    assert _classify_medallion(3) == "gold"
    assert _classify_medallion(10) == "gold"


# ── get_overview: six fields with empty defaults ──────────────────────────────


async def test_get_overview_returns_six_fields_when_empty(service):
    """get_overview returns all six fields; each is empty when DB and DataHub are empty."""
    with (
        patch.object(service, "_build_metric_sections", return_value=({}, {})),
        patch.object(service, "_build_ontology_graph", return_value=OntologyGraph()),
        patch.object(
            service,
            "_build_dataset_sections",
            return_value=([], MedallionSummary(), {}),
        ),
    ):
        snapshot = await service.get_overview()

    assert snapshot.metric_values == {}
    assert snapshot.per_dataset_breakdown == {}
    assert snapshot.blind_spots == []
    assert snapshot.ontology_graph.nodes == []
    assert snapshot.ontology_graph.edges == []
    assert snapshot.medallion.bronze == 0
    assert snapshot.medallion.silver == 0
    assert snapshot.medallion.gold == 0
    assert snapshot.ownership_topology == {}


# ── _build_metric_values ──────────────────────────────────────────────────────


async def test_metric_values_includes_enabled_excludes_disabled(service, db):
    """metric_values only includes metric_ids whose is_enabled=True definition exists."""
    # First execute: enabled_ids query → returns ["m1"]
    enabled_ids_result = MagicMock()
    enabled_ids_result.all.return_value = [("m1",)]

    # Second execute: windowed latest-value query → returns one row
    metric_row = MagicMock()
    metric_row.metric_id = "m1"
    metric_row.value = 0.75
    values_result = MagicMock()
    values_result.all.return_value = [metric_row]

    db.execute = AsyncMock(side_effect=[enabled_ids_result, values_result])

    result = await service._build_metric_values()

    assert result == {"m1": 0.75}


async def test_metric_values_empty_when_no_enabled_definitions(service, db):
    """metric_values returns {} when no enabled metric definitions exist."""
    enabled_ids_result = MagicMock()
    enabled_ids_result.all.return_value = []
    db.execute = AsyncMock(return_value=enabled_ids_result)

    result = await service._build_metric_values()
    assert result == {}
    # Only one db.execute call (no windowed query issued for empty list)
    assert db.execute.await_count == 1


async def test_metric_values_latest_result_wins(service, db):
    """The windowed query returns latest measured_at row per metric_id."""
    enabled_ids_result = MagicMock()
    enabled_ids_result.all.return_value = [("freshness",)]

    latest_row = MagicMock()
    latest_row.metric_id = "freshness"
    latest_row.value = 0.92
    values_result = MagicMock()
    values_result.all.return_value = [latest_row]

    db.execute = AsyncMock(side_effect=[enabled_ids_result, values_result])

    result = await service._build_metric_values()
    assert result["freshness"] == 0.92


# ── _build_per_dataset_breakdown ──────────────────────────────────────────────


async def test_per_dataset_breakdown_mirrors_datasets_list(service, db):
    """per_dataset_breakdown[metric_id] mirrors breakdown.get('datasets', []) from latest result."""
    breakdown_row = make_metric_breakdown_row()
    breakdown_row.metric_id = "ingestion-freshness"

    enabled_ids_result = MagicMock()
    enabled_ids_result.all.return_value = [("ingestion-freshness",)]

    breakdown_result = MagicMock()
    breakdown_result.all.return_value = [breakdown_row]

    db.execute = AsyncMock(side_effect=[enabled_ids_result, breakdown_result])

    result = await service._build_per_dataset_breakdown()

    assert "ingestion-freshness" in result
    datasets = result["ingestion-freshness"]
    assert isinstance(datasets, list)
    assert len(datasets) == 2  # make_metric_breakdown_row default has 2 datasets


async def test_per_dataset_breakdown_empty_when_no_enabled(service, db):
    """per_dataset_breakdown returns {} when no enabled metrics exist."""
    enabled_ids_result = MagicMock()
    enabled_ids_result.all.return_value = []
    db.execute = AsyncMock(return_value=enabled_ids_result)

    result = await service._build_per_dataset_breakdown()
    assert result == {}


async def test_per_dataset_breakdown_none_breakdown_yields_empty_list(service, db):
    """A metric result row with breakdown=None yields an empty list for that metric_id."""
    row = MagicMock()
    row.metric_id = "validation-score"
    row.breakdown = None

    enabled_ids_result = MagicMock()
    enabled_ids_result.all.return_value = [("validation-score",)]

    breakdown_result = MagicMock()
    breakdown_result.all.return_value = [row]

    db.execute = AsyncMock(side_effect=[enabled_ids_result, breakdown_result])

    result = await service._build_per_dataset_breakdown()
    assert result["validation-score"] == []


# ── _build_ontology_graph ─────────────────────────────────────────────────────


async def test_ontology_graph_nodes_only_approved(service, db):
    """ontology_graph.nodes includes only rows with status='approved'."""
    approved_nodes = [
        make_ontogen_node_row(id=f"node-{i}", name=f"Node {i}", status="approved")
        for i in range(3)
    ]

    node_result = MagicMock()
    node_result.scalars.return_value.all.return_value = approved_nodes

    edge_result = MagicMock()
    edge_result.scalars.return_value.all.return_value = []

    triple_result = MagicMock()
    triple_result.scalars.return_value.all.return_value = []

    db.execute = AsyncMock(side_effect=[node_result, edge_result, triple_result])

    graph = await service._build_ontology_graph()

    assert len(graph.nodes) == 3
    assert all(n.type == "ontogen_node" for n in graph.nodes)


async def test_ontology_graph_edges_reference_edge_label(service, db):
    """ontology_graph.edges use ontogen_edges.label for edge metadata."""
    subj = make_ontogen_node_row(id="book", name="Book", status="approved")
    obj = make_ontogen_node_row(id="edition", name="Edition", status="approved")
    edge = make_ontogen_edge_row(id="has-edition", label="has edition", status="approved")
    triple = make_ontogen_triple_row(
        subject_node_id="book", edge_id="has-edition", object_node_id="edition", status="approved"
    )

    node_result = MagicMock()
    node_result.scalars.return_value.all.return_value = [subj, obj]

    edge_result = MagicMock()
    edge_result.scalars.return_value.all.return_value = [edge]

    triple_result = MagicMock()
    triple_result.scalars.return_value.all.return_value = [triple]

    db.execute = AsyncMock(side_effect=[node_result, edge_result, triple_result])

    graph = await service._build_ontology_graph()

    assert len(graph.edges) == 1
    assert graph.edges[0].source == "book"
    assert graph.edges[0].target == "edition"
    assert graph.edges[0].type == "ontogen_triple"
    assert graph.edges[0].metadata["edge_label"] == "has edition"


async def test_ontology_graph_empty_when_no_approved_nodes(service, db):
    """ontology_graph is empty when no approved OntogenNode rows exist (early return)."""
    node_result = MagicMock()
    node_result.scalars.return_value.all.return_value = []

    db.execute = AsyncMock(return_value=node_result)

    graph = await service._build_ontology_graph()

    assert graph.nodes == []
    assert graph.edges == []
    # Only one db.execute call (edge_q and triple_q not issued)
    assert db.execute.await_count == 1


async def test_ontology_graph_triple_endpoints_must_be_approved(service, db):
    """Triples with non-approved endpoints are excluded from the graph."""
    approved_node = make_ontogen_node_row(id="book", name="Book", status="approved")
    edge = make_ontogen_edge_row(id="has-edition", label="has edition", status="approved")
    # The SQL query filters triples by node_ids ∈ approved set; mock simulates no matching rows
    # because the object_node_id ("missing") is not in the approved node set.
    make_ontogen_triple_row(
        subject_node_id="book", edge_id="has-edition", object_node_id="missing", status="approved"
    )

    node_result = MagicMock()
    node_result.scalars.return_value.all.return_value = [approved_node]

    edge_result = MagicMock()
    edge_result.scalars.return_value.all.return_value = [edge]

    triple_result = MagicMock()
    triple_result.scalars.return_value.all.return_value = []

    db.execute = AsyncMock(side_effect=[node_result, edge_result, triple_result])

    graph = await service._build_ontology_graph()

    assert len(graph.nodes) == 1
    assert len(graph.edges) == 0


# ── _build_dataset_sections (blind spots, medallion, ownership) ───────────────


async def test_medallion_counts_from_upstream_classification(service, db, datahub):
    """Medallion tallies bronze/silver/gold from upstream-count classification."""
    urn_bronze = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.raw,PROD)"
    urn_silver = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.clean,PROD)"
    urn_gold = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.curated,PROD)"

    datahub.enumerate_datasets = AsyncMock(return_value=[urn_bronze, urn_silver, urn_gold])
    datahub.get_upstream_lineage = AsyncMock(side_effect=lambda urn: (
        []
        if urn == urn_bronze
        else [urn_bronze]
        if urn == urn_silver
        else [urn_bronze, urn_silver, "urn:other"]
    ))
    datahub.get_aspect = AsyncMock(return_value=None)

    approved_q_result = MagicMock()
    approved_q_result.__iter__ = MagicMock(return_value=iter([]))
    db.execute = AsyncMock(return_value=approved_q_result)

    blind_spots, medallion, ownership_topology = await service._build_dataset_sections()

    assert medallion.bronze == 1
    assert medallion.silver == 1
    assert medallion.gold == 1


async def test_blind_spots_datasets_without_approved_node_map(service, db, datahub):
    """A dataset with no approved DatasetNodeMap row is a blind spot."""
    urn_a = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.a,PROD)"
    urn_b = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.b,PROD)"
    urn_c = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.c,PROD)"

    datahub.enumerate_datasets = AsyncMock(return_value=[urn_a, urn_b, urn_c])
    datahub.get_upstream_lineage = AsyncMock(return_value=[])
    datahub.get_aspect = AsyncMock(return_value=None)

    map_row_a = make_dataset_node_map_row(dataset_urn=urn_a, status="approved")

    approved_q_result = MagicMock()
    # The query selects DatasetNodeMap.dataset_urn; rows have .dataset_urn attribute
    approved_q_result.__iter__ = MagicMock(return_value=iter([map_row_a]))
    db.execute = AsyncMock(return_value=approved_q_result)

    blind_spots, medallion, _ = await service._build_dataset_sections()

    assert urn_a not in blind_spots
    assert urn_b in blind_spots
    assert urn_c in blind_spots
    assert len(blind_spots) == 2


async def test_blind_spots_empty_when_all_mapped(service, db, datahub):
    """No blind spots when all datasets have approved DatasetNodeMap rows."""
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.a,PROD)"

    datahub.enumerate_datasets = AsyncMock(return_value=[urn])
    datahub.get_upstream_lineage = AsyncMock(return_value=[])
    datahub.get_aspect = AsyncMock(return_value=None)

    map_row = make_dataset_node_map_row(dataset_urn=urn, status="approved")
    approved_q_result = MagicMock()
    approved_q_result.__iter__ = MagicMock(return_value=iter([map_row]))
    db.execute = AsyncMock(return_value=approved_q_result)

    blind_spots, _, _ = await service._build_dataset_sections()
    assert blind_spots == []


async def test_ownership_topology_groups_datasets_by_owner(service, db, datahub):
    """ownership_topology[owner_urn] collects dataset_urns for that owner."""
    urn_a = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.a,PROD)"
    urn_b = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.b,PROD)"
    owner_urn = "urn:li:corpuser:alice"

    datahub.enumerate_datasets = AsyncMock(return_value=[urn_a, urn_b])
    datahub.get_upstream_lineage = AsyncMock(return_value=[])

    from unittest.mock import MagicMock as MM

    def _make_ownership(*urns):
        ownership = MM()
        owners = []
        for u in urns:
            o = MM()
            o.owner = u
            owners.append(o)
        ownership.owners = owners
        return ownership

    # Both datasets owned by alice
    datahub.get_aspect = AsyncMock(return_value=_make_ownership(owner_urn))

    approved_q_result = MagicMock()
    approved_q_result.__iter__ = MagicMock(return_value=iter([]))
    db.execute = AsyncMock(return_value=approved_q_result)

    _, _, ownership_topology = await service._build_dataset_sections()

    assert owner_urn in ownership_topology
    assert set(ownership_topology[owner_urn]) == {urn_a, urn_b}


async def test_ownership_failed_get_aspect_skipped(service, db, datahub):
    """Failed OwnershipClass reads are skipped (best-effort); service does not raise."""
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.a,PROD)"

    datahub.enumerate_datasets = AsyncMock(return_value=[urn])
    datahub.get_upstream_lineage = AsyncMock(return_value=[])
    datahub.get_aspect = AsyncMock(side_effect=Exception("DataHub unavailable"))

    approved_q_result = MagicMock()
    approved_q_result.__iter__ = MagicMock(return_value=iter([]))
    db.execute = AsyncMock(return_value=approved_q_result)

    # Should not raise; ownership_topology will be empty
    blind_spots, medallion, ownership_topology = await service._build_dataset_sections()
    assert ownership_topology == {}


# ── get_config ────────────────────────────────────────────────────────────────


async def test_get_config_returns_existing(service, db):
    """get_config returns the existing singleton row from DB."""
    config_row = _make_config_row(layout="hierarchical", color_by="medallion")
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = config_row
    db.execute = AsyncMock(return_value=result_mock)

    config = await service.get_config()
    assert config.layout == "hierarchical"
    assert config.color_by == "medallion"


async def test_get_config_creates_default_when_missing(service, db):
    """get_config creates a default row when none exists."""
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result_mock)

    async def _fake_refresh(obj):
        obj.layout = "force"
        obj.color_by = "quality_score"
        obj.filters = {}
        obj.updated_at = datetime.now(tz=UTC)

    db.refresh = AsyncMock(side_effect=_fake_refresh)

    config = await service.get_config()
    assert config.layout == "force"
    assert config.color_by == "quality_score"
    assert config.filters == {}
    assert db.add.called
    assert db.commit.await_count == 1


# ── patch_config ──────────────────────────────────────────────────────────────


async def test_patch_config_updates_layout(service, db):
    """patch_config updates only the keys passed; other fields remain unchanged."""
    config_row = _make_config_row()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = config_row
    db.execute = AsyncMock(return_value=result_mock)
    mock_db_refresh(db)

    config = await service.patch_config(layout="hierarchical")
    assert config.layout == "hierarchical"
    assert config.color_by == "quality_score"
    assert db.commit.await_count == 1


async def test_patch_config_partial_update_color_by(service, db):
    """patch_config with only color_by leaves layout unchanged."""
    config_row = _make_config_row(layout="force", color_by="quality_score")
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = config_row
    db.execute = AsyncMock(return_value=result_mock)
    mock_db_refresh(db)

    config = await service.patch_config(color_by="medallion")
    assert config.layout == "force"
    assert config.color_by == "medallion"
