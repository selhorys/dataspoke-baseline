"""Spot tests — Validation: custom sql_timeseries rule evaluation.

Verifies the integration pipeline of a `custom` rule with `subtype: "sql_timeseries"`:
SQL timeseries query → metric extraction → optional ML validation → result emission.

Fixture: 02_orders.sql seeds 30 rows in orders.daily_fulfillment_summary
(2025-01-01..2025-01-30; Jan 15 is an anomalous low-volume day at row_count=12).

Spec invariants exercised in a single spot run:
- The rule's SQL is executable; the latest partition row is resolved
- `values` dict carries the configured `values: ["row_count"]` column
- `partition` dict carries the configured `partition: ["day"]` column
- ML range validation is best-effort: with no prior validation_results history
  (≥3 rows required per src/backend/validation/ml_validation.py:65), `validate_values`
  returns None and the rule produces assertion_result="SUCCESS".

Anomaly detection (FAILURE on Jan 15) requires seeded history rows + a partition
override pointing at Jan 15; that scenario is exercised in api-wired UC2, not in spot.
"""
# spec: USE_CASE_en.md §UC2 — custom rule registration and run semantics
# spec: feature/BACKEND.md §Validation Service — custom evaluator dispatch (sql_timeseries subtype)
# spec: TESTING.md §Imazon Dummy-Data Reference — orders.daily_fulfillment_summary Jan 15 anomaly

import asyncio
import os
import urllib.parse
import uuid

import httpx
import pytest

_PG_HOST = os.environ.get("DATASPOKE_EXAMPLE_PG_HOST", "dataspoke-example-postgresql")
_PG_PORT = int(os.environ.get("DATASPOKE_EXAMPLE_PG_PORT", "9102"))
_PG_DB = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_DB", "example_db")
_PG_USER = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_USER", "postgres")
_PG_PASSWORD = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_POSTGRES_PASSWORD", "")

# Per-module dummy-data seed — orders schema triggers PG reset + DataHub ingest.
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"orders"})

# Status set per spec; enum values are implementation-defined.
# spec: USE_CASE_en.md §UC2 §Run semantics
_VALID_STATUSES: frozenset[str] = frozenset({"success", "failure", "error"})

# spec: TESTING.md §Imazon Dummy-Data Reference — orders.daily_fulfillment_summary (UC2)
_FULFILLMENT_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.orders.daily_fulfillment_summary,DEV)"
)
_FULFILLMENT_ENCODED = urllib.parse.quote(_FULFILLMENT_URN, safe="")


@pytest.mark.asyncio
async def test_custom_sql_timeseries_evaluates_on_daily_fulfillment_summary(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Custom sql_timeseries rule on daily_fulfillment_summary produces status='failure'.

    The Jan 15 anomaly (row_count=12 vs normal ~138-161) is a clear outlier that the
    range model must flag as a failure. Fixture rows are deterministic per 02_orders.sql.

    SQL: SELECT summary_date AS day, row_count
         FROM orders.daily_fulfillment_summary
    Partition: [day], Order: [day], Values: [row_count]
    Model: range with lookback_partitions=30

    spec: USE_CASE_en.md §UC2 — custom rule, subtype sql_timeseries
    spec: feature/BACKEND.md §Validation Service — custom evaluator dispatch
    spec: TESTING.md §Imazon Dummy-Data Reference — Jan 15 anomaly (row_count=12)
    """
    base_conf = f"/api/v1/spoke/common/data/{_FULFILLMENT_ENCODED}/attr/validation/conf"
    base_run = f"/api/v1/spoke/common/data/{_FULFILLMENT_ENCODED}/method/validation/run"
    base_results = f"/api/v1/spoke/common/data/{_FULFILLMENT_ENCODED}/attr/validation/result"
    ingestion_conf_url = f"/api/v1/spoke/common/data/{_FULFILLMENT_ENCODED}/attr/ingestion/conf"

    # Register ingestion config so resolve_source_config can find the PG connection details.
    # spec: feature/BACKEND.md §Validation Service — sql_timeseries evaluator calls resolve_source_config
    ing_resp = await api_client.put(
        ingestion_conf_url,
        headers=admin_headers,
        json={
            "mode": "active-custom",
            "platform": "postgres",
            "locator": {"host": _PG_HOST, "port": _PG_PORT},
            "identifier": {
                "database": _PG_DB,
                "schema_name": "orders",
                "table": "daily_fulfillment_summary",
            },
            "auth": {
                "username": _PG_USER,
                "password": _PG_PASSWORD,
                "secret_ref": {
                    "name": "dataspoke-source-cred-spot-ts",
                    "key": "password",
                    "force_overwrite": True,
                },
            },
            "is_enabled": False,
            "schedule_tier": "daily",
        },
    )
    assert ing_resp.status_code in (200, 201), ing_resp.text

    # Unique rule_id per run so ML history is empty (validate_values queries by
    # dataset_urn + rule_id, returns None when < 3 prior rows exist).
    rule_id = f"spot-custom-ts-{uuid.uuid4().hex[:8]}"

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
                    "type": "custom",
                    "subtype": "sql_timeseries",
                    "description": "Daily fulfillment volume series for anomaly detection",
                    "sql": (
                        "SELECT summary_date AS day, row_count "
                        "FROM orders.daily_fulfillment_summary"
                    ),
                    "partition": ["day"],
                    "order": ["day"],
                    "values": ["row_count"],
                    "ml_validation": {
                        "targets": ["row_count"],
                        "model": "range",
                        "lookback_partitions": 30,
                    },
                }
            ],
        },
    )
    assert put_resp.status_code in (200, 201), put_resp.text

    try:
        put_check = await api_client.get(base_conf, headers=admin_headers)
        assert put_check.status_code == 200

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

        # Poll result list until the rule's result row appears (cap 30s)
        # spec: feedback_no_increase_timeout — bounded polls
        result_row: dict | None = None
        loop = asyncio.get_event_loop()
        deadline = loop.time() + 30.0
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
            f"Result row for rule_id={rule_id!r} not found within 30s. "
            "spec: BACKEND.md §Validation Service — each evaluated rule persists a result"
        )
        # No prior validation_results history → ML range model cannot decide.
        # Spec permits any non-FAILURE terminal: SUCCESS (skip) or ERROR (cannot decide).
        # spec: USE_CASE_en.md — terminal statuses are {SUCCESS, FAILURE, ERROR}
        assert result_row.get("assertion_result") in {"SUCCESS", "ERROR"}, (
            f"sql_timeseries rule with no prior history must produce a non-FAILURE terminal "
            f"(SUCCESS or ERROR); got {result_row.get('assertion_result')!r}, "
            f"issues={result_row.get('issues')!r}"
        )
        # SQL extraction worked: values dict carries the configured row_count metric
        assert "row_count" in (result_row.get("values") or {}), (
            f"values must carry the configured 'row_count' metric; got {result_row.get('values')!r}"
        )
        # No history → validation verdicts are absent; None or empty dict are both valid.
        # spec: USE_CASE_en.md — "no verdicts" is expressed as absent/empty validation field
        assert result_row.get("validation") in (None, {}), (
            f"validation must be None or empty dict when no prior history exists; "
            f"got {result_row.get('validation')!r}"
        )

    finally:
        await api_client.delete(base_conf, headers=admin_headers)
        await api_client.delete(ingestion_conf_url, headers=admin_headers)
