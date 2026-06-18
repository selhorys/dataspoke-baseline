"""Per-source ingestion router — /spoke/ingestion.

Covers all source CRUD, run, dataset mapping, events, unmanaged bucket,
and secrets discovery endpoints.

Handler naming: BACKEND.md §Route Handler Naming Convention.
Auth: authenticated; writes require Editor or Admin (require_writer).
Spec: API.md §Ingestion (/spoke/ingestion).
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth.dependencies import (
    AuthContext,
    require_authenticated,
    require_editor,
    require_writer,
)
from src.api.dependencies import get_db, get_ingestion_service
from src.api.schemas.common import parse_sort
from src.api.schemas.events import EventListResponse, EventResponse
from src.api.schemas.ingestion import (
    CreateIngestionSourceRequest,
    IngestionRunResponse,
    IngestionSourceDatasetRow,
    IngestionSourceDatasetsResponse,
    IngestionSourceListResponse,
    IngestionSourceResponse,
    IngestionUnmanagedResponse,
    PatchIngestionSourceRequest,
    ReplaceIngestionSourceRequest,
    SecretRefInfo,
    SecretRefListResponse,
)
from src.backend.ingestion.service import IngestionService
from src.shared.db.models import DatasetRegistry, Event, IngestionSource, IngestionSourceDataset
from src.shared.exceptions import StorageUnavailableError
from src.shared.models.ingestion import Mode
from src.shared.secrets import (
    SecretResolverUnavailable,
    list_source_cred_refs,
)

router = APIRouter(
    prefix="/ingestion",
    tags=["ingestion"],
    dependencies=[Depends(require_authenticated)],
)


def _source_response(record: Any) -> IngestionSourceResponse:
    """Convert an IngestionSourceRecord to the API response model."""
    return IngestionSourceResponse(
        id=record.id,
        mode=Mode(record.mode),
        name=record.name,
        schedule=record.schedule,
        recipe=record.recipe,
        platform=record.platform,
        status=record.status,
        datahub_source_urn=record.datahub_source_urn,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


# ── Source CRUD ───────────────────────────────────────────────────────────────


@router.get("/sources", response_model=IngestionSourceListResponse)
async def get_ingestion_sources(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
    mode: Mode | None = Query(default=None, description="Filter by ingestion mode"),
    sort: str | None = Query(default=None),
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionSourceListResponse:
    """List ingestion sources (paginated; filter by mode).

    Returns all regular sources by default. Pass ``?mode=ACTIVE_CUSTOM_MANAGED``,
    ``?mode=PASSIVE``, or ``?mode=DATAHUB_MANAGED`` to narrow the list. DataHub
    CLI wrapper sources are internal plumbing and never appear in the list — their
    run events surface on the regular parent.
    """
    order_by = parse_sort(sort, {"created_at": IngestionSource.created_at}, None)
    sources, total_count = await service.list_sources(
        offset=offset,
        limit=limit,
        mode_filter=mode.value if mode else None,
        order_by=order_by,
    )
    return IngestionSourceListResponse(
        offset=offset,
        limit=limit,
        total_count=total_count,
        sources=[_source_response(s) for s in sources],
    )


@router.post(
    "/sources",
    response_model=IngestionSourceResponse,
    status_code=status.HTTP_201_CREATED,
)
async def post_ingestion_source(
    body: CreateIngestionSourceRequest,
    service: IngestionService = Depends(get_ingestion_service),
    _writer: AuthContext = Depends(require_writer),
) -> IngestionSourceResponse:
    """Create a new ingestion source.

    Only ACTIVE_CUSTOM_MANAGED and PASSIVE modes are accepted; DATAHUB_MANAGED
    rows are synced from DataHub and are read-only in DataSpoke.

    Returns ``409 INGESTION_SOURCE_READONLY`` when ``mode`` is DATAHUB_MANAGED.
    Returns ``422 SECRET_REF_MALFORMED`` or ``422 SECRET_REF_NOT_FOUND`` when
    recipe secret references are invalid or the Secret/key is absent.
    Returns ``422 INVALID_PARAMETER`` when the schedule does not map to a valid
    tier for ACTIVE_CUSTOM_MANAGED sources.
    """
    record = await service.create_source(
        mode=body.mode.value,
        name=body.name,
        schedule=body.schedule,
        recipe=body.recipe,
    )
    return _source_response(record)


@router.get("/sources/{id}", response_model=IngestionSourceResponse)
async def get_ingestion_source(
    id: str,
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionSourceResponse:
    """Get one source as JSON.

    Recipe ``${name__key}`` secret references are returned verbatim (they are the
    masked form; plaintext is never stored and is never returned on the read path).

    Returns ``404 INGESTION_SOURCE_NOT_FOUND`` when the id is absent.
    """
    record = await service.get_source(id)
    return _source_response(record)


@router.put("/sources/{id}", response_model=IngestionSourceResponse)
async def put_ingestion_source(
    id: str,
    body: ReplaceIngestionSourceRequest,
    service: IngestionService = Depends(get_ingestion_service),
    _writer: AuthContext = Depends(require_writer),
) -> IngestionSourceResponse:
    """Replace a source (full update).

    Returns ``409 INGESTION_SOURCE_READONLY`` for DATAHUB_MANAGED sources.
    Returns ``404 INGESTION_SOURCE_NOT_FOUND`` when the id is absent.
    Returns ``422`` on bad recipe shape, invalid secret refs, or invalid schedule.
    """
    record = await service.replace_source(
        source_id=id,
        mode=body.mode.value,
        name=body.name,
        schedule=body.schedule,
        recipe=body.recipe,
    )
    return _source_response(record)


@router.patch("/sources/{id}", response_model=IngestionSourceResponse)
async def patch_ingestion_source(
    id: str,
    body: PatchIngestionSourceRequest,
    service: IngestionService = Depends(get_ingestion_service),
    _writer: AuthContext = Depends(require_writer),
) -> IngestionSourceResponse:
    """Partially update a source.

    Fields absent from the request body are left unchanged.
    ``mode`` is not patchable — use PUT to change the mode.

    Returns ``409 INGESTION_SOURCE_READONLY`` for DATAHUB_MANAGED sources.
    Returns ``404 INGESTION_SOURCE_NOT_FOUND`` when the id is absent.
    Returns ``422`` on bad recipe shape, invalid secret refs, or invalid schedule.
    """
    patch: dict[str, Any] = body.model_dump(exclude_unset=True)
    record = await service.patch_source(source_id=id, patch=patch)
    return _source_response(record)


@router.delete("/sources/{id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ingestion_source(
    id: str,
    service: IngestionService = Depends(get_ingestion_service),
    _writer: AuthContext = Depends(require_writer),
) -> None:
    """Remove a source and cascade its dataset mappings.

    Returns ``409 INGESTION_SOURCE_READONLY`` for DATAHUB_MANAGED sources.
    Returns ``404 INGESTION_SOURCE_NOT_FOUND`` when the id is absent.
    """
    await service.delete_source(source_id=id)


# ── Run ───────────────────────────────────────────────────────────────────────


@router.post("/sources/{id}/method/run", response_model=IngestionRunResponse)
async def post_ingestion_source_run(
    id: str,
    dry_run: bool = Query(default=False),
    service: IngestionService = Depends(get_ingestion_service),
    _writer: AuthContext = Depends(require_writer),
) -> IngestionRunResponse:
    """Execute the extractor for an ACTIVE_CUSTOM_MANAGED source.

    Pass ``?dry_run=true`` to perform a no-write connection check without
    emitting any aspects to DataHub.

    Returns ``409 INGESTION_RUN_NOT_APPLICABLE`` for non-ACTIVE_CUSTOM_MANAGED sources.
    Returns ``409 INGESTION_RUNNING`` when a concurrent run is already in progress.
    Returns ``404 INGESTION_SOURCE_NOT_FOUND`` when the id is absent.
    """
    result = await service.run(source_id=id, dry_run=dry_run, manual=True)
    return IngestionRunResponse(
        run_id=result.run_id,
        status=result.status,
        detail={
            "entities_ingested": result.entities_ingested,
            "dry_run": result.dry_run,
            "emitted_urns_count": len(result.emitted_urns),
            "errors": result.errors,
            "warnings": result.warnings,
        },
    )


# ── Datasets mapping ──────────────────────────────────────────────────────────


@router.get("/sources/{id}/datasets", response_model=IngestionSourceDatasetsResponse)
async def get_ingestion_source_datasets(
    id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
    sort: str | None = Query(default=None),
    service: IngestionService = Depends(get_ingestion_service),
) -> IngestionSourceDatasetsResponse:
    """List datasets covered by this source (the current mapping).

    Each row carries ``dataset_urn``, ``authority``, ``derivation``,
    ``first_seen_at``, and ``last_seen_at``. The mapping is rebuilt by the
    hourly sync DAG. Paginated; sortable by ``dataset_urn`` (default:
    ``last_seen_at`` descending).

    Returns ``404 INGESTION_SOURCE_NOT_FOUND`` when the id is absent.
    """
    order_by = parse_sort(sort, {"dataset_urn": IngestionSourceDataset.dataset_urn}, None)
    datasets, total_count = await service.list_datasets_for_source(
        source_id=id, offset=offset, limit=limit, order_by=order_by
    )
    return IngestionSourceDatasetsResponse(
        offset=offset,
        limit=limit,
        total_count=total_count,
        datasets=[
            IngestionSourceDatasetRow(
                dataset_urn=d.dataset_urn,
                authority=d.authority,
                derivation=d.derivation,
                first_seen_at=d.first_seen_at,
                last_seen_at=d.last_seen_at,
            )
            for d in datasets
        ],
    )


# ── Events ────────────────────────────────────────────────────────────────────


@router.get("/sources/{id}/event", response_model=EventListResponse)
async def get_ingestion_source_event(
    id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
    sort: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: IngestionService = Depends(get_ingestion_service),
) -> EventListResponse:
    """Run/event history for a source (INGESTION.COMPLETE, INGESTION.FAIL, etc.).

    Returns ``404 INGESTION_SOURCE_NOT_FOUND`` when the id is absent.
    """
    order_by = parse_sort(sort, {"occurred_at": Event.occurred_at}, None)
    events, total_count = await service.get_events_for_source(
        source_id=id,
        offset=offset,
        limit=limit,
        from_dt=from_time,
        to_dt=to_time,
        order_by=order_by,
    )
    return EventListResponse(
        offset=offset,
        limit=limit,
        total_count=total_count,
        events=[
            EventResponse(
                id=str(e["id"]),
                entity_type=e["entity_type"],
                entity_id=e["entity_id"],
                event_type=e["event_type"],
                status=e["status"],
                detail=e.get("detail", {}),
                occurred_at=e["occurred_at"],
                wrapper=e.get("wrapper", False),
            )
            for e in events
        ],
    )


# ── Unmanaged bucket ──────────────────────────────────────────────────────────


@router.get("/unmanaged", response_model=IngestionUnmanagedResponse)
async def get_ingestion_unmanaged(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
    sort: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> IngestionUnmanagedResponse:
    """List DataHub dataset URNs not covered by any ingestion source.

    Returns URNs that exist in DataHub (``datahub_registered=True``) and have
    no row in ``ingestion_source_dataset``. The registry is populated by the
    hourly sync DAG; an empty list means all known datasets have an owning source.
    Paginated; sortable by ``dataset_urn`` (default: ``dataset_urn`` ascending).
    """
    # Subquery: dataset_urns that DO have a source mapping.
    mapped_subq = select(IngestionSourceDataset.dataset_urn).scalar_subquery()

    base_q = select(DatasetRegistry.dataset_urn).where(
        DatasetRegistry.datahub_registered.is_(True),
        DatasetRegistry.dataset_urn.not_in(mapped_subq),
    )

    count_q = select(func.count()).select_from(base_q.subquery())
    total_count = (await db.execute(count_q)).scalar() or 0

    order_by = parse_sort(
        sort,
        {"dataset_urn": DatasetRegistry.dataset_urn},
        DatasetRegistry.dataset_urn,
    )
    rows_q = base_q.order_by(order_by).offset(offset).limit(limit)
    result = await db.execute(rows_q)
    urns = [row[0] for row in result.all()]

    return IngestionUnmanagedResponse(
        offset=offset,
        limit=limit,
        total_count=total_count,
        dataset_urns=urns,
    )


# ── Secrets discovery ─────────────────────────────────────────────────────────


@router.get("/secrets", response_model=SecretRefListResponse)
async def get_ingestion_secrets(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
    sort: str | None = Query(default=None),
    _editor: AuthContext = Depends(require_editor),
) -> SecretRefListResponse:
    """List available source-credential references (no values returned).

    Enumerates Kubernetes Secrets whose name starts with
    ``dataspoke-source-cred-``, and returns one row per (secret, key) pair.
    ``ref`` is the literal string to paste into a recipe as ``${ref}``.

    Requires Editor or Admin role — secret name enumeration is a writer activity.

    **Authoring a new reference (reference-only model).** DataSpoke has no
    secret-write API; new source credentials are provisioned out-of-band by an
    admin and then *referenced* from a recipe. Create the backing Secret with::

        kubectl create secret generic dataspoke-source-cred-<name> \\
            --from-literal=<key>=<value> -n <dataspoke-namespace>

    The ``dataspoke-source-cred-`` name prefix is the security boundary — only
    Secrets under that prefix are resolvable — and ``<dataspoke-namespace>`` is
    the API pod's own namespace. Once created, the recipe references the value as
    ``${<name>__<key>}``. The source editor UI renders this same guide next to
    the reference list (see ``spec/feature/FRONTEND_INGESTION.md`` §Create View
    and ``spec/feature/SECRET_RESOLUTION.md`` §Admin authoring guide).

    Paginated; sortable by ``ref`` (default: ``ref`` ascending). The data source
    is the Kubernetes API (not a DB query), so the page is sliced in the router.

    Returns ``503 STORAGE_UNAVAILABLE`` when the in-cluster Kubernetes config
    is not loadable or the k8s API is unreachable.
    """
    try:
        refs = list_source_cred_refs()
    except SecretResolverUnavailable as exc:
        raise StorageUnavailableError(str(exc)) from exc

    # Whitelisted in-memory sort over the k8s-enumerated refs (not a DB column,
    # so parse_sort's SQLAlchemy clauses do not apply). Allowed field: ``ref``
    # (asc/desc); unknown values fall back to the default ``ref`` ascending.
    reverse = sort == "ref_desc"
    refs = sorted(refs, key=lambda r: r.ref, reverse=reverse)
    total_count = len(refs)
    page = refs[offset : offset + limit]

    return SecretRefListResponse(
        offset=offset,
        limit=limit,
        total_count=total_count,
        secrets=[SecretRefInfo(ref=r.ref, secret_name=r.secret_name, key=r.key) for r in page],
    )
