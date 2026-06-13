"""Dataset service — read-through layer for dataset summary, attributes, and events."""

import json
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

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
        event_type_prefix: str | None = None,
    ) -> tuple[list[EventRecord], int]:
        """Return paginated events for a dataset, optionally filtered by event type prefix.

        Parameters
        ----------
        dataset_urn:
            The dataset URN to query events for.
        event_type_prefix:
            If provided, only return events whose ``event_type`` starts with this
            prefix (e.g. ``"INGESTION."`` or ``"VALIDATION."``).
        """
        base = select(Event).where(
            Event.entity_type == "dataset",
            Event.entity_id == dataset_urn,
        )

        if event_type_prefix is not None:
            base = base.where(Event.event_type.like(f"{event_type_prefix}%"))
        if from_dt is not None:
            base = base.where(Event.occurred_at >= from_dt)
        if to_dt is not None:
            base = base.where(Event.occurred_at <= to_dt)

        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self._db.execute(count_q)).scalar() or 0

        default_order = Event.occurred_at.desc()
        rows_q = (
            base.order_by(order_by if order_by is not None else default_order)
            .offset(offset)
            .limit(limit)
        )
        result = await self._db.execute(rows_q)
        rows = result.scalars().all()

        events = [
            EventRecord(
                id=str(row.id),
                entity_type=row.entity_type,
                entity_id=row.entity_id,
                event_type=row.event_type,
                status=row.status,
                detail=row.detail,
                occurred_at=row.occurred_at,
            )
            for row in rows
        ]
        return events, total_count
