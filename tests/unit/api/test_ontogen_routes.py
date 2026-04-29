"""Unit tests for /spoke/common/ontogen routes."""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.dependencies import get_ontogen_service
from src.api.main import app
from src.shared.exceptions import (
    EntityNotFoundError,
    PreconditionFailedError,
)

from tests.unit.api.conftest import auth_headers, make_token

_BASE = "/api/v1/spoke/common/ontogen"


def _make_conf_row() -> MagicMock:
    row = MagicMock()
    row.is_enabled = False
    row.schedule_tier = None
    row.dataset_filter = {}
    row.max_manual_queries_per_dataset = 20
    row.max_system_queries_per_dataset = 10
    row.default_run_prompt = None
    row.updated_at = datetime.now(tz=UTC)
    return row


def _make_seed_row() -> MagicMock:
    row = MagicMock()
    row.id = uuid.uuid4()
    row.body_md = "# Test seed"
    row.status = "active"
    row.updated_at = datetime.now(tz=UTC)
    return row


@pytest.fixture
def mock_svc() -> AsyncMock:
    return AsyncMock()


@pytest.fixture(autouse=True)
def override_service(mock_svc: AsyncMock):
    app.dependency_overrides[get_ontogen_service] = lambda: mock_svc
    yield
    app.dependency_overrides.pop(get_ontogen_service, None)


# ── Auth checks ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_conf_without_token_returns_401(client) -> None:
    """GET /ontogen/attr/conf without token returns 401."""
    resp = await client.get(f"{_BASE}/attr/conf")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_conf_with_de_group_token_returns_200(client, mock_svc: AsyncMock) -> None:
    """GET /ontogen/attr/conf with 'de' group token returns 200."""
    mock_svc.get_conf = AsyncMock(return_value=_make_conf_row())
    resp = await client.get(f"{_BASE}/attr/conf", headers=auth_headers(["de"]))
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_conf_with_dg_group_token_returns_200(client, mock_svc: AsyncMock) -> None:
    """GET /ontogen/attr/conf with 'dg' group token returns 200."""
    mock_svc.get_conf = AsyncMock(return_value=_make_conf_row())
    resp = await client.get(f"{_BASE}/attr/conf", headers=auth_headers(["dg"]))
    assert resp.status_code == 200


# ── Conf round-trip ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_conf_returns_200(client, mock_svc: AsyncMock) -> None:
    """PUT /ontogen/attr/conf returns 200 with conf data."""
    conf_row = _make_conf_row()
    mock_svc.put_conf = AsyncMock(return_value=conf_row)

    resp = await client.put(
        f"{_BASE}/attr/conf",
        json={"is_enabled": False},
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_enabled"] is False


@pytest.mark.asyncio
async def test_put_conf_validates_dataset_filter_list_cap(client, mock_svc: AsyncMock) -> None:
    """PUT /ontogen/attr/conf with dataset_filter.dataset_urns > 1000 entries returns 422."""
    too_many_urns = [f"urn:li:dataset:(urn:li:dataPlatform:postgres,t{i},PROD)" for i in range(1001)]

    resp = await client.put(
        f"{_BASE}/attr/conf",
        json={
            "is_enabled": False,
            "dataset_filter": {"dataset_urns": too_many_urns},
        },
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 422


# ── Seeds ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_seed_returns_201(client, mock_svc: AsyncMock) -> None:
    """POST /ontogen/attr/seed returns 201 with seed_id."""
    seed = _make_seed_row()
    mock_svc.create_seed = AsyncMock(return_value=seed)

    resp = await client.post(
        f"{_BASE}/attr/seed",
        content=b"# My seed\n\nContent",
        headers={**auth_headers(["de"]), "Content-Type": "text/markdown"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "seed_id" in data


@pytest.mark.asyncio
async def test_post_seed_body_too_large_returns_413(client, mock_svc: AsyncMock) -> None:
    """POST /ontogen/attr/seed with body > 64 KiB returns 413."""
    big_body = b"x" * (64 * 1024 + 1)
    resp = await client.post(
        f"{_BASE}/attr/seed",
        content=big_body,
        headers={
            **auth_headers(["de"]),
            "Content-Type": "text/markdown",
            "Content-Length": str(len(big_body)),
        },
    )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_get_seed_malformed_uuid_returns_422(client, mock_svc: AsyncMock) -> None:
    """GET /ontogen/attr/seed/{bad_id} with non-UUID path segment returns 422."""
    resp = await client.get(
        f"{_BASE}/attr/seed/not-a-uuid-at-all",
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 422


# ── Run endpoint ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_run_returns_200(client, mock_svc: AsyncMock) -> None:
    """POST /ontogen/method/run returns 200."""
    from src.backend.ontogen.service import OntogenRunSummary

    mock_svc.run = AsyncMock(return_value=OntogenRunSummary(
        status="success",
        dry_run=False,
        unresolved_urns=[],
        counts={"nodes_added": 0, "edges_added": 0, "triples_added": 0},
    ))

    resp = await client.post(
        f"{_BASE}/method/run",
        content=b"",
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_post_run_body_too_large_returns_413(client, mock_svc: AsyncMock) -> None:
    """POST /ontogen/method/run with body > 64 KiB returns 413."""
    big_body = b"x" * (64 * 1024 + 1)
    resp = await client.post(
        f"{_BASE}/method/run",
        content=big_body,
        headers={
            **auth_headers(["de"]),
            "Content-Type": "text/markdown",
            "Content-Length": str(len(big_body)),
        },
    )
    assert resp.status_code == 413


# ── Triple review — dependency gate ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_triple_review_dependency_error_returns_422(client, mock_svc: AsyncMock) -> None:
    """POST .../result/triple/{id}/method/review with ONTOGEN_TRIPLE_DEPENDENCY_PENDING returns 422."""
    mock_svc.review_triple = AsyncMock(
        side_effect=PreconditionFailedError("ONTOGEN_TRIPLE_DEPENDENCY_PENDING", "not approved")
    )
    triple_id = "book__has-edition__edition"
    resp = await client.post(
        f"{_BASE}/result/triple/{triple_id}/method/review",
        json={"verdict": "approve"},
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error_code"] == "ONTOGEN_TRIPLE_DEPENDENCY_PENDING"


# ── Review request validation ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_node_review_reason_too_long_returns_422(client, mock_svc: AsyncMock) -> None:
    """POST .../result/node/{id}/method/review with reason > 2000 chars returns 422."""
    resp = await client.post(
        f"{_BASE}/result/node/book/method/review",
        json={"verdict": "approve", "reason": "x" * 2001},
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 422
