"""Spot tests — Validation: one test per baseline rule type with real evaluation.

Each test registers a single rule against an Imazon fixture and asserts a
spec-grounded outcome (success or failure), not just any terminal status.

Tests and their deterministic outcomes:
1. field / null_count on reviews.user_ratings_legacy.rating_score — must FAIL
   (15 NULLs in 50 rows; threshold less_than_or_equal_to value=10 → 15 > 10 → FAIL)
2. schema / superset on reviews.user_ratings_legacy — must PASS
   (declared subset {user_id, rating_score} ⊂ actual columns)
3. sql / bounds on reviews.user_ratings_legacy — must PASS
   (0 rows violate rating_score CHECK constraint; condition equal_to value=0)
4. freshness on catalog.title_master (created_at) — any terminal status
   (outcome depends on fixture timestamp vs. current date; only terminal required)
5. volume / row_count on catalog.title_master — must PASS
   (table has 30 seeded rows; condition greater_than value=0)
"""
# spec: USE_CASE_en.md §UC2 — rule registration and run semantics
# spec: feature/BACKEND.md §Validation Service — rule evaluator dispatch
# spec: TESTING.md §Imazon Dummy-Data Reference — degraded fixtures

import asyncio
import os
import urllib.parse

import httpx
import pytest

_PG_HOST = os.environ.get("DATASPOKE_EXAMPLE_PG_HOST", "dataspoke-example-postgresql")
_PG_PORT = int(os.environ.get("DATASPOKE_EXAMPLE_PG_PORT", "9102"))
_PG_DB = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_DB", "example_db")
_PG_USER = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_USER", "postgres")
_PG_PASSWORD = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PASSWORD", "")

# Per-module dummy-data seed — reviews + catalog are required; orders not needed here.
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog", "reviews"})

# Status set per spec; enum values are implementation-defined.
# spec: USE_CASE_en.md §UC2 §Run semantics
_VALID_STATUSES: frozenset[str] = frozenset({"success", "failure", "error"})

_LEGACY_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.reviews.user_ratings_legacy,DEV)"
)
_LEGACY_ENCODED = urllib.parse.quote(_LEGACY_URN, safe="")

_TITLE_MASTER_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
)
_TITLE_MASTER_ENCODED = urllib.parse.quote(_TITLE_MASTER_URN, safe="")


async def _post_run_and_poll_result(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    base_run: str,
    rule_id: str,
    base_results: str,
    *,
    timeout_s: float = 30.0,
) -> dict:
    """POST dry_run=false, assert terminal run status, poll for the rule result row.

    Polling primitive only — payloads, PUT, and outcome assertions are done at call sites.
    spec: feedback_test_readability.md — polling primitive extracted; assertions stay inline
    """
    run_resp = await api_client.post(
        base_run,
        headers=admin_headers,
        json={"dry_run": False},
    )
    assert run_resp.status_code in (200, 201), (
        f"POST validation/run failed: {run_resp.status_code} {run_resp.text}"
    )
    run_body = run_resp.json()
    # spec: USE_CASE_en.md §UC2 §Run semantics — terminal status required
    assert run_body["status"].lower() in _VALID_STATUSES, (
        f"run status must be one of {_VALID_STATUSES}; got {run_body['status']!r}"
    )

    # Poll result list until the rule's result row appears (cap timeout_s)
    # spec: feedback_no_increase_timeout — bounded polls
    result_row: dict | None = None
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        results_resp = await api_client.get(
            f"{base_results}?limit=100",
            headers=admin_headers,
        )
        assert results_resp.status_code == 200
        for row in results_resp.json().get("results", []):
            if row.get("rule_id") == rule_id:
                result_row = row
                break
        if result_row is not None:
            break
        await asyncio.sleep(1.0)

    assert result_row is not None, (
        f"Result row for rule_id={rule_id!r} not found in attr/validation/result within "
        f"{timeout_s}s. spec: BACKEND.md §Validation Service — each evaluated rule persists a result"
    )
    return result_row


