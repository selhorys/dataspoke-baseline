"""UC2 — Validation: spec-conformance through public REST API (Kafka path).

Validates that the hardened UC2 spec covers Kafka-platform datasets via the
default-source paths (datahub_operation for freshness, datahub_profile for volume).

Spec sources:
  - spec/DATAHUB_INTEGRATION.md §Assertion Aspects
  - spec/feature/BACKEND.md §Validation Service
  - spec/USE_CASE_en.md §UC2
  - spec/TESTING.md §Imazon Dummy-Data Reference (Kafka topics)
"""
# spec: USE_CASE_en.md §UC2

import asyncio
import time
import urllib.parse

import httpx
import pytest
from datahub.metadata.schema_classes import AssertionRunEventClass, AssertionSourceTypeClass

from src.backend.validation.assertions import build_assertion_urn

# Per-module dummy-data seed
DUMMY_DATA_TOPICS: frozenset[str] = frozenset({"imazon.orders.events"})
DUMMY_DATA_DATAHUB_TOPICS: frozenset[str] = frozenset({"imazon.orders.events"})

# Imazon Kafka topic — see spec/TESTING.md §Imazon Dummy-Data Reference
# DataHub ingest uses kafka_instance prefix; conftest module_dummy_data registers this topic.
_KAFKA_ORDERS_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.orders.events,DEV)"
)
_KAFKA_ORDERS_ENCODED = urllib.parse.quote(_KAFKA_ORDERS_URN, safe="")


