"""Unit tests for OverviewService (mocked infrastructure — triple-graph model)."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backend.overview.service import OverviewService, _classify_medallion
from tests.unit.backend.conftest import (
    make_dataset_node_map_row,
    make_ontogen_edge_row,
    make_ontogen_node_row,
    make_ontogen_triple_row,
    make_quality_score_mock,
    mock_db_refresh,
)


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


def _empty_nodes_result() -> MagicMock:
    """Return a DB result mock for node_q that yields no approved nodes.

    When node_ids is empty, _build_triple_graph returns early — edge_q and
    triple_q are NOT issued.  The next db.execute call in get_overview is the
    maps query (blind spots).
    """
    r = MagicMock()
    r.scalars.return_value.all.return_value = []
    return r


def _empty_maps_result() -> MagicMock:
    """Return a DB result mock for the approved_maps_q that yields no rows."""
    r = MagicMock()
    r.__iter__ = MagicMock(return_value=iter([]))
    return r


@pytest.fixture
def age():
    """Mock AgeGraph — no real AGE connection."""
    return AsyncMock()


@pytest.fixture
def service(datahub, db, cache, age):
    return OverviewService(datahub=datahub, db=db, cache=cache, age=age)


# ── classify_medallion ────────────────────────────────────────────────────


def test_medallion_bronze_no_upstream():
    assert _classify_medallion(0) == "bronze"


def test_medallion_silver_one_upstream():
    assert _classify_medallion(1) == "silver"


def test_medallion_silver_two_upstream():
    assert _classify_medallion(2) == "silver"


def test_medallion_gold_three_plus_upstream():
    assert _classify_medallion(3) == "gold"
    assert _classify_medallion(10) == "gold"


# ── get_overview: triple-graph nodes ──────────────────────────────────────


@patch("src.backend.overview.service.compute_quality_score")
async def test_get_overview_assembles_ontogen_nodes(mock_quality, service, db, datahub):
    """Approved OntogenNode rows produce graph nodes of type 'ontogen_node'.

    When nodes are present, _build_triple_graph issues 3 DB calls:
    node_q, edge_q, triple_q.  Then get_overview issues a 4th for maps.
    """
    node_rows = [make_ontogen_node_row(id=f"node-{i}", name=f"Node {i}", status="approved") for i in range(3)]

    nodes_result = MagicMock()
    nodes_result.scalars.return_value.all.return_value = node_rows

    edges_result = MagicMock()
    edges_result.scalars.return_value.all.return_value = []

    triples_result = MagicMock()
    triples_result.scalars.return_value.all.return_value = []

    maps_result = _empty_maps_result()

    db.execute = AsyncMock(side_effect=[nodes_result, edges_result, triples_result, maps_result])
    datahub.enumerate_datasets = AsyncMock(return_value=[])

    snapshot = await service.get_overview()
    ontogen_nodes = [n for n in snapshot.nodes if n.type == "ontogen_node"]
    assert len(ontogen_nodes) == 3


@patch("src.backend.overview.service.compute_quality_score")
async def test_get_overview_triple_graph_edges(mock_quality, service, db, datahub):
    """Approved triples produce graph edges of type 'ontogen_triple'.

    4 DB calls: node_q, edge_q, triple_q, maps_q.
    """
    subj = make_ontogen_node_row(id="book", name="Book", status="approved")
    obj = make_ontogen_node_row(id="edition", name="Edition", status="approved")
    edge = make_ontogen_edge_row(id="has-edition", label="has edition", status="approved")
    triple = make_ontogen_triple_row(
        subject_node_id="book", edge_id="has-edition", object_node_id="edition", status="approved"
    )

    nodes_result = MagicMock()
    nodes_result.scalars.return_value.all.return_value = [subj, obj]

    edges_result = MagicMock()
    edges_result.scalars.return_value.all.return_value = [edge]

    triples_result = MagicMock()
    triples_result.scalars.return_value.all.return_value = [triple]

    maps_result = _empty_maps_result()

    db.execute = AsyncMock(side_effect=[nodes_result, edges_result, triples_result, maps_result])
    datahub.enumerate_datasets = AsyncMock(return_value=[])

    snapshot = await service.get_overview()
    ontology_edges = [e for e in snapshot.edges if e.type == "ontogen_triple"]
    assert len(ontology_edges) == 1
    assert ontology_edges[0].source == "book"
    assert ontology_edges[0].target == "edition"


# ── get_overview: dataset nodes ───────────────────────────────────────────


@patch("src.backend.overview.service.compute_quality_score")
async def test_get_overview_assembles_dataset_nodes(mock_quality, service, db, datahub):
    """Dataset nodes are assembled from datahub.enumerate_datasets().

    No approved ontogen nodes → 2 DB calls: node_q (empty, early return), maps_q.
    """
    mock_quality.return_value = make_quality_score_mock(75.0)

    # No approved nodes → _build_triple_graph returns early after node_q
    db.execute = AsyncMock(side_effect=[_empty_nodes_result(), _empty_maps_result()])

    urns = [
        "urn:li:dataset:(urn:li:dataPlatform:postgres,db.table_a,PROD)",
        "urn:li:dataset:(urn:li:dataPlatform:postgres,db.table_b,PROD)",
    ]
    datahub.enumerate_datasets = AsyncMock(return_value=urns)
    datahub.get_upstream_lineage = AsyncMock(return_value=[])

    snapshot = await service.get_overview()
    dataset_nodes = [n for n in snapshot.nodes if n.type == "dataset"]
    assert len(dataset_nodes) == 2
    assert dataset_nodes[0].metadata["quality_score"] == 75.0


# ── get_overview: lineage edges ───────────────────────────────────────────


@patch("src.backend.overview.service.compute_quality_score")
async def test_get_overview_includes_lineage_edges(mock_quality, service, db, datahub):
    """Upstream lineage produces edges of type 'lineage'.

    No approved nodes → 2 DB calls.
    """
    mock_quality.return_value = make_quality_score_mock(50.0)

    db.execute = AsyncMock(side_effect=[_empty_nodes_result(), _empty_maps_result()])

    urn_a = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.raw,PROD)"
    urn_b = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.clean,PROD)"
    datahub.enumerate_datasets = AsyncMock(return_value=[urn_a, urn_b])

    async def _upstream(urn):
        if urn == urn_b:
            return [urn_a]
        return []

    datahub.get_upstream_lineage = AsyncMock(side_effect=_upstream)

    snapshot = await service.get_overview()
    lineage_edges = [e for e in snapshot.edges if e.type == "lineage"]
    assert len(lineage_edges) == 1
    assert lineage_edges[0].source == urn_a
    assert lineage_edges[0].target == urn_b


# ── get_overview: medallion summary ───────────────────────────────────────


@patch("src.backend.overview.service.compute_quality_score")
async def test_medallion_summary_counts(mock_quality, service, db, datahub):
    """Medallion counts reflect upstream depth. No approved nodes → 2 DB calls."""
    mock_quality.return_value = make_quality_score_mock(60.0)

    db.execute = AsyncMock(side_effect=[_empty_nodes_result(), _empty_maps_result()])

    urn_bronze = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.raw,PROD)"
    urn_silver = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.clean,PROD)"
    urn_gold = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.curated,PROD)"
    datahub.enumerate_datasets = AsyncMock(return_value=[urn_bronze, urn_silver, urn_gold])

    async def _upstream(urn):
        if urn == urn_silver:
            return [urn_bronze]
        if urn == urn_gold:
            return [urn_bronze, urn_silver, "urn:other"]
        return []

    datahub.get_upstream_lineage = AsyncMock(side_effect=_upstream)

    snapshot = await service.get_overview()
    assert snapshot.medallion.bronze == 1
    assert snapshot.medallion.silver == 1
    assert snapshot.medallion.gold == 1


# ── get_overview: blind spots (dataset_node_map-based) ────────────────────


@patch("src.backend.overview.service.compute_quality_score")
async def test_blind_spots_datasets_without_approved_node_map(mock_quality, service, db, datahub):
    """A dataset with no approved DatasetNodeMap row is a blind spot.

    No approved nodes → 2 DB calls: node_q (empty), maps_q.
    """
    mock_quality.return_value = make_quality_score_mock(70.0)

    urn_a = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.a,PROD)"
    urn_b = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.b,PROD)"
    urn_c = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.c,PROD)"

    # urn_a has an approved mapping; urn_b and urn_c do not
    map_row_a = make_dataset_node_map_row(dataset_urn=urn_a, status="approved")

    maps_result = MagicMock()
    maps_result.__iter__ = MagicMock(return_value=iter([map_row_a]))

    db.execute = AsyncMock(side_effect=[_empty_nodes_result(), maps_result])

    datahub.enumerate_datasets = AsyncMock(return_value=[urn_a, urn_b, urn_c])
    datahub.get_upstream_lineage = AsyncMock(return_value=[])

    snapshot = await service.get_overview()
    assert len(snapshot.blind_spots) == 2
    assert urn_a not in snapshot.blind_spots
    assert urn_b in snapshot.blind_spots
    assert urn_c in snapshot.blind_spots


@patch("src.backend.overview.service.compute_quality_score")
async def test_blind_spots_empty_when_all_mapped_approved(mock_quality, service, db, datahub):
    """No blind spots when all datasets have approved DatasetNodeMap rows.

    No approved nodes → 2 DB calls.
    """
    mock_quality.return_value = make_quality_score_mock(80.0)

    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.a,PROD)"
    map_row = make_dataset_node_map_row(dataset_urn=urn, status="approved")

    maps_result = MagicMock()
    maps_result.__iter__ = MagicMock(return_value=iter([map_row]))

    db.execute = AsyncMock(side_effect=[_empty_nodes_result(), maps_result])

    datahub.enumerate_datasets = AsyncMock(return_value=[urn])
    datahub.get_upstream_lineage = AsyncMock(return_value=[])

    snapshot = await service.get_overview()
    assert snapshot.blind_spots == []


@patch("src.backend.overview.service.compute_quality_score")
async def test_blind_spots_all_when_no_mappings(mock_quality, service, db, datahub):
    """All datasets are blind spots when no DatasetNodeMap rows exist.

    No approved nodes → 2 DB calls.
    """
    mock_quality.return_value = make_quality_score_mock(60.0)

    urns = [
        "urn:li:dataset:(urn:li:dataPlatform:postgres,db.a,PROD)",
        "urn:li:dataset:(urn:li:dataPlatform:postgres,db.b,PROD)",
    ]

    db.execute = AsyncMock(side_effect=[_empty_nodes_result(), _empty_maps_result()])

    datahub.enumerate_datasets = AsyncMock(return_value=urns)
    datahub.get_upstream_lineage = AsyncMock(return_value=[])

    snapshot = await service.get_overview()
    assert len(snapshot.blind_spots) == 2


# ── get_overview: stats ───────────────────────────────────────────────────


@patch("src.backend.overview.service.compute_quality_score")
async def test_overview_stats_calculated(mock_quality, service, db, datahub):
    """Stats reflect quality scores and blind spot count. No approved nodes → 2 DB calls."""
    scores = [
        make_quality_score_mock(80.0),
        make_quality_score_mock(0.0),
        make_quality_score_mock(60.0),
    ]
    mock_quality.side_effect = scores

    db.execute = AsyncMock(side_effect=[_empty_nodes_result(), _empty_maps_result()])

    urns = ["urn:a", "urn:b", "urn:c"]
    datahub.enumerate_datasets = AsyncMock(return_value=urns)
    datahub.get_upstream_lineage = AsyncMock(return_value=[])

    snapshot = await service.get_overview()
    assert snapshot.stats.total_datasets == 3
    assert snapshot.stats.monitored_datasets == 2  # scores > 0: 80, 60
    assert snapshot.stats.avg_quality_score == round((80.0 + 0.0 + 60.0) / 3, 2)
    assert snapshot.stats.issues_count == 3  # all are blind spots (no mappings)


# ── get_config ────────────────────────────────────────────────────────────


async def test_get_config_returns_existing(service, db):
    config_row = _make_config_row(layout="hierarchical", color_by="medallion")
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = config_row
    db.execute = AsyncMock(return_value=result_mock)

    config = await service.get_config()
    assert config.layout == "hierarchical"
    assert config.color_by == "medallion"


async def test_get_config_creates_default_when_missing(service, db):
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


# ── patch_config ──────────────────────────────────────────────────────────


async def test_patch_config_updates_fields(service, db):
    config_row = _make_config_row()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = config_row
    db.execute = AsyncMock(return_value=result_mock)
    mock_db_refresh(db)

    config = await service.patch_config(layout="hierarchical")
    assert config.layout == "hierarchical"
    assert config.color_by == "quality_score"
    assert db.commit.await_count == 1


async def test_patch_config_partial_update(service, db):
    config_row = _make_config_row(layout="force", color_by="quality_score")
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = config_row
    db.execute = AsyncMock(return_value=result_mock)
    mock_db_refresh(db)

    config = await service.patch_config(color_by="medallion")
    assert config.layout == "force"
    assert config.color_by == "medallion"
