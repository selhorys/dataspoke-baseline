"""Unit tests for exception-to-HTTP response mapping."""

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.shared.exceptions import (
    ConflictError,
    DataHubUnavailableError,
    DataSpokeError,
    EntityNotFoundError,
    StorageUnavailableError,
)


def _make_app() -> FastAPI:
    """Create a minimal app with the exception handlers from main.py."""
    from src.api.main import create_app

    app = create_app()

    @app.get("/test/not-found")
    async def raise_not_found() -> None:
        raise EntityNotFoundError("dataset", "ds-123")

    @app.get("/test/conflict")
    async def raise_conflict() -> None:
        raise ConflictError("DUPLICATE_CONFIG", "Config already exists")

    @app.get("/test/datahub")
    async def raise_datahub() -> None:
        raise DataHubUnavailableError("GMS unreachable")

    @app.get("/test/storage")
    async def raise_storage() -> None:
        raise StorageUnavailableError("PostgreSQL unreachable")

    @app.get("/test/generic")
    async def raise_generic() -> None:
        raise DataSpokeError("Something went wrong")

    return app


@pytest.fixture
async def exc_client() -> AsyncClient:
    app = _make_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as ac:
        yield ac


@pytest.mark.asyncio
async def test_not_found_returns_404(exc_client: AsyncClient) -> None:
    resp = await exc_client.get("/test/not-found")
    assert resp.status_code == 404
    body = resp.json()
    assert body["error_code"] == "DATASET_NOT_FOUND"
    assert "ds-123" in body["message"]
    assert "trace_id" in body


@pytest.mark.asyncio
async def test_conflict_returns_409(exc_client: AsyncClient) -> None:
    resp = await exc_client.get("/test/conflict")
    assert resp.status_code == 409
    body = resp.json()
    assert body["error_code"] == "DUPLICATE_CONFIG"


@pytest.mark.asyncio
async def test_datahub_returns_502(exc_client: AsyncClient) -> None:
    resp = await exc_client.get("/test/datahub")
    assert resp.status_code == 502
    body = resp.json()
    assert body["error_code"] == "DATAHUB_UNAVAILABLE"


@pytest.mark.asyncio
async def test_storage_returns_503(exc_client: AsyncClient) -> None:
    resp = await exc_client.get("/test/storage")
    assert resp.status_code == 503
    body = resp.json()
    assert body["error_code"] == "STORAGE_UNAVAILABLE"


@pytest.mark.asyncio
async def test_generic_dataspoke_returns_500(exc_client: AsyncClient) -> None:
    resp = await exc_client.get("/test/generic")
    assert resp.status_code == 500
    body = resp.json()
    assert body["error_code"] == "INTERNAL_ERROR"


@pytest.mark.asyncio
async def test_trace_id_echoed(exc_client: AsyncClient) -> None:
    resp = await exc_client.get(
        "/test/not-found",
        headers={"X-Trace-Id": "trace-abc-123"},
    )
    body = resp.json()
    assert body["trace_id"] == "trace-abc-123"


@pytest.mark.asyncio
async def test_error_response_has_required_fields(exc_client: AsyncClient) -> None:
    resp = await exc_client.get("/test/conflict")
    body = resp.json()
    assert "error_code" in body
    assert "message" in body
    assert "trace_id" in body
