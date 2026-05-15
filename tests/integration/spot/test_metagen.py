"""Spot tests for Metadata Generation endpoints.

Concerns covered:
- GET /spoke/common/metagen — list configs (paginated envelope)
- GET /data/{urn}/attr/metagen/conf — 404 for unknown URN
- PUT /data/{urn}/attr/metagen/conf — create config (201)
- PATCH /data/{urn}/attr/metagen/conf — partial update
- DELETE /data/{urn}/attr/metagen/conf — remove config (204)
- POST /data/{urn}/method/metagen/run — dry_run=true emits METAGEN.COMPLETE with
    dry_run=true; result_id=None in detail
- POST /data/{urn}/method/metagen/run — dry_run=false (non-dry-run persisted path)
- POST /data/{urn}/method/metagen/run — 409 GENERATION_DISABLED when is_enabled=False;
    no METAGEN.COMPLETE event emitted on rejected call
- GET /data/{urn}/attr/metagen/result — result list (paginated)
- GET /data/{urn}/attr/metagen/result?latest=true — returns at most one result
- PATCH /data/{urn}/attr/metagen/result/{result_id} — review (approve and reject)
- PATCH /data/{urn}/attr/metagen/result/{result_id} — field-level approve subset
- PATCH /data/{urn}/attr/metagen/result/{result_id} — cross_data.md action approve
    (create, modify, delete)
- PATCH /data/{urn}/attr/metagen/result/{result_id} — follow-up PATCH preserves prior
    field_status entries
- GET /data/{urn}/event/metagen — event list envelope

Spec traceability:
- spec/feature/BACKEND.md §Metadata Generation Service §Approval flow (L440-L453)
- spec/feature/BACKEND.md §Cross-data MD action types
- spec/feature/BACKEND_SCHEMA.md §metagen_results
- spec/USE_CASE_en.md L700 — GENERATION_DISABLED on non-dry run with is_enabled=False
- spec/feature/BACKEND.md L657 — METAGEN.COMPLETE emitted for dry-run and non-dry-run;
    dry_run flag in detail
- spec/API.md L257 — ?latest=true returns most recent result row
"""

import urllib.parse

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
    """POST method/metagen/run with dry_run=true returns MetagenRunResponse envelope
    and emits exactly one METAGEN.COMPLETE event with dry_run=true and result_id=None.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service §Run pipeline
    — dry_run returns a synthetic MetagenResultRecord without persisting.
    MetagenRunResponse must contain id, dataset_urn, proposals.
    Spec: spec/feature/BACKEND.md L657 — METAGEN.COMPLETE recorded for both dry-run and
    non-dry-run; dry_run flag in detail; result_id is null for dry-run (no persisted row).
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"
    base_run = f"/api/v1/spoke/common/data/{_ENCODED_URN}/method/metagen/run"
    event_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/event/metagen"
    conf_targets = ["dataset.description"]

    # Ensure config exists
    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "targets": conf_targets,
            "is_enabled": False,
            "schedule_tier": None,
            "owner": "spot-test@imazon.com",
        },
    )

    # Snapshot count of METAGEN.COMPLETE events before the POST
    pre_resp = await api_client.get(f"{event_url}?limit=100", headers=admin_headers)
    assert pre_resp.status_code == 200, pre_resp.text
    pre_events = pre_resp.json()["events"]
    pre_count = sum(1 for e in pre_events if e["event_type"] == "METAGEN.COMPLETE")

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

    # Assert exactly one new METAGEN.COMPLETE event was emitted
    # spec: BACKEND.md L657 — COMPLETE recorded for dry-run; dry_run flag in detail
    post_resp = await api_client.get(f"{event_url}?limit=100", headers=admin_headers)
    assert post_resp.status_code == 200, post_resp.text
    post_events = post_resp.json()["events"]
    complete_events = [e for e in post_events if e["event_type"] == "METAGEN.COMPLETE"]
    post_count = len(complete_events)

    assert post_count == pre_count + 1, (
        f"Expected exactly one new METAGEN.COMPLETE event after dry-run; "
        f"pre_count={pre_count}, post_count={post_count}. "
        "spec: BACKEND.md L657 — dry-run must emit METAGEN.COMPLETE"
    )

    new_event = complete_events[0]  # newest first (ordered by occurred_at desc)
    assert new_event["detail"].get("dry_run") is True, (
        f"METAGEN.COMPLETE event detail must carry dry_run=true; "
        f"got detail={new_event['detail']!r}. "
        "spec: BACKEND.md L657 — dry_run flag in detail"
    )
    # result_id must be None for dry-run — no row is persisted
    # spec: BACKEND.md L657 — result_id is null for dry-run
    assert new_event["detail"].get("result_id") is None, (
        f"METAGEN.COMPLETE event detail result_id must be None for dry-run; "
        f"got {new_event['detail'].get('result_id')!r}. "
        "spec: BACKEND.md L657 — dry-run does not persist a result row"
    )
    # targets in event detail must match the conf's targets list
    # spec: BACKEND.md L657 — event detail carries targets
    assert new_event["detail"].get("targets") == conf_targets, (
        f"METAGEN.COMPLETE event detail targets={new_event['detail'].get('targets')!r} "
        f"must match conf targets={conf_targets!r}. "
        "spec: BACKEND.md L657"
    )

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
    Spec: spec/feature/BACKEND.md L440-L453 (approve writes DataHub editable aspects).
    """
    import json
    import uuid as _uuid

    from sqlalchemy import text

    result_id = _uuid.uuid4()
    run_id = _uuid.uuid4()
    _proposals = proposals or {"dataset.description": "Seeded test description."}
    _field_status = field_status or {"dataset.description": "pending"}

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
    Spec: spec/feature/BACKEND.md §Metadata Generation Service §Approval flow L440-L453.
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
    Spec: spec/feature/BACKEND.md §Metadata Generation Service §Approval flow L440-L453
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
        # Spec: spec/feature/BACKEND.md L440 — verdict=approve flips all pending fields
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


