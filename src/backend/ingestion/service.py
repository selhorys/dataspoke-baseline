"""Ingestion service — config CRUD, run pipeline, and event recording."""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.ingestion.extractors import EXTRACTOR_REGISTRY, ExtractedMetadata
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import Event, IngestionConfig
from src.shared.exceptions import EntityNotFoundError
from src.shared.llm.client import LLMClient


class IngestionConfigRecord(BaseModel):
    """Value object mirroring the ORM IngestionConfig."""

    id: str
    dataset_urn: str
    sources: dict[str, Any]
    deep_spec_enabled: bool
    schedule: str | None = None
    status: str
    owner: str
    created_at: datetime
    updated_at: datetime


class IngestionRunResult(BaseModel):
    """Value object for the outcome of an ingestion run."""

    run_id: str
    status: str
    detail: dict[str, Any]


def _record_from_row(row: IngestionConfig) -> IngestionConfigRecord:
    return IngestionConfigRecord(
        id=str(row.id),
        dataset_urn=row.dataset_urn,
        sources=row.sources,
        deep_spec_enabled=row.deep_spec_enabled,
        schedule=row.schedule,
        status=row.status,
        owner=row.owner,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class IngestionService:
    """Config CRUD, run pipeline, and event recording for ingestion."""

    def __init__(
        self,
        datahub: DataHubClient,
        db: AsyncSession,
        llm: LLMClient,
    ) -> None:
        self._datahub = datahub
        self._db = db
        self._llm = llm

    # ── Config CRUD ──────────────────────────────────────────────────────

    async def get_config(self, dataset_urn: str) -> IngestionConfigRecord | None:
        result = await self._db.execute(
            select(IngestionConfig).where(IngestionConfig.dataset_urn == dataset_urn)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return _record_from_row(row)

    async def upsert_config(
        self,
        dataset_urn: str,
        sources: dict[str, Any],
        deep_spec_enabled: bool,
        schedule: str | None,
        owner: str,
    ) -> tuple[IngestionConfigRecord, bool]:
        result = await self._db.execute(
            select(IngestionConfig).where(IngestionConfig.dataset_urn == dataset_urn)
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.sources = sources
            existing.deep_spec_enabled = deep_spec_enabled
            existing.schedule = schedule
            existing.owner = owner
            existing.updated_at = datetime.now(tz=UTC)
            self._db.add(existing)
            created = False
        else:
            existing = IngestionConfig(
                dataset_urn=dataset_urn,
                sources=sources,
                deep_spec_enabled=deep_spec_enabled,
                schedule=schedule,
                owner=owner,
            )
            self._db.add(existing)
            created = True

        await self._db.commit()
        await self._db.refresh(existing)
        return _record_from_row(existing), created

    async def patch_config(self, dataset_urn: str, patch: dict[str, Any]) -> IngestionConfigRecord:
        result = await self._db.execute(
            select(IngestionConfig).where(IngestionConfig.dataset_urn == dataset_urn)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("ingestion_config", dataset_urn)

        if "sources" in patch and patch["sources"] is not None:
            row.sources = patch["sources"]
        if "deep_spec_enabled" in patch and patch["deep_spec_enabled"] is not None:
            row.deep_spec_enabled = patch["deep_spec_enabled"]
        if "schedule" in patch and patch["schedule"] is not None:
            row.schedule = patch["schedule"]
        if "status" in patch and patch["status"] is not None:
            row.status = patch["status"]
        row.updated_at = datetime.now(tz=UTC)

        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)
        return _record_from_row(row)

    async def delete_config(self, dataset_urn: str) -> None:
        result = await self._db.execute(
            select(IngestionConfig).where(IngestionConfig.dataset_urn == dataset_urn)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("ingestion_config", dataset_urn)

        await self._db.delete(row)
        await self._db.commit()

    async def list_configs(
        self,
        offset: int = 0,
        limit: int = 20,
        status_filter: str | None = None,
        order_by: Any = None,
    ) -> tuple[list[IngestionConfigRecord], int]:
        base = select(IngestionConfig)
        if status_filter is not None:
            base = base.where(IngestionConfig.status == status_filter)

        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self._db.execute(count_q)).scalar() or 0

        default_order = IngestionConfig.created_at.desc()
        rows_q = base.order_by(order_by if order_by is not None else default_order).offset(offset).limit(limit)
        result = await self._db.execute(rows_q)
        rows = result.scalars().all()

        return [_record_from_row(r) for r in rows], total_count

    # ── Run pipeline (step-level) ─────────────────────────────────────────

    async def extract_metadata(self, dataset_urn: str, run_id: str) -> dict[str, Any]:
        """Phase 1 — load config and extract metadata from all configured sources.

        Returns a dict with keys:
          - run_id: str
          - metadata: list[dict]  (serialised ExtractedMetadata objects)
          - errors: list[str]
          - sources_processed: int
          - deep_spec_enabled: bool
        """
        config = await self.get_config(dataset_urn)
        if config is None:
            raise EntityNotFoundError("ingestion_config", dataset_urn)

        all_metadata: list[ExtractedMetadata] = []
        errors: list[str] = []

        for source_type, source_config in config.sources.items():
            extractor_cls = EXTRACTOR_REGISTRY.get(source_type)
            if extractor_cls is None:
                errors.append(f"Unknown source type: {source_type}")
                continue
            try:
                extractor = extractor_cls()
                extracted = await extractor.extract(source_config)
                all_metadata.extend(extracted)
            except Exception as exc:
                errors.append(f"{source_type}: {exc}")

        return {
            "run_id": run_id,
            "metadata": [m.model_dump() for m in all_metadata],
            "errors": errors,
            "sources_processed": len(config.sources),
            "deep_spec_enabled": config.deep_spec_enabled,
        }

    async def emit_metadata_to_datahub(
        self,
        dataset_urn: str,
        extract_result: dict[str, Any],
    ) -> dict[str, Any]:
        """Phase 2 — transform and emit extracted metadata to DataHub.

        ``extract_result`` is the dict returned by :meth:`extract_metadata`.
        Returns a dict with keys:
          - status: str  ("success" | "partial" | "error")
          - detail: dict
        """
        run_id: str = extract_result["run_id"]
        raw_metadata: list[dict[str, Any]] = extract_result["metadata"]
        errors: list[str] = extract_result["errors"]
        sources_processed: int = extract_result["sources_processed"]
        deep_spec_enabled: bool = extract_result["deep_spec_enabled"]

        all_metadata = [ExtractedMetadata(**m) for m in raw_metadata]

        emit_error: str | None = None
        if all_metadata:
            try:
                await self._emit_to_datahub(dataset_urn, all_metadata, deep_spec_enabled)
            except Exception as exc:
                emit_error = str(exc)

        if emit_error:
            status = "error"
        elif errors and not all_metadata:
            status = "error"
        elif errors:
            status = "partial"
        else:
            status = "success"

        detail: dict[str, Any] = {
            "run_id": run_id,
            "sources_processed": sources_processed,
            "metadata_extracted": len(all_metadata),
            "dry_run": False,
        }
        if errors:
            detail["extractor_errors"] = errors
        if emit_error:
            detail["emit_error"] = emit_error

        return {"status": status, "detail": detail}

    async def record_ingestion_event(
        self,
        dataset_urn: str,
        run_id: str,
        status: str,
        detail: dict[str, Any],
    ) -> dict[str, Any]:
        """Phase 3 — record the ingestion event and return the final run result.

        Returns a dict with keys: run_id, status, detail.
        """
        event_type = "ingestion.completed" if status != "error" else "ingestion.failed"
        await self._record_event(dataset_urn, event_type, status, detail)
        return {"run_id": run_id, "status": status, "detail": detail}

    async def run(self, dataset_urn: str, dry_run: bool = False) -> IngestionRunResult:
        """Run the full ingestion pipeline in a single call (used for non-Kestra paths)."""
        run_id = str(uuid.uuid4())

        extract_result = await self.extract_metadata(dataset_urn, run_id)
        errors: list[str] = extract_result["errors"]
        raw_metadata: list[dict[str, Any]] = extract_result["metadata"]
        all_metadata = [ExtractedMetadata(**m) for m in raw_metadata]

        if not dry_run and all_metadata:
            emit_result = await self.emit_metadata_to_datahub(dataset_urn, extract_result)
            status = emit_result["status"]
            detail = emit_result["detail"]
            detail["dry_run"] = False
        else:
            if errors and not all_metadata:
                status = "error"
            elif errors:
                status = "partial"
            else:
                status = "success"
            detail = {
                "run_id": run_id,
                "sources_processed": extract_result["sources_processed"],
                "metadata_extracted": len(all_metadata),
                "dry_run": dry_run,
            }
            if errors:
                detail["extractor_errors"] = errors

        result = await self.record_ingestion_event(dataset_urn, run_id, status, detail)
        return IngestionRunResult(
            run_id=result["run_id"],
            status=result["status"],
            detail=result["detail"],
        )

    async def _emit_to_datahub(
        self,
        dataset_urn: str,
        metadata: list[ExtractedMetadata],
        deep_spec_enabled: bool,
    ) -> None:
        from datahub.metadata.schema_classes import DatasetPropertiesClass

        custom_props: dict[str, str] = {"source": "dataspoke"}
        descriptions: list[str] = []
        code_refs: list[str] = []
        lineage_tables: list[str] = []

        for m in metadata:
            if m.metadata_type == "description":
                title = m.content.get("title", "")
                descriptions.append(title)
            elif m.metadata_type == "code_ref":
                code_refs.append(m.content.get("path", m.source_ref))
            elif m.metadata_type == "lineage_edge":
                lineage_tables.extend(m.content.get("tables", []))

        if code_refs:
            custom_props["code_references"] = ", ".join(code_refs)
        if lineage_tables:
            custom_props["lineage_sources"] = ", ".join(lineage_tables)

        description = "; ".join(descriptions) if descriptions else None

        if deep_spec_enabled and metadata:
            try:
                enrichment = await self._llm.complete_json(
                    prompt=f"Enrich dataset description from: {descriptions}. "
                    f"Code refs: {code_refs}. Lineage: {lineage_tables}.",
                    system="Generate a concise enriched description and suggest tags. "
                    "Return JSON with keys: description (str), tags (list[str]).",
                )
                if "description" in enrichment:
                    description = enrichment["description"]
                if "tags" in enrichment:
                    custom_props["suggested_tags"] = ", ".join(enrichment["tags"])
            except Exception:
                logger.warning("llm_enrichment_failed", exc_info=True, extra={"dataset_urn": dataset_urn})

        props = DatasetPropertiesClass(
            description=description,
            customProperties=custom_props,
        )
        await self._datahub.emit_aspect(dataset_urn, props)

    # ── Events ───────────────────────────────────────────────────────────

    async def get_events(
        self,
        dataset_urn: str,
        offset: int = 0,
        limit: int = 20,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        order_by: Any = None,
    ) -> tuple[list[dict[str, Any]], int]:
        base = select(Event).where(
            Event.entity_type == "dataset",
            Event.entity_id == dataset_urn,
            Event.event_type.startswith("ingestion."),
        )

        if from_dt is not None:
            base = base.where(Event.occurred_at >= from_dt)
        if to_dt is not None:
            base = base.where(Event.occurred_at <= to_dt)

        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self._db.execute(count_q)).scalar() or 0

        default_order = Event.occurred_at.desc()
        rows_q = base.order_by(order_by if order_by is not None else default_order).offset(offset).limit(limit)
        result = await self._db.execute(rows_q)
        rows = result.scalars().all()

        events = [
            {
                "id": str(row.id),
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "event_type": row.event_type,
                "status": row.status,
                "detail": row.detail,
                "occurred_at": row.occurred_at,
            }
            for row in rows
        ]
        return events, total_count

    async def _record_event(
        self,
        dataset_urn: str,
        event_type: str,
        status: str,
        detail: dict[str, Any],
    ) -> None:
        event = Event(
            entity_type="dataset",
            entity_id=dataset_urn,
            event_type=event_type,
            status=status,
            detail=detail,
            occurred_at=datetime.now(tz=UTC),
        )
        self._db.add(event)
        await self._db.commit()
