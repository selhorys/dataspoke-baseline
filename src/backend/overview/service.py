"""Overview service — graph topology assembly, medallion classification, blind spot detection."""

import asyncio
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.validation.scoring import compute_quality_score
from src.shared.cache.client import RedisClient
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import (
    ConceptCategory,
    ConceptRelationship,
    DatasetConceptMap,
    OverviewConfig,
)

_CONCURRENCY_LIMIT = 10


class GraphNode:
    """A node in the overview graph (concept or dataset)."""

    __slots__ = ("id", "type", "label", "metadata")

    def __init__(self, id: str, type: str, label: str, metadata: dict[str, Any]) -> None:
        self.id = id
        self.type = type
        self.label = label
        self.metadata = metadata


class GraphEdge:
    """An edge in the overview graph."""

    __slots__ = ("source", "target", "type", "metadata")

    def __init__(self, source: str, target: str, type: str, metadata: dict[str, Any]) -> None:
        self.source = source
        self.target = target
        self.type = type
        self.metadata = metadata


class MedallionSummary:
    """Counts per medallion layer."""

    __slots__ = ("bronze", "silver", "gold")

    def __init__(self, bronze: int = 0, silver: int = 0, gold: int = 0) -> None:
        self.bronze = bronze
        self.silver = silver
        self.gold = gold


class SnapshotStats:
    """Summary statistics for the overview snapshot."""

    __slots__ = ("total_datasets", "monitored_datasets", "avg_quality_score", "issues_count")

    def __init__(
        self,
        total_datasets: int = 0,
        monitored_datasets: int = 0,
        avg_quality_score: float = 0.0,
        issues_count: int = 0,
    ) -> None:
        self.total_datasets = total_datasets
        self.monitored_datasets = monitored_datasets
        self.avg_quality_score = avg_quality_score
        self.issues_count = issues_count


class OverviewSnapshot:
    """Full graph topology snapshot."""

    __slots__ = ("nodes", "edges", "medallion", "blind_spots", "stats")

    def __init__(
        self,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        medallion: MedallionSummary,
        blind_spots: list[str],
        stats: SnapshotStats,
    ) -> None:
        self.nodes = nodes
        self.edges = edges
        self.medallion = medallion
        self.blind_spots = blind_spots
        self.stats = stats


class OverviewConfigRecord:
    """Value object mirroring the ORM OverviewConfig."""

    __slots__ = ("layout", "color_by", "filters", "updated_at")

    def __init__(
        self,
        layout: str,
        color_by: str,
        filters: dict[str, Any],
        updated_at: datetime,
    ) -> None:
        self.layout = layout
        self.color_by = color_by
        self.filters = filters
        self.updated_at = updated_at


def _classify_medallion(upstream_count: int) -> str:
    """Classify a dataset into a medallion layer based on upstream count."""
    if upstream_count == 0:
        return "bronze"
    if upstream_count <= 2:
        return "silver"
    return "gold"


class OverviewService:
    """Graph topology assembly, medallion classification, blind spot detection, and config CRUD."""

    def __init__(
        self,
        datahub: DataHubClient,
        db: AsyncSession,
        cache: RedisClient,
    ) -> None:
        self._datahub = datahub
        self._db = db
        self._cache = cache

    # ── Graph assembly ────────────────────────────────────────────────────

    async def get_overview(self) -> OverviewSnapshot:
        nodes: list[GraphNode] = []
        edges: list[GraphEdge] = []

        # Step 1: Ontology graph (PostgreSQL)
        concept_rows = (await self._db.execute(select(ConceptCategory))).scalars().all()
        for row in concept_rows:
            nodes.append(
                GraphNode(
                    id=str(row.id),
                    type="concept",
                    label=row.name,
                    metadata={
                        "description": row.description,
                        "parent_id": str(row.parent_id) if row.parent_id else None,
                        "status": row.status,
                    },
                )
            )

        rel_rows = (await self._db.execute(select(ConceptRelationship))).scalars().all()
        for row in rel_rows:
            edges.append(
                GraphEdge(
                    source=str(row.concept_a),
                    target=str(row.concept_b),
                    type="concept_relationship",
                    metadata={
                        "relationship_type": row.relationship_type,
                        "confidence_score": row.confidence_score,
                    },
                )
            )

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

        quality_scores = await asyncio.gather(*quality_tasks)
        upstream_lists = await asyncio.gather(*upstream_tasks)

        # Step 3 + 4: Build dataset nodes, lineage edges, medallion
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

        # Step 5: Blind spot detection + concept-dataset edges
        mapped_result = await self._db.execute(select(DatasetConceptMap))
        mapped_rows = mapped_result.scalars().all()
        mapped_urns: set[str] = set()
        for row in mapped_rows:
            mapped_urns.add(row.dataset_urn)
            edges.append(
                GraphEdge(
                    source=row.dataset_urn,
                    target=str(row.concept_id),
                    type="concept_dataset",
                    metadata={"confidence_score": row.confidence_score},
                )
            )

        blind_spots = sorted(urn for urn in dataset_urns if urn not in mapped_urns)

        # Step 6: Summary stats
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
