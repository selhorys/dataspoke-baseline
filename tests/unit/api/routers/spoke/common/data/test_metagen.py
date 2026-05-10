"""Unit tests for data metagen sub-resource routes.

Routes under test:
  GET    /data/{urn}/attr/metagen/conf
  PUT    /data/{urn}/attr/metagen/conf
  PATCH  /data/{urn}/attr/metagen/conf
  DELETE /data/{urn}/attr/metagen/conf
  GET    /data/{urn}/attr/metagen/result
  PATCH  /data/{urn}/attr/metagen/result/{id}
  POST   /data/{urn}/method/metagen/run
  GET    /data/{urn}/event/metagen

spec: API.md §Common (/spoke/common) §Metadata Generation routes.
spec: API.md §Authentication — all spoke/common routes require valid JWT.
spec: API_DESIGN_PRINCIPLE_en.md §HTTP method semantics.
spec: feature/BACKEND.md §Metadata Generation Service.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.dependencies import get_metagen_service
from src.api.main import app
from src.shared.exceptions import EntityNotFoundError

from tests.unit.api.conftest import auth_headers

_BASE = "/api/v1/spoke/common/data"
_VALID_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.reviews.user_ratings,DEV)"
_VALID_URN_ENC = (
    _VALID_URN.replace("(", "%28").replace(")", "%29").replace(",", "%2C")
)
_CONF_URL = f"{_BASE}/{_VALID_URN_ENC}/attr/metagen/conf"
_RESULT_URL = f"{_BASE}/{_VALID_URN_ENC}/attr/metagen/result"
_RUN_URL = f"{_BASE}/{_VALID_URN_ENC}/method/metagen/run"
_EVENTS_URL = f"{_BASE}/{_VALID_URN_ENC}/event/metagen"


def _make_config_record() -> MagicMock:
    rec = MagicMock()
    rec.id = str(uuid.uuid4())
    rec.dataset_urn = _VALID_URN
    rec.targets = ["dataset.description"]
    rec.code_refs = None
    rec.is_enabled = True
    rec.schedule_tier = "daily"
    rec.status = "active"
    rec.owner = "test@example.com"
    rec.created_at = datetime.now(tz=UTC)
    rec.updated_at = datetime.now(tz=UTC)
    return rec


def _make_result_record() -> MagicMock:
    rec = MagicMock()
    rec.id = str(uuid.uuid4())
    rec.dataset_urn = _VALID_URN
    rec.proposals = {"dataset.description": "A test rating dataset."}
    rec.field_status = {"dataset.description": "pending"}
    rec.run_id = str(uuid.uuid4())
    rec.generated_at = datetime.now(tz=UTC)
    rec.last_reviewed_at = None
    return rec


@pytest.fixture
def mock_svc() -> AsyncMock:
    return AsyncMock()


@pytest.fixture(autouse=True)
def override_service(mock_svc: AsyncMock):
    app.dependency_overrides[get_metagen_service] = lambda: mock_svc
    yield
    app.dependency_overrides.pop(get_metagen_service, None)


# ── Auth gates: 401 without token ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_metagen_conf_without_token_returns_401(client) -> None:
    """spec: API.md §Authentication — spoke/common routes require valid JWT."""
    resp = await client.get(_CONF_URL)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_put_metagen_conf_without_token_returns_401(client) -> None:
    """spec: API.md §Authentication — all write routes require valid JWT."""
    resp = await client.put(
        _CONF_URL,
        json={"targets": ["dataset.description"], "is_enabled": True, "owner": "a@b.com"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_patch_metagen_conf_without_token_returns_401(client) -> None:
    """spec: API.md §Authentication — all write routes require valid JWT."""
    resp = await client.patch(_CONF_URL, json={"is_enabled": False})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_delete_metagen_conf_without_token_returns_401(client) -> None:
    """spec: API.md §Authentication — all write routes require valid JWT."""
    resp = await client.delete(_CONF_URL)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_metagen_results_without_token_returns_401(client) -> None:
    """spec: API.md §Authentication — spoke/common routes require valid JWT."""
    resp = await client.get(_RESULT_URL)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_patch_metagen_result_without_token_returns_401(client) -> None:
    """spec: API.md §Authentication — all write routes require valid JWT."""
    result_id = str(uuid.uuid4())
    resp = await client.patch(
        f"{_RESULT_URL}/{result_id}",
        json={"verdict": "approve"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_post_metagen_run_without_token_returns_401(client) -> None:
    """spec: API.md §Authentication — all write routes require valid JWT."""
    resp = await client.post(_RUN_URL, json={"dry_run": False})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_metagen_events_without_token_returns_401(client) -> None:
    """spec: API.md §Authentication — spoke/common routes require valid JWT."""
    resp = await client.get(_EVENTS_URL)
    assert resp.status_code == 401


# ── Happy paths ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_metagen_conf_200_when_present(client, mock_svc: AsyncMock) -> None:
    """GET /attr/metagen/conf returns 200 when config exists.

    spec: API.md §Data Resource — GET returns 200 when resource present.
    """
    mock_svc.get_config = AsyncMock(return_value=_make_config_record())

    resp = await client.get(_CONF_URL, headers=auth_headers(["de"]))
    assert resp.status_code == 200
    assert resp.json()["dataset_urn"] == _VALID_URN


@pytest.mark.asyncio
async def test_get_metagen_conf_404_when_absent(client, mock_svc: AsyncMock) -> None:
    """GET /attr/metagen/conf returns 404 when not configured.

    spec: API_DESIGN_PRINCIPLE_en.md §HTTP status codes — 404 for unknown resource.
    """
    mock_svc.get_config = AsyncMock(return_value=None)

    resp = await client.get(_CONF_URL, headers=auth_headers(["de"]))
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_put_metagen_conf_201_on_create(client, mock_svc: AsyncMock) -> None:
    """PUT /attr/metagen/conf returns 201 on first creation.

    spec: API_DESIGN_PRINCIPLE_en.md §HTTP method semantics — PUT is create-or-replace.
    """
    mock_svc.upsert_config = AsyncMock(return_value=(_make_config_record(), True))

    resp = await client.put(
        _CONF_URL,
        json={"targets": ["dataset.description"], "is_enabled": True, "owner": "a@b.com"},
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_put_metagen_conf_200_on_update(client, mock_svc: AsyncMock) -> None:
    """PUT /attr/metagen/conf returns 200 on subsequent update.

    spec: API_DESIGN_PRINCIPLE_en.md §HTTP method semantics — PUT returns 200 on update.
    """
    mock_svc.upsert_config = AsyncMock(return_value=(_make_config_record(), False))

    resp = await client.put(
        _CONF_URL,
        json={"targets": ["dataset.description"], "is_enabled": False, "owner": "a@b.com"},
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_patch_metagen_conf_200(client, mock_svc: AsyncMock) -> None:
    """PATCH /attr/metagen/conf returns 200 with merged record.

    spec: API_DESIGN_PRINCIPLE_en.md §HTTP method semantics — PATCH returns 200.
    """
    mock_svc.patch_config = AsyncMock(return_value=_make_config_record())

    resp = await client.patch(
        _CONF_URL,
        json={"is_enabled": False},
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_delete_metagen_conf_204(client, mock_svc: AsyncMock) -> None:
    """DELETE /attr/metagen/conf returns 204 No Content.

    spec: API_DESIGN_PRINCIPLE_en.md §HTTP method semantics — DELETE returns 204.
    """
    mock_svc.delete_config = AsyncMock(return_value=None)

    resp = await client.delete(_CONF_URL, headers=auth_headers(["de"]))
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_get_metagen_results_200_with_results_key(client, mock_svc: AsyncMock) -> None:
    """GET /attr/metagen/result returns 200 with 'results' key.

    spec: API.md §Standard Response Envelope — list responses use resource-named key.
    """
    mock_svc.list_results = AsyncMock(return_value=([], 0))

    resp = await client.get(_RESULT_URL, headers=auth_headers(["de"]))
    assert resp.status_code == 200
    body = resp.json()
    assert "results" in body
    assert body["total_count"] == 0


@pytest.mark.asyncio
async def test_patch_metagen_result_200_approve(client, mock_svc: AsyncMock) -> None:
    """PATCH /attr/metagen/result/{id} with approve returns 200.

    spec: feature/BACKEND.md §Metadata Generation Service §Approval flow.
    """
    record = _make_result_record()
    mock_svc.review_result = AsyncMock(return_value=record)

    resp = await client.patch(
        f"{_RESULT_URL}/{record.id}",
        json={"verdict": "approve"},
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_post_metagen_run_200(client, mock_svc: AsyncMock) -> None:
    """POST /method/metagen/run returns 200 with result.

    spec: feature/BACKEND.md §Metadata Generation Service — run returns MetagenResult.
    """
    record = _make_result_record()
    mock_svc.run = AsyncMock(return_value=record)

    resp = await client.post(
        _RUN_URL,
        json={"dry_run": False},
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "dataset_urn" in body


@pytest.mark.asyncio
async def test_get_metagen_events_200_with_events_key(client, mock_svc: AsyncMock) -> None:
    """GET /event/metagen returns 200 with 'events' key.

    spec: API.md §Standard Response Envelope — list responses use resource-named key.
    """
    mock_svc.get_events = AsyncMock(return_value=([], 0))

    resp = await client.get(_EVENTS_URL, headers=auth_headers(["de"]))
    assert resp.status_code == 200
    body = resp.json()
    assert "events" in body
    assert body["total_count"] == 0
