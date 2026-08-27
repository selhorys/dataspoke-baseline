"""Ontology Generation router — /spoke/ontogen/...

Routes:
  conf CRUD           GET/PUT/PATCH/DELETE /ontogen/attr/conf
  seed list/CRUD      GET/POST /ontogen/attr/seed
                      GET/PATCH/DELETE /ontogen/attr/seed/{seed_id}
                      PATCH /ontogen/attr/seed/{seed_id}/attr/enabled
  run                 POST /ontogen/method/run
  global events       GET /ontogen/event
  node result set     GET/GET/{id}/GET/{id}/event/POST/{id}/method/review
  edge result set     (same shape)
  triple result set   (same shape; requires dependency check on review)

Handler naming: BACKEND.md §Route Handler Naming Convention.
Auth: authenticated; writes require Editor or Admin (require_writer).
Spec: API.md §Ontology Generation.
"""

from datetime import datetime
from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from src.api.auth.dependencies import AuthContext, require_authenticated, require_writer
from src.api.dependencies import get_ontogen_service
from src.api.schemas._paths import UuidPath
from src.api.schemas.common import parse_sort
from src.api.schemas.events import EventListResponse, EventResponse
from src.api.schemas.ontogen import (
    EdgeListResponse,
    EdgeResponse,
    NodeListResponse,
    NodeResponse,
    OntogenConfPatchRequest,
    OntogenConfPutRequest,
    OntogenConfResponse,
    OntogenRunResponse,
    ReviewRequest,
    SeedEnabledRequest,
    SeedListItem,
    SeedListResponse,
    TripleListResponse,
    TripleResponse,
)
from src.backend.ontogen.service import OntogenService
from src.shared.db.models import (
    Event,
    OntogenEdge,
    OntogenNode,
    OntogenSeed,
    OntogenTriple,
)

# Response models narrow schedule_tier to this union; the DB column is plain str.
_ScheduleTier = Literal["hourly", "daily", "weekly"]


router = APIRouter(
    prefix="/ontogen",
    tags=["ontogen"],
    dependencies=[Depends(require_authenticated)],
)

# Maximum allowed size for text/markdown request bodies (128 KiB).
_MAX_MARKDOWN_BYTES = 128 * 1024


def _markdown_request_body(*, required: bool) -> dict[str, Any]:
    """Shared OpenAPI ``requestBody`` block for the raw-Markdown routes.

    ``maxLength`` counts characters (JSON Schema semantics), while
    ``_read_markdown_body`` enforces ``_MAX_MARKDOWN_BYTES`` on the raw
    UTF-8 byte length — so the schema is a safe (never-tighter) upper
    bound, not an exact contract for multi-byte text. The Request Body
    Object's own ``description`` (mirrored onto the schema for tooling
    that only surfaces schema-level text) spells out the real,
    byte-counted limit. The Media Type Object (``content["text/markdown"]``)
    has no ``description`` field in OpenAPI 3.0/3.1, so it must not live
    there.
    """
    description = (
        "Raw Markdown document. Hard cap is "
        f"{_MAX_MARKDOWN_BYTES} bytes of UTF-8 (maxLength is a "
        "character-count upper bound; multi-byte text reaches "
        "the cap sooner and returns 413 PAYLOAD_TOO_LARGE)."
    )
    return {
        "requestBody": {
            "required": required,
            "description": description,
            "content": {
                "text/markdown": {
                    "schema": {
                        "type": "string",
                        "maxLength": _MAX_MARKDOWN_BYTES,
                        "description": description,
                    },
                }
            },
        }
    }


async def _read_markdown_body(
    request: Request,
    *,
    max_bytes: int = _MAX_MARKDOWN_BYTES,
) -> bytes:
    """Read a raw Markdown body with a size cap.

    Rejects with 413 PAYLOAD_TOO_LARGE when Content-Length is declared
    above *max_bytes* or when the actual body exceeds *max_bytes*.
    """
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            cl = int(content_length)
        except ValueError:
            cl = None
        if cl is not None and cl > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail={
                    "error_code": "PAYLOAD_TOO_LARGE",
                    "message": f"Body exceeds {max_bytes} bytes",
                },
            )
    body = await request.body()
    if len(body) > max_bytes:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail={
                "error_code": "PAYLOAD_TOO_LARGE",
                "message": f"Body exceeds {max_bytes} bytes",
            },
        )
    return body


