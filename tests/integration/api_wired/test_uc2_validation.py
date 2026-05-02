"""UC2 — Validation: end-to-end through public REST API.

Maps `spec/USE_CASE_en.md §UC2` paragraphs to executable steps. REST-only
per `spec/TESTING.md §Api-Wired Integration Tests`.

Tests in this module:
  - test_uc2_register_rules_run_and_history: Four rules (freshness, volume, field, custom),
    dry-run, real run (total == passed + failed + errored), concurrent 409 guard,
    historical result query, cross-dataset overview.
  - test_uc2_unknown_urn_returns_422: Negative path — PUT for URN absent from DataHub
    returns 422 DATASET_NOT_IN_DATAHUB.
"""
# spec: USE_CASE_en.md §UC2

import asyncio
import urllib.parse

import httpx
import pytest

# UC2 dataset: orders.order_items — Imazon multi-hop join table
# spec: TESTING.md §Imazon Dummy-Data Reference — orders.order_items is UC2 primary dataset
_TEST_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.orders.order_items,DEV)"
)
_ENCODED_URN = urllib.parse.quote(_TEST_URN, safe="")

# Unknown URN for negative-path test
# spec: USE_CASE_en.md §UC2 L183-L187 — PUT for unknown URN returns 422 DATASET_NOT_IN_DATAHUB
_UNKNOWN_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,nonexistent.table,DEV)"
_ENCODED_UNKNOWN_URN = urllib.parse.quote(_UNKNOWN_URN, safe="")

