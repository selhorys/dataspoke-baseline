"""Internal activity endpoints — called by Airflow HTTP operator tasks.

Each endpoint corresponds to an Airflow activity. Business logic
remains in the backend service layer; these endpoints are thin wrappers
that handle error translation to HTTP status codes.

These endpoints are NOT exposed to end users — they are called by the
Airflow orchestrator running inside the same K8s namespace.
"""

import json
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.api.auth.internal import require_internal_token
from src.shared.exceptions import DataSpokeError
from src.workflows._common import (
    make_cache,
    make_datahub,
    make_db_session,
    make_llm,
    make_qdrant,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/internal/activities",
    tags=[
        "internal/activities/ingestion",
        "internal/activities/validation",
        "internal/activities/generation",
        "internal/activities/search",
        "internal/activities/metrics",
        "internal/activities/ontology",
    ],
    dependencies=[Depends(require_internal_token)],
)


def _error_response(exc: Exception, non_retryable: bool = True) -> JSONResponse:
    error_code = getattr(exc, "error_code", "INTERNAL_ERROR")
    status_code = 400 if non_retryable else 500
    return JSONResponse(
        status_code=status_code,
        content={"error_code": error_code, "message": str(exc), "non_retryable": non_retryable},
    )


# ── /ingestion ───────────────────────────────────────────────────────────────


class ListPeriodicDatasetsRequest(BaseModel):
    schedule_tier: str


@router.post("/ingestion/list-periodic")
async def list_periodic_datasets(body: ListPeriodicDatasetsRequest) -> list[str]:
    from src.workflows.ingestion import get_datasets_for_tier

    try:
        async with make_db_session() as db:
            return await get_datasets_for_tier(db, body.schedule_tier)
    except DataSpokeError as exc:
        return _error_response(exc)


class RunIngestionRequest(BaseModel):
    dataset_urn: str
    dry_run: bool = False


@router.post("/ingestion/run")
async def run_ingestion(body: RunIngestionRequest) -> dict:
    from src.backend.ingestion.service import IngestionService, run_ingestion_with_lock

    datahub = make_datahub()
    cache = make_cache()
    try:
        async with make_db_session() as db:
            service = IngestionService(datahub=datahub, db=db)
            result = await run_ingestion_with_lock(
                service, cache, body.dataset_urn, dry_run=body.dry_run,
            )
            return {"run_id": result.run_id, "status": result.status, "detail": result.detail}
    except DataSpokeError as exc:
        return _error_response(exc)


# ── /validation ──────────────────────────────────────────────────────────────


class ListPeriodicValidationDatasetsRequest(BaseModel):
    schedule_tier: str


@router.post("/validation/list-periodic")
async def list_periodic_validation_datasets(
    body: ListPeriodicValidationDatasetsRequest,
) -> list[str]:
    """List dataset URNs with active validation configs for the given schedule tier."""
    from src.workflows.validation_sync import get_datasets_for_tier

    try:
        async with make_db_session() as db:
            return await get_datasets_for_tier(db, body.schedule_tier)
    except DataSpokeError as exc:
        return _error_response(exc)


class RunValidationRequest(BaseModel):
    dataset_urn: str
    partition: dict | None = None
    dry_run: bool = False


@router.post("/validation/run")
async def run_validation(body: RunValidationRequest) -> dict:
    from src.backend.validation.service import ValidationService

    datahub = make_datahub()
    cache = make_cache()
    try:
        async with make_db_session() as db:
            service = ValidationService(datahub=datahub, db=db, cache=cache)
            summary = await service.run(
                body.dataset_urn, partition=body.partition, dry_run=body.dry_run,
            )
            return {
                "run_id": summary.run_id,
                "status": summary.status,
                "total": summary.total,
                "passed": summary.passed,
                "failed": summary.failed,
                "errored": summary.errored,
            }
    except DataSpokeError as exc:
        return _error_response(exc)


# ── /generation ──────────────────────────────────────────────────────────────


class RunGenerationRequest(BaseModel):
    dataset_urn: str


