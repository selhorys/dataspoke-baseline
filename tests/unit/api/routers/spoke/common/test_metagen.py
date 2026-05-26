"""Unit tests for /spoke/common/metagen/* routes (global singleton conf + items + run).

Routes under test:
  GET    /spoke/common/metagen/attr/conf
  PUT    /spoke/common/metagen/attr/conf
  PATCH  /spoke/common/metagen/attr/conf
  DELETE /spoke/common/metagen/attr/conf
  POST   /spoke/common/metagen/method/run
  GET    /spoke/common/metagen/event
  GET    /spoke/common/metagen/item
  GET    /spoke/common/metagen/item/{composite_id}

Spec traceability:
  spec/API.md §Common (/spoke/common) §Metadata Generation
  spec/API.md §Authentication & Authorization §Group-to-Route Access Control
  spec/feature/BACKEND.md §Metadata Generation Service
"""

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.dependencies import get_metagen_service
from src.api.main import app
from src.backend.metagen.service import ItemDetailDTO, ItemSummaryDTO, MetagenGlobalConfDTO, RunResultDTO
from src.shared.exceptions import ConflictError, EntityNotFoundError

from tests.unit.api.conftest import auth_headers

_BASE = "/api/v1/spoke/common/metagen"
_VALID_GROUPS = ("de", "da", "dg", "admin")

_VALID_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"


def _make_conf_dto(**overrides) -> MetagenGlobalConfDTO:
    defaults = dict(
        is_enabled=False,
        schedule_tier=None,
        dataset_filter={},
        result_limit=3,
        overwrite_pending=True,
        updated_at=datetime.now(tz=UTC),
    )
    defaults.update(overrides)
    return MetagenGlobalConfDTO(**defaults)


def _make_run_dto(status: str = "success") -> RunResultDTO:
    return RunResultDTO(
        run_id=str(uuid.uuid4()),
        status=status,
        dry_run=False,
        unresolved_urns=[],
        counts={"items_considered": 0},
    )


def _make_item_summary_dto(
    dataset_urn: str = _VALID_URN,
    item_id: str = "dataset.description",
) -> ItemSummaryDTO:
    return ItemSummaryDTO(
        dataset_urn=dataset_urn,
        item_id=item_id,
        kind="dataset.description",
        field_path=None,
        candidate_count=0,
        has_approved=False,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )


def _make_item_detail_dto() -> ItemDetailDTO:
    return ItemDetailDTO(
        dataset_urn=_VALID_URN,
        item_id="dataset.description",
        kind="dataset.description",
        field_path=None,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
        candidates=[],
    )


@pytest.fixture
def mock_svc() -> AsyncMock:
    return AsyncMock()


@pytest.fixture(autouse=True)
def override_service(mock_svc: AsyncMock):
    app.dependency_overrides[get_metagen_service] = lambda: mock_svc
    yield
    app.dependency_overrides.pop(get_metagen_service, None)


# ── 401 without token ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_conf_without_token_returns_401(client) -> None:
    """GET /metagen/attr/conf without token returns 401.

    Spec: API.md §Authentication — all spoke/common routes require valid JWT.
    """
    resp = await client.get(f"{_BASE}/attr/conf")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_put_conf_without_token_returns_401(client) -> None:
    """PUT /metagen/attr/conf without token returns 401.

    Spec: API.md §Authentication — write routes require valid JWT.
    """
    resp = await client.put(f"{_BASE}/attr/conf", json={"is_enabled": False})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_post_run_without_token_returns_401(client) -> None:
    """POST /metagen/method/run without token returns 401.

    Spec: API.md §Authentication — write routes require valid JWT.
    """
    resp = await client.post(f"{_BASE}/method/run", json={})
    assert resp.status_code == 401


