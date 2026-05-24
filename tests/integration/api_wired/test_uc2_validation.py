"""UC2 — Validation: end-to-end through public REST API.

Maps `spec/USE_CASE_en.md §UC2` paragraphs to executable steps. REST-only
per `spec/TESTING.md §Api-Wired Integration Tests`.

Test in this module:
  - test_uc2_passive_result_store: DE creates conf for two datasets (a Postgres table
    and a Kafka topic), pipelines POST results, DA queries historical series in
    descending data_time order, cross-dataset list shows both datasets,
    DE deletes the Postgres conf, resurrection cycle verified.

Prerequisites (spec/TESTING.md §Integration Testing):
  ./helm-charts/bin/install.sh --profile dev --components api --skip-build
  uv run python -m tests.integration.util --reset-seed
  DATASPOKE_TEST_MODE=true uv run pytest tests/integration/api_wired/test_uc2_validation.py

spec: USE_CASE_en.md §UC2
spec: VALIDATION.md §API Surface, §Rule Configuration, §Validation Result
"""
# spec: USE_CASE_en.md §UC2

from datetime import UTC, datetime, timedelta

import httpx
import pytest

# Declare fixture dependencies so module_dummy_data seeds PG + DataHub automatically.
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"orders"})
DUMMY_DATA_DATAHUB_TOPICS: frozenset[str] = frozenset({"imazon.orders.events"})


def _enc(urn: str) -> str:
    return urn.replace("(", "%28").replace(")", "%29").replace(",", "%2C")


# Dataset URNs used in UC2:
#   - daily_fulfillment_summary (Postgres table) — primary subject of the narrative.
#   - imazon.orders.events (Kafka topic) — second dataset, exercises the cross-dataset
#     list with a different platform.
# spec: TESTING.md §Imazon Dummy-Data Reference
_PG_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,"
    "example_db.orders.daily_fulfillment_summary,DEV)"
)
_KAFKA_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:kafka,"
    "example_kafka.imazon.orders.events,DEV)"
)

_PG_CONF_URL = f"/api/v1/spoke/common/data/{_enc(_PG_URN)}/attr/validation/conf"
_PG_RESULT_URL = f"/api/v1/spoke/common/data/{_enc(_PG_URN)}/attr/validation/result"
_KAFKA_CONF_URL = f"/api/v1/spoke/common/data/{_enc(_KAFKA_URN)}/attr/validation/conf"
_KAFKA_RESULT_URL = f"/api/v1/spoke/common/data/{_enc(_KAFKA_URN)}/attr/validation/result"
_VALIDATION_LIST_URL = "/api/v1/spoke/common/validation"

# Consumed by the api-wired `purge_urns` autouse fixture (see conftest.py).
URNS_TO_PURGE: list[str] = [_PG_URN, _KAFKA_URN]


