"""Unit tests for data core routes.

Routes under test:
  GET /spoke/common/data/{urn}         — get_data
  GET /spoke/common/data/{urn}/attr    — get_data_attr
  GET /spoke/common/data/{urn}/event   — get_data_events

spec: API.md §Authentication — all spoke/common routes require valid JWT.
spec: API.md §Data Resource (core: GET dataset, GET attr, GET events)
spec: API_DESIGN_PRINCIPLE_en.md §HTTP status codes — 404 on unknown resource.
"""

from unittest.mock import AsyncMock

import pytest

from src.api.dependencies import get_dataset_service
from src.api.main import app
from src.shared.exceptions import EntityNotFoundError

from tests.unit.api.conftest import auth_headers

_BASE = "/api/v1/spoke/common/data"

_VALID_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.orders.raw_events,DEV)"
_VALID_URN_ENC = (
    _VALID_URN.replace("(", "%28").replace(")", "%29").replace(",", "%2C")
)


@pytest.fixture
def mock_svc() -> AsyncMock:
    return AsyncMock()


@pytest.fixture(autouse=True)
def override_service(mock_svc: AsyncMock):
    app.dependency_overrides[get_dataset_service] = lambda: mock_svc
    yield
    app.dependency_overrides.pop(get_dataset_service, None)


# ── Auth: missing token → 401 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_data_without_token_returns_401(client) -> None:
    """GET /data/{urn} without JWT returns 401.

    spec: API.md §Authentication — spoke/common routes require a valid token.
    """
    resp = await client.get(f"{_BASE}/{_VALID_URN_ENC}")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_data_attr_without_token_returns_401(client) -> None:
    """GET /data/{urn}/attr without JWT returns 401.

    spec: API.md §Authentication — spoke/common routes require a valid token.
    """
    resp = await client.get(f"{_BASE}/{_VALID_URN_ENC}/attr")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_data_events_without_token_returns_401(client) -> None:
    """GET /data/{urn}/event without JWT returns 401.

    spec: API.md §Authentication — spoke/common routes require a valid token.
    """
    resp = await client.get(f"{_BASE}/{_VALID_URN_ENC}/event")
    assert resp.status_code == 401


# ── Happy paths ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_data_returns_200_with_urn_and_name(client, mock_svc: AsyncMock) -> None:
    """GET /data/{urn} returns 200 with urn and name in body.

    spec: API.md §Data Resource — GET dataset returns DatasetResponse.
    """
    from src.shared.models.dataset import DatasetSummary

    mock_svc.get_summary = AsyncMock(
        return_value=DatasetSummary(
            urn=_VALID_URN,
            name="raw_events",
            platform="postgres",
            description="Order event stream",
            owners=[],
            tags=[],
        )
    )

    resp = await client.get(f"{_BASE}/{_VALID_URN_ENC}", headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["urn"] == _VALID_URN
    assert body["name"] == "raw_events"
    assert body["platform"] == "postgres"


@pytest.mark.asyncio
async def test_get_data_attr_returns_200_with_column_count(client, mock_svc: AsyncMock) -> None:
    """GET /data/{urn}/attr returns 200 with column_count in body.

    spec: API.md §Data Resource — GET attr returns DatasetAttributesResponse.
    """
    from src.shared.models.dataset import DatasetAttributes

    mock_svc.get_attributes = AsyncMock(
        return_value=DatasetAttributes(
            urn=_VALID_URN,
            column_count=5,
            fields=["id", "event_type", "payload", "created_at", "updated_at"],
            owners=[],
            tags=[],
            description=None,
            quality_score=None,
        )
    )

    resp = await client.get(f"{_BASE}/{_VALID_URN_ENC}/attr", headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["urn"] == _VALID_URN
    assert body["column_count"] == 5


@pytest.mark.asyncio
async def test_get_data_events_returns_200_with_events_key(client, mock_svc: AsyncMock) -> None:
    """GET /data/{urn}/event returns 200 with resource-named 'events' key.

    spec: API.md §Standard Response Envelope — list responses use the resource-named key.
    """
    mock_svc.get_events = AsyncMock(return_value=([], 0))

    resp = await client.get(f"{_BASE}/{_VALID_URN_ENC}/event", headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert "events" in body
    assert "total_count" in body
    assert body["total_count"] == 0


# ── 404 on unknown URN ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_data_unknown_urn_returns_404(client, mock_svc: AsyncMock) -> None:
    """GET /data/{unknown_urn} returns 404 when dataset not in DataHub.

    spec: API_DESIGN_PRINCIPLE_en.md §HTTP status codes — 404 for unknown resource.
    """
    mock_svc.get_summary = AsyncMock(
        side_effect=EntityNotFoundError("dataset", _VALID_URN)
    )

    resp = await client.get(f"{_BASE}/{_VALID_URN_ENC}", headers=auth_headers())
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_data_attr_unknown_urn_returns_404(client, mock_svc: AsyncMock) -> None:
    """GET /data/{unknown_urn}/attr returns 404.

    spec: API_DESIGN_PRINCIPLE_en.md §HTTP status codes — 404 for unknown resource.
    """
    mock_svc.get_attributes = AsyncMock(
        side_effect=EntityNotFoundError("dataset", _VALID_URN)
    )

    resp = await client.get(f"{_BASE}/{_VALID_URN_ENC}/attr", headers=auth_headers())
    assert resp.status_code == 404
