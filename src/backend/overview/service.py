"""Overview service — metric values, per-dataset breakdown, medallion, blind spots,
ontology graph, and ownership topology.

Spec: spec/feature/BACKEND.md §Overview Service
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.datahub.client import DataHubClient
from src.shared.db.models import (
    DatasetNodeMap,
    MetricDefinition,
    MetricResult,
    OntogenEdge,
    OntogenNode,
    OntogenTriple,
    OverviewConfig,
)

logger = logging.getLogger(__name__)

_CONCURRENCY_LIMIT = 10
_MAX_GRAPH_NODES = 200


class GraphNode(BaseModel):
    id: str
    type: str
    label: str
    metadata: dict[str, Any] = {}


class GraphEdge(BaseModel):
    source: str
    target: str
    type: str
    metadata: dict[str, Any] = {}


class OntologyGraph(BaseModel):
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []


class MedallionSummary(BaseModel):
    bronze: int = 0
    silver: int = 0
    gold: int = 0


class OverviewSnapshot(BaseModel):
    metric_values: dict[str, float] = {}
    per_dataset_breakdown: dict[str, list[dict[str, Any]]] = {}
    blind_spots: list[str] = []
    ontology_graph: OntologyGraph = OntologyGraph()
    medallion: MedallionSummary = MedallionSummary()
    ownership_topology: dict[str, list[str]] = {}


class OverviewConfigRecord(BaseModel):
    layout: str
    color_by: str
    filters: dict[str, Any] = {}
    updated_at: datetime


def _classify_medallion(upstream_count: int) -> str:
    if upstream_count == 0:
        return "bronze"
    if upstream_count <= 2:
        return "silver"
    return "gold"


class OverviewService:
    """Assembles the six-section governance overview snapshot and manages config CRUD."""

    def __init__(
        self,
        datahub: DataHubClient,
        db: AsyncSession,
    ) -> None:
        self._datahub = datahub
        self._db = db

    async def get_overview(self) -> OverviewSnapshot:
        # AsyncSession is not safe for concurrent tasks — issue DB queries sequentially.
        # DataHub fan-out inside _build_dataset_sections stays concurrent (uses DataHubClient
        # with its own Semaphore, not the SQLAlchemy session).
        metric_values, per_dataset_breakdown = await self._build_metric_sections()
        ontology_graph = await self._build_ontology_graph()
        blind_spots, medallion, ownership_topology = await self._build_dataset_sections()

        return OverviewSnapshot(
            metric_values=metric_values,
            per_dataset_breakdown=per_dataset_breakdown,
            blind_spots=blind_spots,
            ontology_graph=ontology_graph,
            medallion=medallion,
            ownership_topology=ownership_topology,
        )

    # ── Metric sections (values + per-dataset breakdown, fused) ──────────────

    async def _build_metric_sections(
        self,
    ) -> tuple[dict[str, float], dict[str, list[dict[str, Any]]]]:
        """Return (metric_values, per_dataset_breakdown) from a single enabled_ids query.

        Issues one enabled_ids query then one windowed query selecting both value and
        breakdown in a single pass, avoiding a duplicate DB round-trip.
        """
        enabled_q = select(MetricDefinition.id).where(MetricDefinition.is_enabled.is_(True))
        enabled_result = await self._db.execute(enabled_q)
        enabled_ids = [row[0] for row in enabled_result.all()]
        if not enabled_ids:
            return {}, {}

        # Windowed query: row_number() over (partition by metric_id order by measured_at desc)
        row_num = (
            func.row_number()
            .over(
                partition_by=MetricResult.metric_id,
                order_by=MetricResult.measured_at.desc(),
            )
            .label("rn")
        )
        subq = (
            select(MetricResult.metric_id, MetricResult.value, MetricResult.breakdown, row_num)
            .where(MetricResult.metric_id.in_(enabled_ids))
            .subquery()
        )
        latest_q = select(subq.c.metric_id, subq.c.value, subq.c.breakdown).where(subq.c.rn == 1)
        result = await self._db.execute(latest_q)

        metric_values: dict[str, float] = {}
        per_dataset_breakdown: dict[str, list[dict[str, Any]]] = {}
        for row in result.all():
            metric_values[row.metric_id] = float(row.value)
            if row.breakdown is not None:
                datasets = row.breakdown.get("datasets", [])
            else:
                datasets = []
            per_dataset_breakdown[row.metric_id] = datasets

        return metric_values, per_dataset_breakdown

    # ── Kept for direct unit-test access (delegate to fused helper) ───────────

    async def _build_metric_values(self) -> dict[str, float]:
        """Return latest metric value per enabled metric_id."""
        metric_values, _ = await self._build_metric_sections()
        return metric_values

    async def _build_per_dataset_breakdown(self) -> dict[str, list[dict[str, Any]]]:
        """Return latest breakdown.datasets list per enabled metric_id."""
        _, per_dataset_breakdown = await self._build_metric_sections()
        return per_dataset_breakdown

    # ── Ontology graph ────────────────────────────────────────────────────────

    async def _build_ontology_graph(self) -> OntologyGraph:
        """Build the ontology graph from approved ontogen relational rows."""
        node_q = (
            select(OntogenNode)
            .where(OntogenNode.status == "approved")
            .order_by(OntogenNode.created_at)
            .limit(_MAX_GRAPH_NODES)
        )
        node_rows = (await self._db.execute(node_q)).scalars().all()
        if not node_rows:
            return OntologyGraph()

        nodes: list[GraphNode] = []
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

        edge_q = select(OntogenEdge).where(OntogenEdge.status == "approved")
        edge_rows = (await self._db.execute(edge_q)).scalars().all()
        edge_map: dict[str, OntogenEdge] = {row.id: row for row in edge_rows}

        triple_q = select(OntogenTriple).where(
            OntogenTriple.status == "approved",
            OntogenTriple.subject_node_id.in_(node_ids),
            OntogenTriple.object_node_id.in_(node_ids),
        )
        triple_rows = (await self._db.execute(triple_q)).scalars().all()

        edges: list[GraphEdge] = []
        seen: set[str] = set()
        for triple in triple_rows:
            if triple.id in seen:
                continue
            seen.add(triple.id)
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

        return OntologyGraph(nodes=nodes, edges=edges)

    # ── Dataset sections (blind spots + medallion + ownership) ───────────────

    async def _build_dataset_sections(
        self,
    ) -> tuple[list[str], MedallionSummary, dict[str, list[str]]]:
        """Return (blind_spots, medallion, ownership_topology) from DataHub enumeration."""
        from datahub.metadata.schema_classes import OwnershipClass

        dataset_urns = await self._datahub.enumerate_datasets()

        semaphore = asyncio.Semaphore(_CONCURRENCY_LIMIT)

        async def _get_upstream(urn: str) -> list[str]:
            async with semaphore:
                try:
                    return await self._datahub.get_upstream_lineage(urn)
                except Exception:
                    return []

        async def _get_ownership(urn: str) -> list[str]:
            async with semaphore:
                try:
                    ownership = await self._datahub.get_aspect(urn, OwnershipClass)
                    if ownership and getattr(ownership, "owners", []):
                        return [o.owner for o in ownership.owners]
                    return []
                except Exception:
                    logger.warning(
                        "overview_ownership_failed",
                        extra={"dataset_urn": urn},
                        exc_info=True,
                    )
                    return []

        upstream_tasks = [_get_upstream(urn) for urn in dataset_urns]
        ownership_tasks = [_get_ownership(urn) for urn in dataset_urns]

        upstream_lists, ownership_lists = await asyncio.gather(
            asyncio.gather(*upstream_tasks),
            asyncio.gather(*ownership_tasks),
        )

        # Medallion counts
        bronze = silver = gold = 0
        for upstreams in upstream_lists:
            layer = _classify_medallion(len(upstreams))
            if layer == "bronze":
                bronze += 1
            elif layer == "silver":
                silver += 1
            else:
                gold += 1
        medallion = MedallionSummary(bronze=bronze, silver=silver, gold=gold)

        # Ownership topology
        ownership_topology: dict[str, list[str]] = {}
        for urn, owner_urns in zip(dataset_urns, ownership_lists):
            for owner_urn in owner_urns:
                ownership_topology.setdefault(owner_urn, []).append(urn)

        # Blind spots — datasets with no approved dataset_node_map row
        approved_q = select(DatasetNodeMap.dataset_urn).where(
            DatasetNodeMap.status == "approved"
        )
        approved_result = await self._db.execute(approved_q)
        mapped_urns: set[str] = {row.dataset_urn for row in approved_result}
        blind_spots = sorted(urn for urn in dataset_urns if urn not in mapped_urns)

        return blind_spots, medallion, ownership_topology

    # ── Config CRUD ───────────────────────────────────────────────────────────

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
