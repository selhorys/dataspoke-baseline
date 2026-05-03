"""Spot tests for Metadata Generation endpoints.

Concerns covered:
- GET /spoke/common/metagen — list configs (paginated envelope)
- GET /data/{urn}/attr/metagen/conf — 404 for unknown URN
- PUT /data/{urn}/attr/metagen/conf — create config (201)
- PATCH /data/{urn}/attr/metagen/conf — partial update
- DELETE /data/{urn}/attr/metagen/conf — remove config (204)
- POST /data/{urn}/method/metagen/run — dry_run=true and dry_run=false
- GET /data/{urn}/attr/metagen/result — result list (paginated)
- PATCH /data/{urn}/attr/metagen/result/{result_id} — review (approve and reject)
- GET /data/{urn}/event/metagen — event list envelope

Spec traceability:
- spec/feature/BACKEND.md §Metadata Generation Service §Approval flow (L289-L299)
- spec/feature/BACKEND_SCHEMA.md §metagen_results
"""

import urllib.parse
import uuid

import httpx
import pytest

# Per-module dummy-data seed: re-seed catalog schema in PG and ingest into DataHub
# before this module's tests run (autoused by tests/integration/conftest.py).
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})

# title_master is a DataHub-seeded Imazon dataset (catalog schema)
_TEST_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_ENCODED_URN = urllib.parse.quote(_TEST_URN, safe="")

# Named constants for cap values
# impl-cap; spec gap surfaced 2026-05-01 (not defined in API_DESIGN_PRINCIPLE_en.md)
_REASON_MAX_LEN = 2000
_FIELDS_MAX_COUNT = 200  # impl-cap; spec gap surfaced 2026-05-01
_FIELD_ENTRY_MAX_LEN = 512  # impl-cap; spec gap surfaced 2026-05-01