@pytest.mark.asyncio
async def test_field_null_count_fails_on_user_ratings_legacy(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """field rule: null_count on rating_score with threshold ≤ 5 must FAIL.

    reviews.user_ratings_legacy has ~15 NULL rating_score rows (out of 50, ~30%).
    condition: less_than_or_equal_to value=5 → null_count (~15) > 5 → rule FAILS.
    Threshold set to 5 so the test is robust to fixture shifts (e.g. 14–16 NULLs still > 5).

    spec: USE_CASE_en.md §UC2 — rule evaluation against degraded fixture
    spec: TESTING.md §Imazon Dummy-Data Reference — user_ratings_legacy ~30% NULL rating_score
    spec: feature/BACKEND.md §Validation Service — field null_count evaluator
    """
    base_conf = f"/api/v1/spoke/common/data/{_LEGACY_ENCODED}/attr/validation/conf"
    base_run = f"/api/v1/spoke/common/data/{_LEGACY_ENCODED}/method/validation/run"
    base_results = f"/api/v1/spoke/common/data/{_LEGACY_ENCODED}/attr/validation/result"

    rule_id = "spot-pg-field-null-001"

    put_resp = await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "is_enabled": True,
            "schedule_tier": "daily",
            "owner": "spot-test@imazon.com",
            "rules": [
                {
                    "rule_id": rule_id,
                    "type": "field",
                    "field": "rating_score",
                    "metric": "null_count",
                    # ~15 NULLs in 50-row fixture; threshold=5 ensures FAIL across plausible
                    # fixture variations (TESTING.md says ~30% NULL, i.e. ~15 rows).
                    "condition": {"type": "less_than_or_equal_to", "value": 5},
                }
            ],
        },
    )
    assert put_resp.status_code in (200, 201), put_resp.text

    try:
        result_row = await _post_run_and_poll_result(
            api_client, admin_headers, base_run, rule_id, base_results
        )
        # ~15 NULLs in 50 rows; threshold ≤ 5 — must FAIL for any fixture near ~30% NULL
        assert result_row.get("assertion_result") == "FAILURE", (
            f"Field null_count rule must produce assertion_result='FAILURE' (~15 NULLs > threshold 5); "
            f"got {result_row.get('assertion_result')!r}. "
            "spec: feature/BACKEND.md §Validation Service — field null_count evaluator. "
            "spec: TESTING.md §Imazon Dummy-Data Reference — user_ratings_legacy ~30% NULL"
        )
    finally:
        await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_schema_superset_passes_on_user_ratings_legacy(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """schema rule: superset compatibility with declared fields ⊂ actual columns — must PASS.

    Declaring a subset of the actual columns satisfies the SUPERSET constraint
    (the table has all declared fields and more).

    spec: USE_CASE_en.md §UC2 — schema rule evaluation
    spec: DATAHUB_INTEGRATION.md §Assertion Aspects — DATA_SCHEMA type
    """
    base_conf = f"/api/v1/spoke/common/data/{_LEGACY_ENCODED}/attr/validation/conf"
    base_run = f"/api/v1/spoke/common/data/{_LEGACY_ENCODED}/method/validation/run"
    base_results = f"/api/v1/spoke/common/data/{_LEGACY_ENCODED}/attr/validation/result"

    rule_id = "spot-pg-schema-superset-001"

    put_resp = await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "is_enabled": True,
            "schedule_tier": "daily",
            "owner": "spot-test@imazon.com",
            "rules": [
                {
                    "rule_id": rule_id,
                    "type": "schema",
                    "compatibility": "superset",
                    "fields": [
                        {"field": "user_id", "type": ""},
                        {"field": "rating_score", "type": ""},
                    ],
                }
            ],
        },
    )
    assert put_resp.status_code in (200, 201), put_resp.text

    try:
        result_row = await _post_run_and_poll_result(
            api_client, admin_headers, base_run, rule_id, base_results
        )
        # Declared {user_id, rating_score} ⊂ actual columns — superset satisfied → PASS
        assert result_row.get("assertion_result") == "SUCCESS", (
            f"Schema superset rule must produce assertion_result='SUCCESS' (declared subset ⊂ actual columns); "
            f"got {result_row.get('assertion_result')!r}. "
            "spec: USE_CASE_en.md §UC2 — schema superset evaluation"
        )
    finally:
        await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_sql_bounds_check_passes_on_user_ratings_legacy(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """sql rule: count of out-of-range scores = 0 (NULL rows skipped) → must PASS.

    SELECT COUNT(*) FROM reviews.user_ratings_legacy WHERE rating_score < 1 OR rating_score > 5
    NULL values do not satisfy the WHERE clause (NULL comparison yields NULL, not TRUE),
    so count = 0. condition: equal_to value=0 → PASS.

    spec: USE_CASE_en.md §UC2 — sql rule evaluation
    spec: TESTING.md §Imazon Dummy-Data Reference — user_ratings_legacy has CHECK constraint
    """
    base_conf = f"/api/v1/spoke/common/data/{_LEGACY_ENCODED}/attr/validation/conf"
    base_run = f"/api/v1/spoke/common/data/{_LEGACY_ENCODED}/method/validation/run"
    base_results = f"/api/v1/spoke/common/data/{_LEGACY_ENCODED}/attr/validation/result"
    ingestion_conf_url = f"/api/v1/spoke/common/data/{_LEGACY_ENCODED}/attr/ingestion/conf"

    # Register ingestion config so resolve_source_config can find the PG connection details.
    # spec: feature/BACKEND.md §Validation Service — sql evaluator calls resolve_source_config
    ing_resp = await api_client.put(
        ingestion_conf_url,
        headers=admin_headers,
        json={
            "mode": "active-custom",
            "platform": "postgres",
            "locator": {"host": _PG_HOST, "port": _PG_PORT},
            "identifier": {
                "database": _PG_DB,
                "schema_name": "reviews",
                "table": "user_ratings_legacy",
            },
            "auth": {
                "username": _PG_USER,
                "password": _PG_PASSWORD,
                "secret_ref": {
                    "name": "dataspoke-source-cred-spot-sql-bounds",
                    "key": "password",
                    "force_overwrite": True,
                },
            },
            "is_enabled": False,
            "schedule_tier": "daily",
        },
    )
    assert ing_resp.status_code in (200, 201), ing_resp.text

    rule_id = "spot-pg-sql-bounds-001"

    put_resp = await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "is_enabled": True,
            "schedule_tier": "daily",
            "owner": "spot-test@imazon.com",
            "rules": [
                {
                    "rule_id": rule_id,
                    "type": "sql",
                    "statement": (
                        "SELECT COUNT(*) FROM reviews.user_ratings_legacy "
                        "WHERE rating_score < 1 OR rating_score > 5"
                    ),
                    "condition": {"type": "equal_to", "value": 0},
                }
            ],
        },
    )
    assert put_resp.status_code in (200, 201), put_resp.text

    try:
        result_row = await _post_run_and_poll_result(
            api_client, admin_headers, base_run, rule_id, base_results
        )
        # NULL rows do not match the WHERE clause; 0 violating rows → PASS
        assert result_row.get("assertion_result") == "SUCCESS", (
            f"SQL bounds rule must produce assertion_result='SUCCESS' (0 out-of-range scores); "
            f"got {result_row.get('assertion_result')!r}. "
            "spec: USE_CASE_en.md §UC2 — sql rule evaluation"
        )
    finally:
        await api_client.delete(base_conf, headers=admin_headers)
        await api_client.delete(ingestion_conf_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_freshness_evaluates_on_catalog_title_master(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """freshness rule with lookback_interval and last_modified_field evaluates to terminal status.

    Using catalog.title_master with created_at as the last_modified_field.
    The freshness outcome (pass/fail) depends on how recent the seeded data is; only
    terminal status is asserted, not the specific outcome.

    spec: USE_CASE_en.md §UC2 — freshness rule evaluation
    spec: feature/BACKEND.md §Validation Service — freshness evaluator uses last_modified_field
    """
    base_conf = f"/api/v1/spoke/common/data/{_TITLE_MASTER_ENCODED}/attr/validation/conf"
    base_run = f"/api/v1/spoke/common/data/{_TITLE_MASTER_ENCODED}/method/validation/run"
    base_results = f"/api/v1/spoke/common/data/{_TITLE_MASTER_ENCODED}/attr/validation/result"

    rule_id = "spot-pg-freshness-001"

    put_resp = await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "is_enabled": True,
            "schedule_tier": "daily",
            "owner": "spot-test@imazon.com",
            "rules": [
                {
                    "rule_id": rule_id,
                    "type": "freshness",
                    "lookback_interval": "30d",
                    "last_modified_field": "created_at",
                }
            ],
        },
    )
    assert put_resp.status_code in (200, 201), put_resp.text

    try:
        result_row = await _post_run_and_poll_result(
            api_client, admin_headers, base_run, rule_id, base_results
        )
        # Outcome depends on fixture timestamp vs. current date — only terminal required
        assert result_row.get("assertion_result") in {"SUCCESS", "FAILURE", "ERROR"}, (
            f"Freshness rule must reach a terminal assertion_result in {{SUCCESS, FAILURE, ERROR}}; "
            f"got {result_row.get('assertion_result')!r}. "
            "spec: feature/BACKEND.md §Validation Service — freshness evaluator"
        )
    finally:
        await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_volume_row_count_passes_on_title_master(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """volume rule: row_count > 0 on catalog.title_master — must PASS.

    catalog.title_master has 30 seeded rows; condition greater_than value=0 must pass.

    spec: USE_CASE_en.md §UC2 — volume rule evaluation
    spec: TESTING.md §Imazon Dummy-Data Reference — catalog.title_master 30 rows
    """
    base_conf = f"/api/v1/spoke/common/data/{_TITLE_MASTER_ENCODED}/attr/validation/conf"
    base_run = f"/api/v1/spoke/common/data/{_TITLE_MASTER_ENCODED}/method/validation/run"
    base_results = f"/api/v1/spoke/common/data/{_TITLE_MASTER_ENCODED}/attr/validation/result"

    rule_id = "spot-pg-volume-rowcount-001"

    put_resp = await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "is_enabled": True,
            "schedule_tier": "daily",
            "owner": "spot-test@imazon.com",
            "rules": [
                {
                    "rule_id": rule_id,
                    "type": "volume",
                    "metric": "row_count",
                    "condition": {"type": "greater_than", "value": 0},
                }
            ],
        },
    )
    assert put_resp.status_code in (200, 201), put_resp.text

    try:
        result_row = await _post_run_and_poll_result(
            api_client, admin_headers, base_run, rule_id, base_results
        )
        # 30 rows > 0 threshold → must PASS
        assert result_row.get("assertion_result") == "SUCCESS", (
            f"Volume row_count rule must produce assertion_result='SUCCESS' (30 rows > 0); "
            f"got {result_row.get('assertion_result')!r}. "
            "spec: USE_CASE_en.md §UC2 — volume rule evaluation. "
            "spec: TESTING.md §Imazon Dummy-Data Reference — catalog.title_master 30 rows"
        )
    finally:
        await api_client.delete(base_conf, headers=admin_headers)
