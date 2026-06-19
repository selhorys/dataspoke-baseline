"""Dataset service — read-through layer for dataset summary, attributes, and events."""

import json
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.ingestion.service import IngestionService
from src.shared.cache.client import QUALITY_CACHE_KEY, RedisClient
from src.shared.datahub.client import DataHubClient
from src.shared.datahub.urn import platform_from_dataset_urn
from src.shared.db.models import Event
from src.shared.exceptions import EntityNotFoundError
from src.shared.models.dataset import DatasetAttributes, DatasetSummary
from src.shared.models.events import EventRecord
from src.shared.models.quality import QualityScore


class DatasetService:
    """Thin read-through layer for dataset identity, attributes, and events."""

    def __init__(
        self,
        datahub: DataHubClient,
        db: AsyncSession,
        cache: RedisClient,
    ) -> None:
        self._datahub = datahub
        self._db = db
        self._cache = cache
        # Compose the ingestion service for the reverse-lookup + source-run
        # aggregation that feeds the unified dataset timeline. Shares the same
        # constructor-injected deps (datahub, db, cache).
        self._ingestion = IngestionService(datahub=datahub, db=db, cache=cache)

    async def get_summary(self, dataset_urn: str) -> DatasetSummary:
        from datahub.metadata.schema_classes import (
            DatasetPropertiesClass,
            GlobalTagsClass,
            OwnershipClass,
        )

        props = await self._datahub.get_aspect(dataset_urn, DatasetPropertiesClass)
        if props is None:
            raise EntityNotFoundError("dataset", dataset_urn)

        ownership = await self._datahub.get_aspect(dataset_urn, OwnershipClass)
        global_tags = await self._datahub.get_aspect(dataset_urn, GlobalTagsClass)

        owners = [o.owner for o in (ownership.owners if ownership else [])]
        tags = [t.tag for t in (global_tags.tags if global_tags else [])]
        platform = platform_from_dataset_urn(dataset_urn) or "unknown"

        return DatasetSummary(
            urn=dataset_urn,
            name=props.name or "",
            platform=platform,
            description=props.description,
            owners=owners,
            tags=tags,
        )

    async def get_attributes(self, dataset_urn: str) -> DatasetAttributes:
        from datahub.metadata.schema_classes import (
            DatasetPropertiesClass,
            GlobalTagsClass,
            OwnershipClass,
            SchemaMetadataClass,
        )

        props = await self._datahub.get_aspect(dataset_urn, DatasetPropertiesClass)
        if props is None:
            raise EntityNotFoundError("dataset", dataset_urn)

        ownership = await self._datahub.get_aspect(dataset_urn, OwnershipClass)
        global_tags = await self._datahub.get_aspect(dataset_urn, GlobalTagsClass)
        schema = await self._datahub.get_aspect(dataset_urn, SchemaMetadataClass)

        owners = [o.owner for o in (ownership.owners if ownership else [])]
        tags = [t.tag for t in (global_tags.tags if global_tags else [])]
        fields = [f.fieldPath for f in (schema.fields if schema else [])]
        column_count = len(fields)

        # Read quality score from cache
        cache_key = QUALITY_CACHE_KEY.format(dataset_urn=dataset_urn)
        cached = await self._cache.get(cache_key)
        quality_score: QualityScore | None = None
        if cached is not None:
            data = json.loads(cached)
            quality_score = QualityScore(
                overall_score=data["overall_score"],
                dimensions=data.get("dimensions", {}),
            )
        # No Redis cache hit: return quality_score=None; the validation-score
        # measurer in src/backend/metrics/ covers the dashboard aggregation.
        # The dataset service does not duplicate that aggregation.

        return DatasetAttributes(
            urn=dataset_urn,
            column_count=column_count,
            fields=fields,
            owners=owners,
            tags=tags,
            description=props.description,
            quality_score=quality_score,
        )

    async def get_events(
        self,
        dataset_urn: str,
        offset: int = 0,
        limit: int = 20,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        order_by: Any = None,
        event_type_prefixes: set[str] | None = None,
    ) -> tuple[list[EventRecord], int]:
        """Return the unified per-dataset event timeline (paginated).

        The timeline is the UNION of:

        (a) dataset-level events (``entity_type='dataset'``) — validation events
            and metagen candidate-review events recorded directly on the dataset, and
        (b) the covering source's ingestion runs — found via
            :meth:`IngestionService.reverse_lookup`, then aggregated (including the
            CLI-wrapper union) via :meth:`IngestionService.get_events_for_source`.
            Rows sourced from a wrapper carry ``wrapper=True``.

        The merged stream is sorted ``occurred_at`` descending, the ``from``/``to``
        range and the major-type prefix filter are applied, and the result is
        paginated in-memory (per-dataset event volume is small). ``order_by`` is
        accepted for API compatibility but the timeline is always newest-first.

        Parameters
        ----------
        dataset_urn:
            The dataset URN to query events for.
        event_type_prefixes:
            If provided, only return events whose ``event_type`` starts with one of
            these prefixes (e.g. ``{"INGESTION.", "VALIDATION.", "METAGEN."}``).
            Omitted / empty means all major types.
        """
        # ── (a) Dataset-level events (validation + metagen candidate review) ──
        dataset_q = select(Event).where(
            Event.entity_type == "dataset",
            Event.entity_id == dataset_urn,
        )
        result = await self._db.execute(dataset_q)
        records: list[EventRecord] = [
            EventRecord(
                id=str(row.id),
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                event_type=row.event_type,
                status=row.status,
                detail=row.detail,
                occurred_at=row.occurred_at,
                wrapper=False,
            )
            for row in result.scalars().all()
        ]

        # ── (b) Covering source ingestion runs (incl. wrapper union) ──────────
        source = await self._ingestion.reverse_lookup(dataset_urn)
        if source is not None:
            # Pull the full run history for the source; the unified timeline
            # paginates over the merged stream, so fetch without source-level
            # pagination (a high limit covers the small per-source volume).
            source_events, _ = await self._ingestion.get_events_for_source(
                source.id, offset=0, limit=10_000
            )
            records.extend(
                EventRecord(
                    id=str(e["id"]),
                    entity_type=e["entity_type"],
                    entity_id=e["entity_id"],
                    event_type=e["event_type"],
                    status=e["status"],
                    detail=e["detail"],
                    occurred_at=e["occurred_at"],
                    wrapper=bool(e.get("wrapper", False)),
                )
                for e in source_events
            )

        # ── Filter: time range + major-type prefixes ─────────────────────────
        if from_dt is not None:
            records = [r for r in records if r.occurred_at >= from_dt]
        if to_dt is not None:
            records = [r for r in records if r.occurred_at <= to_dt]
        if event_type_prefixes:
            prefixes = tuple(event_type_prefixes)
            records = [r for r in records if r.event_type.startswith(prefixes)]

        # ── Sort newest-first, then paginate in-memory ───────────────────────
        records.sort(key=lambda r: r.occurred_at, reverse=True)
        total_count = len(records)
        page = records[offset : offset + limit]
        return page, total_count
