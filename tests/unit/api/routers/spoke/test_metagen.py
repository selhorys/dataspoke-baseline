"""Unit tests for /spoke/metagen/* routes (conf collection + uncovered + items + run).

Routes under test:
  GET    /spoke/metagen/conf
  POST   /spoke/metagen/conf
  GET    /spoke/metagen/conf/{conf_id}
  PUT    /spoke/metagen/conf/{conf_id}
  PATCH  /spoke/metagen/conf/{conf_id}
  DELETE /spoke/metagen/conf/{conf_id}
  POST   /spoke/metagen/conf/{conf_id}/method/run
  GET    /spoke/metagen/conf/{conf_id}/event
  GET    /spoke/metagen/uncovered
  GET    /spoke/metagen/event
  GET    /spoke/metagen/item
  GET    /spoke/metagen/item/{composite_id}

Spec traceability:
  spec/API.md §Metadata Generation (/spoke/metagen)
  spec/feature/BACKEND.md §Metadata Generation Service
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.dependencies import get_metagen_service
from src.api.main import app
from src.backend.metagen.service import (
    ItemDetailDTO,
    ItemSummaryDTO,
    MetagenConfDTO,
    RunResultDTO,
    UncoveredRowDTO,
)
from src.shared.exceptions import ConflictError, EntityNotFoundError
from tests.unit.api.conftest import auth_headers
from tests.unit.conftest import route_db_execute

_BASE = "/api/v1/spoke/metagen"

_VALID_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_CONF_ID = str(uuid.uuid4())


def _make_conf_dto(**overrides) -> MetagenConfDTO:
    defaults = dict(
        id=_CONF_ID,
        name="catalog-docs",
        is_enabled=False,
        schedule_tier=None,
        dataset_filter={},
        result_limit=3,
        overwrite_pending=True,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )
    defaults.update(overrides)
    return MetagenConfDTO(**defaults)


def _make_run_dto(status: str = "success", dry_run: bool = False, **over) -> RunResultDTO:
    base = dict(
        run_id=str(uuid.uuid4()),
        conf_id=_CONF_ID,
        status=status,
        dry_run=dry_run,
        unresolved_urns=[],
        counts={"items_considered": 0},
    )
    base.update(over)
    return RunResultDTO(**base)


def _make_item_summary_dto(
    dataset_urn: str = _VALID_URN,
    item_id: str = "dataset.description",
    kind: str = "dataset.description",
    field_path: str | None = None,
    candidate_count: int = 0,
    non_rejected_count: int | None = None,
    has_approved: bool = False,
) -> ItemSummaryDTO:
    return ItemSummaryDTO(
        dataset_urn=dataset_urn,
        item_id=item_id,
        kind=kind,
        field_path=field_path,
        candidate_count=candidate_count,
        # Default: non_rejected_count tracks candidate_count unless the caller
        # overrides (e.g. an item whose only candidates were rejected).
        non_rejected_count=(
            non_rejected_count if non_rejected_count is not None else candidate_count
        ),
        has_approved=has_approved,
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
@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", f"{_BASE}/conf"),
        ("POST", f"{_BASE}/conf"),
        ("POST", f"{_BASE}/conf/{_CONF_ID}/method/run"),
    ],
)
async def test_route_without_token_returns_401(client, method, url) -> None:
    """Every /metagen route rejects an unauthenticated request.

    The auth dependency runs before body validation, so a bodyless write is
    still rejected with 401 (not 422).

    Spec: API.md §Authentication — all /spoke routes require valid JWT.
    """
    resp = await client.request(method, url)
    assert resp.status_code == 401, (
        f"{method} {url} without a token must return 401, got {resp.status_code}"
    )


# ── GET /conf (list) ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_confs_returns_200_paginated_envelope(client, mock_svc: AsyncMock) -> None:
    """GET /metagen/conf returns 200 with a paginated envelope keyed by 'confs'.

    Spec: API.md §Metadata Generation — GET /metagen/conf lists confs (paginated).
    """
    mock_svc.list_confs = AsyncMock(return_value=([_make_conf_dto(is_enabled=True)], 1))

    resp = await client.get(f"{_BASE}/conf", headers=auth_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] == 1
    assert len(body["confs"]) == 1
    assert body["confs"][0]["id"] == _CONF_ID
    assert body["confs"][0]["name"] == "catalog-docs"


# ── POST /conf (create) ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_conf_returns_201_with_conf_body(client, mock_svc: AsyncMock) -> None:
    """POST /metagen/conf returns 201 with the created conf body.

    Spec: API.md §Metadata Generation — POST /metagen/conf → 201.
    """
    mock_svc.create_conf = AsyncMock(return_value=_make_conf_dto(name="orders-docs"))

    resp = await client.post(
        f"{_BASE}/conf",
        json={"name": "orders-docs", "is_enabled": False, "result_limit": 5},
        headers=auth_headers(),
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "orders-docs"
    assert "id" in body


@pytest.mark.asyncio
async def test_post_conf_duplicate_name_returns_409_conf_exists(
    client, mock_svc: AsyncMock
) -> None:
    """POST /metagen/conf with a duplicate name returns 409 METAGEN_CONF_EXISTS.

    Spec: API.md §Metadata Generation — name unique (409 METAGEN_CONF_EXISTS on collision).
    """
    mock_svc.create_conf = AsyncMock(
        side_effect=ConflictError("METAGEN_CONF_EXISTS", "conf 'orders-docs' already exists")
    )

    resp = await client.post(
        f"{_BASE}/conf",
        json={"name": "orders-docs"},
        headers=auth_headers(),
    )

    assert resp.status_code == 409
    assert resp.json()["error_code"] == "METAGEN_CONF_EXISTS"


@pytest.mark.asyncio
async def test_post_conf_missing_name_returns_422(client, mock_svc: AsyncMock) -> None:
    """POST /metagen/conf without `name` returns 422 (schema validation).

    Spec: API.md §Metadata Generation — `name` is required.
    """
    resp = await client.post(f"{_BASE}/conf", json={"is_enabled": False}, headers=auth_headers())
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_conf_invalid_result_limit_returns_422(client, mock_svc: AsyncMock) -> None:
    """POST /metagen/conf with result_limit=0 returns 422.

    Spec: API.md §Payload caps — conf result_limit ∈ [1, 20].
    """
    resp = await client.post(
        f"{_BASE}/conf",
        json={"name": "c", "result_limit": 0},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_conf_malformed_dataset_urn_returns_422_invalid_dataset_urn(
    client, mock_svc: AsyncMock
) -> None:
    """POST /metagen/conf with a malformed dataset URN returns 422 INVALID_DATASET_URN.

    Spec: API.md §Error Catalogue — 422 INVALID_DATASET_URN; validated at the schema layer.
    """
    resp = await client.post(
        f"{_BASE}/conf",
        json={"name": "c", "dataset_filter": {"dataset_urns": ["not-a-urn"]}},
        headers=auth_headers(),
    )
    assert resp.status_code == 422
    assert resp.json().get("error_code") == "INVALID_DATASET_URN"


@pytest.mark.asyncio
async def test_post_conf_too_many_dataset_urns_returns_422(client, mock_svc: AsyncMock) -> None:
    """POST /metagen/conf with > 1000 dataset_urns returns 422.

    Spec: API.md §Payload caps — dataset_filter.dataset_urns ≤ 1,000 entries.
    """
    too_many = [
        f"urn:li:dataset:(urn:li:dataPlatform:postgres,db.t{i},PROD)" for i in range(1001)
    ]
    resp = await client.post(
        f"{_BASE}/conf",
        json={"name": "c", "dataset_filter": {"dataset_urns": too_many}},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


# ── GET /conf/{conf_id} ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_conf_returns_200_with_conf_body(client, mock_svc: AsyncMock) -> None:
    """GET /metagen/conf/{conf_id} returns 200 with the conf body.

    Spec: API.md §Metadata Generation — GET /metagen/conf/{conf_id}.
    """
    mock_svc.get_conf = AsyncMock(return_value=_make_conf_dto(is_enabled=True))

    resp = await client.get(f"{_BASE}/conf/{_CONF_ID}", headers=auth_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == _CONF_ID
    assert body["is_enabled"] is True


@pytest.mark.asyncio
async def test_get_conf_not_found_returns_404(client, mock_svc: AsyncMock) -> None:
    """GET /metagen/conf/{conf_id} returns 404 METAGEN_CONF_NOT_FOUND when absent.

    Spec: API.md §Metadata Generation — 404 METAGEN_CONF_NOT_FOUND.
    """
    mock_svc.get_conf = AsyncMock(side_effect=EntityNotFoundError("metagen_conf", _CONF_ID))

    resp = await client.get(f"{_BASE}/conf/{_CONF_ID}", headers=auth_headers())

    assert resp.status_code == 404


# ── PUT /conf/{conf_id} ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_conf_returns_200_with_conf_body(client, mock_svc: AsyncMock) -> None:
    """PUT /metagen/conf/{conf_id} returns 200 with the replaced conf.

    Spec: API.md §Metadata Generation — PUT /metagen/conf/{conf_id} replaces a conf.
    """
    mock_svc.put_conf = AsyncMock(return_value=_make_conf_dto(is_enabled=True, result_limit=10))

    resp = await client.put(
        f"{_BASE}/conf/{_CONF_ID}",
        json={"name": "catalog-docs", "is_enabled": True, "result_limit": 10},
        headers=auth_headers(),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["result_limit"] == 10


@pytest.mark.asyncio
async def test_put_conf_not_found_returns_404(client, mock_svc: AsyncMock) -> None:
    """PUT /metagen/conf/{conf_id} returns 404 when the conf does not exist.

    Spec: API.md §Metadata Generation — 404 METAGEN_CONF_NOT_FOUND.
    """
    mock_svc.put_conf = AsyncMock(side_effect=EntityNotFoundError("metagen_conf", _CONF_ID))

    resp = await client.put(
        f"{_BASE}/conf/{_CONF_ID}",
        json={"name": "catalog-docs", "is_enabled": True},
        headers=auth_headers(),
    )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_put_conf_duplicate_name_returns_409(client, mock_svc: AsyncMock) -> None:
    """PUT /metagen/conf/{conf_id} with a colliding name returns 409 METAGEN_CONF_EXISTS.

    Spec: API.md §Metadata Generation — name unique (409 METAGEN_CONF_EXISTS).
    """
    mock_svc.put_conf = AsyncMock(
        side_effect=ConflictError("METAGEN_CONF_EXISTS", "already exists")
    )

    resp = await client.put(
        f"{_BASE}/conf/{_CONF_ID}",
        json={"name": "taken", "is_enabled": True},
        headers=auth_headers(),
    )

    assert resp.status_code == 409
    assert resp.json()["error_code"] == "METAGEN_CONF_EXISTS"


# ── PATCH /conf/{conf_id} ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_conf_returns_200(client, mock_svc: AsyncMock) -> None:
    """PATCH /metagen/conf/{conf_id} returns 200 with the updated conf.

    Spec: API.md §Metadata Generation — PATCH /metagen/conf/{conf_id}.
    """
    mock_svc.patch_conf = AsyncMock(return_value=_make_conf_dto(is_enabled=True))

    resp = await client.patch(
        f"{_BASE}/conf/{_CONF_ID}",
        json={"is_enabled": True},
        headers=auth_headers(),
    )

    assert resp.status_code == 200
    assert resp.json()["is_enabled"] is True


@pytest.mark.asyncio
async def test_patch_conf_malformed_dataset_urn_returns_422(client, mock_svc: AsyncMock) -> None:
    """PATCH /metagen/conf/{conf_id} with a malformed dataset URN returns 422 INVALID_DATASET_URN.

    Spec: API.md §Error Catalogue — 422 INVALID_DATASET_URN validated on PATCH.
    """
    resp = await client.patch(
        f"{_BASE}/conf/{_CONF_ID}",
        json={"dataset_filter": {"dataset_urns": ["not-a-urn"]}},
        headers=auth_headers(),
    )
    assert resp.status_code == 422
    assert resp.json().get("error_code") == "INVALID_DATASET_URN"


# ── DELETE /conf/{conf_id} ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_conf_returns_204(client, mock_svc: AsyncMock) -> None:
    """DELETE /metagen/conf/{conf_id} returns 204 No Content.

    Spec: API_DESIGN_PRINCIPLE_en.md §HTTP method semantics — DELETE returns 204.
    """
    mock_svc.delete_conf = AsyncMock(return_value=None)

    resp = await client.delete(f"{_BASE}/conf/{_CONF_ID}", headers=auth_headers())

    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_conf_not_found_returns_404(client, mock_svc: AsyncMock) -> None:
    """DELETE /metagen/conf/{conf_id} returns 404 when the conf does not exist.

    Spec: API.md §Metadata Generation — 404 METAGEN_CONF_NOT_FOUND.
    """
    mock_svc.delete_conf = AsyncMock(side_effect=EntityNotFoundError("metagen_conf", _CONF_ID))

    resp = await client.delete(f"{_BASE}/conf/{_CONF_ID}", headers=auth_headers())

    assert resp.status_code == 404


# ── POST /conf/{conf_id}/method/run ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_run_returns_200_with_run_response(client, mock_svc: AsyncMock) -> None:
    """POST /metagen/conf/{conf_id}/method/run returns 200 with a conf-scoped run response.

    Spec: API.md §Metadata Generation — POST /metagen/conf/{conf_id}/method/run.
    """
    mock_svc.run = AsyncMock(return_value=_make_run_dto())

    resp = await client.post(f"{_BASE}/conf/{_CONF_ID}/method/run", json={}, headers=auth_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert "run_id" in body
    assert body["conf_id"] == _CONF_ID
    assert body["status"] == "success"
    assert isinstance(body["unresolved_urns"], list)
    assert body["dry_run"] is False


@pytest.mark.asyncio
async def test_post_run_dry_run_query_param_forwarded(client, mock_svc: AsyncMock) -> None:
    """POST /metagen/conf/{conf_id}/method/run?dry_run=true forwards dry_run to the service.

    Spec: API.md §Metadata Generation — dry_run is a ?dry_run=true query parameter.
    """
    mock_svc.run = AsyncMock(
        return_value=_make_run_dto(
            dry_run=True, counts={"items_considered": 2, "candidates_proposed": 5}
        )
    )

    resp = await client.post(
        f"{_BASE}/conf/{_CONF_ID}/method/run?dry_run=true",
        headers=auth_headers(),
    )

    assert resp.status_code == 200
    assert resp.json()["dry_run"] is True
    assert mock_svc.run.call_args.kwargs.get("dry_run") is True


@pytest.mark.asyncio
async def test_post_run_forwards_conf_id_and_dataset_urns(client, mock_svc: AsyncMock) -> None:
    """POST run forwards the path conf_id and body dataset_urns to the service.

    Spec: API.md §Metadata Generation — run is per-conf; optional dataset_urns narrows scope.
    """
    mock_svc.run = AsyncMock(return_value=_make_run_dto())

    resp = await client.post(
        f"{_BASE}/conf/{_CONF_ID}/method/run",
        json={"dataset_urns": [_VALID_URN]},
        headers=auth_headers(),
    )

    assert resp.status_code == 200
    call = mock_svc.run.call_args
    assert call.args[0] == _CONF_ID or call.kwargs.get("conf_id") == _CONF_ID
    assert call.kwargs.get("dataset_urns") == [_VALID_URN]


@pytest.mark.asyncio
async def test_post_run_returns_409_when_running(client, mock_svc: AsyncMock) -> None:
    """POST run returns 409 METAGEN_RUNNING when this conf is already running.

    Spec: API.md §Metadata Generation — 409 METAGEN_RUNNING per-conf lock.
    """
    mock_svc.run = AsyncMock(side_effect=ConflictError("METAGEN_RUNNING", "already running"))

    resp = await client.post(f"{_BASE}/conf/{_CONF_ID}/method/run", json={}, headers=auth_headers())

    assert resp.status_code == 409
    assert resp.json()["error_code"] == "METAGEN_RUNNING"


@pytest.mark.asyncio
async def test_post_run_returns_409_when_disabled(client, mock_svc: AsyncMock) -> None:
    """POST run returns 409 METAGEN_DISABLED when the conf is disabled and not dry-run.

    Spec: API.md §Metadata Generation — 409 METAGEN_DISABLED.
    """
    mock_svc.run = AsyncMock(side_effect=ConflictError("METAGEN_DISABLED", "disabled"))

    resp = await client.post(f"{_BASE}/conf/{_CONF_ID}/method/run", json={}, headers=auth_headers())

    assert resp.status_code == 409
    assert resp.json()["error_code"] == "METAGEN_DISABLED"


@pytest.mark.asyncio
async def test_post_run_missing_conf_returns_404(client, mock_svc: AsyncMock) -> None:
    """POST run returns 404 when the conf does not exist.

    Spec: API.md §Metadata Generation — 404 METAGEN_CONF_NOT_FOUND.
    """
    mock_svc.run = AsyncMock(side_effect=EntityNotFoundError("metagen_conf", _CONF_ID))

    resp = await client.post(f"{_BASE}/conf/{_CONF_ID}/method/run", json={}, headers=auth_headers())

    assert resp.status_code == 404


# ── GET /uncovered ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_uncovered_returns_200_paginated_envelope(client, mock_svc: AsyncMock) -> None:
    """GET /metagen/uncovered returns 200 with rows carrying a reason and a paginated envelope.

    Spec: API.md §Metadata Generation — GET /metagen/uncovered (paginated; each row carries reason).
    """
    mock_svc.list_uncovered = AsyncMock(
        return_value=([UncoveredRowDTO(dataset_urn=_VALID_URN, reason="no_conf_match")], 1)
    )

    resp = await client.get(f"{_BASE}/uncovered", headers=auth_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] == 1
    assert body["datasets"][0]["dataset_urn"] == _VALID_URN
    assert body["datasets"][0]["reason"] == "no_conf_match"


@pytest.mark.asyncio
async def test_get_uncovered_default_include_disallowed_false(client, mock_svc: AsyncMock) -> None:
    """GET /metagen/uncovered defaults include_disallowed=false.

    Spec: API.md §Metadata Generation — include_disallowed defaults to false.
    """
    mock_svc.list_uncovered = AsyncMock(return_value=([], 0))

    resp = await client.get(f"{_BASE}/uncovered", headers=auth_headers())

    assert resp.status_code == 200
    assert mock_svc.list_uncovered.call_args.kwargs.get("include_disallowed") is False


@pytest.mark.asyncio
async def test_get_uncovered_include_disallowed_true_forwarded(client, mock_svc: AsyncMock) -> None:
    """GET /metagen/uncovered?include_disallowed=true forwards the flag and surfaces
    boundary_blocked rows.

    Spec: API.md §Metadata Generation — include_disallowed=true also includes
    boundary-blocked datasets (reason=boundary_blocked).
    """
    mock_svc.list_uncovered = AsyncMock(
        return_value=([UncoveredRowDTO(dataset_urn=_VALID_URN, reason="boundary_blocked")], 1)
    )

    resp = await client.get(f"{_BASE}/uncovered?include_disallowed=true", headers=auth_headers())

    assert resp.status_code == 200
    assert mock_svc.list_uncovered.call_args.kwargs.get("include_disallowed") is True
    assert resp.json()["datasets"][0]["reason"] == "boundary_blocked"


# ── GET /item ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_items_returns_200_with_item_list_envelope(client, mock_svc: AsyncMock) -> None:
    """GET /metagen/item returns 200 with items and total_count.

    Spec: API.md §Metadata Generation — GET /metagen/item paginated list.
    """
    mock_svc.list_items = AsyncMock(return_value=([_make_item_summary_dto()], 1))

    resp = await client.get(f"{_BASE}/item", headers=auth_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] == 1
    assert len(body["items"]) == 1


@pytest.mark.asyncio
async def test_get_items_envelope_carries_no_dataset_level_candidate_count(
    client, mock_svc: AsyncMock
) -> None:
    """The cross-dataset index envelope carries the content key + pagination only.

    The dataset-level ``candidate_count`` aggregate belongs to the *per-dataset*
    sibling route, which scopes it to one dataset. The cross-dataset index spans
    many datasets, so no such aggregate is defined for it and none is returned.

    The absence assertion is meaningful only because non-zero per-row counts are
    injected first: both rows must still report their own ``candidate_count``
    (proving the item path is live and the per-row field survived), while the
    envelope exposes nothing beyond ``items`` + the pagination keys. Without the
    injection the check would pass just as happily against an empty page or a
    handler returning some unrelated shape.

    Spec: API.md §Route Catalogue → Metadata Generation (`/spoke/metagen`),
    `GET /spoke/metagen/item` — "Each row carries `dataset_urn`, `item_id`, `kind`,
    `field_path`, `status`, `candidate_count`, `created_at`, `composite_id`"; the
    row inventory names no envelope-level aggregate.
    Spec: API.md §Route Catalogue → Data Resource (`/spoke/common/data`),
    `GET /spoke/common/data/{dataset_urn}/attr/metagen/item` — "The response
    envelope also carries a dataset-level `candidate_count` … distinct from
    `total_count` (the item count)", documented for that route alone.
    Spec: API.md §Standard Response Envelope — "All collection responses include a
    content key named after the resource + pagination metadata".
    Spec: TESTING.md §Assertion Discipline — "Absence assertions require injection."
    """
    mock_svc.list_items = AsyncMock(
        return_value=(
            [
                _make_item_summary_dto(
                    item_id="dataset.description",
                    kind="dataset.description",
                    candidate_count=4,
                ),
                _make_item_summary_dto(
                    item_id="column.isbn.description",
                    kind="column.description",
                    field_path="isbn",
                    candidate_count=7,
                ),
            ],
            2,
        )
    )

    resp = await client.get(f"{_BASE}/item", headers=auth_headers())

    assert resp.status_code == 200
    body = resp.json()
    # Backstop for the absence assertion below: the injected per-row aggregates are
    # non-zero and reach the response, so the envelope check is made about a live
    # payload rather than an empty one.
    assert [row["candidate_count"] for row in body["items"]] == [4, 7], (
        f"Per-row candidate_count must survive on the cross-dataset index; got {body['items']}"
    )
    # The row inventory quoted above, asserted rather than merely cited — and it is what
    # makes the injected kind / field_path load-bearing rather than dead setup.
    assert set(body["items"][1]) == {
        "dataset_urn",
        "item_id",
        "kind",
        "field_path",
        "status",
        "candidate_count",
        "created_at",
        "composite_id",
    }, f"Row must carry exactly the fields spec/API.md lists; got {sorted(body['items'][1])}"
    assert body["items"][1]["kind"] == "column.description"
    assert body["items"][1]["field_path"] == "isbn"
    assert "candidate_count" not in body, (
        "The cross-dataset item index must not carry an envelope-level candidate_count "
        "— that dataset-scoped aggregate is defined only on the per-dataset item route, "
        f"and a cross-dataset handler cannot populate it meaningfully; got {sorted(body)}"
    )
    assert set(body) == {"items", "offset", "limit", "total_count", "resp_time"}, (
        f"Envelope must be the content key plus pagination metadata; got {sorted(body)}"
    )


@pytest.mark.asyncio
async def test_get_items_forwards_conf_id_filter(client, mock_svc: AsyncMock) -> None:
    """GET /metagen/item?conf_id=... forwards the conf_id filter to the service.

    Spec: API.md §Metadata Generation — item list filterable by conf_id.
    """
    mock_svc.list_items = AsyncMock(return_value=([], 0))

    resp = await client.get(f"{_BASE}/item?conf_id={_CONF_ID}", headers=auth_headers())

    assert resp.status_code == 200
    assert mock_svc.list_items.call_args.kwargs.get("conf_id") == _CONF_ID


@pytest.mark.asyncio
async def test_get_items_forwards_kind_status_filters(client, mock_svc: AsyncMock) -> None:
    """GET /metagen/item?kind=...&status=... forwards kind and status filters.

    Spec: API.md §Metadata Generation — item list filterable by kind and status.
    """
    mock_svc.list_items = AsyncMock(return_value=([], 0))

    resp = await client.get(
        f"{_BASE}/item?kind=dataset.description&status=llm_approved",
        headers=auth_headers(),
    )

    assert resp.status_code == 200
    kw = mock_svc.list_items.call_args.kwargs
    assert kw.get("kind") == "dataset.description"
    assert kw.get("status") == "llm_approved"


@pytest.mark.asyncio
async def test_get_items_invalid_kind_returns_422(client, mock_svc: AsyncMock) -> None:
    """GET /metagen/item?kind=invalid returns 422 (enum validation).

    Spec: API.md §Metadata Generation — kind query param is a Literal type.
    """
    resp = await client.get(f"{_BASE}/item?kind=invalid.kind", headers=auth_headers())
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_items_invalid_status_returns_422(client, mock_svc: AsyncMock) -> None:
    """GET /metagen/item?status=invalid returns 422 (enum validation).

    Spec: API.md §Metadata Generation — status query param is a Literal type.
    """
    resp = await client.get(f"{_BASE}/item?status=nope", headers=auth_headers())
    assert resp.status_code == 422


# ── GET /item/{composite_id} ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_item_by_composite_id_returns_200(client, mock_svc: AsyncMock) -> None:
    """GET /metagen/item/{composite_id} returns 200 with item detail.

    Spec: API.md §Metadata Generation — composite_id = {dataset_urn}::{item_id}.
    """
    mock_svc.get_item = AsyncMock(return_value=_make_item_detail_dto())

    composite_id = f"{_VALID_URN}::dataset.description"
    resp = await client.get(f"{_BASE}/item/{composite_id}", headers=auth_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert "dataset_urn" in body
    assert "candidates" in body


@pytest.mark.asyncio
async def test_get_item_by_composite_id_without_separator_returns_422(
    client, mock_svc: AsyncMock
) -> None:
    """GET /metagen/item/{id} without '::' separator returns 422 with an error_code.

    Spec: API.md §Metadata Generation — composite_id must contain '::' separator.
    """
    resp = await client.get(f"{_BASE}/item/some-id-without-separator", headers=auth_headers())
    assert resp.status_code == 422
    assert "error_code" in resp.json()


@pytest.mark.asyncio
async def test_get_item_by_composite_id_not_found_returns_404(client, mock_svc: AsyncMock) -> None:
    """GET /metagen/item/{composite_id} returns 404 when the item does not exist.

    Spec: API.md §Metadata Generation — 404 when item not found.
    """
    mock_svc.get_item = AsyncMock(side_effect=EntityNotFoundError("metagen_item", "absent::item"))

    composite_id = f"{_VALID_URN}::nonexistent.item"
    resp = await client.get(f"{_BASE}/item/{composite_id}", headers=auth_headers())

    assert resp.status_code == 404


# ── GET /event and GET /conf/{conf_id}/event (DB-direct routes) ───────────────


async def _run_event_route(client, url: str) -> tuple[int, dict]:
    """Drive a DB-direct event route with a mocked db session.

    The event routes query the DB directly (no service method). The auth
    middleware issues one user-lookup query before the route's count + rows
    queries.
    """
    from src.api.dependencies import get_db
    from tests.unit.api.conftest import _make_mock_user

    count_m = MagicMock()
    count_m.scalar.return_value = 0
    rows_m = MagicMock()
    rows_m.scalars.return_value.all.return_value = []
    auth_m = MagicMock()
    auth_m.scalar_one_or_none.return_value = _make_mock_user()

    mock_db_session = AsyncMock()
    # Route by SQL: auth user-lookup (users), the events count(), then the events rows.
    route_db_execute(
        mock_db_session,
        [("users", auth_m), ("count(", count_m)],
        default=rows_m,
    )

    app.dependency_overrides[get_db] = lambda: mock_db_session
    try:
        resp = await client.get(url, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)
    return resp.status_code, resp.json()


@pytest.mark.asyncio
async def test_get_events_returns_200_with_events_envelope(client, mock_svc: AsyncMock) -> None:
    """GET /metagen/event returns 200 with an events envelope (cross-conf union).

    Spec: API.md §Metadata Generation — GET /metagen/event returns the cross-conf
    union of generation-run events.
    """
    status_code, body = await _run_event_route(client, f"{_BASE}/event")
    assert status_code == 200
    assert "events" in body
    assert body["total_count"] == 0


@pytest.mark.asyncio
async def test_get_conf_event_returns_200_with_events_envelope(
    client, mock_svc: AsyncMock
) -> None:
    """GET /metagen/conf/{conf_id}/event returns 200 with an events envelope.

    Spec: API.md §Metadata Generation — GET /metagen/conf/{conf_id}/event is the
    per-conf generation-run event history.
    """
    status_code, body = await _run_event_route(client, f"{_BASE}/conf/{_CONF_ID}/event")
    assert status_code == 200
    assert "events" in body
    assert body["total_count"] == 0
