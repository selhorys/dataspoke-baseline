"""Dataset service — read-through layer for dataset summary, attributes, and events."""

import json
import re
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.cache.client import QUALITY_CACHE_KEY, RedisClient
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import Event
from src.shared.exceptions import EntityNotFoundError
from src.shared.models.dataset import DatasetAttributes, DatasetSummary
from src.shared.models.events import EventRecord
from src.shared.models.quality import QualityScore

_URN_PLATFORM_RE = re.compile(r"urn:li:dataset:\(urn:li:dataPlatform:([^,]+),")


def _parse_platform(urn: str) -> str:
    m = _URN_PLATFORM_RE.search(urn)
    return m.group(1) if m else "unknown"


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
        platform = _parse_platform(dataset_urn)

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
        # TODO: compute on demand when ValidationService is available

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
    ) -> tuple[list[EventRecord], int]:
        base = select(Event).where(
            Event.entity_type == "dataset",
            Event.entity_id == dataset_urn,
        )

        if from_dt is not None:
            base = base.where(Event.occurred_at >= from_dt)
        if to_dt is not None:
            base = base.where(Event.occurred_at <= to_dt)

        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self._db.execute(count_q)).scalar() or 0

        rows_q = base.order_by(Event.occurred_at.desc()).offset(offset).limit(limit)
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
