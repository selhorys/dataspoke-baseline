"""Spot tests — Validation: miscellaneous edge cases.

Concerns covered:
- GET /spoke/common/validation — paginated collection envelope (seeded conf asserts >= 1)
- GET /data/{urn}/attr/validation/conf — 404 for unknown URN
- PUT /data/{urn}/attr/validation/conf for unknown URN → 422 DATASET_NOT_IN_DATAHUB
- POST method/validation/run — concurrent returns 409 VALIDATION_RUNNING
  (uses catalog.editions with a slow SQL rule to guarantee run duration)
- dry_run=true does NOT persist results
- Non-dry-run emits VALIDATION.COMPLETE event (uses catalog.title_master)
- GET /data/{urn}/attr/validation/result — result list envelope
- GET /data/{urn}/event/validation — event list envelope
"""
# spec: USE_CASE_en.md §UC2 §Run semantics
# spec: API.md §Standard Envelope, §Application Error Codes
# spec: BACKEND.md §Validation Service

import asyncio
import urllib.parse

import httpx
import pytest

# Per-module dummy-data seed — catalog schema triggers PG reset + DataHub ingest.
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})

# Status set per spec; enum values are implementation-defined.
# spec: USE_CASE_en.md §UC2 §Run semantics
_VALID_STATUSES: frozenset[str] = frozenset({"success", "failure", "error"})

# spec: TESTING.md §Imazon Dummy-Data Reference — catalog.title_master (UC2 dataset)
_TEST_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_ENCODED_URN = urllib.parse.quote(_TEST_URN, safe="")

# catalog.editions — distinct URN used only for the concurrent-409 test (F8: no cross-test interference)
# spec: TESTING.md §Imazon Dummy-Data Reference — catalog.editions
_EDITIONS_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"
_EDITIONS_ENCODED = urllib.parse.quote(_EDITIONS_URN, safe="")

# A URN that does NOT exist in the seeded DataHub instance
# spec: USE_CASE_en.md §UC2 — PUT for unknown URN returns 422 DATASET_NOT_IN_DATAHUB
_UNKNOWN_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,nonexistent.schema.table,DEV)"
_ENCODED_UNKNOWN_URN = urllib.parse.quote(_UNKNOWN_URN, safe="")