@pytest.mark.asyncio
async def test_uc2_kafka_orders_events_default_source_paths(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    datahub_client,
) -> None:
    """UC2 narrative: register default-source freshness and volume rules on a Kafka dataset.

    spec: USE_CASE_en.md §UC2 lines 234-287 (rule registration and run semantics)
    spec: feature/BACKEND.md L361 (freshness datahub_operation default),
          L364 (volume datahub_profile default)

    Exercises default-source paths on Kafka platform — imazon.orders.events (20 msgs,
    DataHub profile + OperationClass emitted by conftest module_dummy_data via
    tests/integration/util/datahub.py):
      - freshness / datahub_operation (default)  — r-fresh-op
      - volume   / datahub_profile (default)     — r-vol-profile

    The dummy-data ingest emits OperationClass with the current timestamp, so a 30d
    freshness lookback ensures the rule has data to evaluate.
    The dummy-data ingest emits DatasetProfileClass with rowCount = message count (20),
    so a volume range 1–100000 should have data.

    Outcome (SUCCESS/FAILURE) is not asserted — depends on DataHub timeseries state.

    Conventions verified: C1 (typed sub-aspects), C2 (source.type=EXTERNAL),
    C3 (deterministic URN), C4 (lastUpdated audit stamp), C5 (shared runId).
    """
    base_conf = f"/api/v1/spoke/common/data/{_KAFKA_ORDERS_ENCODED}/attr/validation/conf"
    base_run = f"/api/v1/spoke/common/data/{_KAFKA_ORDERS_ENCODED}/method/validation/run"
    rule_ids = ["r-fresh-op", "r-vol-profile"]

    try:
        # ── PUT: register 2 rules (default sources) ───────────────────────────
        # spec: USE_CASE_en.md §UC2 lines 234-260 — rule registration PUT endpoint
        # spec: feature/BACKEND.md L361 (freshness datahub_operation default),
        #       L364 (volume datahub_profile default)
        put_resp = await api_client.put(
            base_conf,
            headers=admin_headers,
            json={
                "is_enabled": True,
                "schedule_tier": "daily",
                "owner": "de-lead@imazon.com",
                "rules": [
                    {
                        "rule_id": "r-fresh-op",
                        "type": "freshness",
                        "source": "datahub_operation",
                        "lookback_interval": "30d",
                    },
                    {
                        "rule_id": "r-vol-profile",
                        "type": "volume",
                        "source": "datahub_profile",
                        "condition": {"type": "between", "min": 1, "max": 100000},
                    },
                ],
            },
        )
        assert put_resp.status_code in (200, 201), (
            f"PUT validation/conf failed: {put_resp.status_code} {put_resp.text}"
        )

        # ── C3: fetch by deterministic URN ────────────────────────────────────
        # spec: DATAHUB_INTEGRATION.md L241-L242 — URN computed by build_assertion_urn
        for rule_id in rule_ids:
            assertion_urn = build_assertion_urn(_KAFKA_ORDERS_URN, rule_id)
            info = await datahub_client.get_assertion_info(assertion_urn)
            assert info is not None, (
                f"spec: DATAHUB_INTEGRATION.md L241-L242 — assertion URN must exist in DataHub "
                f"after PUT (rule_id={rule_id!r})"
            )

        # ── C1: typed sub-aspects populated and correct ───────────────────────
        # spec: DATAHUB_INTEGRATION.md L233-L237 — sub-aspect carries entity URN, schedule, selector

        fresh_urn = build_assertion_urn(_KAFKA_ORDERS_URN, "r-fresh-op")
        fresh_info = await datahub_client.get_assertion_info(fresh_urn)
        assert fresh_info is not None
        # spec: DATAHUB_INTEGRATION.md L223 — FRESHNESS rules require freshnessAssertion sub-aspect
        assert fresh_info.freshnessAssertion is not None, (
            "spec: DATAHUB_INTEGRATION.md L223 — freshnessAssertion sub-aspect must be non-null"
        )
        assert fresh_info.freshnessAssertion.entity == _KAFKA_ORDERS_URN, (
            "spec: DATAHUB_INTEGRATION.md L233-L237 — "
            "freshnessAssertion.entity must equal dataset URN"
        )
        assert fresh_info.freshnessAssertion.schedule is not None, (
            "spec: DATAHUB_INTEGRATION.md L233-L237 — freshnessAssertion.schedule must be set "
            "(carries the lookback interval)"
        )

        vol_urn = build_assertion_urn(_KAFKA_ORDERS_URN, "r-vol-profile")
        vol_info = await datahub_client.get_assertion_info(vol_urn)
        assert vol_info is not None
        # spec: DATAHUB_INTEGRATION.md L224 — VOLUME rules require volumeAssertion sub-aspect
        assert vol_info.volumeAssertion is not None, (
            "spec: DATAHUB_INTEGRATION.md L224 — volumeAssertion sub-aspect must be non-null"
        )
        assert vol_info.volumeAssertion.entity == _KAFKA_ORDERS_URN, (
            "spec: DATAHUB_INTEGRATION.md L233-L237 — volumeAssertion.entity must equal dataset URN"
        )

        # ── C2: source.type = EXTERNAL on every emitted assertion ────────────
        # spec: DATAHUB_INTEGRATION.md L238-L240
        for rule_id, info in [("r-fresh-op", fresh_info), ("r-vol-profile", vol_info)]:
            assert info.source is not None, (
                f"spec: DATAHUB_INTEGRATION.md L238-L240 — source must be set for {rule_id!r}"
            )
            assert info.source.type == AssertionSourceTypeClass.EXTERNAL, (
                f"spec: DATAHUB_INTEGRATION.md L238-L240 — source.type must be EXTERNAL "
                f"for {rule_id!r}; got {info.source.type!r}"
            )

        # ── C4: lastUpdated audit stamp ───────────────────────────────────────
        # spec: DATAHUB_INTEGRATION.md L243-L245 — lastUpdated populated, actor has corpuser prefix
        for rule_id, info in [("r-fresh-op", fresh_info), ("r-vol-profile", vol_info)]:
            assert info.lastUpdated is not None, (
                f"spec: DATAHUB_INTEGRATION.md L243-L245 — lastUpdated must be non-null "
                f"for {rule_id!r}"
            )
            actor = info.lastUpdated.actor or ""
            assert actor.startswith("urn:li:corpuser:"), (
                f"spec: DATAHUB_INTEGRATION.md L243-L245 — lastUpdated.actor must start with "
                f"'urn:li:corpuser:' for {rule_id!r}; got {actor!r}"
            )

        # ── POST run — arithmetic invariant ──────────────────────────────────
        # spec: USE_CASE_en.md §UC2 lines 275-277 — fields exist; sum is bounded above by `total`
        run_resp = await api_client.post(
            base_run,
            headers=admin_headers,
            json={"dry_run": False},
        )
        assert run_resp.status_code in (200, 201), (
            f"spec: USE_CASE_en.md §UC2 L284 — POST validation/run failed: "
            f"{run_resp.status_code} {run_resp.text}"
        )
        run_body = run_resp.json()
        captured_run_id = run_body["run_id"]
        total_check = run_body["passed"] + run_body["failed"] + run_body["errored"]
        assert run_body["total"] >= total_check, (
            f"spec: USE_CASE_en.md §UC2 L275-L277 — fields exist; sum is bounded above by `total`; "
            f"total ({run_body['total']}) < passed + failed + errored ({total_check})"
        )
        assert run_body["passed"] + run_body["failed"] + run_body["errored"] == len(rule_ids), (
            f"spec: feature/BACKEND.md L400-L406 — each registered rule contributes to "
            f"exactly one bucket; expected sum to equal {len(rule_ids)}, got "
            f"{run_body['passed']} + {run_body['failed']} + {run_body['errored']}"
        )

        # ── C5: shared runId across all rules in one run ──────────────────────
        # spec: DATAHUB_INTEGRATION.md L246-L247 — all rules in one run share runId
        deadline = time.monotonic() + 10.0
        missing = False
        while time.monotonic() < deadline:
            missing = False
            for rule_id in rule_ids:
                urn = build_assertion_urn(_KAFKA_ORDERS_URN, rule_id)
                events = await datahub_client.get_timeseries(urn, AssertionRunEventClass)
                if not events:
                    missing = True
                    break
            if not missing:
                break
            await asyncio.sleep(0.5)

        assert not missing, (
            "spec: DATAHUB_INTEGRATION.md L246-L247 — assertionRunEvent timeseries "
            "did not populate within 10s for all rules"
        )
        for rule_id in rule_ids:
            urn = build_assertion_urn(_KAFKA_ORDERS_URN, rule_id)
            events = await datahub_client.get_timeseries(urn, AssertionRunEventClass)
            assert events[0].runId == captured_run_id, (
                f"spec: DATAHUB_INTEGRATION.md L246-L247 — most-recent run event for "
                f"{rule_id!r} must carry the captured runId {captured_run_id!r}; "
                f"got {events[0].runId!r}"
            )

    finally:
        await api_client.delete(base_conf, headers=admin_headers)
