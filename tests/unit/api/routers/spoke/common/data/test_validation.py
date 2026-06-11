"""Unit tests for data validation sub-resource routes (data-sub-router).

Routes under test:
  GET    /data/{urn}/attr/validation/conf
  PUT    /data/{urn}/attr/validation/conf
  PATCH  /data/{urn}/attr/validation/conf
  DELETE /data/{urn}/attr/validation/conf
  POST   /data/{urn}/attr/validation/result
  GET    /data/{urn}/attr/validation/result
  GET    /data/{urn}/event/validation

spec: API.md §Data Resource (validation rows).
spec: API.md §Authentication — all spoke/common routes require valid JWT.
spec: API_DESIGN_PRINCIPLE_en.md §HTTP method semantics.
spec: feature/VALIDATION.md §API Surface.
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from src.api.dependencies import get_validation_service
from src.api.main import app
from tests.unit.api.conftest import auth_headers

_BASE = "/api/v1/spoke/common/data"
_VALID_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,"
    "example_db.reviews.user_ratings_legacy,DEV)"
)
_VALID_URN_ENC = (
    _VALID_URN.replace("(", "%28").replace(")", "%29").replace(",", "%2C")
)
_CONF_URL = f"{_BASE}/{_VALID_URN_ENC}/attr/validation/conf"
_RESULT_URL = f"{_BASE}/{_VALID_URN_ENC}/attr/validation/result"
_EVENTS_URL = f"{_BASE}/{_VALID_URN_ENC}/event/validation"


@pytest.fixture
def mock_svc() -> AsyncMock:
    return AsyncMock()


@pytest.fixture(autouse=True)
def override_service(mock_svc: AsyncMock):
    app.dependency_overrides[get_validation_service] = lambda: mock_svc
    yield
    app.dependency_overrides.pop(get_validation_service, None)


# ── Auth gates: 401 without token ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_validation_conf_without_token_returns_401(client) -> None:
    """spec: API.md §Authentication — spoke/common routes require valid JWT."""
    resp = await client.get(_CONF_URL)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_put_validation_conf_without_token_returns_401(client) -> None:
    """spec: API.md §Authentication — all write routes require valid JWT."""
    resp = await client.put(
        _CONF_URL,
        json={"description": "null rate check", "variables": ["null_rate_rating_score"]},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_patch_validation_conf_without_token_returns_401(client) -> None:
    """spec: API.md §Authentication — all write routes require valid JWT."""
    resp = await client.patch(_CONF_URL, json={"description": "updated"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_delete_validation_conf_without_token_returns_401(client) -> None:
    """spec: API.md §Authentication — all write routes require valid JWT."""
    resp = await client.delete(_CONF_URL)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_post_validation_result_without_token_returns_401(client) -> None:
    """spec: API.md §Authentication — all write routes require valid JWT."""
    resp = await client.post(
        _RESULT_URL,
        json={
            "data_time": "2026-05-01T00:00:00Z",
            "score": 0.7,
            "variables": {"null_rate_rating_score": 0.3},
        },
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_validation_result_without_token_returns_401(client) -> None:
    """spec: API.md §Authentication — spoke/common routes require valid JWT."""
    resp = await client.get(_RESULT_URL)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_validation_events_without_token_returns_401(client) -> None:
    """spec: API.md §Authentication — spoke/common routes require valid JWT."""
    resp = await client.get(_EVENTS_URL)
    assert resp.status_code == 401


# ── Happy paths ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_validation_conf_200_when_present(client, mock_svc: AsyncMock) -> None:
    """GET /attr/validation/conf returns 200 when config present.

    spec: feature/VALIDATION.md §API Surface — GET returns existing configuration.
    """
    from src.backend.validation.service import ValidationConfigRecord

    mock_svc.get_config = AsyncMock(
        return_value=ValidationConfigRecord(
            dataset_urn=_VALID_URN,
            description="null rate check",
            variables=["null_rate_rating_score"],
            is_removed=False,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
    )

    resp = await client.get(_CONF_URL, headers=auth_headers())
    assert resp.status_code == 200
    assert resp.json()["dataset_urn"] == _VALID_URN


@pytest.mark.asyncio
async def test_get_validation_conf_404_when_absent(client, mock_svc: AsyncMock) -> None:
    """GET /attr/validation/conf returns 404 when not found.

    spec: feature/VALIDATION.md §API Surface — 404 if not found.
    """
    mock_svc.get_config = AsyncMock(return_value=None)

    resp = await client.get(_CONF_URL, headers=auth_headers())
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_put_validation_conf_201_on_create(client, mock_svc: AsyncMock) -> None:
    """PUT /attr/validation/conf returns 201 on first creation.

    spec: feature/VALIDATION.md §API Surface — PUT returns 201 on create.
    """
    from src.backend.validation.service import ValidationConfigRecord

    mock_svc.upsert_config = AsyncMock(
        return_value=(
            ValidationConfigRecord(
                dataset_urn=_VALID_URN,
                description="null rate check",
                variables=["null_rate_rating_score"],
                is_removed=False,
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            ),
            True,
        )
    )

    resp = await client.put(
        _CONF_URL,
        json={"description": "null rate check", "variables": ["null_rate_rating_score"]},
        headers=auth_headers(),
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_put_validation_conf_200_on_update(client, mock_svc: AsyncMock) -> None:
    """PUT /attr/validation/conf returns 200 on subsequent update.

    spec: feature/VALIDATION.md §API Surface — PUT is create-or-replace; 200 on update.
    """
    from src.backend.validation.service import ValidationConfigRecord

    mock_svc.upsert_config = AsyncMock(
        return_value=(
            ValidationConfigRecord(
                dataset_urn=_VALID_URN,
                description="updated check",
                variables=["null_rate_rating_score"],
                is_removed=False,
                created_at=datetime.now(tz=UTC),
                updated_at=datetime.now(tz=UTC),
            ),
            False,
        )
    )

    resp = await client.put(
        _CONF_URL,
        json={"description": "updated check", "variables": ["null_rate_rating_score"]},
        headers=auth_headers(),
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_patch_validation_conf_200(client, mock_svc: AsyncMock) -> None:
    """PATCH /attr/validation/conf returns 200 with merged record.

    spec: feature/VALIDATION.md §Rule Configuration — PATCH accepts partial body.
    """
    from src.backend.validation.service import ValidationConfigRecord

    mock_svc.patch_config = AsyncMock(
        return_value=ValidationConfigRecord(
            dataset_urn=_VALID_URN,
            description="patched description",
            variables=["null_rate_rating_score"],
            is_removed=False,
            created_at=datetime.now(tz=UTC),
            updated_at=datetime.now(tz=UTC),
        )
    )

    resp = await client.patch(
        _CONF_URL,
        json={"description": "patched description"},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    assert resp.json()["description"] == "patched description"


@pytest.mark.asyncio
async def test_delete_validation_conf_204(client, mock_svc: AsyncMock) -> None:
    """DELETE /attr/validation/conf returns 204 No Content.

    spec: feature/VALIDATION.md §API Surface — DELETE returns 204.
    """
    mock_svc.delete_config = AsyncMock(return_value=None)

    resp = await client.delete(_CONF_URL, headers=auth_headers())
    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_post_validation_result_201(client, mock_svc: AsyncMock) -> None:
    """POST /attr/validation/result returns 201 Created with the recorded row.

    spec: API.md §HTTP Status Codes — POST that creates a resource returns 201.
    spec: feature/VALIDATION.md §Validation Result — pipeline POSTs results.
    """
    from src.backend.validation.service import ValidationResultRecord

    mock_svc.record_result = AsyncMock(
        return_value=ValidationResultRecord(
            data_time=datetime(2026, 5, 1, tzinfo=UTC),
            score=0.7,
            variables={"null_rate_rating_score": 0.3},
        )
    )

    resp = await client.post(
        _RESULT_URL,
        json={
            "data_time": "2026-05-01T00:00:00Z",
            "score": 0.7,
            "variables": {"null_rate_rating_score": 0.3},
        },
        headers=auth_headers(),
    )
    assert resp.status_code == 201
    assert resp.json()["score"] == 0.7


@pytest.mark.asyncio
async def test_get_validation_result_limit_over_10000_returns_422(
    client, mock_svc: AsyncMock
) -> None:
    """GET /attr/validation/result rejects limit > 10000 with 422.

    spec: API.md §Data Resource — GET result limit capped at 10000 (le=10000).
    """
    resp = await client.get(
        _RESULT_URL,
        params={"limit": 10001},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_validation_result_200_with_results_key(client, mock_svc: AsyncMock) -> None:
    """GET /attr/validation/result returns 200 with 'results' key.

    spec: API.md §Standard Response Envelope — list responses use resource-named key.
    """
    mock_svc.get_results = AsyncMock(return_value=([], 0))

    resp = await client.get(_RESULT_URL, headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert "results" in body
    assert body["total_count"] == 0


@pytest.mark.asyncio
async def test_get_validation_events_200_with_events_key(client, mock_svc: AsyncMock) -> None:
    """GET /event/validation returns 200 with 'events' key.

    spec: API.md §Standard Response Envelope — list responses use resource-named key.
    """
    mock_svc.get_events = AsyncMock(return_value=([], 0))

    resp = await client.get(_EVENTS_URL, headers=auth_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert "events" in body
    assert body["total_count"] == 0
