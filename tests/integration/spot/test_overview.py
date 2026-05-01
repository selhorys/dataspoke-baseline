"""Spot tests for Governance Overview endpoints.

Concerns covered:
- GET /spoke/dg/overview — snapshot with all 6 documented keys per BACKEND.md L478-L483
- GET /spoke/dg/overview/attr — display config (layout, color_by, filters)
- PATCH /spoke/dg/overview/attr — update display config

Spec: spec/feature/BACKEND.md §Overview Service L478-L483 — GET /spoke/dg/overview
composes: metric_values, per_dataset_breakdown, blind_spots, ontology_graph, medallion,
ownership_topology.
"""

import pytest
import httpx

# Named constants for the six documented snapshot keys
# Spec: spec/feature/BACKEND.md §Overview Service L478-L483
_OVERVIEW_KEY_METRIC_VALUES = "metric_values"
_OVERVIEW_KEY_PER_DATASET_BREAKDOWN = "per_dataset_breakdown"
_OVERVIEW_KEY_BLIND_SPOTS = "blind_spots"
_OVERVIEW_KEY_ONTOLOGY_GRAPH = "ontology_graph"
_OVERVIEW_KEY_MEDALLION = "medallion"
_OVERVIEW_KEY_OWNERSHIP_TOPOLOGY = "ownership_topology"


@pytest.mark.asyncio
async def test_overview_snapshot_keys(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/dg/overview returns 200 with all 6 documented top-level snapshot keys.

    Spec: spec/feature/BACKEND.md §Overview Service L478-L483 — GET /spoke/dg/overview
    composes: metric_values (dict), per_dataset_breakdown (dict), blind_spots (list),
    ontology_graph (object with nodes + edges arrays), medallion (bronze/silver/gold ints),
    ownership_topology (dict).
    """
    resp = await api_client.get(
        "/api/v1/spoke/dg/overview",
        headers=admin_headers,
    )

    assert resp.status_code == 200
    body = resp.json()

    # ── All 6 documented keys must be present ────────────────────────────────
    # Spec: spec/feature/BACKEND.md §Overview Service L478-L483
    assert _OVERVIEW_KEY_METRIC_VALUES in body, (
        f"Missing '{_OVERVIEW_KEY_METRIC_VALUES}' — spec/feature/BACKEND.md §Overview Service L478."
    )
    assert _OVERVIEW_KEY_PER_DATASET_BREAKDOWN in body, (
        f"Missing '{_OVERVIEW_KEY_PER_DATASET_BREAKDOWN}' — spec/feature/BACKEND.md §Overview Service L479."
    )
    assert _OVERVIEW_KEY_BLIND_SPOTS in body, (
        f"Missing '{_OVERVIEW_KEY_BLIND_SPOTS}' — spec/feature/BACKEND.md §Overview Service L480."
    )
    assert _OVERVIEW_KEY_ONTOLOGY_GRAPH in body, (
        f"Missing '{_OVERVIEW_KEY_ONTOLOGY_GRAPH}' — spec/feature/BACKEND.md §Overview Service L481."
    )
    assert _OVERVIEW_KEY_MEDALLION in body, (
        f"Missing '{_OVERVIEW_KEY_MEDALLION}' — spec/feature/BACKEND.md §Overview Service L482."
    )
    assert _OVERVIEW_KEY_OWNERSHIP_TOPOLOGY in body, (
        f"Missing '{_OVERVIEW_KEY_OWNERSHIP_TOPOLOGY}' — spec/feature/BACKEND.md §Overview Service L483."
    )

    # ── Type assertions per spec ─────────────────────────────────────────────
    assert isinstance(body[_OVERVIEW_KEY_METRIC_VALUES], dict), (
        "metric_values must be a dict (metric_id → latest value). "
        "Spec: spec/feature/BACKEND.md §Overview Service L478."
    )
    assert isinstance(body[_OVERVIEW_KEY_PER_DATASET_BREAKDOWN], dict), (
        "per_dataset_breakdown must be a dict (metric_id → dataset breakdown list). "
        "Spec: spec/feature/BACKEND.md §Overview Service L479."
    )
    assert isinstance(body[_OVERVIEW_KEY_BLIND_SPOTS], list), (
        "blind_spots must be a list of dataset URNs. "
        "Spec: spec/feature/BACKEND.md §Overview Service L480."
    )

    # ontology_graph has nodes and edges arrays
    ontology_graph = body[_OVERVIEW_KEY_ONTOLOGY_GRAPH]
    assert "nodes" in ontology_graph, (
        "ontology_graph missing 'nodes'. Spec: spec/feature/BACKEND.md §Overview Service L481."
    )
    assert "edges" in ontology_graph, (
        "ontology_graph missing 'edges'. Spec: spec/feature/BACKEND.md §Overview Service L481."
    )
    assert isinstance(ontology_graph["nodes"], list)
    assert isinstance(ontology_graph["edges"], list)

    # medallion has bronze/silver/gold integer fields
    # Spec: spec/feature/BACKEND.md §Overview Service L482 — Medallion layers:
    # Bronze = 0 upstreams, Silver = 1–2, Gold = 3+, derived from upstreamLineage.
    # All three fields are integer dataset counts.
    medallion = body[_OVERVIEW_KEY_MEDALLION]
    assert "bronze" in medallion, (
        "medallion missing 'bronze'. Spec: spec/feature/BACKEND.md §Overview Service L482."
    )
    assert "silver" in medallion, (
        "medallion missing 'silver'. Spec: spec/feature/BACKEND.md §Overview Service L482."
    )
    assert "gold" in medallion, (
        "medallion missing 'gold'. Spec: spec/feature/BACKEND.md §Overview Service L482."
    )
    assert isinstance(medallion["bronze"], int), (
        "medallion.bronze must be an int (dataset count with 0 upstreams). "
        "Spec: spec/feature/BACKEND.md §Overview Service L482."
    )
    assert isinstance(medallion["silver"], int), (
        "medallion.silver must be an int (dataset count with 1–2 upstreams). "
        "Spec: spec/feature/BACKEND.md §Overview Service L482."
    )
    assert isinstance(medallion["gold"], int), (
        "medallion.gold must be an int (dataset count with 3+ upstreams). "
        "Spec: spec/feature/BACKEND.md §Overview Service L482."
    )

    assert isinstance(body[_OVERVIEW_KEY_OWNERSHIP_TOPOLOGY], dict), (
        "ownership_topology must be a dict (owner_urn → dataset_urns). "
        "Spec: spec/feature/BACKEND.md §Overview Service L483."
    )


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