@pytest.mark.asyncio
async def test_validation_list_paginated_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/common/validation returns paginated envelope; seeded conf appears in list.

    Seeds 1 validation conf, verifies total_count >= 1 and the seeded URN is present.

    spec: API.md §Standard Envelope — configs[], total_count, offset, limit
    spec: BACKEND.md §Validation Service — list_configs paginates validation_configs rows
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/conf"

    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "rules": [
                {
                    "rule_id": "spot-misc-list-seed",
                    "type": "volume",
                    "metric": "row_count",
                    "condition": {"type": "greater_than", "value": 0},
                }
            ],
            "schedule_tier": None,
            "is_enabled": False,
            "owner": "spot-test@imazon.com",
        },
    )

    try:
        resp = await api_client.get(
            "/api/v1/spoke/common/validation?offset=0&limit=50",
            headers=admin_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "configs" in body
        assert "offset" in body
        assert "limit" in body
        assert "total_count" in body
        assert isinstance(body["configs"], list)
        assert body["total_count"] >= 1, (
            "Seeded 1 validation conf; total_count must be >= 1. "
            "spec: BACKEND.md §Validation Service — list_configs paginates validation_configs rows"
        )
        urns = [c.get("dataset_urn") for c in body["configs"]]
        assert _TEST_URN in urns, (
            f"Seeded URN {_TEST_URN!r} must appear in configs list; found: {urns}"
        )
    finally:
        await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_validation_conf_get_404_unknown_urn(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET validation conf for an unknown URN returns 404.

    spec: API.md §Error responses — 404 for not-found resource
    """
    unknown_urn = urllib.parse.quote(
        "urn:li:dataset:(urn:li:dataPlatform:postgres,nonexistent.table,DEV)", safe=""
    )
    resp = await api_client.get(
        f"/api/v1/spoke/common/data/{unknown_urn}/attr/validation/conf",
        headers=admin_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_validation_put_unknown_urn_returns_422_dataset_not_in_datahub(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT validation conf for a URN absent from DataHub returns 422 DATASET_NOT_IN_DATAHUB.

    spec: USE_CASE_en.md §UC2 lines 262-266 — PUT for unknown URN returns 422
    spec: API.md §Application Error Codes — DATASET_NOT_IN_DATAHUB
    """
    resp = await api_client.put(
        f"/api/v1/spoke/common/data/{_ENCODED_UNKNOWN_URN}/attr/validation/conf",
        headers=admin_headers,
        json={
            "rules": [
                {
                    "rule_id": "spot-misc-unknown-fresh",
                    "type": "freshness",
                    "lookback_interval": "24h",
                }
            ],
            "schedule_tier": None,
            "is_enabled": False,
            "owner": "spot-test@imazon.com",
        },
    )
    assert resp.status_code == 422, (
        f"Expected 422 DATASET_NOT_IN_DATAHUB for unknown URN; "
        f"got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body.get("error_code") == "DATASET_NOT_IN_DATAHUB", (
        f"Expected error_code='DATASET_NOT_IN_DATAHUB'; got {body}. "
        "spec: USE_CASE_en.md §UC2 lines 262-266"
    )


@pytest.mark.asyncio
async def test_validation_run_concurrent_returns_409_validation_running(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Concurrent runs on catalog.editions return 409 VALIDATION_RUNNING.

    Uses a SQL rule with pg_sleep(0.5) to guarantee run duration, making concurrent
    collision reliable. Uses catalog.editions (distinct URN from the event test) to
    avoid cross-test interference.

    spec: USE_CASE_en.md §UC2 §Run semantics — concurrent runs return 409 VALIDATION_RUNNING
    spec: API.md §Application Error Codes — VALIDATION_RUNNING
    """
    base_conf = f"/api/v1/spoke/common/data/{_EDITIONS_ENCODED}/attr/validation/conf"
    base_run = f"/api/v1/spoke/common/data/{_EDITIONS_ENCODED}/method/validation/run"

    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "rules": [
                {
                    "rule_id": "spot-misc-concurrent-slow-sql",
                    "type": "sql",
                    # pg_sleep(0.5) ensures the run takes long enough for 5 concurrent
                    # POST calls to collide; wrapping in SELECT 0 returns a countable value
                    "statement": "SELECT 0 FROM pg_sleep(0.5)",
                    "condition": {"type": "equal_to", "value": 0},
                }
            ],
            "schedule_tier": "daily",
            "is_enabled": True,
            "owner": "spot-test@imazon.com",
        },
    )

    async def _fire_run() -> httpx.Response:
        return await api_client.post(
            base_run,
            headers=admin_headers,
            json={"dry_run": False},
        )

    results = await asyncio.gather(
        _fire_run(), _fire_run(), _fire_run(), _fire_run(), _fire_run(),
        return_exceptions=True,
    )
    status_codes = [r.status_code for r in results if isinstance(r, httpx.Response)]

    assert 409 in status_codes, (
        f"Expected at least one 409 VALIDATION_RUNNING from 5 concurrent runs; "
        f"got status codes {status_codes}. "
        "spec: USE_CASE_en.md §UC2 §Run semantics"
    )

    conflict_resp = next(
        r for r in results if isinstance(r, httpx.Response) and r.status_code == 409
    )
    assert conflict_resp.json().get("error_code") == "VALIDATION_RUNNING", (
        f"Expected error_code='VALIDATION_RUNNING'; got {conflict_resp.json()}. "
        "spec: API.md §Application Error Codes"
    )

    await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_validation_run_dry_run_does_not_persist_results(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """dry_run=true returns run envelope but does NOT persist result rows.

    spec: USE_CASE_en.md §UC2 §Run semantics — dry_run=true returns would-be summary
    without writing results
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/conf"
    base_run = f"/api/v1/spoke/common/data/{_ENCODED_URN}/method/validation/run"
    base_results = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/result"

    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "rules": [
                {
                    "rule_id": "spot-misc-dryrun-vol",
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
    # spec: USE_CASE_en.md §UC2 §Run semantics — response shape
    assert "run_id" in body
    assert "status" in body
    assert "total" in body
    assert "passed" in body
    assert "failed" in body
    assert "errored" in body
    assert body["status"].lower() in _VALID_STATUSES
    assert isinstance(body["run_id"], str) and body["run_id"]

    # Results must not grow
    after_resp = await api_client.get(base_results, headers=admin_headers)
    assert after_resp.status_code == 200
    count_after = after_resp.json().get("total_count", 0)
    assert count_after == count_before, (
        f"dry_run persisted results: count went from {count_before} to {count_after}. "
        "spec: USE_CASE_en.md §UC2 §Run semantics — dry_run must NOT persist"
    )

    await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_validation_non_dry_run_emits_complete_event(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Non-dry-run on catalog.title_master emits VALIDATION.COMPLETE event.

    Uses catalog.title_master (distinct URN from the concurrent-409 test which uses
    catalog.editions) to prevent cross-test interference.

    spec: BACKEND.md §Validation Service — VALIDATION.COMPLETE event emitted on run
    spec: USE_CASE_en.md §UC2 §Run semantics — POST method/validation/run non-dry-run
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/conf"
    base_run = f"/api/v1/spoke/common/data/{_ENCODED_URN}/method/validation/run"
    base_events = f"/api/v1/spoke/common/data/{_ENCODED_URN}/event/validation"

    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "rules": [
                {
                    "rule_id": "spot-misc-event-vol",
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

    before_resp = await api_client.get(base_events, headers=admin_headers)
    assert before_resp.status_code == 200
    before_total = before_resp.json().get("total_count", 0)

    run_resp = await api_client.post(
        base_run,
        headers=admin_headers,
        json={"dry_run": False},
    )
    assert run_resp.status_code == 200
    run_body = run_resp.json()
    # spec: USE_CASE_en.md §UC2 §Run semantics — arithmetic invariant for non-dry-run
    assert run_body["total"] == run_body["passed"] + run_body["failed"] + run_body["errored"]
    assert run_body["status"].lower() in _VALID_STATUSES
    this_run_id = run_body.get("run_id")

    after_resp = await api_client.get(
        f"{base_events}?limit=100", headers=admin_headers
    )
    assert after_resp.status_code == 200
    after_body = after_resp.json()
    assert after_body.get("total_count", 0) > before_total, (
        "Expected total event count to increase after non-dry-run. "
        "spec: BACKEND.md §Validation Service — VALIDATION.COMPLETE event emitted"
    )
    # Filter by this run's run_id to prevent a stale event from a prior test on the same URN
    # from satisfying the assertion (cross-test isolation).
    # spec: BACKEND.md §Validation Service — VALIDATION.COMPLETE event emitted
    run_complete_events = [
        e for e in after_body.get("events", [])
        if e.get("event_type") == "VALIDATION.COMPLETE"
        and (this_run_id is None or (e.get("detail") or {}).get("run_id") == this_run_id)
    ]
    assert run_complete_events, (
        f"Expected at least one VALIDATION.COMPLETE event with run_id={this_run_id!r}; "
        f"events: {after_body.get('events', [])}. "
        "spec: BACKEND.md §Validation Service"
    )

    await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_validation_result_list_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET attr/validation/result returns paginated result envelope with required keys.

    spec: API.md §Standard Envelope — results[], total_count, offset, limit
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/conf"
    base_results = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/result"

    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "rules": [
                {
                    "rule_id": "spot-misc-result-vol",
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

    await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_validation_events_list_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET event/validation returns paginated event envelope with required keys.

    spec: API.md §Standard Envelope — events[], total_count, offset, limit
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/conf"
    base_events = f"/api/v1/spoke/common/data/{_ENCODED_URN}/event/validation"

    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "rules": [
                {
                    "rule_id": "spot-misc-events-vol",
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

    await api_client.delete(base_conf, headers=admin_headers)