# ── Auth — valid group tokens ─────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("group", list(_VALID_GROUPS))
async def test_get_conf_with_valid_group_returns_200(
    client, mock_svc: AsyncMock, group: str
) -> None:
    """GET /metagen/attr/conf with any valid group token returns 200.

    Spec: API.md §Group-to-Route Access Control — /spoke/common/… accepts de/da/dg/admin.
    """
    mock_svc.get_global_conf = AsyncMock(return_value=_make_conf_dto())
    resp = await client.get(f"{_BASE}/attr/conf", headers=auth_headers([group]))
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_conf_without_token_returns_401_not_403(
    client, mock_svc: AsyncMock
) -> None:
    """GET /metagen/attr/conf without any token returns 401 (not 403).

    The old group-based access check is replaced by role-based auth. Any
    authenticated user may GET /spoke/common/* routes; unauthenticated
    requests get 401.

    Spec: API.md §Authentication — /spoke/common/* requires a valid token.
    """
    mock_svc.get_global_conf = AsyncMock(return_value=_make_conf_dto())
    resp = await client.get(f"{_BASE}/attr/conf")  # no auth header
    assert resp.status_code == 401


# ── GET /attr/conf ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_conf_returns_200_with_conf_body(client, mock_svc: AsyncMock) -> None:
    """GET /metagen/attr/conf returns 200 with conf body.

    Spec: API.md §Metadata Generation — GET /metagen/attr/conf.
    """
    mock_svc.get_global_conf = AsyncMock(return_value=_make_conf_dto(is_enabled=True))

    resp = await client.get(f"{_BASE}/attr/conf", headers=auth_headers(["de"]))

    assert resp.status_code == 200
    body = resp.json()
    assert "is_enabled" in body
    assert body["is_enabled"] is True


@pytest.mark.asyncio
async def test_get_conf_returns_null_when_absent(client, mock_svc: AsyncMock) -> None:
    """GET /metagen/attr/conf returns null body when not yet configured.

    Spec: API.md §Metadata Generation — GET conf returns null when not configured.
    """
    mock_svc.get_global_conf = AsyncMock(return_value=None)

    resp = await client.get(f"{_BASE}/attr/conf", headers=auth_headers(["de"]))

    assert resp.status_code == 200
    assert resp.json() is None


