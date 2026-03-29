"""Internal activity endpoints — called by Kestra HTTP Request tasks.

Each endpoint corresponds to a Kestra activity. Business logic
remains in the backend service layer; these endpoints are thin wrappers
that handle error translation to HTTP status codes.

These endpoints are NOT exposed to end users — they are called by the
Kestra orchestrator running inside the same K8s namespace.
"""

import json
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.shared.exceptions import DataSpokeError
from src.workflows._common import (
    make_cache,
    make_datahub,
    make_db_session,
    make_llm,
    make_notification,
    make_qdrant,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/activities", tags=["internal/activities"])


def _error_response(exc: Exception, non_retryable: bool = True) -> JSONResponse:
    error_code = getattr(exc, "error_code", "INTERNAL_ERROR")
    status_code = 400 if non_retryable else 500
    return JSONResponse(
        status_code=status_code,
        content={"error_code": error_code, "message": str(exc), "non_retryable": non_retryable},
    )


# ── Ingestion periodic activities ────────────────────────────────────────────


class ListPeriodicDatasetsRequest(BaseModel):
    schedule: str


@router.post("/list-periodic-datasets")
async def list_periodic_datasets(body: ListPeriodicDatasetsRequest) -> list[str]:
    from src.backend.ingestion.service import IngestionService

    datahub = make_datahub()
    try:
        async with make_db_session() as db:
            service = IngestionService(datahub=datahub, db=db)
            return await service.list_periodic_datasets(body.schedule)
    except DataSpokeError as exc:
        return _error_response(exc)


class RunIngestionRequest(BaseModel):
    dataset_urn: str
    dry_run: bool = False


@router.post("/run-ingestion")
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


@router.post("/sync-periodic-ingestion-flows")
async def sync_periodic_ingestion_flows() -> dict:
    from src.shared.settings import settings
    from src.workflows.ingestion import sync_periodic_ingestion_flows as _sync
    from src.workflows.kestra.client import KestraClient

    kestra = KestraClient(
        base_url=settings.kestra_url,
        namespace=settings.kestra_namespace,
        username=settings.kestra_user,
        password=settings.kestra_password,
    )
    try:
        async with make_db_session() as db:
            result = await _sync(
                kestra_client=kestra,
                db=db,
                callback_base_url=settings.kestra_callback_base_url,
                concurrent=settings.kestra_ingestion_concurrent,
            )
            return result
    except DataSpokeError as exc:
        return _error_response(exc)
    finally:
        await kestra.close()


# ── Validation activity ──────────────────────────────────────────────────────


class RunValidationRequest(BaseModel):
    dataset_urn: str
    config_id: str | None = None
    dry_run: bool = False


@router.post("/run-validation")
async def run_validation(body: RunValidationRequest) -> dict:
    from src.backend.validation.service import ValidationService

    datahub = make_datahub()
    cache = make_cache()
    llm = make_llm()
    qdrant = make_qdrant()
    try:
        async with make_db_session() as db:
            service = ValidationService(datahub=datahub, db=db, cache=cache, llm=llm, qdrant=qdrant)
            result = await service.run(body.dataset_urn, config_id=body.config_id, dry_run=body.dry_run)
            return {"run_id": result.run_id, "status": result.status, "detail": result.detail}
    except DataSpokeError as exc:
        return _error_response(exc)


# ── Generation activity ──────────────────────────────────────────────────────


class RunGenerationRequest(BaseModel):
    dataset_urn: str


@router.post("/run-generation")
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


# ── Embedding sync activities ────────────────────────────────────────────────


class EnumerateDatasetsRequest(BaseModel):
    mode: str = "full"
    dataset_urn: str = ""


@router.post("/enumerate-datasets")
async def enumerate_datasets(body: EnumerateDatasetsRequest) -> list[str]:
    datahub = make_datahub()
    if body.mode == "single" and body.dataset_urn:
        return [body.dataset_urn]
    return await datahub.enumerate_datasets()


class ReindexBatchRequest(BaseModel):
    dataset_urns: list[str]


@router.post("/reindex-batch")
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


# ── Metrics activities ───────────────────────────────────────────────────────


class RunMetricRequest(BaseModel):
    metric_id: str
    dry_run: bool = False


@router.post("/run-metric")
async def run_metric(body: RunMetricRequest) -> dict:
    from src.backend.metrics.service import MetricsService

    datahub = make_datahub()
    cache = make_cache()
    notification = make_notification()
    try:
        async with make_db_session() as db:
            service = MetricsService(datahub=datahub, db=db, cache=cache, notification=notification)
            result = await service.run(body.metric_id, dry_run=body.dry_run)
            return {"run_id": result.run_id, "status": result.status, "detail": result.detail}
    except DataSpokeError as exc:
        return _error_response(exc)


@router.post("/aggregate-health")
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


class PublishMetricUpdateRequest(BaseModel):
    run_id: str | None = None
    status: str | None = None
    detail: dict | None = None


@router.post("/publish-metric-update")
async def publish_metric_update(body: PublishMetricUpdateRequest) -> dict:
    cache = make_cache()
    await cache.publish("ws:metric:updates", json.dumps(body.model_dump()))
    return {"published": True}


# ── SLA monitor activities ───────────────────────────────────────────────────


class CheckSLARequest(BaseModel):
    dataset_urn: str
    sla_target: dict


@router.post("/check-sla")
async def check_sla(body: CheckSLARequest) -> dict:
    from src.backend.validation.service import ValidationService
    from src.backend.validation.sla import check_sla as _check_sla

    datahub = make_datahub()
    cache = make_cache()
    llm = make_llm()
    qdrant = make_qdrant()

    quality_score = 0.0
    async with make_db_session() as db:
        service = ValidationService(datahub=datahub, db=db, cache=cache, llm=llm, qdrant=qdrant)
        try:
            results, _ = await service.get_results(body.dataset_urn, limit=1)
            if results:
                quality_score = results[0].quality_score
        except Exception:
            logger.warning("sla_quality_score_lookup_failed", exc_info=True)

    from datahub.metadata.schema_classes import DatasetProfileClass

    history = await datahub.get_timeseries(body.dataset_urn, DatasetProfileClass, limit=30)

    result = await _check_sla(
        datahub=datahub,
        dataset_urn=body.dataset_urn,
        sla_target=body.sla_target,
        history=history,
        quality_score=quality_score,
    )

    alerts = []
    if result.is_breaching or result.is_pre_breach:
        alerts.append(
            {
                "dataset_urn": body.dataset_urn,
                "is_breaching": result.is_breaching,
                "is_pre_breach": result.is_pre_breach,
                "violations": result.violations,
                "predicted_breach_at": (
                    result.predicted_breach_at.isoformat() if result.predicted_breach_at else None
                ),
            }
        )

    return {
        "dataset_urn": body.dataset_urn,
        "is_breaching": result.is_breaching,
        "is_pre_breach": result.is_pre_breach,
        "freshness_hours": result.current_freshness_hours,
        "quality_score": result.current_quality_score,
        "violations": result.violations,
        "alerts": alerts,
    }


class SendSLAAlertsRequest(BaseModel):
    alerts: list[dict]
    recipients: list[str]


def _build_recommended_actions(violations: list[str], is_breaching: bool) -> list[str]:
    """Return context-aware recommended actions derived from violation strings.

    Rules (applied in order, multiple may match):
    - "Freshness breach"  → upstream schedule + source availability checks
    - "Quality breach"    → schema change + completeness gap investigation
    - "Pre-breach"        → proactive trending + threshold-adjustment reminder
    - "Row count" / "baseline" → historical comparison + filtering-change check
    Fallback "Investigate upstream pipelines" is always appended when no rule
    matched so that recipients always have at least one action item.
    If ``is_breaching`` is True, an escalation action is added at the end.
    """
    actions: list[str] = []
    matched = False

    combined = " ".join(violations).lower()

    if "freshness breach" in combined:
        actions.extend(
            [
                "Check upstream pipeline schedules",
                "Verify source system availability",
            ]
        )
        matched = True

    if "quality breach" in combined:
        actions.extend(
            [
                "Review recent schema changes",
                "Investigate data completeness gaps",
            ]
        )
        matched = True

    if "pre-breach" in combined:
        actions.extend(
            [
                "Proactively investigate trending metrics",
                "Consider adjusting SLA thresholds if pattern is recurring",
            ]
        )
        matched = True

    if "row count" in combined or "baseline" in combined:
        actions.extend(
            [
                "Compare with historical patterns",
                "Check for upstream data filtering changes",
            ]
        )
        matched = True

    if not matched:
        actions.append("Investigate upstream pipelines")

    if is_breaching:
        actions.append("Escalate to data platform team")

    return actions


@router.post("/send-sla-alerts")
async def send_sla_alerts(body: SendSLAAlertsRequest) -> dict:
    from datetime import UTC, datetime

    from src.shared.notifications.models import SLAAlert

    notification = make_notification()

    for alert_data in body.alerts:
        predicted_str = alert_data.get("predicted_breach_at")
        predicted_dt = (
            datetime.fromisoformat(predicted_str) if predicted_str else datetime.now(tz=UTC)
        )
        violations: list[str] = alert_data.get("violations", [])
        is_breaching: bool = bool(alert_data.get("is_breaching", False))
        alert = SLAAlert(
            dataset_urn=alert_data["dataset_urn"],
            sla_name="freshness",
            predicted_breach_at=predicted_dt,
            root_cause="; ".join(violations),
            recommended_actions=_build_recommended_actions(violations, is_breaching),
        )
        await notification.send_sla_alert(body.recipients, alert)

    return {"sent": len(body.alerts)}


# ── Ontology activities ──────────────────────────────────────────────────────


class ClassifyDatasetsRequest(BaseModel):
    force: bool = False


@router.post("/classify-datasets")
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


@router.post("/build-hierarchy")
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


@router.post("/infer-relationships")
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


@router.post("/detect-drift")
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
