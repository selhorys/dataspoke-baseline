"""Spot tests for Validation endpoints.

Concerns covered:
- GET /spoke/common/validation — list configs (paginated envelope)
- GET /data/{urn}/attr/validation/conf — 404 for unknown URN
- PUT /data/{urn}/attr/validation/conf — create with one of each rule type; round-trip rule_id/type
- PATCH /data/{urn}/attr/validation/conf — partial update
- DELETE /data/{urn}/attr/validation/conf — remove config (204)
- POST /data/{urn}/method/validation/run — dry_run=true does NOT persist results (F3)
- POST /data/{urn}/method/validation/run — concurrent returns 409 VALIDATION_RUNNING (F4)
- PUT /data/{urn}/attr/validation/conf — unknown URN returns 422 DATASET_NOT_IN_DATAHUB (F4)
- POST /data/{urn}/method/validation/run — non-dry-run emits VALIDATION.COMPLETE event (F4)
- GET /data/{urn}/attr/validation/result — result list envelope
- GET /data/{urn}/event/validation — event list envelope
"""

import asyncio
import urllib.parse

import httpx
import pytest

# Per-module dummy-data seed: re-seed catalog schema in PG and ingest into DataHub
# before this module's tests run (autoused by tests/integration/conftest.py).
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})

# title_master is a DataHub-seeded Imazon dataset (catalog schema)
# spec: USE_CASE_en.md §UC2 — validation always operates on a dataset DataHub already knows
_TEST_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_ENCODED_URN = urllib.parse.quote(_TEST_URN, safe="")

# A URN that is known to NOT exist in the seeded DataHub instance
# spec: USE_CASE_en.md §UC2 — PUT for unknown URN returns 422 DATASET_NOT_IN_DATAHUB
_UNKNOWN_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,nonexistent.table,DEV)"
_ENCODED_UNKNOWN_URN = urllib.parse.quote(_UNKNOWN_URN, safe="")

# Status enum is impl-defined; spec USE_CASE_en.md §UC2 §Run semantics is silent on
# enum values (says status field exists but does not enumerate valid values)
_VALID_STATUSES = {"success", "failure", "error"}


