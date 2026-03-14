"""Unit tests for OverviewService (mocked infrastructure)."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backend.overview.service import OverviewService, _classify_medallion
from tests.unit.backend.conftest import (
    make_concept_row,
    make_quality_score_mock,
    make_relationship_row,
    mock_db_refresh,
)


def _make_dataset_concept_map_row(
    dataset_urn: str = "urn:li:dataset:(urn:li:dataPlatform:postgres,test.table,PROD)",
    concept_id: uuid.UUID | None = None,
    confidence_score: float = 0.9,
):
    row = MagicMock()
    row.dataset_urn = dataset_urn
    row.concept_id = concept_id or uuid.uuid4()
    row.confidence_score = confidence_score
    row.created_at = datetime.now(tz=UTC)
    return row


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


@pytest.fixture
def service(datahub, db, cache):
    return OverviewService(datahub=datahub, db=db, cache=cache)


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


# ── get_overview: concept nodes ───────────────────────────────────────────


@patch("src.backend.overview.service.compute_quality_score")
async def test_get_overview_assembles_concept_nodes(mock_quality, service, db, datahub):
    concept_rows = [make_concept_row(name=f"concept_{i}", status="approved") for i in range(3)]

    # DB queries: concepts, relationships, dataset_concept_map
    concepts_result = MagicMock()
    concepts_result.scalars.return_value.all.return_value = concept_rows

    rels_result = MagicMock()
    rels_result.scalars.return_value.all.return_value = []

    maps_result = MagicMock()
    maps_result.scalars.return_value.all.return_value = []

    db.execute = AsyncMock(side_effect=[concepts_result, rels_result, maps_result])
    datahub.enumerate_datasets = AsyncMock(return_value=[])

    snapshot = await service.get_overview()
    concept_nodes = [n for n in snapshot.nodes if n.type == "concept"]
    assert len(concept_nodes) == 3


# ── get_overview: dataset nodes ───────────────────────────────────────────


@patch("src.backend.overview.service.compute_quality_score")
async def test_get_overview_assembles_dataset_nodes(mock_quality, service, db, datahub):
    mock_quality.return_value = make_quality_score_mock(75.0)

    concepts_result = MagicMock()
    concepts_result.scalars.return_value.all.return_value = []

    rels_result = MagicMock()
    rels_result.scalars.return_value.all.return_value = []

    maps_result = MagicMock()
    maps_result.scalars.return_value.all.return_value = []

    db.execute = AsyncMock(side_effect=[concepts_result, rels_result, maps_result])

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


# ── get_overview: concept relationships ───────────────────────────────────


@patch("src.backend.overview.service.compute_quality_score")
async def test_get_overview_includes_concept_relationships(mock_quality, service, db, datahub):
    rel_rows = [make_relationship_row(), make_relationship_row()]

    concepts_result = MagicMock()
    concepts_result.scalars.return_value.all.return_value = []

    rels_result = MagicMock()
    rels_result.scalars.return_value.all.return_value = rel_rows

    maps_result = MagicMock()
    maps_result.scalars.return_value.all.return_value = []

    db.execute = AsyncMock(side_effect=[concepts_result, rels_result, maps_result])
    datahub.enumerate_datasets = AsyncMock(return_value=[])

    snapshot = await service.get_overview()
    cr_edges = [e for e in snapshot.edges if e.type == "concept_relationship"]
    assert len(cr_edges) == 2


# ── get_overview: lineage edges ───────────────────────────────────────────


@patch("src.backend.overview.service.compute_quality_score")
async def test_get_overview_includes_lineage_edges(mock_quality, service, db, datahub):
    mock_quality.return_value = make_quality_score_mock(50.0)

    concepts_result = MagicMock()
    concepts_result.scalars.return_value.all.return_value = []
    rels_result = MagicMock()
    rels_result.scalars.return_value.all.return_value = []
    maps_result = MagicMock()
    maps_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(side_effect=[concepts_result, rels_result, maps_result])

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


# ── get_overview: concept-dataset edges ───────────────────────────────────


@patch("src.backend.overview.service.compute_quality_score")
async def test_get_overview_includes_concept_dataset_edges(mock_quality, service, db, datahub):
    mock_quality.return_value = make_quality_score_mock(80.0)

    concept_id = uuid.uuid4()
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.table,PROD)"
    map_row = _make_dataset_concept_map_row(dataset_urn=urn, concept_id=concept_id)

    concepts_result = MagicMock()
    concepts_result.scalars.return_value.all.return_value = []
    rels_result = MagicMock()
    rels_result.scalars.return_value.all.return_value = []
    maps_result = MagicMock()
    maps_result.scalars.return_value.all.return_value = [map_row]
    db.execute = AsyncMock(side_effect=[concepts_result, rels_result, maps_result])

    datahub.enumerate_datasets = AsyncMock(return_value=[urn])
    datahub.get_upstream_lineage = AsyncMock(return_value=[])

    snapshot = await service.get_overview()
    cd_edges = [e for e in snapshot.edges if e.type == "concept_dataset"]
    assert len(cd_edges) == 1
    assert cd_edges[0].source == urn
    assert cd_edges[0].target == str(concept_id)


# ── get_overview: medallion summary ───────────────────────────────────────


@patch("src.backend.overview.service.compute_quality_score")
async def test_medallion_summary_counts(mock_quality, service, db, datahub):
    mock_quality.return_value = make_quality_score_mock(60.0)

    concepts_result = MagicMock()
    concepts_result.scalars.return_value.all.return_value = []
    rels_result = MagicMock()
    rels_result.scalars.return_value.all.return_value = []
    maps_result = MagicMock()
    maps_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(side_effect=[concepts_result, rels_result, maps_result])

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


# ── get_overview: blind spots ─────────────────────────────────────────────


@patch("src.backend.overview.service.compute_quality_score")
async def test_blind_spots_datasets_without_concept_mapping(mock_quality, service, db, datahub):
    mock_quality.return_value = make_quality_score_mock(70.0)

    urn_a = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.a,PROD)"
    urn_b = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.b,PROD)"
    urn_c = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.c,PROD)"

    map_row = _make_dataset_concept_map_row(dataset_urn=urn_a)

    concepts_result = MagicMock()
    concepts_result.scalars.return_value.all.return_value = []
    rels_result = MagicMock()
    rels_result.scalars.return_value.all.return_value = []
    maps_result = MagicMock()
    maps_result.scalars.return_value.all.return_value = [map_row]
    db.execute = AsyncMock(side_effect=[concepts_result, rels_result, maps_result])

    datahub.enumerate_datasets = AsyncMock(return_value=[urn_a, urn_b, urn_c])
    datahub.get_upstream_lineage = AsyncMock(return_value=[])

    snapshot = await service.get_overview()
    assert len(snapshot.blind_spots) == 2
    assert urn_a not in snapshot.blind_spots
    assert urn_b in snapshot.blind_spots
    assert urn_c in snapshot.blind_spots


@patch("src.backend.overview.service.compute_quality_score")
async def test_blind_spots_empty_when_all_mapped(mock_quality, service, db, datahub):
    mock_quality.return_value = make_quality_score_mock(80.0)

    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.a,PROD)"
    map_row = _make_dataset_concept_map_row(dataset_urn=urn)

    concepts_result = MagicMock()
    concepts_result.scalars.return_value.all.return_value = []
    rels_result = MagicMock()
    rels_result.scalars.return_value.all.return_value = []
    maps_result = MagicMock()
    maps_result.scalars.return_value.all.return_value = [map_row]
    db.execute = AsyncMock(side_effect=[concepts_result, rels_result, maps_result])

    datahub.enumerate_datasets = AsyncMock(return_value=[urn])
    datahub.get_upstream_lineage = AsyncMock(return_value=[])

    snapshot = await service.get_overview()
    assert snapshot.blind_spots == []


@patch("src.backend.overview.service.compute_quality_score")
async def test_blind_spots_all_when_no_mappings(mock_quality, service, db, datahub):
    mock_quality.return_value = make_quality_score_mock(60.0)

    urns = [
        "urn:li:dataset:(urn:li:dataPlatform:postgres,db.a,PROD)",
        "urn:li:dataset:(urn:li:dataPlatform:postgres,db.b,PROD)",
    ]

    concepts_result = MagicMock()
    concepts_result.scalars.return_value.all.return_value = []
    rels_result = MagicMock()
    rels_result.scalars.return_value.all.return_value = []
    maps_result = MagicMock()
    maps_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(side_effect=[concepts_result, rels_result, maps_result])

    datahub.enumerate_datasets = AsyncMock(return_value=urns)
    datahub.get_upstream_lineage = AsyncMock(return_value=[])

    snapshot = await service.get_overview()
    assert len(snapshot.blind_spots) == 2


# ── get_overview: stats ───────────────────────────────────────────────────


@patch("src.backend.overview.service.compute_quality_score")
async def test_overview_stats_calculated(mock_quality, service, db, datahub):
    scores = [
        make_quality_score_mock(80.0),
        make_quality_score_mock(0.0),
        make_quality_score_mock(60.0),
    ]
    mock_quality.side_effect = scores

    concepts_result = MagicMock()
    concepts_result.scalars.return_value.all.return_value = []
    rels_result = MagicMock()
    rels_result.scalars.return_value.all.return_value = []
    maps_result = MagicMock()
    maps_result.scalars.return_value.all.return_value = []
    db.execute = AsyncMock(side_effect=[concepts_result, rels_result, maps_result])

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

    # After insert + refresh, the row returned should have defaults
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
