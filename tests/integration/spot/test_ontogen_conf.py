"""Spot tests for Ontology Generation — singleton conf CRUD and seed CRUD.

Concerns covered (7 test functions):

Singleton conf CRUD:
  test_ontogen_conf_get_returns_defaults
  test_ontogen_conf_put
  test_ontogen_conf_patch
  test_ontogen_conf_delete_resets

Seed CRUD:
  test_ontogen_seed_create_list_get_patch_delete

Payload cap and schedule_tier boundary:
  test_ontogen_conf_put_dataset_filter_dimension_caps
    parametrized over (n, expected_status_set) x dimension x method:
      [at-cap-1000-accepted] — 200/201 accepted
      [over-cap-1001-rejected] — 422 rejected
  test_ontogen_conf_put_invalid_schedule_tier_422

These tests are pure REST and do not require raw-SQL seeding or DataHub documents.

Spec traceability:
- spec/feature/BACKEND.md §Ontology Generation Service §Inference Pipeline
- spec/USE_CASE_en.md §UC3 L389 — OntogenConfResponse fields
- spec/API.md §UC3 Payload caps — dataset_filter.{tags,glossary_terms,dataset_urns} ≤ 1,000
- spec/TESTING.md §Spot vs Api-Wired Integration Tests
"""

from contextlib import suppress

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
    """GET /spoke/ontogen/attr/conf returns 200 with singleton conf structure."""
    resp = await api_client.get(
        "/api/v1/spoke/ontogen/attr/conf",
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
    """PUT /spoke/ontogen/attr/conf creates or replaces the singleton conf."""
    resp = await api_client.put(
        "/api/v1/spoke/ontogen/attr/conf",
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
        "/api/v1/spoke/ontogen/attr/conf",
        headers=admin_headers,
        json={"is_enabled": False},
    )


@pytest.mark.asyncio
async def test_ontogen_conf_patch(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH /spoke/ontogen/attr/conf partially updates the singleton conf."""
    # Ensure a conf exists
    await api_client.put(
        "/api/v1/spoke/ontogen/attr/conf",
        headers=admin_headers,
        json={
            "is_enabled": False,
            "schedule_tier": "daily",
            "dataset_filter": {},
        },
    )

    patch_resp = await api_client.patch(
        "/api/v1/spoke/ontogen/attr/conf",
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
    """DELETE /spoke/ontogen/attr/conf removes/resets the singleton conf (204)."""
    # Ensure conf exists first
    await api_client.put(
        "/api/v1/spoke/ontogen/attr/conf",
        headers=admin_headers,
        json={
            "is_enabled": False,
            "schedule_tier": "daily",
            "dataset_filter": {},
        },
    )

    del_resp = await api_client.delete(
        "/api/v1/spoke/ontogen/attr/conf",
        headers=admin_headers,
    )
    assert del_resp.status_code == 204


@pytest.mark.asyncio
async def test_ontogen_seed_create_list_get_patch_delete(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Seed CRUD: create (201), list, get body, patch, delete (204)."""
    base_seed = "/api/v1/spoke/ontogen/attr/seed"
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


# ── Payload cap and schedule_tier boundary tests ──────────────────────────────


@pytest.mark.parametrize(
    ("n", "expected_status_set"),
    [(1000, {200, 201}), (1001, {422})],
    ids=["at-cap-1000-accepted", "over-cap-1001-rejected"],
)
@pytest.mark.parametrize(
    "dimension",
    ["tags", "glossary_terms", "dataset_urns"],
    ids=["tags", "glossary_terms", "dataset_urns"],
)
@pytest.mark.parametrize(
    "method",
    ["PUT", "PATCH"],
    ids=["PUT", "PATCH"],
)
@pytest.mark.asyncio
async def test_ontogen_conf_put_dataset_filter_dimension_caps(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    method: str,
    dimension: str,
    n: int,
    expected_status_set: set[int],
) -> None:
    """PUT or PATCH at-cap (n=1000, accepted) and over-cap (n=1001, rejected) on a
    single dataset_filter dimension.

    The boundary test (n=1000) verifies the cap itself: a regression dropping the
    limit to 500 would still pass the n=1001 test but fail here.  Well-formed URNs
    are used so cap enforcement — not URN validation — triggers the result.

    spec: API.md §UC3 Payload caps — dataset_filter.{tags,glossary_terms,dataset_urns}
      ≤ 1,000 entries per dimension; exactly 1,000 MUST be accepted; 1,001 MUST be rejected.
    """
    conf_url = "/api/v1/spoke/ontogen/attr/conf"

    # Build n well-formed URN strings for the chosen dimension.
    if dimension == "tags":
        entries = [f"urn:li:tag:t-{i}" for i in range(n)]
    elif dimension == "glossary_terms":
        entries = [f"urn:li:glossaryTerm:gt-{i}" for i in range(n)]
    else:  # dataset_urns
        entries = [
            f"urn:li:dataset:(urn:li:dataPlatform:postgres,db.s.t_{i},DEV)"
            for i in range(n)
        ]

    try:
        if method == "PUT":
            resp = await api_client.put(
                conf_url,
                headers=admin_headers,
                json={
                    "is_enabled": False,
                    "schedule_tier": "daily",
                    "default_run_prompt": "",
                    "dataset_filter": {dimension: entries},
                },
            )
        else:  # PATCH — the ontogen conf always exists (singleton with defaults), so
               # PATCH can target it directly without an explicit seed PUT.
            resp = await api_client.patch(
                conf_url,
                headers=admin_headers,
                json={"dataset_filter": {dimension: entries}},
            )

        # spec: API.md §UC3 Payload caps — exactly 1,000 entries MUST be accepted
        # (200 or 201); 1,001 entries MUST be rejected (422) at the Pydantic boundary
        # before any service-layer or DB call.
        assert resp.status_code in expected_status_set, (
            f"{method} with n={n} {dimension} entries: expected status in {expected_status_set}, "
            f"got {resp.status_code}: {resp.text}. "
            "spec: API.md §UC3 Payload caps — dataset_filter cap is 1,000 per dimension"
        )

        if 422 in expected_status_set:
            # The 422 body must be non-empty JSON (we do not pin the error message wording).
            # spec: API.md §UC3 Payload caps — over-cap dimension rejected at schema boundary.
            body = resp.json()
            assert body, (
                f"422 response body must be non-empty JSON; got: {resp.text!r}. "
                "spec: API.md §Error Codes — validation errors return structured JSON body"
            )

    finally:
        # The at-cap PUT/PATCH may have written to the singleton conf; reset to
        # a clean disabled state so subsequent parameterized cases are independent.
        with suppress(Exception):
            await api_client.patch(
                conf_url,
                headers=admin_headers,
                json={"is_enabled": False, "dataset_filter": {}},
            )


@pytest.mark.asyncio
async def test_ontogen_conf_put_origin_filter_round_trips(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT dataset_filter with origin+tags, then GET verifies the filter is persisted.

    Exercises the unified four-dimension dataset_filter shape for UC3.

    spec: spec/API.md §UC3 — dataset_filter unified four-dimension shape; origin dimension.
    spec: USE_CASE_en.md §UC3 §Conf — dataset_filter is optional scope filter.
    """
    conf_url = "/api/v1/spoke/ontogen/attr/conf"
    expected_filter = {
        "origin": "DEV",
        "tags": ["urn:li:tag:area:fulfillment"],
    }

    try:
        put_resp = await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "is_enabled": False,
                "schedule_tier": "daily",
                "dataset_filter": expected_filter,
            },
        )
        assert put_resp.status_code in (200, 201), (
            f"PUT with origin+tags dataset_filter failed: {put_resp.status_code} {put_resp.text}. "
            "spec: API.md §UC3 — dataset_filter unified four-dimension shape"
        )
        put_body = put_resp.json()
        assert put_body["dataset_filter"] == expected_filter, (
            f"PUT response dataset_filter not preserved: {put_body.get('dataset_filter')!r}. "
            "spec: USE_CASE_en.md §UC3 §Conf — dataset_filter round-trip"
        )

        get_resp = await api_client.get(conf_url, headers=admin_headers)
        assert get_resp.status_code == 200
        get_body = get_resp.json()
        assert get_body["dataset_filter"] == expected_filter, (
            f"GET round-trip dataset_filter mismatch: {get_body.get('dataset_filter')!r}. "
            "spec: USE_CASE_en.md §UC3 §Conf — dataset_filter must be persisted"
        )

    finally:
        from contextlib import suppress
        with suppress(Exception):
            await api_client.patch(
                conf_url,
                headers=admin_headers,
                json={"is_enabled": False, "dataset_filter": {}},
            )


@pytest.mark.asyncio
async def test_ontogen_conf_patch_adds_origin_to_existing_conf(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH adding origin='DEV' to an existing conf persists the updated filter.

    spec: spec/API.md §UC3 — dataset_filter unified four-dimension shape; PATCH is partial.
    spec: USE_CASE_en.md §UC3 §Conf — PATCH must update only the provided fields.
    """
    conf_url = "/api/v1/spoke/ontogen/attr/conf"

    try:
        # Seed an existing conf without origin
        await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "is_enabled": False,
                "schedule_tier": "daily",
                "dataset_filter": {"tags": ["urn:li:tag:area:catalog"]},
            },
        )

        patch_resp = await api_client.patch(
            conf_url,
            headers=admin_headers,
            json={"dataset_filter": {"origin": "DEV", "tags": ["urn:li:tag:area:catalog"]}},
        )
        assert patch_resp.status_code == 200, (
            f"PATCH with origin failed: {patch_resp.status_code} {patch_resp.text}. "
            "spec: API.md §UC3 — dataset_filter unified four-dimension shape"
        )
        patch_body = patch_resp.json()
        assert patch_body["dataset_filter"].get("origin") == "DEV", (
            f"PATCH did not persist origin='DEV': {patch_body.get('dataset_filter')!r}. "
            "spec: USE_CASE_en.md §UC3 §Conf — PATCH updates dataset_filter"
        )

    finally:
        from contextlib import suppress
        with suppress(Exception):
            await api_client.patch(
                conf_url,
                headers=admin_headers,
                json={"is_enabled": False, "dataset_filter": {}},
            )


@pytest.mark.asyncio
async def test_ontogen_conf_put_invalid_schedule_tier_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT with an unlisted schedule_tier value returns 422 at the Pydantic boundary.

    schedule_tier is a Literal["hourly","daily","weekly"] | None field; "monthly"
    is not a member of that union so Pydantic rejects it with 422 before the
    service layer is reached.

    spec: BACKEND.md §UC3 Ontology Generation — schedule_tier ∈ {"hourly","daily","weekly"};
      Pydantic Literal auto-422
    """
    conf_url = "/api/v1/spoke/ontogen/attr/conf"

    resp = await api_client.put(
        conf_url,
        headers=admin_headers,
        json={
            "is_enabled": False,
            "schedule_tier": "monthly",
        },
    )
    # spec: BACKEND.md §UC3 Ontology Generation — schedule_tier ∈ {"hourly","daily","weekly"};
    # Pydantic Literal auto-422; "monthly" is not a valid member.
    assert resp.status_code == 422, (
        f"PUT with schedule_tier='monthly' must return 422; "
        f"got {resp.status_code}: {resp.text}. "
        "spec: BACKEND.md §UC3 Ontology Generation — schedule_tier Pydantic Literal auto-422"
    )
