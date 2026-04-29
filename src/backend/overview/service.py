"""Overview service — triple-graph topology assembly, medallion classification, blind spots.

The graph snapshot is built from the approved ontogen triple-graph stored in
PostgreSQL (ontogen_nodes, ontogen_edges, ontogen_triples) and optionally
extended via AgeGraph.traverse for neighbourhood expansion.  Blind spots are
datasets that have no approved DatasetNodeMap row.

Spec: spec/feature/BACKEND.md §Overview Service
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.dataset.scoring import compute_quality_score
from src.shared.cache.client import RedisClient
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import (
    DatasetNodeMap,
    OntogenEdge,
    OntogenNode,
    OntogenTriple,
    OverviewConfig,
)
from src.shared.graph.client import AgeGraph

logger = logging.getLogger(__name__)

_CONCURRENCY_LIMIT = 10
_MAX_GRAPH_NODES = 200
_TRIPLE_GRAPH_MAX_HOPS = 2


class GraphNode(BaseModel):
    """A node in the overview graph (ontogen node or dataset)."""

    id: str
    type: str
    label: str
    metadata: dict[str, Any] = {}


class GraphEdge(BaseModel):
    """An edge in the overview graph."""

    source: str
    target: str
    type: str
    metadata: dict[str, Any] = {}


class MedallionSummary(BaseModel):
    """Counts per medallion layer."""

    bronze: int = 0
    silver: int = 0
    gold: int = 0


class SnapshotStats(BaseModel):
    """Summary statistics for the overview snapshot."""

    total_datasets: int = 0
    monitored_datasets: int = 0
    avg_quality_score: float = 0.0
    issues_count: int = 0


class OverviewSnapshot(BaseModel):
    """Full graph topology snapshot."""

    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    medallion: MedallionSummary
    blind_spots: list[str] = []
    stats: SnapshotStats


class OverviewConfigRecord(BaseModel):
    """Value object mirroring the ORM OverviewConfig."""

    layout: str
    color_by: str
    filters: dict[str, Any] = {}
    updated_at: datetime


def _classify_medallion(upstream_count: int) -> str:
    """Classify a dataset into a medallion layer based on upstream count.

    0 upstreams → bronze, 1-2 → silver, 3+ → gold.
    """
    if upstream_count == 0:
        return "bronze"
    if upstream_count <= 2:
        return "silver"
    return "gold"


class OverviewService:
    """Triple-graph topology assembly, medallion classification, blind spots, and config CRUD."""

    def __init__(
        self,
        datahub: DataHubClient,
        db: AsyncSession,
        cache: RedisClient,
        age: AgeGraph,
    ) -> None:
        self._datahub = datahub
        self._db = db
        self._cache = cache
        self._age = age

    # ── Graph assembly ────────────────────────────────────────────────────

    async def get_overview(self) -> OverviewSnapshot:
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []

        # Step 1: Build ontogen triple-graph from approved relational rows.
        # The AGE graph is a read-side replica; relational tables are SSOT.
        await self._build_triple_graph(nodes, edges)

        # Step 2: Dataset nodes (DataHub)
        dataset_urns = await self._datahub.enumerate_datasets()

        semaphore = asyncio.Semaphore(_CONCURRENCY_LIMIT)

        async def _get_quality(urn: str) -> float:
            async with semaphore:
                try:
                    score = await compute_quality_score(self._datahub, urn, cache=self._cache)
                    return score.overall_score
                except Exception:
                    return 0.0

        async def _get_upstream(urn: str) -> list[str]:
            async with semaphore:
                try:
                    return await self._datahub.get_upstream_lineage(urn)
                except Exception:
                    return []

        quality_tasks = [_get_quality(urn) for urn in dataset_urns]
        upstream_tasks = [_get_upstream(urn) for urn in dataset_urns]

        quality_scores, upstream_lists = await asyncio.gather(
            asyncio.gather(*quality_tasks),
            asyncio.gather(*upstream_tasks),
        )

        # Step 3: Build dataset nodes, lineage edges, medallion counts.
        seen_lineage_edges: set[tuple[str, str]] = set()
        bronze = silver = gold = 0

        for urn, q_score, upstreams in zip(dataset_urns, quality_scores, upstream_lists):
            layer = _classify_medallion(len(upstreams))
            if layer == "bronze":
                bronze += 1
            elif layer == "silver":
                silver += 1
            else:
                gold += 1

            nodes.append(
                GraphNode(
                    id=urn,
                    type="dataset",
                    label=urn.split(",")[-1].rstrip(")") if "," in urn else urn,
                    metadata={
                        "quality_score": q_score,
                        "medallion_layer": layer,
                    },
                )
            )

            for upstream_urn in upstreams:
                edge_key = (upstream_urn, urn)
                if edge_key not in seen_lineage_edges:
                    seen_lineage_edges.add(edge_key)
                    edges.append(
                        GraphEdge(
                            source=upstream_urn,
                            target=urn,
                            type="lineage",
                            metadata={},
                        )
                    )

        medallion = MedallionSummary(bronze=bronze, silver=silver, gold=gold)

        # Step 4: Blind spot detection — datasets with no approved DatasetNodeMap row.
        # A dataset is a blind spot if it has no approved node membership.
        approved_maps_q = select(DatasetNodeMap.dataset_urn).where(
            DatasetNodeMap.status == "approved"
        )
        approved_map_result = await self._db.execute(approved_maps_q)
        mapped_urns: set[str] = {row.dataset_urn for row in approved_map_result}

        blind_spots = sorted(urn for urn in dataset_urns if urn not in mapped_urns)

        # Step 5: Summary stats
        monitored = sum(1 for s in quality_scores if s > 0)
        avg_quality = (sum(quality_scores) / len(quality_scores)) if quality_scores else 0.0

        stats = SnapshotStats(
            total_datasets=len(dataset_urns),
            monitored_datasets=monitored,
            avg_quality_score=round(avg_quality, 2),
            issues_count=len(blind_spots),
        )

        return OverviewSnapshot(
            nodes=nodes,
            edges=edges,
            medallion=medallion,
            blind_spots=blind_spots,
            stats=stats,
        )

    async def _build_triple_graph(
        self,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
    ) -> None:
        """Populate *nodes* and *edges* from approved ontogen triple-graph.

        Uses relational tables (ontogen_nodes, ontogen_edges, ontogen_triples) as
        SSOT.  Attempts AGE.traverse to extend with neighbourhood triples;
        failures degrade gracefully to the relational snapshot.

        Caps at ``_MAX_GRAPH_NODES`` (200) ontogen nodes to bound response size.
        """
        # Load approved nodes
        node_q = (
            select(OntogenNode)
            .where(OntogenNode.status == "approved")
            .order_by(OntogenNode.created_at)
            .limit(_MAX_GRAPH_NODES)
        )
        node_rows = (await self._db.execute(node_q)).scalars().all()
        node_ids: set[str] = set()
        for row in node_rows:
            nodes.append(
                GraphNode(
                    id=row.id,
                    type="ontogen_node",
                    label=row.name,
                    metadata={
                        "description": row.description,
                        "confidence_score": row.confidence_score,
                        "glossary_term_urn": row.glossary_term_urn,
                    },
                )
            )
            node_ids.add(row.id)

        if not node_ids:
            return

        # Load approved edges
        edge_q = select(OntogenEdge).where(OntogenEdge.status == "approved")
        edge_rows = (await self._db.execute(edge_q)).scalars().all()
        edge_map: dict[str, OntogenEdge] = {row.id: row for row in edge_rows}

        # Load approved triples that connect known approved nodes
        triple_q = select(OntogenTriple).where(
            OntogenTriple.status == "approved",
            OntogenTriple.subject_node_id.in_(node_ids),
            OntogenTriple.object_node_id.in_(node_ids),
        )
        triple_rows = (await self._db.execute(triple_q)).scalars().all()

        seen_edges: set[str] = set()
        for triple in triple_rows:
            if triple.id in seen_edges:
                continue
            seen_edges.add(triple.id)
            edge_obj = edge_map.get(triple.edge_id)
            edges.append(
                GraphEdge(
                    source=triple.subject_node_id,
                    target=triple.object_node_id,
                    type="ontogen_triple",
                    metadata={
                        "edge_id": triple.edge_id,
                        "edge_label": edge_obj.label if edge_obj else triple.edge_id,
                        "confidence_score": triple.confidence_score,
                    },
                )
            )

        # Optional: extend graph via AGE.traverse from each node (best-effort).
        # Only add triples not already present to avoid duplicates.
        try:
            age_tuples: list[tuple[str, str, str]] = []
            for node_id in list(node_ids)[:10]:  # cap traversal seeds
                try:
                    tuples = await self._age.traverse(node_id, max_hops=_TRIPLE_GRAPH_MAX_HOPS)
                    age_tuples.extend(tuples)
                except Exception:
                    pass  # best-effort per-node

            for subject_id, edge_id, object_id in age_tuples:
                triple_id = f"{subject_id}__{edge_id}__{object_id}"
                if triple_id in seen_edges:
                    continue
                seen_edges.add(triple_id)
                edge_obj = edge_map.get(edge_id)
                edges.append(
                    GraphEdge(
                        source=subject_id,
                        target=object_id,
                        type="ontogen_triple",
                        metadata={
                            "edge_id": edge_id,
                            "edge_label": edge_obj.label if edge_obj else edge_id,
                            "source": "age_traverse",
                        },
                    )
                )
        except Exception:
            logger.warning(
                "overview_age_traverse_failed",
                exc_info=True,
            )

    # ── Config CRUD ───────────────────────────────────────────────────────

    async def get_config(self) -> OverviewConfigRecord:
        result = await self._db.execute(select(OverviewConfig).where(OverviewConfig.id == 1))
        row = result.scalar_one_or_none()
        if row is None:
            row = OverviewConfig(id=1, layout="force", color_by="quality_score", filters={})
            self._db.add(row)
            await self._db.commit()
            await self._db.refresh(row)
        return OverviewConfigRecord(
            layout=row.layout,
            color_by=row.color_by,
            filters=row.filters,
            updated_at=row.updated_at,
        )

    async def patch_config(
        self,
        layout: str | None = None,
        color_by: str | None = None,
        filters: dict[str, Any] | None = None,
    ) -> OverviewConfigRecord:
        result = await self._db.execute(select(OverviewConfig).where(OverviewConfig.id == 1))
        row = result.scalar_one_or_none()
        if row is None:
            row = OverviewConfig(id=1, layout="force", color_by="quality_score", filters={})
            self._db.add(row)
            await self._db.flush()

        if layout is not None:
            row.layout = layout
        if color_by is not None:
            row.color_by = color_by
        if filters is not None:
            row.filters = filters

        row.updated_at = datetime.now(tz=UTC)
        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)
        return OverviewConfigRecord(
            layout=row.layout,
            color_by=row.color_by,
            filters=row.filters,
            updated_at=row.updated_at,
        )