@pytest.mark.asyncio
async def test_uc2_passive_result_store(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """UC2 narrative: 'As a data engineer, I want to register validation rules for both
    a fulfillment summary table and an upstream events topic, POST daily quality
    results from each pipeline, and later query the historical series as a baseline,
    so that I can detect anomalies in today's partition without re-scanning sources.'

    Steps mirror USE_CASE_en.md §UC2:
      1. DE creates confs via PUT for postgres + kafka datasets — 2 × 201
      2. Pipelines POST results: 3 days for postgres, 2 days for kafka — 5 × 200
      3. DA queries postgres GET result?from=…&until=… → 3 rows, descending by data_time
      4. Cross-dataset GET /validation → shows BOTH datasets with their descriptions,
         variable counts, latest_data_time, latest_score
      5. DELETE postgres conf → 204; GET conf → 404; ?removed=true includes postgres,
         ?removed=false includes kafka but not postgres
      6. PUT postgres again → 201 (resurrected); GET conf → 200 with new description
    """
    try:
        # ── Step 1: DE creates confs for both datasets ───────────────────────
        # UC2 narrative: "The data engineer registers validation slots for the
        # fulfillment table and the upstream order-events topic."
        # spec: VALIDATION.md §Rule Configuration — description + variables required.

        put_pg_resp = await api_client.put(
            _PG_CONF_URL,
            headers=admin_headers,
            json={
                "description": "Daily order fulfillment quality: row count, fill rate, and anomaly score",
                "variables": ["row_cnt", "fill_rate", "anomaly_score"],
            },
        )
        assert put_pg_resp.status_code == 201, (
            f"Step 1: PUT postgres conf expected 201, got {put_pg_resp.status_code}: {put_pg_resp.text}"
        )
        assert put_pg_resp.json()["variables"] == ["row_cnt", "fill_rate", "anomaly_score"]

        put_kafka_resp = await api_client.put(
            _KAFKA_CONF_URL,
            headers=admin_headers,
            json={
                "description": "Order events stream quality: message count and lag",
                "variables": ["msg_cnt", "lag_seconds"],
            },
        )
        assert put_kafka_resp.status_code == 201, (
            f"Step 1: PUT kafka conf expected 201, got {put_kafka_resp.status_code}: {put_kafka_resp.text}"
        )
        assert put_kafka_resp.json()["variables"] == ["msg_cnt", "lag_seconds"]

        # ── Step 2: Pipelines POST results for both datasets ─────────────────
        # UC2 narrative: "Each night, the validation task runs after the partition
        # write and POSTs the day's metrics to DataSpoke."
        # spec: VALIDATION.md §Validation Result — data_time is partition timestamp.

        day_0 = datetime(2026, 5, 1, tzinfo=UTC)
        day_1 = datetime(2026, 5, 2, tzinfo=UTC)
        day_2 = datetime(2026, 5, 3, tzinfo=UTC)

        # Postgres: 3 days
        for payload in [
            {
                "data_time": day_0.isoformat(),
                "score": 1.0,
                "variables": {"row_cnt": 1250.0, "fill_rate": 0.98, "anomaly_score": 0.02},
            },
            {
                "data_time": day_1.isoformat(),
                "score": 0.9,
                "variables": {"row_cnt": 1180.0, "fill_rate": 0.92, "anomaly_score": 0.08},
            },
            {
                "data_time": day_2.isoformat(),
                "score": 1.0,
                "variables": {"row_cnt": 1305.0, "fill_rate": 0.99, "anomaly_score": 0.01},
            },
        ]:
            resp = await api_client.post(_PG_RESULT_URL, headers=admin_headers, json=payload)
            assert resp.status_code == 200, (
                f"Step 2: POST postgres result for {payload['data_time']} expected 200, "
                f"got {resp.status_code}: {resp.text}"
            )

        # Kafka: 2 days
        for payload in [
            {
                "data_time": day_0.isoformat(),
                "score": 1.0,
                "variables": {"msg_cnt": 48000.0, "lag_seconds": 1.2},
            },
            {
                "data_time": day_1.isoformat(),
                "score": 0.85,
                "variables": {"msg_cnt": 47220.0, "lag_seconds": 5.4},
            },
        ]:
            resp = await api_client.post(_KAFKA_RESULT_URL, headers=admin_headers, json=payload)
            assert resp.status_code == 200, (
                f"Step 2: POST kafka result for {payload['data_time']} expected 200, "
                f"got {resp.status_code}: {resp.text}"
            )

        # ── Step 3: DA queries postgres historical GET result?from=…&until=… ─
        # UC2 narrative: "The next day's validation task GETs the prior 30-day
        # series to compute a rolling baseline without re-scanning source tables."
        # spec: VALIDATION.md §GET result — from inclusive, until exclusive;
        # rows ordered by data_time descending (newest first).

        from_dt = day_0.isoformat()
        until_dt = (day_2 + timedelta(days=1)).isoformat()  # exclusive upper bound

        # Pass as `params=` so httpx URL-encodes the timezone `+` to `%2B`.
        # An inline f-string in the URL would let the server decode `+` as a space.
        get_resp = await api_client.get(
            _PG_RESULT_URL,
            params={"from": from_dt, "until": until_dt, "limit": 10},
            headers=admin_headers,
        )
        assert get_resp.status_code == 200, (
            f"Step 3: GET result expected 200, got {get_resp.status_code}: {get_resp.text}"
        )
        result_payload = get_resp.json()

        assert result_payload["total_count"] == 3, (
            f"Step 3: expected total_count=3, got {result_payload['total_count']}"
        )
        results = result_payload["results"]
        assert len(results) == 3, (
            f"Step 3: expected 3 rows in results, got {len(results)}"
        )

        # spec: VALIDATION.md §GET result — descending data_time order.
        returned_dates = [r["data_time"][:10] for r in results]
        assert returned_dates == ["2026-05-03", "2026-05-02", "2026-05-01"], (
            f"Step 3: expected descending data_time order [05-03, 05-02, 05-01], got {returned_dates}"
        )

        # ── Step 4: Cross-dataset list shows BOTH datasets ───────────────────
        # UC2 narrative: "The data analyst checks the cross-dataset validation list
        # to see which datasets have recent quality signals."
        # spec: VALIDATION.md §API Surface — GET /spoke/common/validation aggregates conf + latest result.

        list_resp = await api_client.get(
            f"{_VALIDATION_LIST_URL}?limit=100",
            headers=admin_headers,
        )
        assert list_resp.status_code == 200, (
            f"Step 4: GET /validation expected 200, got {list_resp.status_code}: {list_resp.text}"
        )
        items = list_resp.json()["validations"]
        by_urn = {i["dataset_urn"]: i for i in items}
        assert _PG_URN in by_urn, (
            f"Step 4: postgres dataset not found in cross-dataset list; got: {list(by_urn)}"
        )
        assert _KAFKA_URN in by_urn, (
            f"Step 4: kafka dataset not found in cross-dataset list; got: {list(by_urn)}"
        )

        # spec: VALIDATION.md §API Surface — each row aggregates description + variable_count + latest result
        pg_item = by_urn[_PG_URN]
        assert pg_item["description"] == "Daily order fulfillment quality: row count, fill rate, and anomaly score"
        assert pg_item["variable_count"] == 3, (
            f"Step 4: postgres expected variable_count=3, got {pg_item['variable_count']}"
        )
        assert pg_item["latest_data_time"] is not None
        assert pg_item["latest_score"] is not None
        assert pg_item["is_removed"] is False

        kafka_item = by_urn[_KAFKA_URN]
        assert kafka_item["description"] == "Order events stream quality: message count and lag"
        assert kafka_item["variable_count"] == 2, (
            f"Step 4: kafka expected variable_count=2, got {kafka_item['variable_count']}"
        )
        assert kafka_item["latest_data_time"] is not None
        assert kafka_item["latest_score"] is not None
        assert kafka_item["is_removed"] is False

        # ── Step 5: DELETE postgres → 204; GET conf → 404; list visibility ───
        # UC2 narrative: "The DE retires the rule for the fulfillment table."
        # spec: VALIDATION.md §Rule Configuration — DELETE performs soft delete.

        del_resp = await api_client.delete(_PG_CONF_URL, headers=admin_headers)
        assert del_resp.status_code == 204, (
            f"Step 5: DELETE expected 204, got {del_resp.status_code}: {del_resp.text}"
        )

        get_after_del = await api_client.get(_PG_CONF_URL, headers=admin_headers)
        assert get_after_del.status_code == 404, (
            f"Step 5: GET conf after DELETE expected 404, got {get_after_del.status_code}"
        )

        # ?removed=true → includes postgres (kafka is still active so not required here)
        list_removed_resp = await api_client.get(
            f"{_VALIDATION_LIST_URL}?removed=true&limit=100",
            headers=admin_headers,
        )
        assert list_removed_resp.status_code == 200
        removed_urns = [i["dataset_urn"] for i in list_removed_resp.json()["validations"]]
        assert _PG_URN in removed_urns, (
            f"Step 5: ?removed=true should include postgres dataset; got: {removed_urns}"
        )

        # ?removed=false → kafka present, postgres absent
        list_active_resp = await api_client.get(
            f"{_VALIDATION_LIST_URL}?removed=false&limit=100",
            headers=admin_headers,
        )
        assert list_active_resp.status_code == 200
        active_urns = [i["dataset_urn"] for i in list_active_resp.json()["validations"]]
        assert _PG_URN not in active_urns, (
            f"Step 5: ?removed=false must NOT include deleted postgres dataset; got: {active_urns}"
        )
        assert _KAFKA_URN in active_urns, (
            f"Step 5: ?removed=false should still include active kafka dataset; got: {active_urns}"
        )

        # ── Step 5.5: PATCH on soft-deleted slot → 404 ────────────────────────────
        # spec: VALIDATION.md §Rule Configuration — after DELETE, PATCH targets
        # the same resource view as GET; tombstoned slot is invisible.
        patch_deleted_resp = await api_client.patch(
            _PG_CONF_URL,
            headers=admin_headers,
            json={"description": "should not apply to soft-deleted slot"},
        )
        assert patch_deleted_resp.status_code == 404, (
            f"Step 5.5: PATCH on soft-deleted conf expected 404, "
            f"got {patch_deleted_resp.status_code}: {patch_deleted_resp.text}"
        )
        # spec: API.md §Standard Envelope — every non-2xx response carries an error_code field.
        patch_deleted_body = patch_deleted_resp.json()
        assert patch_deleted_body.get("error_code"), (
            f"Step 5.5: 404 response must carry error_code per API.md §Standard Envelope; "
            f"got: {patch_deleted_body}"
        )

        # ── Step 6: PUT-after-DELETE resurrects the postgres assertion ───────
        # UC2 narrative: "The DE reinstates the rule with updated variable names."
        # spec: VALIDATION.md §Rule Configuration — subsequent PUT resurrects; same URN reused.

        resurrect_resp = await api_client.put(
            _PG_CONF_URL,
            headers=admin_headers,
            json={
                "description": "Reinstated quality check with extended variables",
                "variables": ["row_cnt", "fill_rate", "anomaly_score", "null_rate"],
            },
        )
        assert resurrect_resp.status_code == 201, (
            f"Step 6: PUT-after-DELETE expected 201, got {resurrect_resp.status_code}: {resurrect_resp.text}"
        )

        get_after_resurrect = await api_client.get(_PG_CONF_URL, headers=admin_headers)
        assert get_after_resurrect.status_code == 200, (
            f"Step 6: GET conf after resurrection expected 200, got {get_after_resurrect.status_code}"
        )
        resurrected = get_after_resurrect.json()
        assert resurrected["description"] == "Reinstated quality check with extended variables"
        assert "null_rate" in resurrected["variables"]

    finally:
        # Cleanup — best effort: delete both confs to restore clean state
        await api_client.delete(_PG_CONF_URL, headers=admin_headers)
        await api_client.delete(_KAFKA_CONF_URL, headers=admin_headers)
