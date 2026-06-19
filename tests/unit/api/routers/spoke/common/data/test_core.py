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


# ── event_major_type → prefix mapping (router layer) ──────────────────────────


@pytest.mark.asyncio
async def test_get_data_events_major_type_maps_to_prefix(
    client, mock_svc: AsyncMock
) -> None:
    """The repeatable ``event_major_type`` query param maps to the service's
    ``event_type_prefixes`` set (VALIDATION → 'VALIDATION.', METAGEN → 'METAGEN.').

    The service-layer prefix filtering itself is covered in
    tests/unit/backend/dataset/test_service.py; this asserts only the router's
    public-filter-value → prefix mapping (core.py:_MAJOR_TYPE_PREFIX).

    spec: spec/feature/BACKEND.md §Dataset service / Event Catalogue — the unified
      timeline's major-type filter; spec/feature/FRONTEND_BASIC.md §Per-dataset page.
    """
    mock_svc.get_events = AsyncMock(return_value=([], 0))

    resp = await client.get(
        f"{_BASE}/{_VALID_URN_ENC}/event"
        "?event_major_type=VALIDATION&event_major_type=METAGEN",
        headers=auth_headers(),
    )
    assert resp.status_code == 200

    mock_svc.get_events.assert_awaited_once()
    _, kwargs = mock_svc.get_events.await_args
    assert kwargs["event_type_prefixes"] == {"VALIDATION.", "METAGEN."}


@pytest.mark.asyncio
async def test_get_data_events_omitted_major_type_passes_none(
    client, mock_svc: AsyncMock
) -> None:
    """Omitting ``event_major_type`` passes ``event_type_prefixes=None`` (all
    major types), so the service returns the full unified timeline.

    spec: spec/feature/FRONTEND_BASIC.md §Per-dataset page — omitted filter = all.
    """
    mock_svc.get_events = AsyncMock(return_value=([], 0))

    resp = await client.get(
        f"{_BASE}/{_VALID_URN_ENC}/event", headers=auth_headers()
    )
    assert resp.status_code == 200

    mock_svc.get_events.assert_awaited_once()
    _, kwargs = mock_svc.get_events.await_args
    assert kwargs["event_type_prefixes"] is None


@pytest.mark.asyncio
async def test_get_data_events_unknown_major_type_ignored(
    client, mock_svc: AsyncMock
) -> None:
    """An unrecognized ``event_major_type`` value is dropped from the prefix set;
    a lone unknown value collapses to ``None`` (no filter).

    spec: spec/feature/FRONTEND_BASIC.md §Per-dataset page — only the three known
      major types map to prefixes.
    """
    mock_svc.get_events = AsyncMock(return_value=([], 0))

    resp = await client.get(
        f"{_BASE}/{_VALID_URN_ENC}/event"
        "?event_major_type=VALIDATION&event_major_type=BOGUS",
        headers=auth_headers(),
    )
    assert resp.status_code == 200

    mock_svc.get_events.assert_awaited_once()
    _, kwargs = mock_svc.get_events.await_args
    assert kwargs["event_type_prefixes"] == {"VALIDATION."}


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