@pytest.mark.asyncio
async def test_uc2_register_rules_run_and_history(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """UC2 narrative: 'register rules per dataset, run them on schedule or on demand,
    dry-run them from a coding agent before shipping a pipeline, and query historical
    results, so that data quality is observable and verifiable without building bespoke
    checks.'

    Steps mirror USE_CASE_en.md §UC2:
      1. Register four rules (freshness, volume, field, custom/sql_timeseries)
      2. Dry-run — no result row written
      3. Real run — total == passed + failed + errored invariant
      4. Concurrent run guard — at least one 409 VALIDATION_RUNNING
      5. Historical result query — paginated envelope
      6. Cross-dataset overview — paginated envelope
      7. Cleanup — DELETE conf
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/conf"
    base_run = f"/api/v1/spoke/common/data/{_ENCODED_URN}/method/validation/run"
    base_results = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/validation/result"

    try:
        # ── Step 1: Register four rules ───────────────────────────────────────
        # UC2 narrative: "The orders team registers four rules on orders.line_items —
        # one per rule type the team needs."
        # spec: USE_CASE_en.md §UC2 L215-L234
        put_resp = await api_client.put(
            base_conf,
            headers=admin_headers,
            json={
                "is_enabled": False,
                "schedule_tier": "daily",
                "rules": [
                    {
                        "rule_id": "uc2-fresh-daily",
                        "type": "freshness",
                        "lookback_interval": "24 hours",
                        "last_modified_field": "updated_at",
                    },
                    {
                        "rule_id": "uc2-daily-volume",
                        "type": "volume",
                        "metric": "row_count",
                        "condition": {"type": "between", "min": 1, "max": 100000},
                    },
                    {
                        "rule_id": "uc2-qty-positive",
                        "type": "field",
                        "field": "quantity",
                        "metric": "null_count",
                        "condition": {"type": "less_than_or_equal_to", "value": 0},
                    },
                    {
                        "rule_id": "uc2-qty-anomaly",
                        "type": "custom",
                        "subtype": "sql_timeseries",
                        "sql": (
                            "SELECT count(*) FROM orders.order_items"
                            " WHERE event_date = :partition"
                        ),
                    },
                ],
            },
        )
        assert put_resp.status_code in (200, 201), (
            f"PUT validation conf failed: {put_resp.status_code} {put_resp.text}"
        )
        conf_body = put_resp.json()
        assert conf_body["dataset_urn"] == _TEST_URN
        assert len(conf_body["rules"]) == 4

        # Round-trip: verify rule_id and type survive PUT→GET
        # spec: USE_CASE_en.md §UC2 — rule_id and type are stable identifiers
        get_conf_resp = await api_client.get(base_conf, headers=admin_headers)
        assert get_conf_resp.status_code == 200
        stored_rules = get_conf_resp.json()["rules"]
        stored_by_id = {r["rule_id"]: r for r in stored_rules}
        for expected_id, expected_type in [
            ("uc2-fresh-daily", "freshness"),
            ("uc2-daily-volume", "volume"),
            ("uc2-qty-positive", "field"),
            ("uc2-qty-anomaly", "custom"),
        ]:
            assert expected_id in stored_by_id, f"rule_id {expected_id!r} missing after round-trip"
            assert stored_by_id[expected_id]["type"] == expected_type, (
                f"rule {expected_id!r} type changed: expected {expected_type!r}, "
                f"got {stored_by_id[expected_id]['type']!r}"
            )
        # Volume rule body shape (metric/condition) is impl-internal; spec example at L224-L226
        # uses {comparison, threshold, window, partition}. Round-trip on rule_id+type is the
        # spec-anchored invariant.
        # spec: USE_CASE_en.md §UC2 L218-L235 — custom rule retains subtype
        custom_stored = stored_by_id["uc2-qty-anomaly"]
        assert custom_stored["subtype"] == "sql_timeseries", (
            f"custom rule subtype expected 'sql_timeseries'; got {custom_stored.get('subtype')!r}. "
            "spec: USE_CASE_en.md §UC2 L218-L235"
        )

        # ── Step 2: Dry-run from coding agent ────────────────────────────────
        # UC2 narrative: "While a developer ships a new fulfillment pipeline, an AI
        # coding agent calls POST .../method/validation/run { 'dry_run': true } to
        # verify the rules pass against yesterday's data before merging."
        # spec: USE_CASE_en.md §UC2 L242-L246
        count_before_resp = await api_client.get(base_results, headers=admin_headers)
        assert count_before_resp.status_code == 200
        count_before = count_before_resp.json().get("total_count", 0)

        dry_run_resp = await api_client.post(
            base_run,
            headers=admin_headers,
            json={"dry_run": True},
        )
        assert dry_run_resp.status_code == 200, (
            f"POST dry-run failed: {dry_run_resp.status_code} {dry_run_resp.text}"
        )
        dry_body = dry_run_resp.json()
        # spec: USE_CASE_en.md §UC2 L196-L197 — run response shape
        assert "run_id" in dry_body
        assert "status" in dry_body
        assert "total" in dry_body
        assert "passed" in dry_body
        assert "failed" in dry_body
        assert "errored" in dry_body
        assert isinstance(dry_body["run_id"], str) and dry_body["run_id"]
        # spec: USE_CASE_en.md §UC2 L196-L197 — status key present; values not enumerated by spec
        assert isinstance(dry_body["status"], str) and dry_body["status"], (
            f"dry-run status must be a non-empty string; got {dry_body['status']!r}. "
            "spec: USE_CASE_en.md §UC2 L196-L197"
        )
        # spec: USE_CASE_en.md §UC2 L196-L197 — counts are non-negative and bounded by total
        assert dry_body["total"] >= 0
        assert dry_body["passed"] >= 0
        assert dry_body["failed"] >= 0
        assert dry_body["errored"] >= 0
        assert dry_body["passed"] <= dry_body["total"]
        assert dry_body["failed"] <= dry_body["total"]
        assert dry_body["errored"] <= dry_body["total"]

        # Dry-run must NOT persist results
        # spec: USE_CASE_en.md §UC2 L205 — dry_run=true: no result write
        count_after_dry_resp = await api_client.get(base_results, headers=admin_headers)
        assert count_after_dry_resp.status_code == 200
        count_after_dry = count_after_dry_resp.json().get("total_count", 0)
        assert count_after_dry == count_before, (
            f"dry_run persisted results: count went from {count_before} to {count_after_dry}. "
            "spec: USE_CASE_en.md §UC2 L205"
        )

        # ── Step 3: Real run — arithmetic invariant ───────────────────────────
        # UC2 narrative: "The daily Airflow validation DAG executes all four rules."
        # spec: USE_CASE_en.md §UC2 L196-L197 — total == passed + failed + errored
        real_run_resp = await api_client.post(
            base_run,
            headers=admin_headers,
            json={"dry_run": False},
        )
        assert real_run_resp.status_code == 200, (
            f"POST real run failed: {real_run_resp.status_code} {real_run_resp.text}"
        )
        real_body = real_run_resp.json()
        # spec: USE_CASE_en.md §UC2 L196-L197 — status key present; values not enumerated by spec
        assert isinstance(real_body["status"], str) and real_body["status"]
        assert isinstance(real_body["run_id"], str) and real_body["run_id"]
        # spec: USE_CASE_en.md §UC2 L197 — non-dry-run: total == passed + failed + errored
        total_check = real_body["passed"] + real_body["failed"] + real_body["errored"]
        assert real_body["total"] == total_check, (
            f"total ({real_body['total']}) != passed + failed + errored "
            f"({real_body['passed']} + {real_body['failed']} + {real_body['errored']}). "
            "spec: USE_CASE_en.md §UC2 L197"
        )

        # ── Step 4: Concurrent run guard ──────────────────────────────────────
        # UC2 narrative: "Runs are serialized per dataset: a duplicate method/run
        # while one is in flight returns 409 VALIDATION_RUNNING."
        # spec: USE_CASE_en.md §UC2 L195-L196
        async def _fire_run() -> httpx.Response:
            return await api_client.post(
                base_run,
                headers=admin_headers,
                json={"dry_run": False},
            )

        concurrent_results = await asyncio.gather(
            _fire_run(), _fire_run(), _fire_run(), _fire_run(), _fire_run(),
            return_exceptions=True,
        )
        status_codes = [
            r.status_code for r in concurrent_results if isinstance(r, httpx.Response)
        ]
        assert 409 in status_codes, (
            f"Expected at least one 409 VALIDATION_RUNNING; got status codes {status_codes}. "
            "spec: USE_CASE_en.md §UC2 L195-L196"
        )
        conflict_resp = next(
            r for r in concurrent_results
            if isinstance(r, httpx.Response) and r.status_code == 409
        )
        assert conflict_resp.json().get("error_code") == "VALIDATION_RUNNING", (
            f"Expected error_code 'VALIDATION_RUNNING'; got: {conflict_resp.json()}. "
            "spec: USE_CASE_en.md §UC2 L195-L196"
        )

        # ── Step 5: Historical result query ──────────────────────────────────
        # UC2 narrative: "A week later, an analyst checks last week's results:
        # GET .../attr/validation/result?from=…&to=…"
        # spec: USE_CASE_en.md §UC2 L248-L250
        hist_resp = await api_client.get(
            f"{base_results}?from=2026-01-01T00:00:00Z&to=2026-12-31T23:59:59Z",
            headers=admin_headers,
        )
        assert hist_resp.status_code == 200, (
            f"GET historical results failed: {hist_resp.status_code}"
        )
        hist_body = hist_resp.json()
        # spec: API.md §Standard Envelope
        assert "results" in hist_body
        assert "offset" in hist_body
        assert "limit" in hist_body
        assert "total_count" in hist_body
        assert isinstance(hist_body["results"], list)

        # ── Step 6: Cross-dataset overview ───────────────────────────────────
        # UC2 narrative: "Ops teams browse GET /spoke/common/validation to see
        # per-dataset latest pass/fail."
        # spec: USE_CASE_en.md §UC2 L252-L253
        overview_resp = await api_client.get(
            "/api/v1/spoke/common/validation?offset=0&limit=10",
            headers=admin_headers,
        )
        assert overview_resp.status_code == 200
        overview_body = overview_resp.json()
        assert "configs" in overview_body
        assert "total_count" in overview_body
        assert isinstance(overview_body["configs"], list)

    finally:
        # ── Step 7: Cleanup ───────────────────────────────────────────────────
        await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_uc2_unknown_urn_returns_422(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """UC2 negative path: PUT validation/conf for a URN not in DataHub returns
    422 DATASET_NOT_IN_DATAHUB.

    UC2 narrative: 'PUT validation/conf requires the dataset to already exist in
    DataHub — registering rules for a URN that DataHub doesn't track returns
    422 DATASET_NOT_IN_DATAHUB.'
    spec: USE_CASE_en.md §UC2 L183-L187
    """
    # UC2 narrative: "unlike ingestion (which can create the dataset), validation
    # always operates on a dataset DataHub already knows about; this keeps
    # validation aligned with DataHub-as-SSOT."
    # spec: USE_CASE_en.md §UC2 L183-L187
    resp = await api_client.put(
        f"/api/v1/spoke/common/data/{_ENCODED_UNKNOWN_URN}/attr/validation/conf",
        headers=admin_headers,
        json={
            "rules": [
                {
                    "rule_id": "uc2-neg-fresh",
                    "type": "freshness",
                    "lookback_interval": "24 hours",
                }
            ],
            "schedule_tier": None,
            "is_enabled": False,
        },
    )
    assert resp.status_code == 422, (
        f"Expected 422 DATASET_NOT_IN_DATAHUB for unknown URN; "
        f"got {resp.status_code}: {resp.text}. "
        "spec: USE_CASE_en.md §UC2 L183-L187"
    )
    body = resp.json()
    assert body.get("error_code") == "DATASET_NOT_IN_DATAHUB", (
        f"Expected error_code 'DATASET_NOT_IN_DATAHUB'; got: {body}. "
        "spec: USE_CASE_en.md §UC2 L183-L187"
    )
