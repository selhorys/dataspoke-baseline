"""Unit tests for DG overview routes.

Routes under test:
  GET   /api/v1/spoke/dg/overview      — get_overview (snapshot)
  GET   /api/v1/spoke/dg/overview/attr — get_overview_attr (display config)
  PATCH /api/v1/spoke/dg/overview/attr — patch_overview_attr (display config)

spec: API.md §DG routes — require 'dg' group.
spec: feature/BACKEND.md §Overview Service — snapshot contains metric_values,
      per_dataset_breakdown, ontology_graph, medallion, ownership_topology.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.dependencies import get_overview_service
from src.api.main import app

from tests.unit.api.conftest import auth_headers

_OVERVIEW_URL = "/api/v1/spoke/dg/overview"
_ATTR_URL = "/api/v1/spoke/dg/overview/attr"


def _make_snapshot_mock() -> MagicMock:
    snap = MagicMock()
    snap.metric_values = {"pct_fresh": 0.8}
    snap.per_dataset_breakdown = {}
    snap.blind_spots = []
    snap.ownership_topology = {}
    # ontology_graph
    snap.ontology_graph = MagicMock()
    snap.ontology_graph.nodes = []
    snap.ontology_graph.edges = []
    # medallion
    snap.medallion = MagicMock()
    snap.medallion.bronze = 3
    snap.medallion.silver = 2
    snap.medallion.gold = 1
    return snap


def _make_config_mock() -> MagicMock:
    cfg = MagicMock()
    cfg.layout = "force"
    cfg.color_by = "quality_score"
    cfg.filters = {}
    return cfg


@pytest.fixture
def mock_svc() -> AsyncMock:
    return AsyncMock()


@pytest.fixture(autouse=True)
def override_service(mock_svc: AsyncMock):
    app.dependency_overrides[get_overview_service] = lambda: mock_svc
    yield
    app.dependency_overrides.pop(get_overview_service, None)


# ── Auth gate: missing token → 401, wrong group → 403 ────────────────────────


@pytest.mark.asyncio
async def test_get_overview_without_token_returns_401(client) -> None:
    """GET /spoke/dg/overview without JWT returns 401.

    spec: API.md §Authentication — all protected routes require valid JWT.
    """
    resp = await client.get(_OVERVIEW_URL)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_overview_non_dg_group_returns_403(client) -> None:
    """GET /spoke/dg/overview with 'de' group (not 'dg') returns 403.

    spec: API.md §DG routes — require 'dg' group claim.
    """
    resp = await client.get(_OVERVIEW_URL, headers=auth_headers(["de"]))
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_get_overview_attr_without_token_returns_401(client) -> None:
    """GET /spoke/dg/overview/attr without JWT returns 401.

    spec: API.md §Authentication — all protected routes require valid JWT.
    """
    resp = await client.get(_ATTR_URL)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_patch_overview_attr_without_token_returns_401(client) -> None:
    """PATCH /spoke/dg/overview/attr without JWT returns 401.

    spec: API.md §Authentication — all write routes require valid JWT.
    """
    resp = await client.patch(_ATTR_URL, json={"layout": "hierarchical"})
    assert resp.status_code == 401


# ── Happy paths ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_overview_returns_200_with_snapshot_shape(client, mock_svc: AsyncMock) -> None:
    """GET /spoke/dg/overview returns 200 with metric_values, ontology_graph, medallion.

    spec: feature/BACKEND.md §Overview Service — snapshot contains all documented fields.
    """
    mock_svc.get_overview = AsyncMock(return_value=_make_snapshot_mock())

    resp = await client.get(_OVERVIEW_URL, headers=auth_headers(["dg"]))
    assert resp.status_code == 200
    body = resp.json()
    assert "metric_values" in body
    assert "ontology_graph" in body
    assert "medallion" in body
    assert "blind_spots" in body
    assert "per_dataset_breakdown" in body


@pytest.mark.asyncio
async def test_get_overview_attr_returns_200_with_layout_and_color_by(
    client, mock_svc: AsyncMock
) -> None:
    """GET /spoke/dg/overview/attr returns 200 with layout and color_by.

    spec: feature/BACKEND.md §Overview Service — config carries layout, color_by, filters.
    """
    mock_svc.get_config = AsyncMock(return_value=_make_config_mock())

    resp = await client.get(_ATTR_URL, headers=auth_headers(["dg"]))
    assert resp.status_code == 200
    body = resp.json()
    assert "layout" in body
    assert "color_by" in body
    assert "filters" in body


@pytest.mark.asyncio
async def test_patch_overview_attr_returns_200_with_updated_layout(
    client, mock_svc: AsyncMock
) -> None:
    """PATCH /spoke/dg/overview/attr returns 200 with updated config.

    spec: feature/BACKEND.md §Overview Service — PATCH config accepts partial body.
    """
    cfg = _make_config_mock()
    cfg.layout = "hierarchical"
    mock_svc.patch_config = AsyncMock(return_value=cfg)

    resp = await client.patch(
        _ATTR_URL,
        json={"layout": "hierarchical"},
        headers=auth_headers(["dg"]),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["layout"] == "hierarchical"