# ── Helpers ───────────────────────────────────────────────────────────────────


def _event_list(
    events: list[dict[str, Any]], total_count: int, offset: int, limit: int
) -> EventListResponse:
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
            )
            for e in events
        ],
    )


def _node_resp(row: object) -> NodeResponse:
    return NodeResponse(
        id=row.id,  # type: ignore[attr-defined]
        name=row.name,  # type: ignore[attr-defined]
        description=row.description or "",  # type: ignore[attr-defined]
        confidence_score=row.confidence_score,  # type: ignore[attr-defined]
        status=row.status,  # type: ignore[attr-defined]
        run_id=row.run_id,  # type: ignore[attr-defined]
        created_at=row.created_at,  # type: ignore[attr-defined]
        updated_at=row.updated_at,  # type: ignore[attr-defined]
    )


def _edge_resp(row: object) -> EdgeResponse:
    return EdgeResponse(
        id=row.id,  # type: ignore[attr-defined]
        label=row.label,  # type: ignore[attr-defined]
        semantics=row.semantics,  # type: ignore[attr-defined]
        confidence_score=row.confidence_score,  # type: ignore[attr-defined]
        status=row.status,  # type: ignore[attr-defined]
        run_id=row.run_id,  # type: ignore[attr-defined]
        created_at=row.created_at,  # type: ignore[attr-defined]
        updated_at=row.updated_at,  # type: ignore[attr-defined]
    )


def _triple_resp(row: object) -> TripleResponse:
    return TripleResponse(
        id=row.id,  # type: ignore[attr-defined]
        subject_node_id=row.subject_node_id,  # type: ignore[attr-defined]
        edge_id=row.edge_id,  # type: ignore[attr-defined]
        object_node_id=row.object_node_id,  # type: ignore[attr-defined]
        confidence_score=row.confidence_score,  # type: ignore[attr-defined]
        status=row.status,  # type: ignore[attr-defined]
        run_id=row.run_id,  # type: ignore[attr-defined]
        created_at=row.created_at,  # type: ignore[attr-defined]
        updated_at=row.updated_at,  # type: ignore[attr-defined]
    )


# ── Singleton conf ────────────────────────────────────────────────────────────


@router.get("/attr/conf", response_model=OntogenConfResponse)
async def get_ontogen_conf(
    service: OntogenService = Depends(get_ontogen_service),
) -> OntogenConfResponse:
    """Get the singleton ontogen operational conf."""
    row = await service.get_conf()
    return OntogenConfResponse(
        is_enabled=row.is_enabled,
        schedule_tier=cast(_ScheduleTier | None, row.schedule_tier),
        dataset_filter=row.dataset_filter or "",
        default_run_prompt=row.default_run_prompt,
        updated_at=row.updated_at,
    )


@router.put("/attr/conf", response_model=OntogenConfResponse)
async def put_ontogen_conf(
    body: OntogenConfPutRequest,
    service: OntogenService = Depends(get_ontogen_service),
    _writer: AuthContext = Depends(require_writer),
) -> OntogenConfResponse:
    """Create or replace the singleton ontogen operational conf."""
    row = await service.put_conf(body.model_dump())
    return OntogenConfResponse(
        is_enabled=row.is_enabled,
        schedule_tier=cast(_ScheduleTier | None, row.schedule_tier),
        dataset_filter=row.dataset_filter or "",
        default_run_prompt=row.default_run_prompt,
        updated_at=row.updated_at,
    )


@router.patch("/attr/conf", response_model=OntogenConfResponse)
async def patch_ontogen_conf(
    body: OntogenConfPatchRequest,
    service: OntogenService = Depends(get_ontogen_service),
    _writer: AuthContext = Depends(require_writer),
) -> OntogenConfResponse:
    """Partially update the singleton ontogen operational conf."""
    row = await service.patch_conf(body.model_dump(exclude_unset=True))
    return OntogenConfResponse(
        is_enabled=row.is_enabled,
        schedule_tier=cast(_ScheduleTier | None, row.schedule_tier),
        dataset_filter=row.dataset_filter or "",
        default_run_prompt=row.default_run_prompt,
        updated_at=row.updated_at,
    )


