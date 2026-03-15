"""Generation service — config CRUD, LLM-powered generate pipeline, apply flow, and events."""

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.generation.analyzer import SourceCodeAnalyzer
from src.shared.datahub.client import DataHubClient
from src.shared.db.models import Event, GenerationConfig, GenerationResult
from src.shared.exceptions import ConflictError, EntityNotFoundError
from src.shared.llm.client import LLMClient
from src.shared.vector.client import QdrantManager


class GenerationProposalSchema(BaseModel):
    """Structured output schema for LLM generation proposals."""

    field_descriptions: dict[str, str]
    table_summary: str
    suggested_tags: list[str]


class GenerationConfigRecord(BaseModel):
    """Value object mirroring the ORM GenerationConfig."""

    id: str
    dataset_urn: str
    target_fields: dict[str, Any]
    code_refs: dict[str, Any] | None = None
    schedule: str | None = None
    status: str
    owner: str
    created_at: datetime
    updated_at: datetime


class GenerationRunResult(BaseModel):
    """Value object for the outcome of a generation run."""

    run_id: str
    status: str
    detail: dict[str, Any]


class GenerationResultRecord(BaseModel):
    """Value object mirroring the ORM GenerationResult."""

    id: str
    dataset_urn: str
    proposals: dict[str, Any]
    similar_diffs: list[dict[str, Any]] = []
    approval_status: str
    run_id: str
    generated_at: datetime
    applied_at: datetime | None = None


