"""UC2 — Validation: end-to-end through public REST API.

Maps `spec/USE_CASE_en.md §UC2` paragraphs to executable steps. REST-only
per `spec/TESTING.md §Api-Wired Integration Tests`.

Test in this module:
  - test_uc2_passive_result_store: caller creates conf for two datasets (a Postgres
    table and a Kafka topic), pipelines POST results, caller queries historical
    series in descending data_time order, cross-dataset list shows both datasets,
    caller hard-deletes the Postgres conf (cascading its results + validation
    events), the dataset reads as never-created afterwards, and a fresh PUT creates
    a brand-new conf.

Prerequisites (spec/TESTING.md §Integration Testing):
  ./helm-charts/bin/install.sh --profile dev --components api --skip-build
  uv run python -m tests.integration.util --reset-seed
  uv run pytest tests/integration/api_wired/test_uc2_01_validation.py

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


def _var(name: str, description: str = "") -> dict[str, str]:
    """Conf variable object: {name, description}.

    spec: VALIDATION.md §Rule Configuration — each variable is a {name, description}.
    """
    return {"name": name, "description": description}


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
_PG_EVENTS_URL = f"/api/v1/spoke/common/data/{_enc(_PG_URN)}/event/validation"
_KAFKA_CONF_URL = f"/api/v1/spoke/common/data/{_enc(_KAFKA_URN)}/attr/validation/conf"
_KAFKA_RESULT_URL = (
    f"/api/v1/spoke/common/data/{_enc(_KAFKA_URN)}/attr/validation/result"
)
_VALIDATION_LIST_URL = "/api/v1/spoke/validation"

# Consumed by the api-wired `purge_urns` autouse fixture (see conftest.py).
URNS_TO_PURGE: list[str] = [_PG_URN, _KAFKA_URN]

_PG_DESCRIPTION = (
    "Daily order fulfillment quality: row count, fill rate, and anomaly score"
)


@pytest.mark.asyncio
async def test_uc2_passive_result_store(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """UC2 narrative: 'As a data team member, I want to register validation rules for
    both a fulfillment summary table and an upstream events topic, POST daily quality
    results from each pipeline, and later query the historical series as a baseline,
    so that I can detect anomalies in today's partition without re-scanning sources.'

    Steps mirror USE_CASE_en.md §UC2:
      1. PUT validation conf for postgres + kafka datasets — 2 × 201
      2. Pipelines POST results: 3 days for postgres, 2 days for kafka — 5 × 201
      3. GET postgres result?from=…&until=… → 3 rows, descending by data_time
      4. Cross-dataset GET /validation → shows BOTH datasets with their descriptions,
         variable counts, latest_data_time, latest_score
      5. DELETE postgres conf → 204 (hard delete + cascade). Afterwards: GET conf →
         404 CONFIG_NOT_FOUND; PATCH → 404 CONFIG_NOT_FOUND; the postgres result
         series is gone (cascade); validation events for postgres are gone (cascade);
         the dataset is absent from /spoke/validation; kafka is untouched
      6. PUT postgres conf again → 201 (a brand-new conf, no resurrection); its fresh
         result series starts empty and can be re-populated
    """
    try:
        # ── Step 1: Caller creates confs for both datasets ───────────────────
        # UC2 narrative: "The caller registers validation slots for the
        # fulfillment table and the upstream order-events topic."
        # spec: VALIDATION.md §Rule Configuration — description + variables required.

        pg_variables = [
            _var("row_cnt", "Daily fulfillment row count"),
            _var("fill_rate", "Fraction of orders fully shipped"),
            _var("anomaly_score", "Detector score for the day"),
        ]
        put_pg_resp = await api_client.put(
            _PG_CONF_URL,
            headers=admin_headers,
            json={"description": _PG_DESCRIPTION, "variables": pg_variables},
        )
        assert put_pg_resp.status_code == 201, (
            f"Step 1: PUT postgres conf expected 201, "
            f"got {put_pg_resp.status_code}: {put_pg_resp.text}"
        )
        # spec: VALIDATION.md §Rule Configuration — variables round-trip as objects.
        assert put_pg_resp.json()["variables"] == pg_variables

        kafka_variables = [
            _var("msg_cnt", "Messages produced in the window"),
            _var("lag_seconds", "Consumer lag in seconds"),
        ]
        put_kafka_resp = await api_client.put(
            _KAFKA_CONF_URL,
            headers=admin_headers,
            json={
                "description": "Order events stream quality: message count and lag",
                "variables": kafka_variables,
            },
        )
        assert put_kafka_resp.status_code == 201, (
            f"Step 1: PUT kafka conf expected 201, "
            f"got {put_kafka_resp.status_code}: {put_kafka_resp.text}"
        )
        assert put_kafka_resp.json()["variables"] == kafka_variables

        # ── Step 2: Pipelines POST results for both datasets ─────────────────
        # UC2 narrative: "Each night, the validation task runs after the partition
        # write and POSTs the day's metrics to DataSpoke."
        # spec: VALIDATION.md §Validation Result — data_time is partition timestamp.
        # spec: API.md §HTTP Status Codes — POST that creates a resource returns 201.

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
            assert resp.status_code == 201, (
                f"Step 2: POST postgres result for {payload['data_time']} expected 201, "
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
            assert resp.status_code == 201, (
                f"Step 2: POST kafka result for {payload['data_time']} expected 201, "
                f"got {resp.status_code}: {resp.text}"
            )

        # ── Step 3: GET postgres result?from=…&until=… (historical range) ────
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
        expected_dates = ["2026-05-03", "2026-05-02", "2026-05-01"]
        assert returned_dates == expected_dates, (
            f"Step 3: expected descending order {expected_dates}, got {returned_dates}"
        )

        # ── Step 4: Cross-dataset list shows BOTH datasets ───────────────────
        # UC2 narrative: "The caller checks the cross-dataset validation list
        # to see which datasets have recent quality signals."
        # spec: VALIDATION.md §API Surface — GET /spoke/validation aggregates
        # conf + latest result.

        list_resp = await api_client.get(
            f"{_VALIDATION_LIST_URL}?limit=100",
            headers=admin_headers,
        )
        assert list_resp.status_code == 200, (
            f"Step 4: GET /validation expected 200, "
            f"got {list_resp.status_code}: {list_resp.text}"
        )
        items = list_resp.json()["validations"]
        by_urn = {i["dataset_urn"]: i for i in items}
        assert _PG_URN in by_urn, (
            f"Step 4: postgres dataset not found in cross-dataset list; "
            f"got: {list(by_urn)}"
        )
        assert _KAFKA_URN in by_urn, (
            f"Step 4: kafka dataset not found in cross-dataset list; "
            f"got: {list(by_urn)}"
        )

        # spec: VALIDATION.md §API Surface — each row aggregates:
        # description + variable_count + latest result.
        pg_item = by_urn[_PG_URN]
        assert pg_item["description"] == _PG_DESCRIPTION
        assert pg_item["variable_count"] == 3, (
            f"Step 4: postgres expected variable_count=3, got {pg_item['variable_count']}"
        )
        assert pg_item["latest_data_time"] is not None
        assert pg_item["latest_score"] is not None
        assert "is_removed" not in pg_item, (
            "Step 4: aggregated row must not carry is_removed — soft-delete is gone. "
            f"got keys: {list(pg_item)}"
        )

        kafka_item = by_urn[_KAFKA_URN]
        assert kafka_item["description"] == "Order events stream quality: message count and lag"
        assert kafka_item["variable_count"] == 2, (
            f"Step 4: kafka expected variable_count=2, got {kafka_item['variable_count']}"
        )
        assert kafka_item["latest_data_time"] is not None
        assert kafka_item["latest_score"] is not None

        # Before deleting, confirm postgres has validation events (so the cascade in
        # Step 5 is provably wiping a non-empty history).
        # spec: API.md §GET event/validation — validation events for the dataset.
        events_before = await api_client.get(
            f"{_PG_EVENTS_URL}?limit=100", headers=admin_headers
        )
        assert events_before.status_code == 200
        assert events_before.json()["total_count"] > 0, (
            "Step 4: postgres must have validation events before delete (conf-create + "
            "result-recorded) so the Step 5 cascade is observable"
        )

        # ── Step 5: DELETE postgres → 204 (hard delete + cascade) ────────────
        # UC2 narrative: "The DE deletes the rule for the fulfillment table."
        # spec: API.md §DELETE attr/validation/conf — hard delete: removes the conf
        # row, cascades to delete the dataset's validation results and validation
        # events, hard-deletes the DataHub assertion. Afterwards the dataset reads as
        # never-created.

        del_resp = await api_client.delete(_PG_CONF_URL, headers=admin_headers)
        assert del_resp.status_code == 204, (
            f"Step 5: DELETE expected 204, got {del_resp.status_code}: {del_resp.text}"
        )

        # GET conf after DELETE → 404 CONFIG_NOT_FOUND (never-created, not a tombstone).
        get_after_del = await api_client.get(_PG_CONF_URL, headers=admin_headers)
        assert get_after_del.status_code == 404, (
            f"Step 5: GET conf after DELETE expected 404, got {get_after_del.status_code}"
        )
        assert get_after_del.json().get("error_code") == "CONFIG_NOT_FOUND", (
            f"Step 5: GET on deleted slot must carry CONFIG_NOT_FOUND (never-created); "
            f"got: {get_after_del.json()}"
        )

        # PATCH after DELETE → 404 CONFIG_NOT_FOUND (no resurrection / no tombstone).
        patch_after_del = await api_client.patch(
            _PG_CONF_URL,
            headers=admin_headers,
            json={"description": "no slot to patch"},
        )
        assert patch_after_del.status_code == 404, (
            f"Step 5: PATCH on deleted conf expected 404, "
            f"got {patch_after_del.status_code}: {patch_after_del.text}"
        )
        assert patch_after_del.json().get("error_code") == "CONFIG_NOT_FOUND", (
            f"Step 5: PATCH on deleted slot must carry CONFIG_NOT_FOUND; "
            f"got: {patch_after_del.json()}"
        )

        # Cascade: the postgres result series is gone.
        # spec: API.md §DELETE attr/validation/conf — cascades validation results.
        results_after_del = await api_client.get(
            _PG_RESULT_URL,
            params={"from": from_dt, "until": until_dt, "limit": 10},
            headers=admin_headers,
        )
        assert results_after_del.status_code == 200
        assert results_after_del.json()["total_count"] == 0, (
            "Step 5: postgres result series must be gone after hard delete (cascade); "
            f"got total_count={results_after_del.json()['total_count']}"
        )

        # Cascade: the postgres validation events are gone.
        # spec: API.md §DELETE attr/validation/conf — cascades validation events.
        events_after_del = await api_client.get(
            f"{_PG_EVENTS_URL}?limit=100", headers=admin_headers
        )
        assert events_after_del.status_code == 200
        assert events_after_del.json()["total_count"] == 0, (
            "Step 5: postgres validation events must be gone after hard delete (cascade); "
            f"got total_count={events_after_del.json()['total_count']}"
        )

        # The dataset is absent from /spoke/validation entirely; kafka is untouched.
        list_after_del = await api_client.get(
            f"{_VALIDATION_LIST_URL}?limit=100",
            headers=admin_headers,
        )
        assert list_after_del.status_code == 200
        urns_after_del = [i["dataset_urn"] for i in list_after_del.json()["validations"]]
        assert _PG_URN not in urns_after_del, (
            f"Step 5: hard-deleted postgres dataset must be absent from /spoke/validation; "
            f"got: {urns_after_del}"
        )
        assert _KAFKA_URN in urns_after_del, (
            f"Step 5: kafka dataset must be untouched by the postgres delete; "
            f"got: {urns_after_del}"
        )

        # ── Step 6: PUT again creates a brand-new conf (no resurrection) ──────
        # UC2 narrative: "The DE re-registers a rule for the fulfillment table; it is
        # a fresh slot, not a resurrected one."
        # spec: API.md §DELETE attr/validation/conf — a fresh PUT creates a new conf
        # (201); there is no resurrection concept.
        recreate_variables = [
            _var("row_cnt", "Daily fulfillment row count"),
            _var("fill_rate", "Fraction of orders fully shipped"),
        ]
        recreate_resp = await api_client.put(
            _PG_CONF_URL,
            headers=admin_headers,
            json={
                "description": "Freshly re-registered fulfillment quality check",
                "variables": recreate_variables,
            },
        )
        assert recreate_resp.status_code == 201, (
            f"Step 6: PUT after delete must create a new conf → expected 201, "
            f"got {recreate_resp.status_code}: {recreate_resp.text}"
        )
        assert recreate_resp.json()["variables"] == recreate_variables, (
            "Step 6: the new conf carries exactly the freshly-supplied variables"
        )

        # The fresh conf's result series starts empty — the prior cascade is not undone.
        fresh_results = await api_client.get(
            _PG_RESULT_URL,
            params={"from": from_dt, "until": until_dt, "limit": 10},
            headers=admin_headers,
        )
        assert fresh_results.status_code == 200
        assert fresh_results.json()["total_count"] == 0, (
            "Step 6: a re-created conf starts with an empty result series; "
            f"got total_count={fresh_results.json()['total_count']}"
        )

    finally:
        # Cleanup — best effort: delete both confs to restore clean state
        await api_client.delete(_PG_CONF_URL, headers=admin_headers)
        await api_client.delete(_KAFKA_CONF_URL, headers=admin_headers)
