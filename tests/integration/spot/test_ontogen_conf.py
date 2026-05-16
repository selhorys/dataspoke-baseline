"""Spot tests for Ontology Generation — singleton conf CRUD and seed CRUD.

Concerns covered (5 test functions):

Singleton conf CRUD:
  test_ontogen_conf_get_returns_defaults
  test_ontogen_conf_put
  test_ontogen_conf_patch
  test_ontogen_conf_delete_resets

Seed CRUD:
  test_ontogen_seed_create_list_get_patch_delete

These tests are pure REST and do not require raw-SQL seeding or DataHub documents.

Spec traceability:
- spec/feature/BACKEND.md §Ontology Generation Service §Inference Pipeline
- spec/USE_CASE_en.md §UC3 L389 — OntogenConfResponse fields
- spec/TESTING.md §Spot vs Api-Wired Integration Tests
"""

import httpx
import pytest

# Declare DataHub fixture dependency so module_dummy_data ingests catalog.title_master
# into DataHub before tests that seed NATIVE documents (evidence path tests).
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})


@pytest.mark.asyncio
async def test_ontogen_conf_get_returns_defaults(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/common/ontogen/attr/conf returns 200 with singleton conf structure."""
    resp = await api_client.get(
        "/api/v1/spoke/common/ontogen/attr/conf",
        headers=admin_headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    # spec: USE_CASE_en.md §UC3 L389 — OntogenConfResponse fields: is_enabled, schedule_tier,
    # dataset_filter, default_run_prompt, updated_at (max_manual/system_queries removed)
    assert "is_enabled" in body
    assert "schedule_tier" in body
    assert "dataset_filter" in body
    assert "default_run_prompt" in body
    assert "updated_at" in body


@pytest.mark.asyncio
async def test_ontogen_conf_put(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT /spoke/common/ontogen/attr/conf creates or replaces the singleton conf."""
    resp = await api_client.put(
        "/api/v1/spoke/common/ontogen/attr/conf",
        headers=admin_headers,
        json={
            "is_enabled": True,
            "schedule_tier": "daily",
            "dataset_filter": {},
            "default_run_prompt": None,
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["is_enabled"] is True
    assert body["schedule_tier"] == "daily"

    # Cleanup — reset to disabled
    await api_client.patch(
        "/api/v1/spoke/common/ontogen/attr/conf",
        headers=admin_headers,
        json={"is_enabled": False},
    )


@pytest.mark.asyncio
async def test_ontogen_conf_patch(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH /spoke/common/ontogen/attr/conf partially updates the singleton conf."""
    # Ensure a conf exists
    await api_client.put(
        "/api/v1/spoke/common/ontogen/attr/conf",
        headers=admin_headers,
        json={
            "is_enabled": False,
            "schedule_tier": "daily",
            "dataset_filter": {},
        },
    )

    patch_resp = await api_client.patch(
        "/api/v1/spoke/common/ontogen/attr/conf",
        headers=admin_headers,
        json={"schedule_tier": "weekly"},
    )

    assert patch_resp.status_code == 200
    body = patch_resp.json()
    assert body["schedule_tier"] == "weekly"


@pytest.mark.asyncio
async def test_ontogen_conf_delete_resets(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """DELETE /spoke/common/ontogen/attr/conf removes/resets the singleton conf (204)."""
    # Ensure conf exists first
    await api_client.put(
        "/api/v1/spoke/common/ontogen/attr/conf",
        headers=admin_headers,
        json={
            "is_enabled": False,
            "schedule_tier": "daily",
            "dataset_filter": {},
        },
    )

    del_resp = await api_client.delete(
        "/api/v1/spoke/common/ontogen/attr/conf",
        headers=admin_headers,
    )
    assert del_resp.status_code == 204


@pytest.mark.asyncio
async def test_ontogen_seed_create_list_get_patch_delete(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Seed CRUD: create (201), list, get body, patch, delete (204)."""
    base_seed = "/api/v1/spoke/common/ontogen/attr/seed"
    seed_md = "# Imazon Ontology Seed\n\nImazon is an online bookstore."

    # Create
    create_resp = await api_client.post(
        base_seed,
        headers={**admin_headers, "content-type": "text/markdown"},
        content=seed_md.encode(),
    )
    assert create_resp.status_code == 201, create_resp.text
    seed_id = create_resp.json()["seed_id"]

    # List — seed_id must appear
    list_resp = await api_client.get(base_seed, headers=admin_headers)
    assert list_resp.status_code == 200
    list_body = list_resp.json()
    assert "seeds" in list_body
    seed_ids = [s["seed_id"] for s in list_body["seeds"]]
    assert seed_id in seed_ids

    # Get body
    get_resp = await api_client.get(f"{base_seed}/{seed_id}", headers=admin_headers)
    assert get_resp.status_code == 200
    assert "Imazon" in get_resp.text

    # Patch (replace body)
    new_md = "# Updated Seed\n\nUpdated body for spot test."
    patch_resp = await api_client.patch(
        f"{base_seed}/{seed_id}",
        headers={**admin_headers, "content-type": "text/markdown"},
        content=new_md.encode(),
    )
    assert patch_resp.status_code == 200

    # Delete (soft-delete: status -> "retired"; DELETE returns 204)
    del_resp = await api_client.delete(f"{base_seed}/{seed_id}", headers=admin_headers)
    assert del_resp.status_code == 204

    # Soft-delete semantics: GET still returns the markdown body (record stays for audit),
    # but the seed_id is removed from the active list.
    list_after = await api_client.get(base_seed, headers=admin_headers)
    assert list_after.status_code == 200
    active_ids_after = [s["seed_id"] for s in list_after.json()["seeds"]]
    assert seed_id not in active_ids_after