def _config_from_row(row: GenerationConfig) -> GenerationConfigRecord:
    return GenerationConfigRecord(
        id=str(row.id),
        dataset_urn=row.dataset_urn,
        target_fields=row.target_fields,
        code_refs=row.code_refs,
        schedule=row.schedule,
        status=row.status,
        owner=row.owner,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _result_from_row(row: GenerationResult) -> GenerationResultRecord:
    return GenerationResultRecord(
        id=str(row.id),
        dataset_urn=row.dataset_urn,
        proposals=row.proposals,
        similar_diffs=row.similar_diffs,
        approval_status=row.approval_status,
        run_id=str(row.run_id),
        generated_at=row.generated_at,
        applied_at=row.applied_at,
    )


class GenerationService:
    """Config CRUD, LLM-powered generate pipeline, apply flow, and event recording."""

    def __init__(
        self,
        datahub: DataHubClient,
        db: AsyncSession,
        llm: LLMClient,
        qdrant: QdrantManager,
    ) -> None:
        self._datahub = datahub
        self._db = db
        self._llm = llm
        self._qdrant = qdrant

    # ── Config CRUD ──────────────────────────────────────────────────────

    async def get_config(self, dataset_urn: str) -> GenerationConfigRecord | None:
        result = await self._db.execute(
            select(GenerationConfig).where(GenerationConfig.dataset_urn == dataset_urn)
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return _config_from_row(row)

    async def upsert_config(
        self,
        dataset_urn: str,
        target_fields: dict[str, Any],
        code_refs: dict[str, Any] | None,
        schedule: str | None,
        owner: str,
    ) -> GenerationConfigRecord:
        result = await self._db.execute(
            select(GenerationConfig).where(GenerationConfig.dataset_urn == dataset_urn)
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.target_fields = target_fields
            existing.code_refs = code_refs
            existing.schedule = schedule
            existing.owner = owner
            existing.updated_at = datetime.now(tz=UTC)
            self._db.add(existing)
        else:
            existing = GenerationConfig(
                dataset_urn=dataset_urn,
                target_fields=target_fields,
                code_refs=code_refs,
                schedule=schedule,
                owner=owner,
            )
            self._db.add(existing)

        await self._db.commit()
        await self._db.refresh(existing)
        return _config_from_row(existing)

    async def patch_config(self, dataset_urn: str, patch: dict[str, Any]) -> GenerationConfigRecord:
        result = await self._db.execute(
            select(GenerationConfig).where(GenerationConfig.dataset_urn == dataset_urn)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("generation_config", dataset_urn)

        if "target_fields" in patch and patch["target_fields"] is not None:
            row.target_fields = patch["target_fields"]
        if "code_refs" in patch and patch["code_refs"] is not None:
            row.code_refs = patch["code_refs"]
        if "schedule" in patch and patch["schedule"] is not None:
            row.schedule = patch["schedule"]
        if "status" in patch and patch["status"] is not None:
            row.status = patch["status"]
        row.updated_at = datetime.now(tz=UTC)

        self._db.add(row)
        await self._db.commit()
        await self._db.refresh(row)
        return _config_from_row(row)

    async def delete_config(self, dataset_urn: str) -> None:
        result = await self._db.execute(
            select(GenerationConfig).where(GenerationConfig.dataset_urn == dataset_urn)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("generation_config", dataset_urn)

        await self._db.delete(row)
        await self._db.commit()

    async def list_configs(
        self,
        offset: int = 0,
        limit: int = 20,
        status_filter: str | None = None,
    ) -> tuple[list[GenerationConfigRecord], int]:
        base = select(GenerationConfig)
        if status_filter is not None:
            base = base.where(GenerationConfig.status == status_filter)

        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self._db.execute(count_q)).scalar() or 0

        rows_q = base.order_by(GenerationConfig.created_at.desc()).offset(offset).limit(limit)
        result = await self._db.execute(rows_q)
        rows = result.scalars().all()

        return [_config_from_row(r) for r in rows], total_count

    # ── Results ──────────────────────────────────────────────────────────

    async def get_results(
        self,
        dataset_urn: str,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        offset: int = 0,
        limit: int = 20,
    ) -> tuple[list[GenerationResultRecord], int]:
        base = select(GenerationResult).where(GenerationResult.dataset_urn == dataset_urn)

        if from_dt is not None:
            base = base.where(GenerationResult.generated_at >= from_dt)
        if to_dt is not None:
            base = base.where(GenerationResult.generated_at <= to_dt)

        count_q = select(func.count()).select_from(base.subquery())
        total_count = (await self._db.execute(count_q)).scalar() or 0

        rows_q = base.order_by(GenerationResult.generated_at.desc()).offset(offset).limit(limit)
        result = await self._db.execute(rows_q)
        rows = result.scalars().all()

        return [_result_from_row(r) for r in rows], total_count

    # ── Generate pipeline ────────────────────────────────────────────────

    async def generate(self, dataset_urn: str) -> GenerationRunResult:
        config = await self.get_config(dataset_urn)
        if config is None:
            raise EntityNotFoundError("generation_config", dataset_urn)

        run_id = str(uuid.uuid4())

        # 1. Read DataHub aspects for the dataset
        from datahub.metadata.schema_classes import (
            DatasetPropertiesClass,
            GlobalTagsClass,
            SchemaMetadataClass,
            UpstreamLineageClass,
        )

        schema_meta = await self._datahub.get_aspect(dataset_urn, SchemaMetadataClass)
        dataset_props = await self._datahub.get_aspect(dataset_urn, DatasetPropertiesClass)
        global_tags = await self._datahub.get_aspect(dataset_urn, GlobalTagsClass)
        upstream_lineage = await self._datahub.get_aspect(dataset_urn, UpstreamLineageClass)

        # 2. Extract schema fields for LLM context
        schema_fields: list[dict[str, Any]] = []
        if schema_meta and hasattr(schema_meta, "fields"):
            for field in schema_meta.fields:
                schema_fields.append(
                    {
                        "fieldPath": field.fieldPath,
                        "nativeDataType": getattr(field, "nativeDataType", ""),
                        "description": getattr(field, "description", "") or "",
                    }
                )

        dataset_description = ""
        if dataset_props:
            dataset_description = getattr(dataset_props, "description", "") or ""

        current_tags: list[str] = []
        if global_tags and hasattr(global_tags, "tags"):
            current_tags = [str(t.tag) for t in global_tags.tags]

        upstream_urns: list[str] = []
        if upstream_lineage and hasattr(upstream_lineage, "upstreams"):
            upstream_urns = [
                str(u.dataset) for u in upstream_lineage.upstreams if hasattr(u, "dataset")
            ]

        # 3. Similar datasets via Qdrant embedding search
        similar_schemas: list[dict[str, Any]] = []
        try:
            from src.backend.search.embedding import generate_embedding

            embedding, _ = await generate_embedding(self._llm, self._datahub, dataset_urn)
            scored_points = await self._qdrant.search(
                collection=EMBEDDING_COLLECTION,
                vector=embedding,
                limit=6,
                score_threshold=SEARCH_SCORE_THRESHOLD,
            )
            for pt in scored_points:
                payload = pt.payload or {}
                candidate_urn = payload.get("dataset_urn", "")
                if candidate_urn == dataset_urn:
                    continue
                sim_schema_meta = await self._datahub.get_aspect(candidate_urn, SchemaMetadataClass)
                sim_fields: list[dict[str, Any]] = []
                if sim_schema_meta and hasattr(sim_schema_meta, "fields"):
                    sim_fields = [
                        {
                            "fieldPath": f.fieldPath,
                            "nativeDataType": getattr(f, "nativeDataType", ""),
                            "description": getattr(f, "description", "") or "",
                        }
                        for f in sim_schema_meta.fields
                    ]
                similar_schemas.append({"urn": candidate_urn, "fields": sim_fields})
        except Exception:
            pass

        # 4. Code reference analysis (if configured)
        code_insights: dict[str, Any] = {}
        if config.code_refs:
            analyzer = SourceCodeAnalyzer(self._llm)
            try:
                code_insights = await analyzer.analyze(config.code_refs, schema_fields)
            except Exception:
                pass

        # 5. Build LLM prompt
        field_info = "\n".join(
            f"- {f['fieldPath']} ({f['nativeDataType']}): {f['description']}" for f in schema_fields
        )

        prompt_parts = [
            f"Dataset URN: {dataset_urn}",
            f"Current description: {dataset_description}",
            f"Current tags: {current_tags}",
            f"Upstream dependencies: {upstream_urns}",
            f"\nSchema fields:\n{field_info}" if field_info else "No schema fields available.",
        ]

        if code_insights:
            prompt_parts.append(f"\nCode analysis insights: {code_insights}")

        prompt = "\n".join(prompt_parts)
        prompt += (
            "\n\nGenerate metadata proposals for this dataset. "
            "For each field, provide a business-friendly description. "
            "Also provide a table-level summary and suggest relevant tags."
        )

        system = (
            "You are a data documentation assistant that generates metadata for datasets. "
            "Respond with structured JSON containing: field_descriptions (dict mapping "
            "fieldPath to description), table_summary (string), suggested_tags (list of strings)."
        )

        # 6. Call LLM for structured proposals
        proposals = await self._llm.complete_json(
            prompt, system=system, schema=GenerationProposalSchema
        )

        # 7. Build similar diffs (empty for now — no Qdrant embeddings yet)
        analyzer = SourceCodeAnalyzer(self._llm)
        similar_diffs = await analyzer.diff_similar_tables(schema_fields, similar_schemas)

        # 8. Persist result
        result_row = GenerationResult(
            dataset_urn=dataset_urn,
            proposals=proposals,
            similar_diffs=similar_diffs,
            approval_status="pending",
            run_id=uuid.UUID(run_id),
            generated_at=datetime.now(tz=UTC),
        )
        self._db.add(result_row)
        await self._db.commit()

        # 9. Record event
        detail: dict[str, Any] = {
            "run_id": run_id,
            "fields_count": len(schema_fields),
            "proposals_keys": list(proposals.keys()),
            "code_refs_used": config.code_refs is not None,
        }
        await self._record_event(dataset_urn, "generation.completed", "success", detail)

        return GenerationRunResult(run_id=run_id, status="success", detail=detail)

    # ── Apply flow ───────────────────────────────────────────────────────

    async def apply(self, dataset_urn: str, result_id: str) -> GenerationRunResult:
        result = await self._db.execute(
            select(GenerationResult).where(GenerationResult.id == uuid.UUID(result_id))
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise EntityNotFoundError("generation_result", result_id)

        if row.approval_status != "approved":
            raise ConflictError(
                "GENERATION_NOT_APPROVED",
                f"Result {result_id} has approval_status='{row.approval_status}', expected 'approved'",
            )

        proposals = row.proposals

        # Apply field descriptions via EditableSchemaMetadata
        field_descriptions = proposals.get("field_descriptions", {})
        if field_descriptions:
            from datahub.metadata.schema_classes import (
                EditableSchemaFieldInfoClass,
                EditableSchemaMetadataClass,
            )

            editable_fields = [
                EditableSchemaFieldInfoClass(
                    fieldPath=field_path,
                    description=desc,
                )
                for field_path, desc in field_descriptions.items()
            ]
            editable_schema = EditableSchemaMetadataClass(
                editableSchemaFieldInfo=editable_fields,
            )
            await self._datahub.emit_aspect(dataset_urn, editable_schema)

        # Apply table summary via DatasetProperties
        table_summary = proposals.get("table_summary", "")
        if table_summary:
            from datahub.metadata.schema_classes import DatasetPropertiesClass

            props = DatasetPropertiesClass(description=table_summary)
            await self._datahub.emit_aspect(dataset_urn, props)

        # Apply suggested tags via GlobalTags
        suggested_tags = proposals.get("suggested_tags", [])
        if suggested_tags:
            from datahub.metadata.schema_classes import GlobalTagsClass, TagAssociationClass

            tag_associations = [
                TagAssociationClass(tag=f"urn:li:tag:{tag}") for tag in suggested_tags
            ]
            tags_aspect = GlobalTagsClass(tags=tag_associations)
            await self._datahub.emit_aspect(dataset_urn, tags_aspect)

        # Mark as applied
        row.applied_at = datetime.now(tz=UTC)
        self._db.add(row)
        await self._db.commit()

        detail = {
            "result_id": result_id,
            "fields_applied": len(field_descriptions),
            "tags_applied": len(suggested_tags),
            "summary_applied": bool(table_summary),
        }
        await self._record_event(dataset_urn, "generation.applied", "success", detail)

        return GenerationRunResult(run_id=str(row.run_id), status="applied", detail=detail)

    # ── Events ───────────────────────────────────────────────────────────

    async def get_events(
        self,
        dataset_urn: str,
        offset: int = 0,
        limit: int = 20,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        base = select(Event).where(
            Event.entity_type == "dataset",
            Event.entity_id == dataset_urn,
            Event.event_type.startswith("generation."),
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
