"""Spot tests — Validation: Kafka default-source rule paths.

Two tests, one per default-source path on imazon.orders.events:
1. freshness via datahub_operation — util.datahub emits OperationClass with current timestamp
   during ingest; 30d lookback must PASS (operation timestamp is fresh).
2. volume via datahub_profile — util.datahub emits DatasetProfileClass with rowCount=20;
   condition between 1 and 100000 must PASS.
"""
# spec: USE_CASE_en.md §UC2 — default-source rule paths for Kafka
# spec: feature/BACKEND.md §Validation Service — source discriminator table
#   freshness default: datahub_operation
#   volume default: datahub_profile
# spec: TESTING.md §Imazon Dummy-Data Reference — imazon.orders.events

import asyncio
import os
import urllib.parse

import httpx
import pytest

# Per-module dummy-data seed — Kafka topic triggers DataHub ingest (with OperationClass
# and DatasetProfileClass emitted by tests/integration/util/datahub.py).
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_TOPICS: frozenset[str] = frozenset({"imazon.orders.events"})

# Status set per spec; enum values are implementation-defined.
# spec: USE_CASE_en.md §UC2 §Run semantics
_VALID_STATUSES: frozenset[str] = frozenset({"success", "failure", "error"})

_KAFKA_INSTANCE = os.environ.get("DATASPOKE_DEV_KUBE_DUMMY_DATA_KAFKA_INSTANCE", "example_kafka")
_TOPIC = "imazon.orders.events"

# spec: TESTING.md §Imazon Dummy-Data Reference — Kafka topic imazon.orders.events
_KAFKA_ORDERS_URN = (
    f"urn:li:dataset:(urn:li:dataPlatform:kafka,{_KAFKA_INSTANCE}.{_TOPIC},DEV)"
)
_KAFKA_ORDERS_ENCODED = urllib.parse.quote(_KAFKA_ORDERS_URN, safe="")


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
    assert run_body["status"].lower() in _VALID_STATUSES, (
        f"run status must be terminal; got {run_body['status']!r}"
    )

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
        f"Result row for rule_id={rule_id!r} not found within {timeout_s}s. "
        "spec: BACKEND.md §Validation Service — each evaluated rule persists a result"
    )
    return result_row


@pytest.mark.asyncio
async def test_freshness_via_datahub_operation_on_kafka_topic(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """freshness rule with default-source datahub_operation on imazon.orders.events — must PASS.

    The DUMMY_DATA_DATAHUB_TOPICS ingest emits an OperationClass with the current
    timestamp. A 30d lookback must PASS because the operation is fresh (emitted during
    this test run's data reset).

    spec: USE_CASE_en.md §UC2 — freshness rule, default source path
    spec: feature/BACKEND.md §Validation Service — freshness default: datahub_operation
    """
    base_conf = f"/api/v1/spoke/common/data/{_KAFKA_ORDERS_ENCODED}/attr/validation/conf"
    base_run = f"/api/v1/spoke/common/data/{_KAFKA_ORDERS_ENCODED}/method/validation/run"
    base_results = f"/api/v1/spoke/common/data/{_KAFKA_ORDERS_ENCODED}/attr/validation/result"

    rule_id = "spot-kafka-fresh-op-001"

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
                    "source": "datahub_operation",
                    "lookback_interval": "30d",
                }
            ],
        },
    )
    assert put_resp.status_code in (200, 201), put_resp.text

    try:
        result_row = await _post_run_and_poll_result(
            api_client, admin_headers, base_run, rule_id, base_results
        )
        # OperationClass emitted with current timestamp; 30d lookback must PASS
        assert result_row.get("assertion_result") == "SUCCESS", (
            f"Freshness Kafka rule must produce assertion_result='SUCCESS' (operation emitted now, 30d lookback); "
            f"got {result_row.get('assertion_result')!r}. "
            "spec: feature/BACKEND.md §Validation Service — freshness default: datahub_operation"
        )
    finally:
        await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_volume_via_datahub_profile_on_kafka_topic(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """volume rule with default-source datahub_profile on imazon.orders.events — must PASS.

    The DUMMY_DATA_DATAHUB_TOPICS ingest emits a DatasetProfileClass with
    rowCount = message_count (20 messages per fixture). Condition between 1 and 100000
    must PASS because 20 is in range.

    spec: USE_CASE_en.md §UC2 — volume rule, default source path
    spec: feature/BACKEND.md §Validation Service — volume default: datahub_profile
    spec: TESTING.md §Imazon Dummy-Data Reference — imazon.orders.events 20 seed messages
    """
    base_conf = f"/api/v1/spoke/common/data/{_KAFKA_ORDERS_ENCODED}/attr/validation/conf"
    base_run = f"/api/v1/spoke/common/data/{_KAFKA_ORDERS_ENCODED}/method/validation/run"
    base_results = f"/api/v1/spoke/common/data/{_KAFKA_ORDERS_ENCODED}/attr/validation/result"

    rule_id = "spot-kafka-vol-profile-001"

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
                    "source": "datahub_profile",
                    "condition": {"type": "between", "min": 1, "max": 100000},
                }
            ],
        },
    )
    assert put_resp.status_code in (200, 201), put_resp.text

    try:
        result_row = await _post_run_and_poll_result(
            api_client, admin_headers, base_run, rule_id, base_results
        )
        # rowCount=20 from DatasetProfileClass; condition between 1 and 100000 → PASS
        assert result_row.get("assertion_result") == "SUCCESS", (
            f"Volume Kafka rule must produce assertion_result='SUCCESS' (rowCount=20 is between 1 and 100000); "
            f"got {result_row.get('assertion_result')!r}. "
            "spec: feature/BACKEND.md §Validation Service — volume default: datahub_profile"
        )
    finally:
        await api_client.delete(base_conf, headers=admin_headers)