@router.post("/generation/run")
async def run_generation(body: RunGenerationRequest) -> dict:
    from src.backend.generation.service import GenerationService

    datahub = make_datahub()
    llm = make_llm()
    qdrant = make_qdrant()
    try:
        async with make_db_session() as db:
            service = GenerationService(datahub=datahub, db=db, llm=llm, qdrant=qdrant)
            result = await service.generate(body.dataset_urn)
            return {"run_id": result.run_id, "status": result.status, "detail": result.detail}
    except DataSpokeError as exc:
        return _error_response(exc)


# ── /search ──────────────────────────────────────────────────────────────────


class EnumerateDatasetsRequest(BaseModel):
    mode: str = "full"
    dataset_urn: str = ""


@router.post("/search/enumerate")
async def enumerate_datasets(body: EnumerateDatasetsRequest) -> list[str]:
    datahub = make_datahub()
    if body.mode == "single" and body.dataset_urn:
        return [body.dataset_urn]
    return await datahub.enumerate_datasets()


class ReindexBatchRequest(BaseModel):
    dataset_urns: list[str]


@router.post("/search/reindex-batch")
async def reindex_batch(body: ReindexBatchRequest) -> dict:
    from src.backend.search.service import SearchService

    datahub = make_datahub()
    cache = make_cache()
    llm = make_llm()
    qdrant = make_qdrant()
    service = SearchService(datahub=datahub, cache=cache, llm=llm, qdrant=qdrant)

    indexed = 0
    errors = []
    for urn in body.dataset_urns:
        try:
            await service.reindex(urn)
            indexed += 1
        except Exception as exc:
            errors.append(f"{urn}: {exc}")

    return {"indexed": indexed, "errors": errors}


# ── /metrics ─────────────────────────────────────────────────────────────────


class RunMetricRequest(BaseModel):
    metric_id: str
    dry_run: bool = False


@router.post("/metrics/run")
async def run_metric(body: RunMetricRequest) -> dict:
    from src.backend.metrics.service import MetricsService

    datahub = make_datahub()
    cache = make_cache()
    try:
        async with make_db_session() as db:
            service = MetricsService(datahub=datahub, db=db, cache=cache)
            result = await service.run(body.metric_id, dry_run=body.dry_run)
            return {"run_id": result.run_id, "status": result.status, "detail": result.detail}
    except DataSpokeError as exc:
        return _error_response(exc)


@router.post("/metrics/aggregate-health")
async def aggregate_health() -> dict:
    from src.backend.metrics.aggregator import aggregate_health_scores

    datahub = make_datahub()
    cache = make_cache()
    async with make_db_session() as db:
        health_map = await aggregate_health_scores(datahub=datahub, db=db, cache=cache)
        return {
            dept: {
                "department": h.department,
                "avg_score": h.avg_score,
                "dataset_count": h.dataset_count,
                "worst_datasets": h.worst_datasets,
            }
            for dept, h in health_map.items()
        }


class ListPeriodicMetricsRequest(BaseModel):
    schedule_tier: str


@router.post("/metrics/list-periodic")
async def list_periodic_metrics(body: ListPeriodicMetricsRequest) -> list[str]:
    """Return metric IDs with active configs matching the given schedule tier."""
    from src.workflows.metrics import get_metrics_for_tier

    try:
        async with make_db_session() as db:
            return await get_metrics_for_tier(db, body.schedule_tier)
    except DataSpokeError as exc:
        return _error_response(exc)


class PublishMetricUpdateRequest(BaseModel):
    run_id: str | None = None
    status: str | None = None
    detail: dict | None = None


@router.post("/metrics/publish-update")
async def publish_metric_update(body: PublishMetricUpdateRequest) -> dict:
    cache = make_cache()
    await cache.publish("ws:metric:updates", json.dumps(body.model_dump()))
    return {"published": True}


# ── /ontology ────────────────────────────────────────────────────────────────


class ClassifyDatasetsRequest(BaseModel):
    force: bool = False


