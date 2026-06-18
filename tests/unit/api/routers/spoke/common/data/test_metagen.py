"""Unit tests for data metagen sub-resource routes.

Routes under test:
  GET    /data/{urn}/attr/metagen/boundary
  PUT    /data/{urn}/attr/metagen/boundary
  PATCH  /data/{urn}/attr/metagen/boundary
  DELETE /data/{urn}/attr/metagen/boundary
  GET    /data/{urn}/attr/metagen/item
  GET    /data/{urn}/attr/metagen/item/{item_id}
  POST   /data/{urn}/attr/metagen/item/{item_id}/candidate/{cid}/method/review
  GET    /data/{urn}/event/metagen

spec: API.md §Common (/spoke/common) §Metadata Generation routes.
spec: API.md §Authentication — all spoke/common routes require valid JWT.
spec: API_DESIGN_PRINCIPLE_en.md §HTTP method semantics.
spec: feature/BACKEND.md §Metadata Generation Service — boundary, item, candidate review.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.dependencies import get_metagen_service
from src.api.main import app
from src.shared.exceptions import ConflictError, EntityNotFoundError, PreconditionFailedError
from tests.unit.api.conftest import auth_headers

_BASE = "/api/v1/spoke/common/data"
_VALID_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.reviews.user_ratings,DEV)"
_VALID_URN_ENC = (
    _VALID_URN.replace("(", "%28").replace(")", "%29").replace(",", "%2C")
)

_BOUNDARY_URL = f"{_BASE}/{_VALID_URN_ENC}/attr/metagen/boundary"
_ITEM_LIST_URL = f"{_BASE}/{_VALID_URN_ENC}/attr/metagen/item"
_EVENTS_URL = f"{_BASE}/{_VALID_URN_ENC}/event/metagen"


def _item_id(kind: str = "dataset") -> str:
    return "dataset.description" if kind == "dataset" else "column.field_a.description"


def _candidate_review_url(item_id: str, candidate_id: str) -> str:
    encoded_urn = _VALID_URN_ENC
    return (
        f"{_BASE}/{encoded_urn}/attr/metagen/item"
        f"/{item_id}/candidate/{candidate_id}/method/review"
    )


def _make_boundary_dto() -> MagicMock:
    dto = MagicMock()
    dto.dataset_urn = _VALID_URN
    dto.is_enabled = True
    dto.allowed = ["dataset.description", "column.description"]
    dto.owner = "alice@example.com"
    dto.created_at = datetime.now(tz=UTC)
    dto.updated_at = datetime.now(tz=UTC)
    return dto


def _make_item_summary_dto(
    item_id: str = "dataset.description",
    kind: str = "dataset.description",
    has_approved: bool = False,
    candidate_count: int = 1,
    non_rejected_count: int | None = None,
) -> MagicMock:
    dto = MagicMock()
    dto.dataset_urn = _VALID_URN
    dto.item_id = item_id
    dto.kind = kind
    dto.field_path = None
    dto.has_approved = has_approved
    dto.candidate_count = candidate_count
    # Default: non_rejected_count tracks candidate_count unless the caller overrides
    # (e.g. an item whose only candidates are rejected → non_rejected_count=0).
    dto.non_rejected_count = (
        non_rejected_count if non_rejected_count is not None else candidate_count
    )
    dto.created_at = datetime.now(tz=UTC)
    dto.updated_at = datetime.now(tz=UTC)
    return dto


def _make_candidate_dto(
    status: str = "llm_approved",
    candidate_id: str | None = None,
    item_id: str = "dataset.description",
    conf_id: str | None = None,
    conf_name: str | None = "catalog-docs",
) -> MagicMock:
    dto = MagicMock()
    dto.candidate_id = candidate_id or str(uuid.uuid4())
    dto.conf_id = conf_id if conf_id is not None else str(uuid.uuid4())
    dto.conf_name = conf_name
    dto.item_id = item_id
    dto.dataset_urn = _VALID_URN
    dto.value = "A synthetic description from the LLM."
    dto.confidence_score = 0.87
    dto.status = status
    dto.evidence = {}
    dto.created_at = datetime.now(tz=UTC)
    dto.reviewed_at = None
    dto.reviewer_id = None
    return dto


def _make_item_detail_dto(candidates: list | None = None) -> MagicMock:
    cands = candidates if candidates is not None else [_make_candidate_dto()]
    dto = MagicMock()
    dto.dataset_urn = _VALID_URN
    dto.item_id = "dataset.description"
    dto.kind = "dataset.description"
    dto.field_path = None
    dto.has_approved = any(c.status == "approved" for c in cands)
    dto.candidate_count = len(cands)
    dto.non_rejected_count = sum(1 for c in cands if c.status != "rejected")
    dto.created_at = datetime.now(tz=UTC)
    dto.updated_at = datetime.now(tz=UTC)
    dto.candidates = cands
    return dto


@pytest.fixture
def mock_svc() -> AsyncMock:
    return AsyncMock()


@pytest.fixture(autouse=True)
def override_service(mock_svc: AsyncMock):
    app.dependency_overrides[get_metagen_service] = lambda: mock_svc
    yield
    app.dependency_overrides.pop(get_metagen_service, None)


# ── Auth gates: 401 without token ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_boundary_without_token_returns_401(client) -> None:
    """GET /attr/metagen/boundary requires a valid JWT.

    spec: API.md §Authentication — all spoke/common routes require a valid token.
    """
    resp = await client.get(_BOUNDARY_URL)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_put_boundary_without_token_returns_401(client) -> None:
    """PUT /attr/metagen/boundary requires a valid JWT.

    spec: API.md §Authentication — all write routes require valid JWT.
    """
    resp = await client.put(
        _BOUNDARY_URL,
        json={"is_enabled": True, "allowed": ["dataset.description"]},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_patch_boundary_without_token_returns_401(client) -> None:
    """PATCH /attr/metagen/boundary requires a valid JWT.

    spec: API.md §Authentication — all write routes require valid JWT.
    """
    resp = await client.patch(_BOUNDARY_URL, json={"is_enabled": False})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_delete_boundary_without_token_returns_401(client) -> None:
    """DELETE /attr/metagen/boundary requires a valid JWT.

    spec: API.md §Authentication — all write routes require valid JWT.
    """
    resp = await client.delete(_BOUNDARY_URL)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_item_list_without_token_returns_401(client) -> None:
    """GET /attr/metagen/item requires a valid JWT.

    spec: API.md §Authentication — all spoke/common routes require a valid token.
    """
    resp = await client.get(_ITEM_LIST_URL)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_candidate_review_without_token_returns_401(client) -> None:
    """POST .../candidate/{id}/method/review requires a valid JWT.

    spec: API.md §Authentication — all write routes require valid JWT.
    """
    url = _candidate_review_url("dataset.description", str(uuid.uuid4()))
    resp = await client.post(url, json={"verdict": "approve"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_events_without_token_returns_401(client) -> None:
    """GET /event/metagen requires a valid JWT.

    spec: API.md §Authentication — all spoke/common routes require a valid token.
    """
    resp = await client.get(_EVENTS_URL)
    assert resp.status_code == 401


# ── Boundary CRUD ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_boundary_returns_200_when_present(client, mock_svc: AsyncMock) -> None:
    """GET /attr/metagen/boundary returns 200 with boundary fields when one exists.

    spec: API.md §Data Resource — GET returns 200 when resource present.
    spec: feature/BACKEND.md §Metadata Generation Service §Boundary CRUD.
    """
    mock_svc.get_boundary = AsyncMock(return_value=_make_boundary_dto())

    resp = await client.get(_BOUNDARY_URL, headers=auth_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["dataset_urn"] == _VALID_URN
    assert body["is_enabled"] is True
    assert "dataset.description" in body["allowed"]
    assert "column.description" in body["allowed"]
    assert body["owner"] == "alice@example.com"


@pytest.mark.asyncio
async def test_get_boundary_returns_null_when_absent(client, mock_svc: AsyncMock) -> None:
    """GET /attr/metagen/boundary returns null body (200) when no boundary exists.

    spec: API.md §Data Resource — absent boundary is a 200 null, not 404.
    """
    mock_svc.get_boundary = AsyncMock(return_value=None)

    resp = await client.get(_BOUNDARY_URL, headers=auth_headers())

    assert resp.status_code == 200
    assert resp.json() is None


@pytest.mark.asyncio
async def test_put_boundary_returns_200_with_boundary_fields(client, mock_svc: AsyncMock) -> None:
    """PUT /attr/metagen/boundary returns 200 with boundary fields (create-or-replace).

    spec: API_DESIGN_PRINCIPLE_en.md §HTTP method semantics — PUT is create-or-replace.
    spec: feature/BACKEND.md §Metadata Generation Service §Boundary CRUD.
    """
    mock_svc.put_boundary = AsyncMock(return_value=_make_boundary_dto())

    resp = await client.put(
        _BOUNDARY_URL,
        json={"is_enabled": True, "allowed": ["dataset.description", "column.description"]},
        headers=auth_headers(),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["dataset_urn"] == _VALID_URN
    assert body["is_enabled"] is True


@pytest.mark.asyncio
async def test_put_boundary_rejects_invalid_kind(client, mock_svc: AsyncMock) -> None:
    """PUT /attr/metagen/boundary with an unknown 'allowed' kind returns 422.

    spec: API.md §Data Resource — metagen boundary allowed values are
    'dataset.description' and 'column.description' only.
    """
    resp = await client.put(
        _BOUNDARY_URL,
        json={"is_enabled": True, "allowed": ["invalid.kind"]},
        headers=auth_headers(),
    )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_boundary_with_owner_returns_owner_in_response(
    client, mock_svc: AsyncMock
) -> None:
    """PUT /attr/metagen/boundary with owner returns the owner field in the response body.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service §Boundary CRUD —
    boundary has optional owner field; PUT round-trips it in the response.
    """
    dto = _make_boundary_dto()
    dto.owner = "some-user"
    mock_svc.put_boundary = AsyncMock(return_value=dto)

    resp = await client.put(
        _BOUNDARY_URL,
        json={"is_enabled": True, "allowed": ["dataset.description"], "owner": "some-user"},
        headers=auth_headers(),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["owner"] == "some-user", (
        "PUT with owner must round-trip the owner value in the response. "
        "spec: BACKEND.md §Boundary CRUD — boundary has optional owner field"
    )


@pytest.mark.asyncio
async def test_patch_boundary_returns_200_with_updated_fields(client, mock_svc: AsyncMock) -> None:
    """PATCH /attr/metagen/boundary returns 200 with the updated boundary.

    spec: API_DESIGN_PRINCIPLE_en.md §HTTP method semantics — PATCH returns 200.
    """
    updated = _make_boundary_dto()
    updated.is_enabled = False
    mock_svc.patch_boundary = AsyncMock(return_value=updated)

    resp = await client.patch(
        _BOUNDARY_URL,
        json={"is_enabled": False},
        headers=auth_headers(),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_enabled"] is False


@pytest.mark.asyncio
async def test_delete_boundary_returns_204(client, mock_svc: AsyncMock) -> None:
    """DELETE /attr/metagen/boundary returns 204 No Content.

    spec: API_DESIGN_PRINCIPLE_en.md §HTTP method semantics — DELETE returns 204.
    """
    mock_svc.delete_boundary = AsyncMock(return_value=None)

    resp = await client.delete(_BOUNDARY_URL, headers=auth_headers())

    assert resp.status_code == 204


# ── Per-dataset item list ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_items_returns_200_with_items_and_total_count(
    client, mock_svc: AsyncMock
) -> None:
    """GET /attr/metagen/item returns 200 with items list and total_count.

    spec: API.md §Standard Response Envelope — list responses carry total_count.
    spec: feature/BACKEND.md §Metadata Generation Service §Item list.
    """
    dto = _make_item_summary_dto()
    mock_svc.list_items_for_dataset = AsyncMock(return_value=([dto], 1))

    resp = await client.get(_ITEM_LIST_URL, headers=auth_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] == 1
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["dataset_urn"] == _VALID_URN
    assert item["item_id"] == "dataset.description"
    assert item["kind"] == "dataset.description"


@pytest.mark.asyncio
async def test_get_items_returns_empty_list_when_none_exist(
    client, mock_svc: AsyncMock
) -> None:
    """GET /attr/metagen/item returns empty items list when dataset has no items.

    spec: API.md §Standard Response Envelope — empty list is valid.
    """
    mock_svc.list_items_for_dataset = AsyncMock(return_value=([], 0))

    resp = await client.get(_ITEM_LIST_URL, headers=auth_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] == 0
    assert body["items"] == []


@pytest.mark.asyncio
async def test_get_items_item_status_approved_when_has_approved_true(
    client, mock_svc: AsyncMock
) -> None:
    """Item status is 'approved' when has_approved=True.

    spec: feature/BACKEND.md — metagen_candidates.status ∈ {llm_approved, approved, rejected}.
    When an item has at least one approved candidate, its summary status is 'approved'.
    """
    dto_approved = _make_item_summary_dto(
        item_id="dataset.description",
        has_approved=True,
        candidate_count=2,
    )
    mock_svc.list_items_for_dataset = AsyncMock(return_value=([dto_approved], 1))

    resp = await client.get(_ITEM_LIST_URL, headers=auth_headers())

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items[0]["status"] == "approved", (
        "has_approved=True must yield status='approved'. "
        "spec: BACKEND.md — metagen_candidates.status values"
    )


@pytest.mark.asyncio
async def test_get_items_item_status_llm_approved_when_no_approved_but_candidates_exist(
    client, mock_svc: AsyncMock
) -> None:
    """Item status is 'llm_approved' when has_approved=False and candidate_count > 0.

    spec: feature/BACKEND.md — metagen_candidates.status ∈ {llm_approved, approved, rejected}.
    When an item has candidates but none is approved, its summary status is 'llm_approved'.
    """
    dto_llm_approved = _make_item_summary_dto(
        item_id="column.field_a.description",
        kind="column.description",
        has_approved=False,
        candidate_count=1,
    )
    mock_svc.list_items_for_dataset = AsyncMock(return_value=([dto_llm_approved], 1))

    resp = await client.get(_ITEM_LIST_URL, headers=auth_headers())

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items[0]["status"] == "llm_approved", (
        "has_approved=False with candidates must yield status='llm_approved'. "
        "spec: BACKEND.md — metagen_candidates.status values"
    )


@pytest.mark.asyncio
async def test_get_items_item_status_pending_when_no_non_rejected_candidates(
    client, mock_svc: AsyncMock
) -> None:
    """Item with no non-rejected candidates yields status='pending'.

    spec: feature/BACKEND.md §Item status — 'pending' when no non-rejected candidates
    exist for the item yet (a freshly enumerated slot before its first successful run).
    """
    dto_no_cands = _make_item_summary_dto(
        item_id="column.field_b.description",
        kind="column.description",
        has_approved=False,
        candidate_count=0,
        non_rejected_count=0,
    )
    mock_svc.list_items_for_dataset = AsyncMock(return_value=([dto_no_cands], 1))

    resp = await client.get(_ITEM_LIST_URL, headers=auth_headers())

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items[0]["status"] == "pending", (
        "An item with no non-rejected candidates must be 'pending'. "
        "spec: feature/BACKEND.md §Item status"
    )


@pytest.mark.asyncio
async def test_get_items_item_status_pending_when_only_rejected_candidates(
    client, mock_svc: AsyncMock
) -> None:
    """Item whose only candidates are rejected yields status='pending' (status is derived
    over NON-rejected candidates).

    spec: feature/BACKEND.md §Item status — status is derived over non-rejected candidates;
    'pending' when no non-rejected candidate exists, even if rejected ones do.
    """
    dto_rejected_only = _make_item_summary_dto(
        item_id="dataset.description",
        has_approved=False,
        candidate_count=2,  # two candidates exist...
        non_rejected_count=0,  # ...but both are rejected
    )
    mock_svc.list_items_for_dataset = AsyncMock(return_value=([dto_rejected_only], 1))

    resp = await client.get(_ITEM_LIST_URL, headers=auth_headers())

    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items[0]["status"] == "pending", (
        "An item whose only candidates are rejected must be 'pending', not 'llm_approved'. "
        "spec: feature/BACKEND.md §Item status — derived over non-rejected candidates"
    )


@pytest.mark.asyncio
async def test_get_items_composite_id_uses_double_colon_separator(
    client, mock_svc: AsyncMock
) -> None:
    """Item composite_id is '{dataset_urn}::{item_id}'.

    spec: API.md §Data Resource — composite_id format is {dataset_urn}::{item_id}.
    """
    dto = _make_item_summary_dto(item_id="dataset.description")
    mock_svc.list_items_for_dataset = AsyncMock(return_value=([dto], 1))

    resp = await client.get(_ITEM_LIST_URL, headers=auth_headers())

    assert resp.status_code == 200
    item = resp.json()["items"][0]
    expected_composite = f"{_VALID_URN}::dataset.description"
    assert item["composite_id"] == expected_composite


@pytest.mark.asyncio
async def test_get_items_respects_offset_and_limit(client, mock_svc: AsyncMock) -> None:
    """GET /attr/metagen/item passes offset and limit to the service.

    spec: API.md §Pagination — offset and limit query params forwarded to service.
    """
    mock_svc.list_items_for_dataset = AsyncMock(return_value=([], 0))

    resp = await client.get(
        f"{_ITEM_LIST_URL}?offset=10&limit=5",
        headers=auth_headers(),
    )

    assert resp.status_code == 200
    mock_svc.list_items_for_dataset.assert_called_once_with(
        _VALID_URN, offset=10, limit=5, order_by=None
    )


# ── Per-dataset item detail ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_item_detail_returns_200_with_candidates(
    client, mock_svc: AsyncMock
) -> None:
    """GET /attr/metagen/item/{item_id} returns 200 with candidates list.

    spec: API.md §Data Resource — item detail includes candidates.
    spec: feature/BACKEND.md §Metadata Generation Service §Item detail.
    """
    conf_id = str(uuid.uuid4())
    cand = _make_candidate_dto(status="llm_approved", conf_id=conf_id, conf_name="catalog-docs")
    detail_dto = _make_item_detail_dto(candidates=[cand])
    mock_svc.get_item_for_dataset = AsyncMock(return_value=detail_dto)

    resp = await client.get(
        f"{_ITEM_LIST_URL}/dataset.description",
        headers=auth_headers(),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["item_id"] == "dataset.description"
    assert len(body["candidates"]) == 1
    assert body["candidates"][0]["status"] == "llm_approved"
    assert body["candidates"][0]["confidence_score"] == pytest.approx(0.87)


@pytest.mark.asyncio
async def test_get_item_detail_candidate_carries_conf_id_and_conf_name(
    client, mock_svc: AsyncMock
) -> None:
    """Each candidate in the item detail carries conf_id and conf_name.

    spec: API.md §Metadata Generation — item detail includes all candidates with
    conf_id/conf_name (the conf that produced each candidate).
    """
    conf_id = str(uuid.uuid4())
    cand = _make_candidate_dto(status="llm_approved", conf_id=conf_id, conf_name="catalog-docs")
    detail_dto = _make_item_detail_dto(candidates=[cand])
    mock_svc.get_item_for_dataset = AsyncMock(return_value=detail_dto)

    resp = await client.get(
        f"{_ITEM_LIST_URL}/dataset.description",
        headers=auth_headers(),
    )

    assert resp.status_code == 200
    candidate = resp.json()["candidates"][0]
    assert candidate["conf_id"] == conf_id, (
        "Candidate must carry the producing conf_id. "
        "spec: API.md §Metadata Generation — candidate exposes conf_id/conf_name"
    )
    assert candidate["conf_name"] == "catalog-docs", (
        "Candidate must carry the producing conf_name. "
        "spec: API.md §Metadata Generation — candidate exposes conf_id/conf_name"
    )


@pytest.mark.asyncio
async def test_get_item_detail_returns_404_when_not_found(
    client, mock_svc: AsyncMock
) -> None:
    """GET /attr/metagen/item/{item_id} returns 404 when item does not exist.

    spec: API_DESIGN_PRINCIPLE_en.md §HTTP status codes — 404 for unknown resource.
    """
    mock_svc.get_item_for_dataset = AsyncMock(
        side_effect=EntityNotFoundError("metagen_item", "dataset.description")
    )

    resp = await client.get(
        f"{_ITEM_LIST_URL}/dataset.description",
        headers=auth_headers(),
    )

    assert resp.status_code == 404


# ── Candidate review ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_review_approve_returns_200_with_approved_status(
    client, mock_svc: AsyncMock
) -> None:
    """POST .../candidate/{id}/method/review with verdict=approve returns 200.

    Approved candidate dto is returned with status='approved'.

    spec: feature/BACKEND.md §Metadata Generation Service §Approval flow —
    approve transitions candidate to 'approved'.
    """
    cid = str(uuid.uuid4())
    approved_dto = _make_candidate_dto(status="approved", candidate_id=cid)
    mock_svc.review_candidate = AsyncMock(return_value=approved_dto)

    url = _candidate_review_url("dataset.description", cid)
    resp = await client.post(
        url,
        json={"verdict": "approve", "reason": "Looks good"},
        headers=auth_headers(),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["candidate_id"] == cid
    assert body["status"] == "approved"


@pytest.mark.asyncio
async def test_review_reject_returns_200_with_rejected_status(
    client, mock_svc: AsyncMock
) -> None:
    """POST .../candidate/{id}/method/review with verdict=reject returns 200.

    Rejected candidate dto is returned with status='rejected'.

    spec: feature/BACKEND.md §Metadata Generation Service §Approval flow —
    reject transitions llm_approved candidate to 'rejected'.
    """
    cid = str(uuid.uuid4())
    rejected_dto = _make_candidate_dto(status="rejected", candidate_id=cid)
    mock_svc.review_candidate = AsyncMock(return_value=rejected_dto)

    url = _candidate_review_url("dataset.description", cid)
    resp = await client.post(
        url,
        json={"verdict": "reject", "reason": "Not relevant"},
        headers=auth_headers(),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["candidate_id"] == cid
    assert body["status"] == "rejected"


@pytest.mark.asyncio
async def test_review_reject_approved_returns_409(client, mock_svc: AsyncMock) -> None:
    """POST .../method/review with verdict=reject on an approved candidate returns 409.

    spec: feature/BACKEND.md §Metadata Generation Service §Approval flow —
    cannot reject an approved candidate (METAGEN_CANNOT_REJECT_APPROVED).
    """
    cid = str(uuid.uuid4())
    mock_svc.review_candidate = AsyncMock(
        side_effect=ConflictError(
            "METAGEN_CANNOT_REJECT_APPROVED",
            "Cannot reject an approved candidate — approve a different sibling to demote it",
        )
    )

    url = _candidate_review_url("dataset.description", cid)
    resp = await client.post(
        url,
        json={"verdict": "reject"},
        headers=auth_headers(),
    )

    assert resp.status_code == 409
    body = resp.json()
    assert body["error_code"] == "METAGEN_CANNOT_REJECT_APPROVED"


@pytest.mark.asyncio
async def test_review_dataset_not_in_boundary_returns_422(
    client, mock_svc: AsyncMock
) -> None:
    """POST .../method/review returns 422 when dataset has no active boundary.

    spec: feature/BACKEND.md §Metadata Generation Service — review_candidate
    raises METAGEN_DATASET_NOT_IN_BOUNDARY when no is_enabled=true boundary exists.
    """
    cid = str(uuid.uuid4())
    mock_svc.review_candidate = AsyncMock(
        side_effect=PreconditionFailedError(
            "METAGEN_DATASET_NOT_IN_BOUNDARY",
            f"Dataset {_VALID_URN!r} has no active boundary",
        )
    )

    url = _candidate_review_url("dataset.description", cid)
    resp = await client.post(
        url,
        json={"verdict": "approve"},
        headers=auth_headers(),
    )

    assert resp.status_code == 422
    body = resp.json()
    assert body["error_code"] == "METAGEN_DATASET_NOT_IN_BOUNDARY"


@pytest.mark.asyncio
async def test_review_candidate_not_found_returns_404(
    client, mock_svc: AsyncMock
) -> None:
    """POST .../method/review returns 404 when candidate_id does not exist.

    spec: API_DESIGN_PRINCIPLE_en.md §HTTP status codes — 404 for unknown resource.
    """
    cid = str(uuid.uuid4())
    mock_svc.review_candidate = AsyncMock(
        side_effect=EntityNotFoundError("metagen_candidate", cid)
    )

    url = _candidate_review_url("dataset.description", cid)
    resp = await client.post(
        url,
        json={"verdict": "approve"},
        headers=auth_headers(),
    )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_review_invalid_verdict_returns_422(client, mock_svc: AsyncMock) -> None:
    """POST .../method/review with invalid verdict returns 422.

    spec: API.md §Data Resource — verdict must be 'approve' or 'reject'.
    """
    cid = str(uuid.uuid4())
    url = _candidate_review_url("dataset.description", cid)

    resp = await client.post(
        url,
        json={"verdict": "invalid_verdict"},
        headers=auth_headers(),
    )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_review_reason_too_long_returns_422(client, mock_svc: AsyncMock) -> None:
    """POST .../method/review with reason > 2000 chars returns 422.

    spec: API.md §Data Resource — reason max_length=2000.
    """
    cid = str(uuid.uuid4())
    url = _candidate_review_url("dataset.description", cid)

    resp = await client.post(
        url,
        json={"verdict": "approve", "reason": "x" * 2001},
        headers=auth_headers(),
    )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_review_reason_exactly_2000_chars_is_accepted(
    client, mock_svc: AsyncMock
) -> None:
    """POST .../method/review with reason exactly 2000 chars is valid.

    spec: API.md §Data Resource — reason max_length=2000 is inclusive.
    """
    cid = str(uuid.uuid4())
    approved_dto = _make_candidate_dto(status="approved", candidate_id=cid)
    mock_svc.review_candidate = AsyncMock(return_value=approved_dto)

    url = _candidate_review_url("dataset.description", cid)
    resp = await client.post(
        url,
        json={"verdict": "approve", "reason": "x" * 2000},
        headers=auth_headers(),
    )

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_review_reviewer_id_forwarded_from_auth_token(
    client, mock_svc: AsyncMock
) -> None:
    """review_candidate is called with reviewer_id equal to the authenticated user's UUID.

    spec: feature/BACKEND.md §Metadata Generation Service §Approval flow —
    reviewer_id is the authenticated user's identity (str(ctx.user.id)).

    The base client fixture provides a mock Admin user with a stable UUID
    (_TEST_USER_ID from tests/unit/api/conftest.py). A regression that forwards
    reviewer_id=None must fail this test.
    """
    from tests.unit.api.conftest import _TEST_USER_ID

    cid = str(uuid.uuid4())
    approved_dto = _make_candidate_dto(status="approved", candidate_id=cid)
    mock_svc.review_candidate = AsyncMock(return_value=approved_dto)

    url = _candidate_review_url("dataset.description", cid)
    resp = await client.post(
        url,
        json={"verdict": "approve"},
        headers=auth_headers(),
    )

    assert resp.status_code == 200
    call_kwargs = mock_svc.review_candidate.call_args.kwargs
    assert "reviewer_id" in call_kwargs, (
        "review_candidate must be called with reviewer_id kwarg. "
        "spec: BACKEND.md §Approval flow — reviewer_id is the authenticated user's identity"
    )
    assert call_kwargs["reviewer_id"] == str(_TEST_USER_ID), (
        f"reviewer_id must equal the mock user's UUID str '{_TEST_USER_ID}'. "
        "A regression forwarding reviewer_id=None must fail this assertion. "
        "spec: BACKEND.md §Approval flow — reviewer_id is the authenticated user's identity"
    )


# ── Per-dataset metagen events ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_events_returns_200_with_events_envelope(
    client, mock_svc: AsyncMock
) -> None:
    """GET /event/metagen returns 200 with 'events' key and total_count.

    spec: API.md §Standard Response Envelope — list responses use resource-named key.
    """
    # The event route queries DB directly (no service method); mock db via dependency
    # override is not straightforward here — test at the network boundary using the
    # route's empty-result happy path via the real DB dep being replaced by a mock.
    # For this unit test we verify the route is wired and returns the correct shape
    # by letting the DB mock return empty results through the existing override chain.
    # Since the route uses get_db (not get_metagen_service), we override get_db.
    from sqlalchemy.ext.asyncio import AsyncSession

    from src.api.dependencies import get_db

    mock_db = AsyncMock(spec=AsyncSession)

    # count query
    count_result = MagicMock()
    count_result.scalar.return_value = 0
    # rows query
    rows_result = MagicMock()
    rows_scalars = MagicMock()
    rows_scalars.all.return_value = []
    rows_result.scalars.return_value = rows_scalars

    # Auth (require_authenticated) issues one user-lookup query before the
    # route's count + rows queries when an auth header is present.
    from unittest.mock import MagicMock as _MagicMock

    from tests.unit.api.conftest import _make_mock_user

    auth_result = _MagicMock()
    auth_result.scalar_one_or_none.return_value = _make_mock_user()

    mock_db.execute = AsyncMock(side_effect=[auth_result, count_result, rows_result])

    app.dependency_overrides[get_db] = lambda: mock_db

    try:
        resp = await client.get(_EVENTS_URL, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert "events" in body
    assert body["total_count"] == 0
    assert body["events"] == []