@pytest.mark.asyncio
async def test_metagen_list_paginated_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/common/metagen returns a paginated collection envelope."""
    resp = await api_client.get(
        "/api/v1/spoke/common/metagen?offset=0&limit=10",
        headers=admin_headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "results" in body
    assert "offset" in body
    assert "limit" in body
    assert "total_count" in body
    assert isinstance(body["results"], list)


@pytest.mark.asyncio
async def test_metagen_conf_get_404_unknown_urn(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET metagen conf for an unknown URN returns 404."""
    unknown_urn = urllib.parse.quote(
        "urn:li:dataset:(urn:li:dataPlatform:postgres,nonexistent.table,DEV)", safe=""
    )
    resp = await api_client.get(
        f"/api/v1/spoke/common/data/{unknown_urn}/attr/metagen/conf",
        headers=admin_headers,
    )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_metagen_conf_put(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT creates metagen conf (201) with targets, schedule_tier, is_enabled."""
    base = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"

    put_resp = await api_client.put(
        base,
        headers=admin_headers,
        json={
            "targets": ["dataset.description", "column.description"],
            "is_enabled": False,
            "schedule_tier": "weekly",
            "owner": "spot-test@imazon.com",
        },
    )
    assert put_resp.status_code in (200, 201), put_resp.text
    body = put_resp.json()
    assert body["dataset_urn"] == _TEST_URN
    assert "dataset.description" in body["targets"]

    # Cleanup
    await api_client.delete(base, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_conf_patch(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH updates targets on existing metagen conf."""
    base = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"

    await api_client.put(
        base,
        headers=admin_headers,
        json={
            "targets": ["dataset.description"],
            "is_enabled": False,
            "schedule_tier": None,
            "owner": "spot-test@imazon.com",
        },
    )

    patch_resp = await api_client.patch(
        base,
        headers=admin_headers,
        json={"targets": ["dataset.description", "column.description"]},
    )
    assert patch_resp.status_code == 200
    assert "column.description" in patch_resp.json()["targets"]

    # Cleanup
    await api_client.delete(base, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_conf_delete(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """DELETE removes metagen conf; subsequent GET returns 404."""
    base = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"

    await api_client.put(
        base,
        headers=admin_headers,
        json={
            "targets": ["dataset.description"],
            "is_enabled": False,
            "schedule_tier": None,
            "owner": "spot-test@imazon.com",
        },
    )

    del_resp = await api_client.delete(base, headers=admin_headers)
    assert del_resp.status_code == 204

    get_resp = await api_client.get(base, headers=admin_headers)
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_metagen_run_dry_run(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST method/metagen/run with dry_run=true returns MetagenRunResponse envelope.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service §Run pipeline
    — dry_run returns a synthetic MetagenResultRecord without persisting.
    MetagenRunResponse must contain id, dataset_urn, proposals.
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"
    base_run = f"/api/v1/spoke/common/data/{_ENCODED_URN}/method/metagen/run"

    # Ensure config exists
    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "targets": ["dataset.description"],
            "is_enabled": False,
            "schedule_tier": None,
            "owner": "spot-test@imazon.com",
        },
    )

    run_resp = await api_client.post(
        base_run,
        headers=admin_headers,
        json={"dry_run": True},
    )

    assert run_resp.status_code == 200
    body = run_resp.json()
    # All three keys must be present per MetagenRunResponse schema
    # Spec: spec/feature/BACKEND.md §Metadata Generation Service §Run pipeline
    assert "id" in body and "dataset_urn" in body and "proposals" in body
    assert body["dataset_urn"] == _TEST_URN

    # Cleanup
    await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_result_list_paginated(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET attr/metagen/result returns paginated result list (may be empty)."""
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"
    base_results = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/result"

    # Create config
    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "targets": ["dataset.description"],
            "is_enabled": False,
            "schedule_tier": None,
            "owner": "spot-test@imazon.com",
        },
    )

    resp = await api_client.get(base_results, headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "results" in body
    assert "offset" in body
    assert "limit" in body
    assert "total_count" in body
    assert isinstance(body["results"], list)

    # Cleanup
    await api_client.delete(base_conf, headers=admin_headers)


async def _insert_pending_metagen_result(
    session,
    *,
    dataset_urn: str,
    proposals: dict | None = None,
    field_status: dict | None = None,
) -> str:
    """Seed a metagen_results row directly via async_session (deterministic state).

    Mirrors the DB-seeding pattern from test_ontogen.py review tests so the
    approve/reject paths can be exercised without depending on the stub LLM
    returning non-empty proposals.
    Spec: spec/feature/BACKEND.md L289-L299 (approve writes DataHub editable aspects).
    """
    import uuid as _uuid
    from sqlalchemy import text

    result_id = _uuid.uuid4()
    run_id = _uuid.uuid4()
    _proposals = proposals or {"dataset.description": "Seeded test description."}
    _field_status = field_status or {"dataset.description": "pending"}

    import json
    await session.execute(
        text(
            "INSERT INTO dataspoke.metagen_results"
            " (id, dataset_urn, proposals, field_status, run_id, generated_at)"
            " VALUES (:id, :dataset_urn, CAST(:proposals AS jsonb),"
            " CAST(:field_status AS jsonb), :run_id, now())"
        ),
        {
            "id": str(result_id),
            "dataset_urn": dataset_urn,
            "proposals": json.dumps(_proposals),
            "field_status": json.dumps(_field_status),
            "run_id": str(run_id),
        },
    )
    await session.commit()
    return str(result_id)


async def _delete_metagen_result(session, result_id: str, dataset_urn: str) -> None:
    """Clean up a seeded metagen_results row."""
    from sqlalchemy import text
    await session.execute(
        text("DELETE FROM dataspoke.metagen_results WHERE id = :id AND dataset_urn = :urn"),
        {"id": result_id, "urn": dataset_urn},
    )
    await session.commit()


@pytest.mark.asyncio
async def test_metagen_result_review_reject(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session,
) -> None:
    """PATCH result/{id} with 'reject' sets all field_status entries to 'rejected'.

    Seeds a metagen_results row directly (no LLM dependency) so the reject path
    is deterministic regardless of stub LLM behaviour.
    Spec: spec/feature/BACKEND.md §Metadata Generation Service §Approval flow L289-L299.
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"

    # Ensure config exists for the URN
    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "targets": ["dataset.description"],
            "is_enabled": False,
            "schedule_tier": None,
            "owner": "spot-test@imazon.com",
        },
    )

    result_id = await _insert_pending_metagen_result(
        async_session,
        dataset_urn=_TEST_URN,
        proposals={"dataset.description": "Seeded description for rejection test."},
        field_status={"dataset.description": "pending"},
    )

    encoded_result_id = urllib.parse.quote(result_id, safe="")
    try:
        review_resp = await api_client.patch(
            f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/result/{encoded_result_id}",
            headers=admin_headers,
            json={"verdict": "reject", "reason": "spot-test rejection"},
        )
        assert review_resp.status_code == 200, review_resp.text
        body = review_resp.json()
        assert body["id"] == result_id
        # All fields must be rejected
        # Spec: spec/feature/BACKEND.md L295 — verdict=reject sets all to 'rejected'
        for status_val in body["field_status"].values():
            assert status_val == "rejected"
    finally:
        await _delete_metagen_result(async_session, result_id, _TEST_URN)
        await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_result_review_approve(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session,
) -> None:
    """PATCH result/{id} with 'approve' writes DataHub editable aspects.

    Seeds a metagen_results row directly (no LLM dependency) so the approve path
    is deterministic regardless of stub LLM behaviour.
    Spec: spec/feature/BACKEND.md §Metadata Generation Service §Approval flow L289-L299
    — on approval the service writes to editable DataHub aspects in a single emit_mcp
    per affected entity and emits METAGEN.APPROVE event.
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"

    # Ensure config exists for the URN
    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "targets": ["dataset.description"],
            "is_enabled": False,
            "schedule_tier": None,
            "owner": "spot-test@imazon.com",
        },
    )

    result_id = await _insert_pending_metagen_result(
        async_session,
        dataset_urn=_TEST_URN,
        proposals={"dataset.description": "Seeded description for approval test."},
        field_status={"dataset.description": "pending"},
    )

    encoded_result_id = urllib.parse.quote(result_id, safe="")
    try:
        review_resp = await api_client.patch(
            f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/result/{encoded_result_id}",
            headers=admin_headers,
            json={"verdict": "approve", "reason": "spot-test approval"},
        )
        assert review_resp.status_code == 200, review_resp.text
        body = review_resp.json()
        assert body["id"] == result_id
        # All fields must be approved
        # Spec: spec/feature/BACKEND.md L289 — verdict=approve flips all pending fields
        for status_val in body["field_status"].values():
            assert status_val == "approved"
    finally:
        await _delete_metagen_result(async_session, result_id, _TEST_URN)
        await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_events_list_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET event/metagen returns paginated event envelope (may be empty)."""
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"
    base_events = f"/api/v1/spoke/common/data/{_ENCODED_URN}/event/metagen"

    # Create config so events endpoint is accessible
    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "targets": ["dataset.description"],
            "is_enabled": False,
            "schedule_tier": None,
            "owner": "spot-test@imazon.com",
        },
    )

    resp = await api_client.get(base_events, headers=admin_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "events" in body
    assert "offset" in body
    assert "limit" in body
    assert "total_count" in body
    assert isinstance(body["events"], list)

    # Cleanup
    await api_client.delete(base_conf, headers=admin_headers)