# ── New-boundary + negative-coverage tests ─────────────────────────────────────


@pytest.mark.asyncio
async def test_metagen_run_is_enabled_false_non_dry_run_returns_409_GENERATION_DISABLED(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST method/metagen/run with is_enabled=False returns 409 GENERATION_DISABLED;
    no METAGEN.COMPLETE event is emitted on the rejected call.

    spec: USE_CASE_en.md L700 — 'When is_enabled=false, non-dry-run calls to
    method/metagen/run return 409 GENERATION_DISABLED.'
    spec: BACKEND.md L452 — 'method/run with is_enabled=false and dry_run=false raises
    409 GENERATION_DISABLED. Dry-run is permitted regardless of is_enabled.'
    spec: BACKEND.md L657 — METAGEN.COMPLETE is emitted only when the run completes;
    a rejected (409) call must not emit it.
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"
    base_run = f"/api/v1/spoke/common/data/{_ENCODED_URN}/method/metagen/run"
    event_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/event/metagen"

    try:
        # Ensure config exists with is_enabled=False
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

        # Snapshot METAGEN.COMPLETE count before the rejected call
        pre_resp = await api_client.get(f"{event_url}?limit=100", headers=admin_headers)
        assert pre_resp.status_code == 200, pre_resp.text
        pre_complete_count = sum(
            1 for e in pre_resp.json()["events"]
            if e["event_type"] == "METAGEN.COMPLETE"
        )

        run_resp = await api_client.post(
            base_run,
            headers=admin_headers,
            json={"dry_run": False},
        )
        assert run_resp.status_code == 409, (
            f"Expected 409 GENERATION_DISABLED when is_enabled=False and dry_run=False; "
            f"got {run_resp.status_code}: {run_resp.text}. "
            "spec: USE_CASE_en.md L700"
        )
        body = run_resp.json()
        assert body.get("error_code") == "GENERATION_DISABLED", (
            f"Expected error_code 'GENERATION_DISABLED'; got: {body!r}. "
            "spec: USE_CASE_en.md L700; BACKEND.md L452"
        )

        # Negative-parity: no METAGEN.COMPLETE event must have been emitted
        # spec: BACKEND.md L657 — event is for completed runs only; rejected calls are not runs
        post_resp = await api_client.get(f"{event_url}?limit=100", headers=admin_headers)
        assert post_resp.status_code == 200, post_resp.text
        post_complete_count = sum(
            1 for e in post_resp.json()["events"]
            if e["event_type"] == "METAGEN.COMPLETE"
        )
        assert post_complete_count == pre_complete_count, (
            f"No new METAGEN.COMPLETE event must be emitted after a 409-rejected run; "
            f"pre={pre_complete_count}, post={post_complete_count}. "
            "spec: BACKEND.md L657"
        )

        # Dry-run must still succeed when is_enabled=False — disabled gate is
        # scoped to non-dry-run only. spec: USE_CASE_en.md L700; BACKEND.md L452
        dry_resp = await api_client.post(
            base_run,
            headers=admin_headers,
            json={"dry_run": True},
        )
        assert dry_resp.status_code == 200, (
            f"Dry-run must succeed even when is_enabled=False; "
            f"got {dry_resp.status_code}: {dry_resp.text}. "
            "spec: USE_CASE_en.md L700; BACKEND.md L452"
        )
    finally:
        await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_field_level_approve_subset(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session,
) -> None:
    """PATCH result with verdict=approve + fields=[subset] only flips those fields to 'approved'.

    Fields not in the subset must remain 'pending'.

    spec: BACKEND.md L444 — 'verdict: approve + fields: [...] → approve only the listed
    field paths and/or cross-data actions'
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"
    encoded_urn = urllib.parse.quote(_TEST_URN, safe="")

    result_id: str | None = None

    try:
        await api_client.put(
            base_conf,
            headers=admin_headers,
            json={
                "targets": ["dataset.description", "column.description"],
                "is_enabled": False,
                "schedule_tier": None,
                "owner": "spot-test@imazon.com",
            },
        )

        # Seed a result with four pending fields
        result_id = await _insert_pending_metagen_result(
            async_session,
            dataset_urn=_TEST_URN,
            proposals={
                "dataset.description": "Book catalog master list.",
                "column.description": {
                    "book_id": "Unique identifier for a book.",
                    "title": "Book title shown to customers.",
                    "author": "Author name.",
                },
            },
            field_status={
                "dataset.description": "pending",
                "column.description.book_id": "pending",
                "column.description.title": "pending",
                "column.description.author": "pending",
            },
        )
        encoded_result_id = urllib.parse.quote(result_id, safe="")
        patch_url = (
            f"/api/v1/spoke/common/data/{encoded_urn}/attr/metagen/result/{encoded_result_id}"
        )

        approve_resp = await api_client.patch(
            patch_url,
            headers=admin_headers,
            json={
                "verdict": "approve",
                "fields": ["dataset.description", "column.description.book_id"],
                "reason": "spot-test field-level approve",
            },
        )
        assert approve_resp.status_code == 200, approve_resp.text
        body = approve_resp.json()
        fs = body["field_status"]

        # Approved fields must flip
        # spec: BACKEND.md L444 — field-level approve flips only listed fields
        assert fs.get("dataset.description") == "approved", (
            f"'dataset.description' should be 'approved'; got {fs.get('dataset.description')!r}. "
            "spec: BACKEND.md L444"
        )
        assert fs.get("column.description.book_id") == "approved", (
            f"'column.description.book_id' should be 'approved'; "
            f"got {fs.get('column.description.book_id')!r}. spec: BACKEND.md L444"
        )
        # Non-listed fields must remain pending
        assert fs.get("column.description.title") == "pending", (
            f"'column.description.title' should remain 'pending'; "
            f"got {fs.get('column.description.title')!r}. spec: BACKEND.md L444"
        )
        assert fs.get("column.description.author") == "pending", (
            f"'column.description.author' should remain 'pending'; "
            f"got {fs.get('column.description.author')!r}. spec: BACKEND.md L444"
        )
    finally:
        if result_id is not None:
            await _delete_metagen_result(async_session, result_id, _TEST_URN)
        await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_cross_data_action_approve(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session,
) -> None:
    """PATCH result approving cross_data.md.a1 flips that action's field_status to 'approved'.

    spec: BACKEND_SCHEMA.md L134 — proposals['cross_data.md'] is a list of action dicts;
    field_status uses flat cross_data.md.<action_id> keys.
    spec: BACKEND.md §Cross-data MD action types (create action: title, body,
    related_assets required).
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"
    encoded_urn = urllib.parse.quote(_TEST_URN, safe="")

    result_id: str | None = None

    try:
        await api_client.put(
            base_conf,
            headers=admin_headers,
            json={
                "targets": ["cross_data.md"],
                "is_enabled": False,
                "schedule_tier": None,
                "owner": "spot-test@imazon.com",
            },
        )

        # Seed a result with a cross_data.md create action
        # spec: BACKEND.md §Cross-data MD action types — create requires title, body, related_assets
        result_id = await _insert_pending_metagen_result(
            async_session,
            dataset_urn=_TEST_URN,
            proposals={
                "cross_data.md": [
                    {
                        "action_id": "a1",
                        "action": "create",
                        "title": "title_master onboarding guide for catalog editors",
                        "body": (
                            "# title_master onboarding\n\n"
                            "`catalog.title_master` is the per-ISBN source of truth for every "
                            "Imazon listing — title, subtitle, primary author, publisher, "
                            "genre code, and the manufacturer suggested retail price that the "
                            "storefront falls back to when an edition is missing its own price.\n\n"
                            "Editors update this row when a publisher submits cover artwork or "
                            "revises the marketing blurb; the `editions` table holds the "
                            "per-format variants (Hardcover, Paperback, eBook, Audiobook) and "
                            "joins back here on `isbn`. Inactive titles are kept for historical "
                            "sales analytics rather than being deleted, so set `is_active=false` "
                            "instead of removing the row."
                        ),
                        "related_assets": [
                            "urn:li:dataset:(urn:li:dataPlatform:postgres,"
                            "example_db.catalog.title_master,DEV)"
                        ],
                        "confidence": 0.75,
                    }
                ]
            },
            field_status={"cross_data.md.a1": "pending"},
        )
        encoded_result_id = urllib.parse.quote(result_id, safe="")
        patch_url = (
            f"/api/v1/spoke/common/data/{encoded_urn}/attr/metagen/result/{encoded_result_id}"
        )

        approve_resp = await api_client.patch(
            patch_url,
            headers=admin_headers,
            json={
                "verdict": "approve",
                "fields": ["cross_data.md.a1"],
                "reason": "spot-test cross_data action approve",
            },
        )
        assert approve_resp.status_code == 200, approve_resp.text
        body = approve_resp.json()
        # spec: BACKEND_SCHEMA.md L134-L135 — field_status key is cross_data.md.<action_id>
        assert body["field_status"].get("cross_data.md.a1") == "approved", (
            f"field_status['cross_data.md.a1'] should be 'approved'; "
            f"got {body['field_status'].get('cross_data.md.a1')!r}. "
            "spec: BACKEND_SCHEMA.md L134; BACKEND.md §Cross-data MD action types"
        )
    finally:
        # Approval emits a urn:li:document:<uuid> via metagen cross_data.create_document.
        # The URN is not deterministic, so clean up by scanning relatedAssets for _TEST_URN.
        from tests.integration.util.datahub import hard_delete_documents_for_dataset

        hard_delete_documents_for_dataset(_TEST_URN)
        if result_id is not None:
            await _delete_metagen_result(async_session, result_id, _TEST_URN)
        await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_result_latest_returns_at_most_one(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session,
) -> None:
    """GET attr/metagen/result?latest=true returns at most one result row for the dataset.

    Seeds two results rows with different generated_at values; asserts the response
    carries at most one row and that row belongs to _TEST_URN.

    spec: API.md L257 — '?latest=true for most recent only'
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"
    base_results = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/result"

    result_id1: str | None = None
    result_id2: str | None = None

    try:
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

        result_id1 = await _insert_pending_metagen_result(
            async_session,
            dataset_urn=_TEST_URN,
            proposals={"dataset.description": "First seeded description."},
            field_status={"dataset.description": "pending"},
        )
        result_id2 = await _insert_pending_metagen_result(
            async_session,
            dataset_urn=_TEST_URN,
            proposals={"dataset.description": "Second seeded description."},
            field_status={"dataset.description": "pending"},
        )

        latest_resp = await api_client.get(
            f"{base_results}?latest=true",
            headers=admin_headers,
        )
        assert latest_resp.status_code == 200, latest_resp.text
        body = latest_resp.json()
        assert "results" in body and isinstance(body["results"], list)
        # spec: API.md L257 — ?latest=true returns the most recent row; two rows
        # were seeded for this URN, so exactly one must be returned.
        assert len(body["results"]) == 1, (
            f"?latest=true must return exactly one result row when results exist; "
            f"got {len(body['results'])}. spec: API.md L257"
        )
        assert body["results"][0]["dataset_urn"] == _TEST_URN, (
            f"latest result dataset_urn expected {_TEST_URN!r}; "
            f"got {body['results'][0].get('dataset_urn')!r}. spec: API.md L257"
        )
    finally:
        if result_id1 is not None:
            await _delete_metagen_result(async_session, result_id1, _TEST_URN)
        if result_id2 is not None:
            await _delete_metagen_result(async_session, result_id2, _TEST_URN)
        await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_cross_data_modify_and_delete_actions(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session,
) -> None:
    """PATCH approving cross_data.md modify and delete actions flips both to 'approved'.

    Companion to test_metagen_cross_data_action_approve, which covers the create action.
    Seeds a result with two actions:
      - a1: modify (requires document_urn and body)
      - a2: delete (requires document_urn only)
    PATCH approves both; asserts both field_status entries flip to 'approved' and the
    proposals payload round-trips with the correct action types.

    document_urn values are fictitious; on approval apply_actions() warns and continues
    per impl best-effort emit. End-to-end DataHub apply for modify/delete actions is out
    of scope for this test.

    spec: BACKEND.md §Cross-data MD action types — modify: document_urn + body;
    delete: document_urn only.
    spec: BACKEND_SCHEMA.md L134-L135 — field_status uses cross_data.md.<action_id> keys.
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"

    result_id: str | None = None

    try:
        await api_client.put(
            base_conf,
            headers=admin_headers,
            json={
                "targets": ["cross_data.md"],
                "is_enabled": False,
                "schedule_tier": None,
                "owner": "spot-test@imazon.com",
            },
        )

        result_id = await _insert_pending_metagen_result(
            async_session,
            dataset_urn=_TEST_URN,
            proposals={
                "cross_data.md": [
                    {
                        "action_id": "a1",
                        "action": "modify",
                        "document_urn": "urn:li:document:spot-test-existing-doc-1",
                        "body": "Updated cross-data body for modify action test.",
                        "confidence": 0.78,
                    },
                    {
                        "action_id": "a2",
                        "action": "delete",
                        "document_urn": "urn:li:document:spot-test-existing-doc-2",
                        "confidence": 0.65,
                    },
                ],
            },
            field_status={
                "cross_data.md.a1": "pending",
                "cross_data.md.a2": "pending",
            },
        )
        encoded_result_id = urllib.parse.quote(result_id, safe="")
        patch_url = (
            f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/result/{encoded_result_id}"
        )

        approve_resp = await api_client.patch(
            patch_url,
            headers=admin_headers,
            json={
                "verdict": "approve",
                "fields": ["cross_data.md.a1", "cross_data.md.a2"],
                "reason": "spot-test: modify+delete action approve",
            },
        )
        assert approve_resp.status_code == 200, (
            f"PATCH approve modify+delete actions failed: "
            f"{approve_resp.status_code} {approve_resp.text}"
        )
        body = approve_resp.json()
        assert body["id"] == result_id
        # spec: BACKEND_SCHEMA.md L134-L135 — field_status key is cross_data.md.<action_id>
        assert body["field_status"].get("cross_data.md.a1") == "approved", (
            f"field_status['cross_data.md.a1'] (modify) should be 'approved'; "
            f"got {body['field_status'].get('cross_data.md.a1')!r}. "
            "spec: BACKEND_SCHEMA.md L134"
        )
        assert body["field_status"].get("cross_data.md.a2") == "approved", (
            f"field_status['cross_data.md.a2'] (delete) should be 'approved'; "
            f"got {body['field_status'].get('cross_data.md.a2')!r}. "
            "spec: BACKEND_SCHEMA.md L134"
        )
        # Shape-check: proposals round-trip with correct action types
        # spec: BACKEND.md §Cross-data MD action types
        actions_by_id = {a["action_id"]: a for a in body["proposals"]["cross_data.md"]}
        assert actions_by_id.get("a1", {}).get("action") == "modify", (
            f"Proposal a1 should have action='modify'; got {actions_by_id.get('a1')!r}. "
            "spec: BACKEND.md §Cross-data MD action types"
        )
        assert actions_by_id.get("a2", {}).get("action") == "delete", (
            f"Proposal a2 should have action='delete'; got {actions_by_id.get('a2')!r}. "
            "spec: BACKEND.md §Cross-data MD action types"
        )
    finally:
        if result_id is not None:
            await _delete_metagen_result(async_session, result_id, _TEST_URN)
        await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_review_preserves_prior_field_status_on_followup_patch(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session,
) -> None:
    """Follow-up PATCH only mutates listed fields; prior approved/pending entries stay intact.

    Seeds a five-field result, then issues two PATCHes:
      1. verdict=approve on three fields → those flip to 'approved'
      2. verdict=reject on one different field → only that field flips to 'rejected'

    Asserts that after the second PATCH:
      - the three fields approved by PATCH 1 are still 'approved' (not clobbered)
      - the one field never referenced is still 'pending'
      - the one field rejected by PATCH 2 is 'rejected'

    spec: BACKEND.md L296-L302 — field-level review preserves prior status for fields
    not referenced in the current PATCH.
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"

    result_id: str | None = None

    try:
        await api_client.put(
            base_conf,
            headers=admin_headers,
            json={
                "targets": ["dataset.description", "column.description", "cross_data.md"],
                "is_enabled": False,
                "schedule_tier": None,
                "owner": "spot-test@imazon.com",
            },
        )

        result_id = await _insert_pending_metagen_result(
            async_session,
            dataset_urn=_TEST_URN,
            proposals={
                "dataset.description": "Master catalog of every title Imazon offers.",
                "column.description": {
                    "book_id": "Stable, opaque identifier for a book.",
                    "title": "Display title shown to customers.",
                    "author": "Free-text author / creator name.",
                },
                "cross_data.md": [
                    {
                        "action_id": "a1",
                        "action": "create",
                        "title": "How orders reference books",
                        "body": (
                            "`orders.order_items.book_id` joins to "
                            "`catalog.title_master.book_id`."
                        ),
                        "related_assets": [
                            "urn:li:dataset:(urn:li:dataPlatform:postgres,"
                            "example_db.catalog.title_master,DEV)",
                        ],
                        "confidence": 0.81,
                    },
                ],
            },
            field_status={
                "dataset.description": "pending",
                "column.description.book_id": "pending",
                "column.description.title": "pending",
                "column.description.author": "pending",
                "cross_data.md.a1": "pending",
            },
        )
        encoded_result_id = urllib.parse.quote(result_id, safe="")
        patch_url = (
            f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/result/{encoded_result_id}"
        )

        # PATCH 1: approve a subset of three fields
        approve_resp = await api_client.patch(
            patch_url,
            headers=admin_headers,
            json={
                "verdict": "approve",
                "fields": [
                    "dataset.description",
                    "column.description.book_id",
                    "column.description.title",
                ],
                "reason": "spot-test: approve three fields",
            },
        )
        assert approve_resp.status_code == 200, approve_resp.text

        # PATCH 2: reject a different field (cross_data.md.a1)
        # spec: BACKEND.md L296-L302 — second PATCH must not clobber the first PATCH's approvals
        reject_resp = await api_client.patch(
            patch_url,
            headers=admin_headers,
            json={
                "verdict": "reject",
                "fields": ["cross_data.md.a1"],
                "reason": "spot-test: reject one cross_data action",
            },
        )
        assert reject_resp.status_code == 200, reject_resp.text
        fs = reject_resp.json()["field_status"]

        # The rejected field flips to 'rejected'
        assert fs.get("cross_data.md.a1") == "rejected", (
            f"'cross_data.md.a1' should be 'rejected' after second PATCH; "
            f"got {fs.get('cross_data.md.a1')!r}. spec: BACKEND.md L296-L302"
        )
        # Previously-approved fields stay 'approved' — second PATCH must not clobber them
        for field in (
            "dataset.description",
            "column.description.book_id",
            "column.description.title",
        ):
            assert fs.get(field) == "approved", (
                f"{field!r} should remain 'approved' after follow-up PATCH; "
                f"got {fs.get(field)!r}. spec: BACKEND.md L296-L302"
            )
        # Never-touched field stays 'pending'
        assert fs.get("column.description.author") == "pending", (
            f"'column.description.author' should remain 'pending' (never referenced); "
            f"got {fs.get('column.description.author')!r}. spec: BACKEND.md L296-L302"
        )
    finally:
        if result_id is not None:
            await _delete_metagen_result(async_session, result_id, _TEST_URN)
        await api_client.delete(base_conf, headers=admin_headers)
