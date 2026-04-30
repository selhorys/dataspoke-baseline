"""Spot tests for Governance Overview endpoints.

Concerns covered:
- GET /spoke/dg/overview — snapshot with metric_values, blind_spots, ontology_graph, medallion keys
- GET /spoke/dg/overview/attr — display config (layout, color_by, filters)
- PATCH /spoke/dg/overview/attr — update display config
"""

import pytest
import httpx


@pytest.mark.asyncio
async def test_overview_snapshot_keys(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/dg/overview returns 200 with expected top-level snapshot keys."""
    resp = await api_client.get(
        "/api/v1/spoke/dg/overview",
        headers=admin_headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    # Spec: OverviewSnapshotResponse has metric_values, per_dataset_breakdown,
    # blind_spots, ontology_graph, medallion, ownership_topology
    assert "metric_values" in body
    assert "blind_spots" in body
    assert "ontology_graph" in body
    assert "medallion" in body
    # metric_values and blind_spots are collections
    assert isinstance(body["metric_values"], dict)
    assert isinstance(body["blind_spots"], list)
    # ontology_graph has nodes and edges arrays
    assert "nodes" in body["ontology_graph"]
    assert "edges" in body["ontology_graph"]
    # medallion has bronze/silver/gold
    assert "bronze" in body["medallion"]
    assert "silver" in body["medallion"]
    assert "gold" in body["medallion"]


@pytest.mark.asyncio
async def test_overview_attr_get(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/dg/overview/attr returns display config with layout, color_by, filters."""
    resp = await api_client.get(
        "/api/v1/spoke/dg/overview/attr",
        headers=admin_headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "layout" in body
    assert "color_by" in body
    assert "filters" in body


@pytest.mark.asyncio
async def test_overview_attr_patch(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH /spoke/dg/overview/attr updates display config fields."""
    resp = await api_client.patch(
        "/api/v1/spoke/dg/overview/attr",
        headers=admin_headers,
        json={"layout": "hierarchical", "color_by": "freshness"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["layout"] == "hierarchical"
    assert body["color_by"] == "freshness"

    # Restore defaults
    await api_client.patch(
        "/api/v1/spoke/dg/overview/attr",
        headers=admin_headers,
        json={"layout": "force", "color_by": "quality_score"},
    )
