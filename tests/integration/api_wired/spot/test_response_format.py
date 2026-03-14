"""End-to-end response format smoke tests.

Hits each major router via HTTP and verifies response format compliance
(snake_case fields, pagination envelope, resp_time, error envelope).

Prerequisites:
- All dev-env port-forwards active (DataHub, PostgreSQL, Redis, Qdrant)
- Dummy data ingested via conftest.py
"""

import re
from unittest.mock import patch

import pytest
import pytest_asyncio

from tests.integration.conftest import (
    _auth_headers,
    _datahub_gms_url,
    _resolve_datahub_token,
    override_app,
)

_HEADERS = _auth_headers()
_CAMEL_RE = re.compile(r"[a-z][A-Z]")


@pytest_asyncio.fixture
async def http_client(datahub_client, redis_client, async_session):
    """HTTP client with real DI providers pointing to dev-env infra."""
    # Also patch hub proxy settings for DataHub pass-through tests
    hub_token = _resolve_datahub_token()
    with patch("src.api.routers.hub.settings") as mock_settings:
        mock_settings.datahub_gms_url = _datahub_gms_url
        mock_settings.datahub_token = hub_token if hub_token else ""

        async with override_app(
            datahub=datahub_client, redis=redis_client, db=async_session
        ) as client:
            yield client


def _assert_snake_case_keys(data: dict | list, path: str = "") -> None:
    """Recursively assert all dict keys are snake_case."""
    if isinstance(data, dict):
        for key, value in data.items():
            full_path = f"{path}.{key}" if path else key
            assert not _CAMEL_RE.search(key), f"camelCase key found: {full_path}"
            _assert_snake_case_keys(value, full_path)
    elif isinstance(data, list):
        for i, item in enumerate(data):
            _assert_snake_case_keys(item, f"{path}[{i}]")


def _assert_pagination_envelope(body: dict) -> None:
    """Assert response has pagination fields."""
    assert "offset" in body, "Missing 'offset'"
    assert "limit" in body, "Missing 'limit'"
    assert "total_count" in body, "Missing 'total_count'"
    assert "resp_time" in body, "Missing 'resp_time'"


def _assert_single_response(body: dict) -> None:
    """Assert response has resp_time."""
    assert "resp_time" in body, "Missing 'resp_time'"


def _assert_error_envelope(body: dict) -> None:
    """Assert error response has required fields."""
    assert "error_code" in body, "Missing 'error_code'"
    assert "message" in body, "Missing 'message'"
    assert "trace_id" in body, "Missing 'trace_id'"


# ── Ontology ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ontology_list_response_format(http_client):
    resp = await http_client.get("/api/v1/spoke/common/ontology", headers=_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    _assert_pagination_envelope(body)
    assert "concepts" in body
    _assert_snake_case_keys(body)


# ── Ingestion ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ingestion_list_response_format(http_client):
    resp = await http_client.get("/api/v1/spoke/common/ingestion", headers=_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    _assert_pagination_envelope(body)
    assert "configs" in body
    _assert_snake_case_keys(body)


# ── Validation ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_validation_list_response_format(http_client):
    resp = await http_client.get("/api/v1/spoke/common/validation", headers=_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    _assert_pagination_envelope(body)
    assert "configs" in body
    _assert_snake_case_keys(body)


# ── Generation ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gen_list_response_format(http_client):
    resp = await http_client.get("/api/v1/spoke/common/gen", headers=_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    _assert_pagination_envelope(body)
    assert "configs" in body
    _assert_snake_case_keys(body)


# ── Error response ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_error_response_format(http_client):
    fake_urn = "urn:li:dataset:(urn:li:dataPlatform:none,does.not.exist,PROD)"
    resp = await http_client.get(
        f"/api/v1/spoke/common/data/{fake_urn}",
        headers=_HEADERS,
    )
    assert resp.status_code == 404
    body = resp.json()
    _assert_error_envelope(body)


# ── Hub GraphQL (raw pass-through — no envelope) ─────────────────────────────


@pytest.mark.asyncio
async def test_hub_graphql_response_format(http_client):
    resp = await http_client.post(
        "/api/v1/hub/graphql",
        json={"query": "{ listDatasets(input: {start: 0, count: 1}) { total } }"},
        headers=_HEADERS,
    )
    assert resp.status_code == 200
    body = resp.json()
    # Hub proxy returns raw DataHub response — must NOT have resp_time envelope
    assert "resp_time" not in body
    assert "data" in body
