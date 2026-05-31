"""Unit tests for per-dataset ingestion sub-resource routes.

Routes under test (per-source model — read-only per-dataset surface):
  GET /data/{urn}/attr/ingestion         — reverse-lookup (source that covers this dataset)
  GET /data/{urn}/event/ingestion        — ingestion event history for this dataset

The old per-dataset CRUD/run routes are gone in the per-source model.
Source CRUD lives under /spoke/ingestion/sources/{id}.

Spec: API.md §Ingestion — 'GET /data/{urn}/attr/ingestion … Reverse-lookup (read-only)'
Spec: API.md §Data Resource (ingestion rows) — auth gate + HTTP status codes.
Spec: feature/BACKEND.md §Ingestion Service.
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
_REVERSE_LOOKUP_URL = f"{_BASE}/{_VALID_URN_ENC}/attr/ingestion"
_EVENTS_URL = f"{_BASE}/{_VALID_URN_ENC}/event/ingestion"


@pytest.fixture
def mock_svc() -> AsyncMock:
    return AsyncMock()


@pytest.fixture(autouse=True)
def override_service(mock_svc: AsyncMock):
    app.dependency_overrides[get_ingestion_service] = lambda: mock_svc
    yield
    app.dependency_overrides.pop(get_ingestion_service, None)


# ── Auth gate: 401 without token ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_ingestion_reverse_lookup_without_token_returns_401(client) -> None:
    """GET /attr/ingestion without token returns 401.

    Spec: API.md §Authentication — spoke/common routes require valid JWT.
    """
    resp = await client.get(_REVERSE_LOOKUP_URL)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_ingestion_events_without_token_returns_401(client) -> None:
    """GET /event/ingestion without token returns 401.

    Spec: API.md §Authentication — all routes require valid JWT.
    """
    resp = await client.get(_EVENTS_URL)
    assert resp.status_code == 401


# ── Reverse-lookup: unmapped dataset returns nulls ────────────────────────────


@pytest.mark.asyncio
async def test_get_ingestion_reverse_lookup_unmapped_returns_null_source(
    client, mock_svc: AsyncMock
) -> None:
    """GET /attr/ingestion for a dataset with no owning source returns null source fields.

    Spec: API.md §Ingestion — 'Returns the owning source for a dataset, or null if unmapped'.
    """
    mock_svc.reverse_lookup = AsyncMock(return_value=None)
    mock_svc.get_events_for_source = AsyncMock(return_value=([], 0))

    headers = auth_headers()
    resp = await client.get(_REVERSE_LOOKUP_URL, headers=headers)
    assert resp.status_code == 200

    body = resp.json()
    assert body["source_id"] is None
    assert body["mode"] is None
    assert body["name"] is None
    assert body["latest_run"] is None


@pytest.mark.asyncio
async def test_get_ingestion_reverse_lookup_mapped_returns_source_info(
    client, mock_svc: AsyncMock
) -> None:
    """GET /attr/ingestion for a mapped dataset returns the owning source's id, mode, name.

    Spec: API.md §Ingestion — reverse-lookup returns source_id, mode, name.
    """
    source_id = str(uuid.uuid4())
    now = datetime.now(tz=UTC)
    source_record = MagicMock()
    source_record.id = source_id
    source_record.mode = "ACTIVE_CUSTOM_MANAGED"
    source_record.name = "imazon catalog pg"
    source_record.platform = "postgres"
    source_record.schedule = "0 0 * * *"
    source_record.schedule_tier = "daily"
    source_record.recipe = {"source": {"type": "postgres", "config": {}}}
    source_record.datahub_source_urn = None
    source_record.status = "OK"
    source_record.created_at = now
    source_record.updated_at = now

    mock_svc.reverse_lookup = AsyncMock(return_value=source_record)
    mock_svc.get_events_for_source = AsyncMock(return_value=([], 0))

    headers = auth_headers()
    resp = await client.get(_REVERSE_LOOKUP_URL, headers=headers)
    assert resp.status_code == 200

    body = resp.json()
    assert body["source_id"] == source_id
    assert body["mode"] == "ACTIVE_CUSTOM_MANAGED"
    assert body["name"] == "imazon catalog pg"


@pytest.mark.asyncio
async def test_get_ingestion_reverse_lookup_has_no_schedule_tier(
    client, mock_svc: AsyncMock
) -> None:
    """Reverse-lookup response does NOT expose schedule_tier.

    Spec: BACKEND_SCHEMA.md — schedule_tier is internal, never in the API.
    """
    source_id = str(uuid.uuid4())
    now = datetime.now(tz=UTC)
    source_record = MagicMock()
    source_record.id = source_id
    source_record.mode = "ACTIVE_CUSTOM_MANAGED"
    source_record.name = "test source"
    source_record.platform = "postgres"
    source_record.schedule = "0 0 * * *"
    source_record.schedule_tier = "daily"  # internal — must not appear in response
    source_record.recipe = {"source": {"type": "postgres", "config": {}}}
    source_record.datahub_source_urn = None
    source_record.status = "OK"
    source_record.created_at = now
    source_record.updated_at = now

    mock_svc.reverse_lookup = AsyncMock(return_value=source_record)
    mock_svc.get_events_for_source = AsyncMock(return_value=([], 0))

    headers = auth_headers()
    resp = await client.get(_REVERSE_LOOKUP_URL, headers=headers)
    assert resp.status_code == 200

    body = resp.json()
    assert "schedule_tier" not in body, (
        f"schedule_tier must not appear in the reverse-lookup response. "
        f"Spec: BACKEND_SCHEMA.md — schedule_tier is internal. "
        f"Body keys: {list(body.keys())}"
    )


# ── Event history ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_ingestion_events_returns_paginated_envelope(
    client, mock_svc: AsyncMock
) -> None:
    """GET /event/ingestion returns a paginated event envelope.

    Spec: API.md §Standard Envelope — events[], total_count, offset, limit.
    """
    mock_svc.get_events_for_source = AsyncMock(return_value=([], 0))
    mock_svc.reverse_lookup = AsyncMock(return_value=None)

    headers = auth_headers()
    resp = await client.get(_EVENTS_URL, headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "events" in body
    assert "total_count" in body
