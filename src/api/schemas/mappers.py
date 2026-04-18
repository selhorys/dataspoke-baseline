"""Shared mapper functions from DB/service models to API response schemas.

These helpers are used by both the canonical /data/{urn}/attr/* handlers and
the dedicated list/detail routers (ingestion, validation, gen) to avoid code
duplication.
"""

from src.api.schemas.generation import GenerationConfigResponse
from src.api.schemas.ingestion import IngestionConfigResponse
from src.api.schemas.validation import ValidationConfigResponse


def ingestion_config_response(c: object) -> IngestionConfigResponse:
    """Map an IngestionConfig DB/service model to IngestionConfigResponse."""
    return IngestionConfigResponse(
        id=c.id if isinstance(c.id, str) else str(c.id),  # type: ignore[union-attr]
        dataset_urn=c.dataset_urn,  # type: ignore[union-attr]
        platform=c.platform,  # type: ignore[union-attr]
        locator=c.locator,  # type: ignore[union-attr]
        identifier=c.identifier,  # type: ignore[union-attr]
        auth=c.auth,  # type: ignore[union-attr]
        is_active=c.is_active,  # type: ignore[union-attr]
        schedule_tier=c.schedule_tier,  # type: ignore[union-attr]
        enrichment_sources=c.enrichment_sources,  # type: ignore[union-attr]
        custom_extractors=c.custom_extractors,  # type: ignore[union-attr]
        workflow_dag_id=c.workflow_dag_id,  # type: ignore[union-attr]
        status=c.status,  # type: ignore[union-attr]
        created_at=c.created_at,  # type: ignore[union-attr]
        updated_at=c.updated_at,  # type: ignore[union-attr]
    )


def validation_config_response(c: object) -> ValidationConfigResponse:
    """Map a ValidationConfig DB/service model to ValidationConfigResponse."""
    return ValidationConfigResponse(
        id=c.id if isinstance(c.id, str) else str(c.id),  # type: ignore[union-attr]
        dataset_urn=c.dataset_urn,  # type: ignore[union-attr]
        rules=c.rules,  # type: ignore[union-attr]
        schedule_tier=c.schedule_tier,  # type: ignore[union-attr]
        is_active=c.is_active,  # type: ignore[union-attr]
        owner=c.owner,  # type: ignore[union-attr]
        created_at=c.created_at,  # type: ignore[union-attr]
        updated_at=c.updated_at,  # type: ignore[union-attr]
    )


def generation_config_response(c: object) -> GenerationConfigResponse:
    """Map a GenerationConfig DB/service model to GenerationConfigResponse."""
    return GenerationConfigResponse(
        id=c.id if isinstance(c.id, str) else str(c.id),  # type: ignore[union-attr]
        dataset_urn=c.dataset_urn,  # type: ignore[union-attr]
        target_fields=c.target_fields,  # type: ignore[union-attr]
        code_refs=c.code_refs,  # type: ignore[union-attr]
        schedule_cron=c.schedule_cron,  # type: ignore[union-attr]
        status=c.status,  # type: ignore[union-attr]
        owner=c.owner,  # type: ignore[union-attr]
        created_at=c.created_at,  # type: ignore[union-attr]
        updated_at=c.updated_at,  # type: ignore[union-attr]
    )