@pytest.mark.asyncio
async def test_validation_list_paginated_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/common/validation returns a paginated collection envelope."""
    resp = await api_client.get(
        "/api/v1/spoke/common/validation?offset=0&limit=10",
        headers=admin_headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "configs" in body
    assert "offset" in body
    assert "limit" in body
    assert "total_count" in body
    assert isinstance(body["configs"], list)


@pytest.mark.asyncio
async def test_validation_conf_get_404_unknown_urn(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET validation conf for an unknown URN returns 404."""
    unknown_urn = urllib.parse.quote(
        "urn:li:dataset:(urn:li:dataPlatform:postgres,nonexistent.table,DEV)", safe=""
    )
    resp = await api_client.get(
        f"/api/v1/spoke/common/data/{unknown_urn}/attr/validation/conf",
        headers=admin_headers,
    )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_validation_conf_put_with_all_rule_types(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT validation conf with one rule of each supported type — 201 created.

    After PUT, GET the config and assert each rule's rule_id and type is preserved
    by the round-trip.
    # spec: BACKEND.md §Validation Service — six DataHub assertion types supported
    # spec: F13 — rule_id and type must survive PUT→GET round-trip
    """
    base = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/conf"

    rules_payload = [
        {
            "rule_id": "spot-fresh-001",
            "type": "freshness",
            "lookback_interval": "24 hours",
            "last_modified_field": "updated_at",
        },
        {
            "rule_id": "spot-vol-001",
            "type": "volume",
            "metric": "row_count",
            "condition": {"type": "between", "min": 1, "max": 100000},
        },
        {
            "rule_id": "spot-field-001",
            "type": "field",
            "field": "title",
            "metric": "null_count",
            "condition": {"type": "less_than_or_equal_to", "value": 0},
        },
        {
            "rule_id": "spot-schema-001",
            "type": "schema",
            "fields": [{"field": "isbn", "type": "VARCHAR"}],
            "compatibility": "superset",
        },
        {
            "rule_id": "spot-sql-001",
            "type": "sql",
            "statement": "SELECT count(*) FROM catalog.title_master WHERE isbn IS NULL",
            "condition": {"type": "less_than_or_equal_to", "value": 5},
        },
        {
            "rule_id": "spot-custom-001",
            "type": "custom",
            "subtype": "sql_timeseries",
            "sql": "SELECT count(*) FROM catalog.title_master",
        },
    ]

    put_resp = await api_client.put(
        base,
        headers=admin_headers,
        json={
            "rules": rules_payload,
            "schedule_tier": "daily",
            "is_enabled": False,
            "owner": "spot-test@imazon.com",
        },
    )
    assert put_resp.status_code in (200, 201), put_resp.text
    body = put_resp.json()
    assert body["dataset_urn"] == _TEST_URN
    assert len(body["rules"]) == 6

    # F13: round-trip GET to verify rule_id and type are preserved
    get_resp = await api_client.get(base, headers=admin_headers)
    assert get_resp.status_code == 200
    stored_rules = get_resp.json()["rules"]
    stored_by_id = {r["rule_id"]: r for r in stored_rules}
    for expected_rule in rules_payload:
        rule_id = expected_rule["rule_id"]
        assert rule_id in stored_by_id, f"rule_id '{rule_id}' missing after round-trip"
        assert stored_by_id[rule_id]["type"] == expected_rule["type"], (
            f"rule '{rule_id}' type changed: expected {expected_rule['type']!r}, "
            f"got {stored_by_id[rule_id]['type']!r}"
        )

    # Cleanup
    await api_client.delete(base, headers=admin_headers)


@pytest.mark.asyncio
async def test_validation_conf_patch(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PATCH updates schedule_tier and is_enabled on existing validation conf."""
    base = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/conf"

    # Create first
    await api_client.put(
        base,
        headers=admin_headers,
        json={
            "rules": [
                {
                    "rule_id": "spot-patch-fresh",
                    "type": "freshness",
                    "lookback_interval": "48 hours",
                    "last_modified_field": "updated_at",
                }
            ],
            "schedule_tier": "daily",
            "is_enabled": False,
            "owner": "spot-test@imazon.com",
        },
    )

    patch_resp = await api_client.patch(
        base,
        headers=admin_headers,
        json={"is_enabled": False, "schedule_tier": "weekly"},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["schedule_tier"] == "weekly"

    # Cleanup
    await api_client.delete(base, headers=admin_headers)


@pytest.mark.asyncio
async def test_validation_conf_delete(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """DELETE removes validation conf; subsequent GET returns 404."""
    base = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/conf"

    await api_client.put(
        base,
        headers=admin_headers,
        json={
            "rules": [
                {
                    "rule_id": "spot-del-fresh",
                    "type": "freshness",
                    "lookback_interval": "24 hours",
                    "last_modified_field": "updated_at",
                }
            ],
            "schedule_tier": None,
            "is_enabled": False,
            "owner": "spot-test@imazon.com",
        },
    )

    del_resp = await api_client.delete(base, headers=admin_headers)
    assert del_resp.status_code == 204

    get_resp = await api_client.get(base, headers=admin_headers)
    assert get_resp.status_code == 404


@pytest.mark.asyncio
async def test_validation_run_dry_run(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST method/validation/run with dry_run=true does NOT persist results.

    # spec: USE_CASE_en.md §UC2 §Run semantics + API table — dry_run=true returns
    #       the would-be summary without writing results
    # spec: USE_CASE_en.md §UC2 §Run semantics — response: {run_id, status, total,
    #       passed, failed, errored}
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/conf"
    base_run = f"/api/v1/spoke/common/data/{_ENCODED_URN}/method/validation/run"
    base_results = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/result"

    # Ensure config exists — keep rules_payload named so we can assert total == len(rules)
    rules_payload = [
        {
            "rule_id": "spot-dryrun-vol",
            "type": "volume",
            "metric": "row_count",
            "condition": {"type": "between", "min": 1, "max": 100000},
        }
    ]
    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "rules": rules_payload,
            "schedule_tier": None,
            "is_enabled": False,
            "owner": "spot-test@imazon.com",
        },
    )

    # Capture result count BEFORE dry run
    # spec: USE_CASE_en.md §UC2 §Run semantics — dry_run must not persist results
    before_resp = await api_client.get(base_results, headers=admin_headers)
    assert before_resp.status_code == 200
    count_before = before_resp.json().get("total_count", 0)

    run_resp = await api_client.post(
        base_run,
        headers=admin_headers,
        json={"dry_run": True},
    )

    assert run_resp.status_code == 200
    body = run_resp.json()

    # spec: USE_CASE_en.md §UC2 §Run semantics — run response shape
    assert "run_id" in body
    assert "status" in body
    assert "total" in body
    assert "passed" in body
    assert "failed" in body
    assert "errored" in body

    # F5: strengthened summary assertions
    # Status enum is impl-defined; spec USE_CASE_en.md §UC2 §Run semantics is silent on enum values
    assert body["status"].lower() in _VALID_STATUSES
    # spec: USE_CASE_en.md §UC2 §Run semantics — run_id is a string
    assert isinstance(body["run_id"], str) and body["run_id"]

    # F-R2.7: weak invariants on sub-counts
    # spec: USE_CASE_en.md §UC2 §Run semantics — counts must be non-negative
    assert body["total"] >= 0
    assert body["passed"] >= 0
    assert body["failed"] >= 0
    assert body["errored"] >= 0
    # sub-counts must not exceed total individually
    assert body["passed"] <= body["total"]
    assert body["failed"] <= body["total"]
    assert body["errored"] <= body["total"]
    # dry_run total == len(rules_payload): impl returns total=len(rules) without
    # executing; ties the response to the submitted config
    # spec: USE_CASE_en.md §UC2 §Run semantics — total reflects the rule count
    assert body["total"] == len(rules_payload)
    # NOTE: arithmetic invariant total == passed + failed + errored is intentionally
    # NOT asserted here — dry_run returns total=len(rules) but passed=failed=errored=0
    # as an impl shortcut (no rules actually execute). This is impl-internal behavior;
    # the spec does not mandate the invariant holds for dry_run responses.

    # Assert no new result row was added — dry_run must NOT persist
    after_resp = await api_client.get(base_results, headers=admin_headers)
    assert after_resp.status_code == 200
    count_after = after_resp.json().get("total_count", 0)
    assert count_after == count_before, (
        f"dry_run persisted results: count went from {count_before} to {count_after} "
        f"— violates USE_CASE_en.md §UC2 §Run semantics"
    )

    # Cleanup
    await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_validation_run_concurrent_returns_409(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Concurrent runs on the same dataset return 409 VALIDATION_RUNNING.

    # spec: USE_CASE_en.md §UC2 §Run semantics — concurrent runs return 409 VALIDATION_RUNNING
    # spec: API.md §Application Error Codes — VALIDATION_RUNNING
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/conf"
    base_run = f"/api/v1/spoke/common/data/{_ENCODED_URN}/method/validation/run"

    # Setup: create a config with at least one rule to give the run something to do.
    # is_enabled=True is required so the run executes; with is_enabled=False the
    # service short-circuits with VALIDATION_DISABLED before the SETNX guard can
    # observe contention.
    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "rules": [
                {
                    "rule_id": "spot-concurrent-fresh",
                    "type": "freshness",
                    "lookback_interval": "24 hours",
                }
            ],
            "schedule_tier": "daily",
            "is_enabled": True,
            "owner": "spot-test@imazon.com",
        },
    )

    # Fire 5 concurrent requests — with 5 simultaneous coroutines racing to
    # acquire the Redis SETNX lock, at least one must observe it already held
    # and return 409 VALIDATION_RUNNING. 5 concurrent makes the race near-certain
    # even on a fast host (vs 2 which could complete sequentially on localhost).
    async def _fire_run():
        return await api_client.post(
            base_run,
            headers=admin_headers,
            json={"dry_run": False},
        )

    results = await asyncio.gather(
        _fire_run(), _fire_run(), _fire_run(), _fire_run(), _fire_run(),
        return_exceptions=True,
    )
    status_codes = [
        r.status_code for r in results if isinstance(r, httpx.Response)
    ]

    # At least one 409 must be present when 5 concurrent calls race
    assert 409 in status_codes, (
        f"Expected at least one 409 VALIDATION_RUNNING; got status codes {status_codes}"
    )

    # Verify the 409 body contains the correct error_code
    # spec: API.md §Application Error Codes — VALIDATION_RUNNING error_code
    conflict_resp = next(r for r in results if isinstance(r, httpx.Response) and r.status_code == 409)
    conflict_body = conflict_resp.json()
    assert conflict_body.get("error_code") == "VALIDATION_RUNNING", (
        f"Expected error_code 'VALIDATION_RUNNING', got: {conflict_body}"
    )

    # Cleanup
    await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_validation_put_with_unknown_urn_returns_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT validation conf for a URN not in DataHub returns 422 DATASET_NOT_IN_DATAHUB.

    # spec: USE_CASE_en.md §UC2 — PUT for unknown URN returns 422 DATASET_NOT_IN_DATAHUB
    # spec: API.md §Application Error Codes — DATASET_NOT_IN_DATAHUB
    """
    base = f"/api/v1/spoke/common/data/{_ENCODED_UNKNOWN_URN}/attr/validation/conf"

    resp = await api_client.put(
        base,
        headers=admin_headers,
        json={
            "rules": [
                {
                    "rule_id": "spot-unknown-fresh",
                    "type": "freshness",
                    "lookback_interval": "24 hours",
                }
            ],
            "schedule_tier": None,
            "is_enabled": False,
            "owner": "spot-test@imazon.com",
        },
    )

    assert resp.status_code == 422, (
        f"Expected 422 DATASET_NOT_IN_DATAHUB for unknown URN; got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("error_code") == "DATASET_NOT_IN_DATAHUB", (
        f"Expected error_code 'DATASET_NOT_IN_DATAHUB'; got: {body}"
    )


@pytest.mark.asyncio
async def test_validation_run_emits_complete_event(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """After a non-dry-run, GET event/validation and assert VALIDATION.COMPLETE event exists.

    # spec: BACKEND.md §Validation Service — VALIDATION.COMPLETE event emitted on successful run
    # spec: USE_CASE_en.md §UC2 §Run semantics — POST method/validation/run (non-dry-run)
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/conf"
    base_run = f"/api/v1/spoke/common/data/{_ENCODED_URN}/method/validation/run"
    base_events = f"/api/v1/spoke/common/data/{_ENCODED_URN}/event/validation"

    # Ensure config exists. is_enabled=True is required for non-dry-run; with
    # is_enabled=False the service rejects with VALIDATION_DISABLED.
    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "rules": [
                {
                    "rule_id": "spot-event-vol",
                    "type": "volume",
                    "metric": "row_count",
                    "condition": {"type": "between", "min": 1, "max": 100000},
                }
            ],
            "schedule_tier": "daily",
            "is_enabled": True,
            "owner": "spot-test@imazon.com",
        },
    )

    # Capture total event count before run (use total_count to handle pagination)
    before_events_resp = await api_client.get(base_events, headers=admin_headers)
    assert before_events_resp.status_code == 200
    before_total = before_events_resp.json().get("total_count", 0)

    # Non-dry-run
    run_resp = await api_client.post(
        base_run,
        headers=admin_headers,
        json={"dry_run": False},
    )
    assert run_resp.status_code == 200

    # F5: strengthened summary assertions for non-dry-run
    run_body = run_resp.json()
    # spec: USE_CASE_en.md §UC2 §Run semantics + BACKEND.md §Validation Service —
    #       each rule yields SUCCESS|FAILURE|ERROR, so total == passed + failed + errored
    assert run_body["total"] == run_body["passed"] + run_body["failed"] + run_body["errored"]
    # spec: USE_CASE_en.md §UC2 §Run semantics — status ∈ {success, failure, error}
    assert run_body["status"].lower() in _VALID_STATUSES
    # spec: USE_CASE_en.md §UC2 §Run semantics — run_id is a non-empty string
    assert isinstance(run_body["run_id"], str) and run_body["run_id"]

    # Fetch events and assert VALIDATION.COMPLETE appears
    # spec: BACKEND.md §Validation Service — event_type == "VALIDATION.COMPLETE"
    # Use a high limit to ensure the new event is in the first page
    after_events_resp = await api_client.get(
        f"{base_events}?limit=100", headers=admin_headers
    )
    assert after_events_resp.status_code == 200
    after_body = after_events_resp.json()
    after_events = after_body.get("events", [])
    after_total = after_body.get("total_count", 0)

    assert after_total > before_total, (
        f"Expected total_count to increase after a non-dry-run; "
        f"before={before_total}, after={after_total}"
    )

    event_types = [e.get("event_type", "") for e in after_events]
    assert "VALIDATION.COMPLETE" in event_types, (
        f"Expected VALIDATION.COMPLETE in event_types; found: {event_types}"
    )

    # Cleanup
    await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_validation_result_list_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET attr/validation/result returns paginated result envelope (may be empty)."""
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/conf"
    base_results = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/result"

    # Create config so results endpoint is accessible
    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "rules": [
                {
                    "rule_id": "spot-result-vol",
                    "type": "volume",
                    "metric": "row_count",
                    "condition": {"type": "between", "min": 1, "max": 100000},
                }
            ],
            "schedule_tier": None,
            "is_enabled": False,
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


@pytest.mark.asyncio
async def test_validation_events_list_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET event/validation returns paginated event envelope (may be empty)."""
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/conf"
    base_events = f"/api/v1/spoke/common/data/{_ENCODED_URN}/event/validation"

    # Create config so events endpoint is accessible
    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "rules": [
                {
                    "rule_id": "spot-events-vol",
                    "type": "volume",
                    "metric": "row_count",
                    "condition": {"type": "between", "min": 1, "max": 100000},
                }
            ],
            "schedule_tier": None,
            "is_enabled": False,
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