@router.post("/ontology/classify")
async def classify_datasets(body: ClassifyDatasetsRequest) -> list[dict]:
    from src.shared.config import ONTOLOGY_CONFIDENCE_THRESHOLD

    datahub = make_datahub()
    llm = make_llm()

    urns = await datahub.enumerate_datasets()
    classifications = []

    for urn in urns:
        try:
            from datahub.metadata.schema_classes import SchemaMetadataClass

            schema = await datahub.get_aspect(urn, SchemaMetadataClass)
            field_names = []
            if schema and hasattr(schema, "fields"):
                field_names = [f.fieldPath for f in schema.fields]

            prompt = (
                f"Classify this dataset into a concept category.\n"
                f"Dataset URN: {urn}\n"
                f"Fields: {', '.join(field_names[:20])}\n"
                f'Return JSON: {{"category": "<name>", "confidence": <0-1>}}'
            )
            result = await llm.complete_json(
                prompt=prompt,
                system="You are a data governance expert. "
                "Classify datasets into concept categories.",
            )
            confidence = result.get("confidence", 0.0)
            if confidence >= ONTOLOGY_CONFIDENCE_THRESHOLD:
                classifications.append(
                    {
                        "dataset_urn": urn,
                        "category": result.get("category", "unknown"),
                        "confidence": confidence,
                        "field_count": len(field_names),
                    }
                )
        except Exception:
            logger.warning("dataset_classification_failed", exc_info=True, extra={"dataset_urn": urn})

    return classifications


class BuildHierarchyRequest(BaseModel):
    classifications: list[dict]


@router.post("/ontology/build-hierarchy")
async def build_hierarchy(body: BuildHierarchyRequest) -> list[dict]:
    from sqlalchemy import select

    from src.shared.db.models import ConceptCategory

    async with make_db_session() as db:
        categories: dict[str, list[str]] = {}
        for c in body.classifications:
            categories.setdefault(c["category"], []).append(c["dataset_urn"])

        hierarchy = []
        for category_name, dataset_urns in categories.items():
            try:
                result = await db.execute(
                    select(ConceptCategory).where(ConceptCategory.name == category_name)
                )
                row = result.scalar_one_or_none()

                if row is None:
                    row = ConceptCategory(
                        name=category_name,
                        description=f"Auto-classified category with {len(dataset_urns)} datasets",
                        status="pending",
                        version=1,
                    )
                    db.add(row)
                    await db.commit()
                    await db.refresh(row)

                hierarchy.append(
                    {
                        "concept_id": str(row.id),
                        "name": category_name,
                        "dataset_count": len(dataset_urns),
                        "dataset_urns": dataset_urns,
                    }
                )
            except Exception:
                logger.warning("hierarchy_build_failed", exc_info=True)

        return hierarchy


class InferRelationshipsRequest(BaseModel):
    hierarchy: list[dict]


@router.post("/ontology/infer-relationships")
async def infer_relationships(body: InferRelationshipsRequest) -> list[dict]:
    relationships = []

    for i, cat_a in enumerate(body.hierarchy):
        urns_a = set(cat_a.get("dataset_urns", []))
        for cat_b in body.hierarchy[i + 1 :]:
            urns_b = set(cat_b.get("dataset_urns", []))
            shared = urns_a & urns_b
            if shared:
                relationships.append(
                    {
                        "source": cat_a["name"],
                        "target": cat_b["name"],
                        "type": "shared_datasets",
                        "shared_count": len(shared),
                    }
                )

    return relationships


class DetectDriftRequest(BaseModel):
    current_hierarchy: list[dict]


@router.post("/ontology/detect-drift")
async def detect_drift(body: DetectDriftRequest) -> list[dict]:
    from src.backend.ontology.service import OntologyService

    async with make_db_session() as db:
        service = OntologyService(db=db)
        existing_concepts, _ = await service.list_concepts(offset=0, limit=1000)

    existing_names = {c.name for c in existing_concepts}
    current_names = {h["name"] for h in body.current_hierarchy}

    drift = []
    for name in current_names - existing_names:
        drift.append({"type": "new_category", "name": name})
    for name in existing_names - current_names:
        drift.append({"type": "removed_category", "name": name})

    return drift