@router.delete("/attr/conf", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ontogen_conf(
    service: OntogenService = Depends(get_ontogen_service),
    _writer: AuthContext = Depends(require_writer),
) -> None:
    """Remove the singleton ontogen operational conf (resets to defaults)."""
    await service.delete_conf()


# ── Seeds ─────────────────────────────────────────────────────────────────────


@router.get("/attr/seed", response_model=SeedListResponse)
async def get_ontogen_seeds(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
    sort: str | None = Query(default=None),
    service: OntogenService = Depends(get_ontogen_service),
) -> SeedListResponse:
    """List inference seeds (enabled and disabled) with preview snippets.

    Paginated; sortable by created_at, updated_at (default: updated_at descending).
    """
    order_by = parse_sort(
        sort,
        {"created_at": OntogenSeed.created_at, "updated_at": OntogenSeed.updated_at},
        None,
    )
    previews, total = await service.list_seeds(offset=offset, limit=limit, order_by=order_by)
    return SeedListResponse(
        offset=offset,
        limit=limit,
        total_count=total,
        seeds=[
            SeedListItem(
                seed_id=p.seed_id,
                is_enabled=p.is_enabled,
                updated_at=p.updated_at,
                preview=p.preview,
            )
            for p in previews
        ],
    )


@router.post(
    "/attr/seed",
    status_code=status.HTTP_201_CREATED,
    openapi_extra=_markdown_request_body(required=True),
)
async def post_ontogen_seed(
    request: Request,
    service: OntogenService = Depends(get_ontogen_service),
    _writer: AuthContext = Depends(require_writer),
) -> dict[str, str]:
    """Create a new inference seed — body is a raw Markdown document (text/markdown)."""
    body_bytes = await _read_markdown_body(request)
    body_md = body_bytes.decode("utf-8", errors="replace")
    seed = await service.create_seed(body_md)
    return {"seed_id": str(seed.id), "updated_at": seed.updated_at.isoformat()}


@router.get("/attr/seed/{seed_id}")
async def get_ontogen_seed(
    seed_id: UuidPath,
    service: OntogenService = Depends(get_ontogen_service),
) -> Response:
    """Get a seed Markdown document (returns text/markdown)."""
    seed = await service.get_seed(seed_id)
    return Response(
        content=seed.body_md,
        media_type="text/markdown",
    )


@router.patch(
    "/attr/seed/{seed_id}",
    openapi_extra=_markdown_request_body(required=True),
)
async def patch_ontogen_seed(
    seed_id: UuidPath,
    request: Request,
    service: OntogenService = Depends(get_ontogen_service),
    _writer: AuthContext = Depends(require_writer),
) -> dict[str, str]:
    """Replace a seed Markdown body — body is a raw Markdown document (text/markdown)."""
    body_bytes = await _read_markdown_body(request)
    body_md = body_bytes.decode("utf-8", errors="replace")
    seed = await service.patch_seed(seed_id, body_md)
    return {"seed_id": str(seed.id), "updated_at": seed.updated_at.isoformat()}


@router.patch("/attr/seed/{seed_id}/attr/enabled")
async def patch_ontogen_seed_enabled(
    seed_id: UuidPath,
    body: SeedEnabledRequest,
    service: OntogenService = Depends(get_ontogen_service),
    _writer: AuthContext = Depends(require_writer),
) -> dict[str, Any]:
    """Enable or disable a seed (JSON ``{is_enabled}``).

    A disabled seed is retained and fully visible but excluded from the
    inference pipeline. Toggling is reversible both ways.
    """
    seed = await service.set_seed_enabled(seed_id, body.is_enabled)
    return {
        "seed_id": str(seed.id),
        "is_enabled": seed.is_enabled,
        "updated_at": seed.updated_at.isoformat(),
    }


@router.delete("/attr/seed/{seed_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_ontogen_seed(
    seed_id: UuidPath,
    service: OntogenService = Depends(get_ontogen_service),
    _writer: AuthContext = Depends(require_writer),
) -> None:
    """Delete a seed (hard delete)."""
    await service.delete_seed(seed_id)


# ── Run ───────────────────────────────────────────────────────────────────────


@router.post(
    "/method/run",
    response_model=OntogenRunResponse,
    openapi_extra=_markdown_request_body(required=False),
)
async def post_ontogen_run(
    request: Request,
    dry_run: bool = Query(default=False),
    service: OntogenService = Depends(get_ontogen_service),
    _writer: AuthContext = Depends(require_writer),
) -> OntogenRunResponse:
    """Trigger a manual ontogen re-inference run.

    Optional body: raw Markdown (Content-Type: text/markdown) as a one-shot
    prompt for this run only (not stored).  Falls back to
    conf.default_run_prompt when body is absent.

    ?dry_run=true evaluates without persisting.
    Concurrent runs return 409 ONTOGEN_RUNNING.
    """
    content_type = request.headers.get("content-type", "")
    prompt_md: str | None = None
    if "text/markdown" in content_type:
        body_bytes = await _read_markdown_body(request)
        if body_bytes:
            prompt_md = body_bytes.decode("utf-8", errors="replace")

    summary = await service.run(prompt_md=prompt_md, dry_run=dry_run)
    return OntogenRunResponse(
        status=summary.status,
        dry_run=summary.dry_run,
        unresolved_urns=summary.unresolved_urns,
        counts=summary.counts,
    )


# ── Global events ─────────────────────────────────────────────────────────────


@router.get("/event", response_model=EventListResponse)
async def get_ontogen_events(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
    sort: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: OntogenService = Depends(get_ontogen_service),
) -> EventListResponse:
    """Global ontogen inference-run event history.

    Paginated; sortable by ``occurred_at`` (default: ``occurred_at`` descending).
    """
    from src.shared.events import ONTOGEN_PREFIX

    order_by = parse_sort(sort, {"occurred_at": Event.occurred_at}, None)
    events, total = await service.list_global_events(
        "ontogen",
        "singleton",
        ONTOGEN_PREFIX,
        offset,
        limit,
        from_dt=from_time,
        to_dt=to_time,
        order_by=order_by,
    )
    return _event_list(events, total, offset, limit)


# ── Node result set ───────────────────────────────────────────────────────────


@router.get("/result/node", response_model=NodeListResponse)
async def get_ontogen_nodes(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
    status_filter: str | None = Query(default=None, alias="status"),
    sort: str | None = Query(default=None),
    service: OntogenService = Depends(get_ontogen_service),
) -> NodeListResponse:
    """List ontology nodes with confidence and lifecycle status.

    Paginated; sortable by ``created_at`` (default: ``created_at`` descending).
    """
    order_by = parse_sort(sort, {"created_at": OntogenNode.created_at}, None)
    rows, total = await service.list_nodes(
        status_filter=status_filter, offset=offset, limit=limit, order_by=order_by
    )
    return NodeListResponse(
        offset=offset,
        limit=limit,
        total_count=total,
        nodes=[_node_resp(r) for r in rows],
    )


@router.get("/result/node/{node_id}", response_model=NodeResponse)
async def get_ontogen_node(
    node_id: str,
    service: OntogenService = Depends(get_ontogen_service),
) -> NodeResponse:
    """Get ontology node detail including member datasets."""
    row = await service.get_node(node_id)
    return _node_resp(row)


@router.get("/result/node/{node_id}/event", response_model=EventListResponse)
async def get_ontogen_node_events(
    node_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
    sort: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: OntogenService = Depends(get_ontogen_service),
) -> EventListResponse:
    """Node-level change history (NODE.APPROVE, NODE.REJECT).

    Paginated; sortable by ``occurred_at`` (default: ``occurred_at`` descending).
    """
    order_by = parse_sort(sort, {"occurred_at": Event.occurred_at}, None)
    events, total = await service.list_node_events(
        node_id=node_id,
        offset=offset,
        limit=limit,
        from_dt=from_time,
        to_dt=to_time,
        order_by=order_by,
    )
    return _event_list(events, total, offset, limit)


@router.post("/result/node/{node_id}/method/review", response_model=NodeResponse)
async def post_ontogen_node_review(
    node_id: str,
    body: ReviewRequest,
    service: OntogenService = Depends(get_ontogen_service),
    _writer: AuthContext = Depends(require_writer),
) -> NodeResponse:
    """Review a pending node — approve or reject."""
    row = await service.review_node(node_id, verdict=body.verdict, reason=body.reason)
    return _node_resp(row)


# ── Edge result set ───────────────────────────────────────────────────────────


@router.get("/result/edge", response_model=EdgeListResponse)
async def get_ontogen_edges(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
    status_filter: str | None = Query(default=None, alias="status"),
    sort: str | None = Query(default=None),
    service: OntogenService = Depends(get_ontogen_service),
) -> EdgeListResponse:
    """List ontology edges (predicates) with confidence and status.

    Paginated; sortable by ``created_at`` (default: ``created_at`` descending).
    """
    order_by = parse_sort(sort, {"created_at": OntogenEdge.created_at}, None)
    rows, total = await service.list_edges(
        status_filter=status_filter, offset=offset, limit=limit, order_by=order_by
    )
    return EdgeListResponse(
        offset=offset,
        limit=limit,
        total_count=total,
        edges=[_edge_resp(r) for r in rows],
    )


@router.get("/result/edge/{edge_id}", response_model=EdgeResponse)
async def get_ontogen_edge(
    edge_id: str,
    service: OntogenService = Depends(get_ontogen_service),
) -> EdgeResponse:
    """Get ontology edge detail."""
    row = await service.get_edge(edge_id)
    return _edge_resp(row)


@router.get("/result/edge/{edge_id}/event", response_model=EventListResponse)
async def get_ontogen_edge_events(
    edge_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
    sort: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: OntogenService = Depends(get_ontogen_service),
) -> EventListResponse:
    """Edge-level change history (EDGE.APPROVE, EDGE.REJECT).

    Paginated; sortable by ``occurred_at`` (default: ``occurred_at`` descending).
    """
    order_by = parse_sort(sort, {"occurred_at": Event.occurred_at}, None)
    events, total = await service.list_edge_events(
        edge_id=edge_id,
        offset=offset,
        limit=limit,
        from_dt=from_time,
        to_dt=to_time,
        order_by=order_by,
    )
    return _event_list(events, total, offset, limit)


@router.post("/result/edge/{edge_id}/method/review", response_model=EdgeResponse)
async def post_ontogen_edge_review(
    edge_id: str,
    body: ReviewRequest,
    service: OntogenService = Depends(get_ontogen_service),
    _writer: AuthContext = Depends(require_writer),
) -> EdgeResponse:
    """Review a pending edge — approve or reject."""
    row = await service.review_edge(edge_id, verdict=body.verdict, reason=body.reason)
    return _edge_resp(row)


# ── Triple result set ─────────────────────────────────────────────────────────


@router.get("/result/triple", response_model=TripleListResponse)
async def get_ontogen_triples(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
    status_filter: str | None = Query(default=None, alias="status"),
    sort: str | None = Query(default=None),
    service: OntogenService = Depends(get_ontogen_service),
) -> TripleListResponse:
    """List ontology triples (subject/edge/object facts) with confidence and status.

    Paginated; sortable by ``created_at`` (default: ``created_at`` descending).
    """
    order_by = parse_sort(sort, {"created_at": OntogenTriple.created_at}, None)
    rows, total = await service.list_triples(
        status_filter=status_filter, offset=offset, limit=limit, order_by=order_by
    )
    return TripleListResponse(
        offset=offset,
        limit=limit,
        total_count=total,
        triples=[_triple_resp(r) for r in rows],
    )


@router.get("/result/triple/{triple_id}", response_model=TripleResponse)
async def get_ontogen_triple(
    triple_id: str,
    service: OntogenService = Depends(get_ontogen_service),
) -> TripleResponse:
    """Get ontology triple detail (resolved subject node, edge, object node)."""
    row = await service.get_triple(triple_id)
    return _triple_resp(row)


@router.get("/result/triple/{triple_id}/event", response_model=EventListResponse)
async def get_ontogen_triple_events(
    triple_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
    sort: str | None = Query(default=None),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    service: OntogenService = Depends(get_ontogen_service),
) -> EventListResponse:
    """Triple-level change history (TRIPLE.APPROVE, TRIPLE.REJECT).

    Paginated; sortable by ``occurred_at`` (default: ``occurred_at`` descending).
    """
    order_by = parse_sort(sort, {"occurred_at": Event.occurred_at}, None)
    events, total = await service.list_triple_events(
        triple_id=triple_id,
        offset=offset,
        limit=limit,
        from_dt=from_time,
        to_dt=to_time,
        order_by=order_by,
    )
    return _event_list(events, total, offset, limit)


@router.post("/result/triple/{triple_id}/method/review", response_model=TripleResponse)
async def post_ontogen_triple_review(
    triple_id: str,
    body: ReviewRequest,
    service: OntogenService = Depends(get_ontogen_service),
    _writer: AuthContext = Depends(require_writer),
) -> TripleResponse:
    """Review a pending triple — approve or reject.

    Returns 422 ONTOGEN_TRIPLE_DEPENDENCY_PENDING if the subject node,
    predicate edge, or object node is not yet approved.
    """
    row = await service.review_triple(triple_id, verdict=body.verdict, reason=body.reason)
    return _triple_resp(row)