# ── PUT /attr/conf ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_conf_returns_200_with_conf_body(client, mock_svc: AsyncMock) -> None:
    """PUT /metagen/attr/conf returns 200 with updated conf body.

    Spec: API.md §Metadata Generation — PUT /metagen/attr/conf.
    """
    mock_svc.put_global_conf = AsyncMock(return_value=_make_conf_dto(is_enabled=True, result_limit=5))

    resp = await client.put(
        f"{_BASE}/attr/conf",
        json={"is_enabled": True, "result_limit": 5},
        headers=auth_headers(["de"]),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_enabled"] is True
    assert body["result_limit"] == 5


@pytest.mark.asyncio
async def test_put_conf_with_invalid_result_limit_returns_422(client, mock_svc: AsyncMock) -> None:
    """PUT /metagen/attr/conf with result_limit=0 returns 422.

    Spec: spec/feature/BACKEND_SCHEMA.md — result_limit ∈ [1, 20].
    """
    resp = await client.put(
        f"{_BASE}/attr/conf",
        json={"is_enabled": False, "result_limit": 0},
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_conf_with_too_many_dataset_filter_urns_returns_422(
    client, mock_svc: AsyncMock
) -> None:
    """PUT /metagen/attr/conf with dataset_filter.dataset_urns > 1000 returns 422.

    Spec: API.md §Payload caps — dataset_filter.dataset_urns ≤ 1,000 entries.
    """
    too_many = [f"urn:li:dataset:(urn:li:dataPlatform:postgres,db.t{i},PROD)" for i in range(1001)]
    resp = await client.put(
        f"{_BASE}/attr/conf",
        json={"is_enabled": False, "dataset_filter": {"dataset_urns": too_many}},
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_put_conf_with_malformed_dataset_urn_returns_422_invalid_dataset_urn(
    client, mock_svc: AsyncMock
) -> None:
    """PUT /metagen/attr/conf with a malformed dataset URN returns 422 INVALID_DATASET_URN.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service — validates
    dataset_filter.dataset_urns; malformed URN raises INVALID_DATASET_URN.
    Spec: API.md §Metadata Generation — error code INVALID_DATASET_URN.
    """
    from src.shared.exceptions import InvalidDatasetUrnError

    mock_svc.put_global_conf = AsyncMock(
        side_effect=InvalidDatasetUrnError("not-a-urn")
    )

    resp = await client.put(
        f"{_BASE}/attr/conf",
        json={"is_enabled": False, "dataset_filter": {"dataset_urns": ["not-a-urn"]}},
        headers=auth_headers(["de"]),
    )

    assert resp.status_code == 422, (
        "Malformed dataset URN in dataset_filter must return 422. "
        "spec: API.md §Metadata Generation — INVALID_DATASET_URN"
    )
    body = resp.json()
    assert body.get("error_code") == "INVALID_DATASET_URN", (
        "Response must carry error_code='INVALID_DATASET_URN'. "
        "spec: API.md §Metadata Generation error codes"
    )


@pytest.mark.asyncio
async def test_patch_conf_with_malformed_dataset_urn_returns_422_invalid_dataset_urn(
    client, mock_svc: AsyncMock
) -> None:
    """PATCH /metagen/attr/conf with a malformed dataset URN returns 422 INVALID_DATASET_URN.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service — validates
    dataset_filter.dataset_urns on PATCH too; malformed URN raises INVALID_DATASET_URN.
    Spec: API.md §Metadata Generation — error code INVALID_DATASET_URN.
    """
    from src.shared.exceptions import InvalidDatasetUrnError

    mock_svc.patch_global_conf = AsyncMock(
        side_effect=InvalidDatasetUrnError("not-a-urn")
    )

    resp = await client.patch(
        f"{_BASE}/attr/conf",
        json={"dataset_filter": {"dataset_urns": ["not-a-urn"]}},
        headers=auth_headers(["de"]),
    )

    assert resp.status_code == 422, (
        "Malformed dataset URN in PATCH dataset_filter must return 422. "
        "spec: API.md §Metadata Generation — INVALID_DATASET_URN"
    )
    body = resp.json()
    assert body.get("error_code") == "INVALID_DATASET_URN", (
        "Response must carry error_code='INVALID_DATASET_URN'. "
        "spec: API.md §Metadata Generation error codes"
    )


# ── PATCH /attr/conf ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_conf_returns_200(client, mock_svc: AsyncMock) -> None:
    """PATCH /metagen/attr/conf returns 200 with updated conf body.

    Spec: API.md §Metadata Generation — PATCH /metagen/attr/conf.
    """
    mock_svc.patch_global_conf = AsyncMock(return_value=_make_conf_dto(is_enabled=True))

    resp = await client.patch(
        f"{_BASE}/attr/conf",
        json={"is_enabled": True},
        headers=auth_headers(["de"]),
    )

    assert resp.status_code == 200
    assert resp.json()["is_enabled"] is True


# ── DELETE /attr/conf ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_conf_returns_204(client, mock_svc: AsyncMock) -> None:
    """DELETE /metagen/attr/conf returns 204 No Content.

    Spec: API_DESIGN_PRINCIPLE_en.md §HTTP method semantics — DELETE returns 204.
    """
    mock_svc.delete_global_conf = AsyncMock(return_value=None)

    resp = await client.delete(f"{_BASE}/attr/conf", headers=auth_headers(["de"]))

    assert resp.status_code == 204


# ── POST /method/run ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_run_returns_200_with_run_response(client, mock_svc: AsyncMock) -> None:
    """POST /metagen/method/run returns 200 with run response body.

    Spec: API.md §Metadata Generation — POST /metagen/method/run.
    """
    mock_svc.run = AsyncMock(return_value=_make_run_dto(status="success"))

    resp = await client.post(
        f"{_BASE}/method/run",
        json={},
        headers=auth_headers(["de"]),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "run_id" in body
    assert body["status"] == "success"
    assert "unresolved_urns" in body
    assert isinstance(body["unresolved_urns"], list)
    assert "dry_run" in body


@pytest.mark.asyncio
async def test_post_run_returns_200_with_dry_run_true(client, mock_svc: AsyncMock) -> None:
    """POST /metagen/method/run with dry_run=true returns 200.

    Spec: API.md §Metadata Generation — dry_run flag in request.
    """
    mock_svc.run = AsyncMock(return_value=RunResultDTO(
        run_id=str(uuid.uuid4()),
        status="success",
        dry_run=True,
        unresolved_urns=[],
        counts={"items_considered": 2, "candidates_proposed": 5},
    ))

    resp = await client.post(
        f"{_BASE}/method/run",
        json={"dry_run": True},
        headers=auth_headers(["de"]),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["dry_run"] is True


@pytest.mark.asyncio
async def test_post_run_returns_409_when_metagen_running(client, mock_svc: AsyncMock) -> None:
    """POST /metagen/method/run returns 409 METAGEN_RUNNING when already running.

    Spec: API.md §Metadata Generation — 409 METAGEN_RUNNING when concurrent run.
    """
    mock_svc.run = AsyncMock(
        side_effect=ConflictError("METAGEN_RUNNING", "Metagen inference is already running")
    )

    resp = await client.post(
        f"{_BASE}/method/run",
        json={},
        headers=auth_headers(["de"]),
    )

    assert resp.status_code == 409
    body = resp.json()
    assert body["error_code"] == "METAGEN_RUNNING", (
        "Response error_code must be METAGEN_RUNNING. "
        "spec: API.md §Metadata Generation — 409 error codes"
    )


@pytest.mark.asyncio
async def test_post_run_returns_409_when_metagen_disabled(client, mock_svc: AsyncMock) -> None:
    """POST /metagen/method/run returns 409 METAGEN_DISABLED when conf is disabled.

    Spec: API.md §Metadata Generation — 409 METAGEN_DISABLED when is_enabled=false.
    """
    mock_svc.run = AsyncMock(
        side_effect=ConflictError("METAGEN_DISABLED", "Metagen is disabled")
    )

    resp = await client.post(
        f"{_BASE}/method/run",
        json={},
        headers=auth_headers(["de"]),
    )

    assert resp.status_code == 409
    body = resp.json()
    assert body["error_code"] == "METAGEN_DISABLED", (
        "Response error_code must be METAGEN_DISABLED. "
        "spec: API.md §Metadata Generation — 409 error codes"
    )


# ── GET /item ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_items_returns_200_with_item_list_envelope(
    client, mock_svc: AsyncMock
) -> None:
    """GET /metagen/item returns 200 with items and total_count.

    Spec: API.md §Metadata Generation — GET /metagen/item paginated list.
    """
    mock_svc.list_items = AsyncMock(return_value=([_make_item_summary_dto()], 1))

    resp = await client.get(f"{_BASE}/item", headers=auth_headers(["de"]))

    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert "total_count" in body
    assert body["total_count"] == 1
    assert len(body["items"]) == 1


@pytest.mark.asyncio
async def test_get_items_with_kind_filter(client, mock_svc: AsyncMock) -> None:
    """GET /metagen/item?kind=dataset.description passes filter to service.

    Spec: API.md §Metadata Generation — item list supports kind filter.
    """
    mock_svc.list_items = AsyncMock(return_value=([], 0))

    resp = await client.get(
        f"{_BASE}/item?kind=dataset.description",
        headers=auth_headers(["de"]),
    )

    assert resp.status_code == 200
    # Verify the kind filter was forwarded to the service
    mock_svc.list_items.assert_called_once()
    call_kwargs = mock_svc.list_items.call_args
    assert call_kwargs.kwargs.get("kind") == "dataset.description" or (
        call_kwargs.args and "dataset.description" in call_kwargs.args
    )


@pytest.mark.asyncio
async def test_get_items_with_invalid_kind_returns_422(client, mock_svc: AsyncMock) -> None:
    """GET /metagen/item?kind=invalid returns 422 (enum validation fails).

    Spec: API.md §Metadata Generation — kind query param is a Literal type.
    """
    resp = await client.get(
        f"{_BASE}/item?kind=invalid.kind",
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 422


# ── GET /item/{composite_id} ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_item_by_composite_id_returns_200(client, mock_svc: AsyncMock) -> None:
    """GET /metagen/item/{composite_id} returns 200 with item detail.

    Spec: API.md §Metadata Generation — composite_id = {dataset_urn}::{item_id}.
    """
    mock_svc.get_item = AsyncMock(return_value=_make_item_detail_dto())

    composite_id = f"{_VALID_URN}::dataset.description"

    resp = await client.get(
        f"{_BASE}/item/{composite_id}",
        headers=auth_headers(["de"]),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "dataset_urn" in body
    assert "candidates" in body


@pytest.mark.asyncio
async def test_get_item_by_composite_id_without_separator_returns_422(
    client, mock_svc: AsyncMock
) -> None:
    """GET /metagen/item/{id} without '::' separator returns 422 with error_code in envelope.

    Spec: API.md §Metadata Generation — composite_id must contain '::' separator.
    Spec: API.md §Standard Error Envelope — 422 responses carry error_code field.
    """
    resp = await client.get(
        f"{_BASE}/item/some-id-without-separator",
        headers=auth_headers(["de"]),
    )
    # Router raises PreconditionFailedError → mapped to 422
    assert resp.status_code == 422
    body = resp.json()
    assert "error_code" in body, (
        "422 response must carry an error_code field per project envelope. "
        "spec: API.md §Standard Error Envelope"
    )


@pytest.mark.asyncio
async def test_get_item_by_composite_id_not_found_returns_404(
    client, mock_svc: AsyncMock
) -> None:
    """GET /metagen/item/{composite_id} returns 404 when item does not exist.

    Spec: API.md §Metadata Generation — 404 when item not found.
    """
    mock_svc.get_item = AsyncMock(
        side_effect=EntityNotFoundError("metagen_item", "absent::item")
    )

    composite_id = f"{_VALID_URN}::nonexistent.item"

    resp = await client.get(
        f"{_BASE}/item/{composite_id}",
        headers=auth_headers(["de"]),
    )

    assert resp.status_code == 404


# ── GET /event ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_events_returns_200_with_events_envelope(
    client, mock_svc: AsyncMock, db
) -> None:
    """GET /metagen/event returns 200 with events envelope.

    Spec: API.md §Metadata Generation — GET /metagen/event returns paginated events.
    """
    # The event endpoint hits DB directly, so we stub via app db dep
    from src.api.dependencies import get_db

    count_m = MagicMock()
    count_m.scalar.return_value = 0
    rows_m = MagicMock()
    rows_m.scalars.return_value.all.return_value = []

    # Auth (require_authenticated) issues a user-lookup query first.
    from tests.unit.api.conftest import _make_mock_user
    auth_m = MagicMock()
    auth_m.scalar_one_or_none.return_value = _make_mock_user()

    mock_db_session = AsyncMock()
    mock_db_session.execute = AsyncMock(side_effect=[auth_m, count_m, rows_m])

    app.dependency_overrides[get_db] = lambda: mock_db_session

    try:
        resp = await client.get(f"{_BASE}/event", headers=auth_headers(["de"]))
    finally:
        app.dependency_overrides.pop(get_db, None)

    assert resp.status_code == 200
    body = resp.json()
    assert "events" in body
    assert "total_count" in body


# ── dataset_filter origin — HTTP-level (router + schema layer) ────────────────


@pytest.mark.asyncio
async def test_put_conf_with_origin_and_tags_returns_200(
    client, mock_svc: AsyncMock
) -> None:
    """PUT /metagen/attr/conf with dataset_filter={"origin": "DEV", "tags": [...]} returns 200.

    The schema layer accepts the four-dimension filter; the service layer is called
    with the validated dict. This exercises the unified dataset_filter shape at the
    HTTP layer for UC4.

    Spec: spec/API.md §UC4 Metadata Generation — dataset_filter unified four-dimension shape.
    """
    mock_svc.put_global_conf = AsyncMock(
        return_value=_make_conf_dto(
            dataset_filter={"origin": "DEV", "tags": ["urn:li:tag:area:fulfillment"]}
        )
    )

    resp = await client.put(
        f"{_BASE}/attr/conf",
        json={
            "is_enabled": False,
            "dataset_filter": {"origin": "DEV", "tags": ["urn:li:tag:area:fulfillment"]},
        },
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 200, (
        f"PUT with origin+tags dataset_filter must return 200; "
        f"got {resp.status_code}: {resp.text}. "
        "spec: API.md §UC4 — dataset_filter unified four-dimension shape"
    )
