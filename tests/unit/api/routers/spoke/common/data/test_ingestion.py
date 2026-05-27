"""Unit tests for data ingestion sub-resource routes.

Routes under test:
  GET    /data/{urn}/attr/ingestion/conf
  PUT    /data/{urn}/attr/ingestion/conf
  PATCH  /data/{urn}/attr/ingestion/conf
  DELETE /data/{urn}/attr/ingestion/conf
  POST   /data/{urn}/method/ingestion/run
  GET    /data/{urn}/event/ingestion

spec: API.md §Data Resource (ingestion rows) — auth gate + HTTP status codes.
spec: API_DESIGN_PRINCIPLE_en.md §HTTP method semantics.
spec: feature/BACKEND.md §Ingestion Service.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

import pytest

from src.api.dependencies import get_ingestion_service
from src.api.main import app
from src.shared.exceptions import EntityNotFoundError

from tests.unit.api.conftest import auth_headers

_BASE = "/api/v1/spoke/common/data"

_VALID_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_VALID_URN_ENC = (
    _VALID_URN.replace("(", "%28").replace(")", "%29").replace(",", "%2C")
)
_CONF_URL = f"{_BASE}/{_VALID_URN_ENC}/attr/ingestion/conf"
_RUN_URL = f"{_BASE}/{_VALID_URN_ENC}/method/ingestion/run"
_EVENTS_URL = f"{_BASE}/{_VALID_URN_ENC}/event/ingestion"


class _FakeConfig:
    """Minimal stand-in for IngestionConfig ORM row for use with model_validate."""

    def __init__(self):
        self.id = str(uuid.uuid4())
        self.dataset_urn = _VALID_URN
        self.mode = "active-custom"
        self.platform = "postgres"
        self.locator = {"host": "db.example.com", "port": 5432}
        self.identifier = {"database": "example_db", "schema_name": "catalog", "table": "title_master"}
        self.auth = None
        self.is_enabled = True
        self.schedule_tier = "daily"
        self.workflow_dag_id = "ingestion-active-daily"
        self.status = "OK"
        self.created_at = datetime.now(tz=UTC)
        self.updated_at = datetime.now(tz=UTC)


def _make_config_record() -> _FakeConfig:
    return _FakeConfig()


@pytest.fixture
def mock_svc() -> AsyncMock:
    return AsyncMock()


@pytest.fixture(autouse=True)
def override_service(mock_svc: AsyncMock):
    app.dependency_overrides[get_ingestion_service] = lambda: mock_svc
    yield
    app.dependency_overrides.pop(get_ingestion_service, None)


# ── Auth gates: 401 without token ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_ingestion_conf_without_token_returns_401(client) -> None:
    """GET /attr/ingestion/conf without token returns 401.

    spec: API.md §Authentication — spoke/common routes require valid JWT.
    """
    resp = await client.get(_CONF_URL)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_put_ingestion_conf_without_token_returns_401(client) -> None:
    """PUT /attr/ingestion/conf without token returns 401.

    spec: API.md §Authentication — all write routes require valid JWT.
    """
    resp = await client.put(
        _CONF_URL,
        json={
            "mode": "active-custom",
            "platform": "postgres",
            "locator": "postgresql://host:5432/db",
            "identifier": "example_db.catalog.title_master",
            "is_enabled": True,
        },
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_patch_ingestion_conf_without_token_returns_401(client) -> None:
    """PATCH /attr/ingestion/conf without token returns 401.

    spec: API.md §Authentication — all write routes require valid JWT.
    """
    resp = await client.patch(_CONF_URL, json={"is_enabled": False})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_delete_ingestion_conf_without_token_returns_401(client) -> None:
    """DELETE /attr/ingestion/conf without token returns 401.

    spec: API.md §Authentication — all write routes require valid JWT.
    """
    resp = await client.delete(_CONF_URL)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_post_ingestion_run_without_token_returns_401(client) -> None:
    """POST /method/ingestion/run without token returns 401.

    spec: API.md §Authentication — all write routes require valid JWT.
    """
    resp = await client.post(_RUN_URL, json={"dry_run": False})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_ingestion_events_without_token_returns_401(client) -> None:
    """GET /event/ingestion without token returns 401.

    spec: API.md §Authentication — spoke/common routes require valid JWT.
    """
    resp = await client.get(_EVENTS_URL)
    assert resp.status_code == 401


# ── Happy paths ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_ingestion_conf_200_when_present(client, mock_svc: AsyncMock) -> None:
    """GET /attr/ingestion/conf returns 200 when config exists.

    spec: API.md §Data Resource — GET returns 200 when resource present.
    """
    mock_svc.get_config = AsyncMock(return_value=_make_config_record())

    resp = await client.get(_CONF_URL, headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["dataset_urn"] == _VALID_URN


@pytest.mark.asyncio
async def test_get_ingestion_conf_404_when_absent(client, mock_svc: AsyncMock) -> None:
    """GET /attr/ingestion/conf returns 404 when config does not exist.

    spec: API_DESIGN_PRINCIPLE_en.md §HTTP status codes — 404 for unknown resource.
    """
    mock_svc.get_config = AsyncMock(return_value=None)

    resp = await client.get(_CONF_URL, headers=auth_headers())
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_put_ingestion_conf_201_on_create(client, mock_svc: AsyncMock) -> None:
    """PUT /attr/ingestion/conf returns 201 on first creation.

    spec: API_DESIGN_PRINCIPLE_en.md §HTTP method semantics — PUT returns 201 on create.
    """
    # Patch _vault_or_verify to bypass k8s secret ops (not a secret_ref path here)
    mock_svc.upsert_config = AsyncMock(return_value=(_make_config_record(), True))

    resp = await client.put(
        _CONF_URL,
        json={
            "mode": "passive",
            "platform": "kafka",
            "identifier": {"topic": "imazon.orders.events", "cluster": "prod"},
            "is_enabled": False,
        },
        headers=auth_headers(),
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_put_ingestion_conf_200_on_update(client, mock_svc: AsyncMock) -> None:
    """PUT /attr/ingestion/conf returns 200 on subsequent update.

    spec: API_DESIGN_PRINCIPLE_en.md §HTTP method semantics — PUT returns 200 on update.
    """
    mock_svc.upsert_config = AsyncMock(return_value=(_make_config_record(), False))

    resp = await client.put(
        _CONF_URL,
        json={
            "mode": "passive",
            "platform": "kafka",
            "identifier": {"topic": "imazon.orders.events", "cluster": "prod"},
            "is_enabled": False,
        },
        headers=auth_headers(),
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_patch_ingestion_conf_200(client, mock_svc: AsyncMock) -> None:
    """PATCH /attr/ingestion/conf returns 200 with merged record.

    spec: API_DESIGN_PRINCIPLE_en.md §HTTP method semantics — PATCH returns 200.
    """
    mock_svc.patch_config = AsyncMock(return_value=_make_config_record())

    resp = await client.patch(
        _CONF_URL,
        json={"is_enabled": False},
        headers=auth_headers(),
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_delete_ingestion_conf_204(client, mock_svc: AsyncMock) -> None:
    """DELETE /attr/ingestion/conf returns 204 No Content.

    spec: API_DESIGN_PRINCIPLE_en.md §HTTP method semantics — DELETE returns 204.
    """
    mock_svc.delete_config = AsyncMock(return_value=None)

    resp = await client.delete(_CONF_URL, headers=auth_headers())
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_post_ingestion_run_200(client, mock_svc: AsyncMock) -> None:
    """POST /method/ingestion/run returns 200 with run_id and status.

    spec: feature/BACKEND.md §Ingestion Service — run returns RunResult.
    """
    run_result = MagicMock()
    run_result.run_id = str(uuid.uuid4())
    run_result.status = "success"
    run_result.detail = {}
    mock_svc.run = AsyncMock(return_value=run_result)

    resp = await client.post(
        _RUN_URL,
        json={"dry_run": False},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "run_id" in body
    assert "status" in body


@pytest.mark.asyncio
async def test_get_ingestion_events_200_returns_events_key(client, mock_svc: AsyncMock) -> None:
    """GET /event/ingestion returns 200 with 'events' list key.

    spec: API.md §Standard Response Envelope — list responses use resource-named key.
    """
    mock_svc.get_events = AsyncMock(return_value=([], 0))

    resp = await client.get(_EVENTS_URL, headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert "events" in body
    assert body["total_count"] == 0
