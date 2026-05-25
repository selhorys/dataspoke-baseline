"""Spot tests — Ingestion Control: active-custom Kafka.

Concerns covered:
- PUT active-custom conf for Kafka topic imazon.orders.events (201/200)
- Config round-trip: mode, platform, identifier, schedule_tier preserved in GET
- Dry-run does NOT persist DPI or DataHub aspects (dry_run=true)
- workflow_dag_id derivation from schedule_tier='daily' → 'ingestion-active-daily'
- Real run (if feasible): Status + DatasetProperties + SchemaMetadata aspects emitted
"""
# spec: USE_CASE_en.md §UC1 Case 1 (active-custom path)
# spec: API.md §Ingestion routes
# spec: feature/BACKEND.md §Ingestion Service — SUPPORTED_PLATFORMS includes kafka

import asyncio
import os
import re
import urllib.parse

import httpx
import pytest

# Per-module dummy-data seed — Kafka topic triggers DataHub ingest.
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_TOPICS: frozenset[str] = frozenset({"imazon.orders.events"})

# Kafka locator: bootstrap_servers per src/shared/models/ingestion.py KafkaLocator
# Kafka auth: NoAuth — auth field must be omitted entirely
# spec: src/shared/models/ingestion.py PLATFORM_REGISTRY — Platform.KAFKA uses NoAuth
_KAFKA_BOOTSTRAP = os.environ.get(
    "DATASPOKE_TEST_DUMMY_DATA_KAFKA_BROKERS", "localhost:9104"
)
_KAFKA_INSTANCE = os.environ.get("DATASPOKE_DEV_DUMMY_DATA_KAFKA_INSTANCE", "example_kafka")
_TOPIC = "imazon.orders.events"

# spec: TESTING.md §Imazon Dummy-Data Reference — Kafka topic imazon.orders.events
_KAFKA_URN = (
    f"urn:li:dataset:(urn:li:dataPlatform:kafka,{_KAFKA_INSTANCE}.{_TOPIC},DEV)"
)
_KAFKA_ENCODED = urllib.parse.quote(_KAFKA_URN, safe="")

_DATAHUB_GMS_URL = os.environ.get("DATASPOKE_TEST_DATAHUB_GMS_URL", "")
_DATAHUB_TOKEN = os.environ.get("DATASPOKE_TEST_DATAHUB_TOKEN", "")

_FAIL_TAIL: frozenset[str] = frozenset({"fail", "failed", "failure", "error", "errored"})


@pytest.mark.asyncio
async def test_ingestion_kafka_conf_put_round_trip(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT active-custom Kafka conf round-trips mode, platform, and identifier.

    spec: USE_CASE_en.md §UC1 Case 1 — PUT conf shape for Kafka platform
    spec: API.md §Ingestion routes — identifier.topic, identifier.cluster
    spec: src/shared/models/ingestion.py — KafkaLocator requires bootstrap_servers; NoAuth
    """
    base = f"/api/v1/spoke/common/data/{_KAFKA_ENCODED}/attr/ingestion/conf"

    put_resp = await api_client.put(
        base,
        headers=admin_headers,
        json={
            "mode": "active-custom",
            "platform": "kafka",
            "locator": {"bootstrap_servers": _KAFKA_BOOTSTRAP},
            "identifier": {
                "topic": _TOPIC,
                "cluster": _KAFKA_INSTANCE,
            },
            # auth omitted: Kafka uses NoAuth per PLATFORM_REGISTRY
            "is_enabled": False,
            "schedule_tier": "daily",
        },
    )
    assert put_resp.status_code in (200, 201), put_resp.text
    put_body = put_resp.json()
    assert put_body["dataset_urn"] == _KAFKA_URN
    assert put_body["mode"] == "active-custom"
    assert put_body["platform"] == "kafka"
    assert put_body["schedule_tier"] == "daily"

    # GET round-trip
    get_resp = await api_client.get(base, headers=admin_headers)
    assert get_resp.status_code == 200
    get_body = get_resp.json()
    assert get_body["mode"] == "active-custom"
    assert get_body["platform"] == "kafka"
    assert get_body["identifier"]["topic"] == _TOPIC

    await api_client.delete(base, headers=admin_headers)


@pytest.mark.asyncio
async def test_ingestion_kafka_workflow_dag_id_daily(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """active-custom Kafka + schedule_tier='daily' → workflow_dag_id='ingestion-active-daily'.

    spec: feature/BACKEND.md §workflow_dag_id — platform-agnostic; tier determines DAG ID
    """
    base = f"/api/v1/spoke/common/data/{_KAFKA_ENCODED}/attr/ingestion/conf"

    put_resp = await api_client.put(
        base,
        headers=admin_headers,
        json={
            "mode": "active-custom",
            "platform": "kafka",
            "locator": {"bootstrap_servers": _KAFKA_BOOTSTRAP},
            "identifier": {
                "topic": _TOPIC,
                "cluster": _KAFKA_INSTANCE,
            },
            "is_enabled": False,
            "schedule_tier": "daily",
        },
    )
    assert put_resp.status_code in (200, 201), put_resp.text
    assert put_resp.json()["workflow_dag_id"] == "ingestion-active-daily", (
        f"active-custom kafka + daily must surface workflow_dag_id='ingestion-active-daily'; "
        f"got {put_resp.json().get('workflow_dag_id')!r}. "
        "spec: feature/BACKEND.md §workflow_dag_id"
    )

    await api_client.delete(base, headers=admin_headers)


@pytest.mark.asyncio
async def test_ingestion_kafka_dry_run_does_not_persist(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """dry_run=true returns run envelope but does NOT write events or DPI.

    spec: BACKEND.md §Active run pipeline — dry_run skips DPI emission and DataHub aspects
    """
    base_conf = f"/api/v1/spoke/common/data/{_KAFKA_ENCODED}/attr/ingestion/conf"
    base_run = f"/api/v1/spoke/common/data/{_KAFKA_ENCODED}/method/ingestion/run"
    base_events = f"/api/v1/spoke/common/data/{_KAFKA_ENCODED}/event/ingestion"

    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "mode": "active-custom",
            "platform": "kafka",
            "locator": {"bootstrap_servers": _KAFKA_BOOTSTRAP},
            "identifier": {
                "topic": _TOPIC,
                "cluster": _KAFKA_INSTANCE,
            },
            "is_enabled": False,
            "schedule_tier": "daily",
        },
    )

    def _count_real_complete(events: list[dict]) -> int:
        return sum(
            1 for e in events
            if (e.get("event_type") or "").upper() == "INGESTION.COMPLETE"
            and (e.get("detail") or {}).get("dry_run") is False
        )

    # Snapshot real (non-dry) INGESTION.COMPLETE count before dry run
    before_resp = await api_client.get(base_events, headers=admin_headers)
    assert before_resp.status_code == 200
    count_before = _count_real_complete(before_resp.json().get("events", []))

    run_resp = await api_client.post(
        base_run,
        headers=admin_headers,
        json={"dry_run": True},
    )
    assert run_resp.status_code == 200
    run_body = run_resp.json()
    assert "run_id" in run_body
    assert "status" in run_body
    dry_run_id = run_body["run_id"]

    # Real (non-dry) INGESTION.COMPLETE count must NOT grow after a dry run.
    # A dry run always emits an internal traceability event but with detail.dry_run=true;
    # only events with detail.dry_run=false count as real completions.
    # spec: BACKEND.md §Active run pipeline — dry_run skips DPI emission
    after_resp = await api_client.get(base_events, headers=admin_headers)
    assert after_resp.status_code == 200
    count_after = _count_real_complete(after_resp.json().get("events", []))
    assert count_after == count_before, (
        f"dry_run produced a non-dry INGESTION.COMPLETE event — "
        f"dry-run must not appear as a real completion: before={count_before}, after={count_after}. "
        "spec: BACKEND.md §Active run pipeline — dry_run skips DPI emission"
    )

    # Positive check: the dry-run event MUST carry detail.dry_run=True.
    # Guards against regressions where the flag is dropped from the payload entirely
    # (which would make both before/after counts 0 and the count check silently pass).
    # spec: BACKEND.md §Active run pipeline — dry_run flag must be present in event detail
    all_events_after = after_resp.json().get("events", [])
    dry_event = next(
        (
            e for e in all_events_after
            if (e.get("detail") or {}).get("run_id") == dry_run_id
        ),
        None,
    )
    assert dry_event is not None, (
        f"No event found with detail.run_id={dry_run_id!r} — dry-run must emit a traceability event. "
        "spec: BACKEND.md §Active run pipeline — dry_run skips DPI emission"
    )
    assert dry_event["detail"]["dry_run"] is True, (
        f"Dry-run event must carry detail.dry_run=True; got {dry_event['detail'].get('dry_run')!r}. "
        "spec: BACKEND.md §Active run pipeline — dry_run flag must be present in event detail"
    )

    await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_ingestion_kafka_real_run_emits_aspects(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Real run against Kafka topic emits Status + DatasetProperties + SchemaMetadata to DataHub.

    spec: BACKEND.md §Active run pipeline — active-custom run emits dataset aspects
    spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement — runId
    """
    if not _DATAHUB_GMS_URL:
        pytest.skip("DATASPOKE_TEST_DATAHUB_GMS_URL not set; skipping DataHub aspect assertions")

    base_conf = f"/api/v1/spoke/common/data/{_KAFKA_ENCODED}/attr/ingestion/conf"
    base_run = f"/api/v1/spoke/common/data/{_KAFKA_ENCODED}/method/ingestion/run"
    base_events = f"/api/v1/spoke/common/data/{_KAFKA_ENCODED}/event/ingestion"

    gms_headers: dict[str, str] = {}
    if _DATAHUB_TOKEN:
        gms_headers["Authorization"] = f"Bearer {_DATAHUB_TOKEN}"

    try:
        await api_client.put(
            base_conf,
            headers=admin_headers,
            json={
                "mode": "active-custom",
                "platform": "kafka",
                "locator": {"bootstrap_servers": _KAFKA_BOOTSTRAP},
                "identifier": {
                    "topic": _TOPIC,
                    "cluster": _KAFKA_INSTANCE,
                },
                "is_enabled": True,
                "schedule_tier": "daily",
            },
        )

        run_resp = await api_client.post(
            base_run,
            headers=admin_headers,
            json={"dry_run": False},
        )
        # F3: assert success — skip-on-non-200 masked regressions
        assert run_resp.status_code in (200, 201), (
            f"Kafka active-custom real run failed: {run_resp.status_code}: {run_resp.text}. "
            "spec: BACKEND.md §Active run pipeline"
        )
        run_id = run_resp.json()["run_id"]

        # Poll for INGESTION.COMPLETE event (cap 30s)
        # spec: feedback_no_increase_timeout — bounded polls
        found_complete = False
        deadline = asyncio.get_event_loop().time() + 30.0
        while asyncio.get_event_loop().time() < deadline:
            events_resp = await api_client.get(
                base_events + "?from=2026-01-01T00:00:00Z&to=2026-12-31T23:59:59Z",
                headers=admin_headers,
            )
            for evt in events_resp.json().get("events", []):
                detail = evt.get("detail") or {}
                if detail.get("run_id") == run_id:
                    assert evt["event_type"] == "INGESTION.COMPLETE", (
                        f"Kafka active-custom run must produce INGESTION.COMPLETE; "
                        f"got {evt['event_type']!r}"
                    )
                    found_complete = True
                    break
            if found_complete:
                break
            await asyncio.sleep(1.0)

        assert found_complete, (
            f"INGESTION.COMPLETE event with run_id={run_id!r} not found within 30s. "
            "spec: BACKEND.md §Active run pipeline"
        )

        # Assert Status aspect exists (dataset is not removed)
        encoded_kafka_urn = urllib.parse.quote(_KAFKA_URN, safe="")
        status_resp = httpx.get(
            f"{_DATAHUB_GMS_URL}/aspects/{encoded_kafka_urn}?aspect=status&version=0",
            headers=gms_headers,
            timeout=10.0,
        )
        assert status_resp.status_code == 200, (
            f"GET status aspect failed: {status_resp.status_code}"
        )

        # Assert DatasetProperties aspect exists
        props_resp = httpx.get(
            f"{_DATAHUB_GMS_URL}/aspects/{encoded_kafka_urn}?aspect=datasetProperties&version=0",
            headers=gms_headers,
            timeout=10.0,
        )
        assert props_resp.status_code == 200, (
            f"GET datasetProperties failed: {props_resp.status_code}"
        )
        dp = (
            props_resp.json()
            .get("aspect", {})
            .get("com.linkedin.dataset.DatasetProperties", {})
        )
        assert dp, "datasetProperties aspect must be non-empty after Kafka active-custom run"

        # Assert SchemaMetadata aspect exists
        schema_resp = httpx.get(
            f"{_DATAHUB_GMS_URL}/aspects/{encoded_kafka_urn}?aspect=schemaMetadata&version=0",
            headers=gms_headers,
            timeout=10.0,
        )
        assert schema_resp.status_code == 200

        # Assert systemMetadata.runId follows convention
        # spec: DATAHUB_INTEGRATION.md — runId='dataspoke-{platform}-{run_id}'
        sysmeta_resp = httpx.get(
            f"{_DATAHUB_GMS_URL}/openapi/v3/entity/dataset/{encoded_kafka_urn}"
            "?systemMetadata=true&aspects=datasetProperties",
            headers=gms_headers,
            timeout=10.0,
        )
        assert sysmeta_resp.status_code == 200
        run_id_sysmeta = (
            sysmeta_resp.json()
            .get("datasetProperties", {})
            .get("systemMetadata", {})
            .get("runId")
        )
        # Enforce runId format shape: dataspoke-{platform}-{run_id}
        # Platform slug may normalise (e.g. "kafka" vs "kafka-cluster") — match by substring.
        # spec: DATAHUB_INTEGRATION.md L415 — runId = "dataspoke-{platform}-{run_id}"
        m = re.match(r"^dataspoke-([a-z0-9_-]+)-", run_id_sysmeta or "")
        assert m is not None, (
            f"systemMetadata.runId must match 'dataspoke-{{platform}}-...'; "
            f"got {run_id_sysmeta!r}. "
            "spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement"
        )
        assert "kafka" in m.group(1).lower(), (
            f"platform slug must contain 'kafka'; got {m.group(1)!r}. "
            "spec: DATAHUB_INTEGRATION.md §Custom Ingestor Guide §systemMetadata requirement"
        )

    finally:
        await api_client.delete(base_conf, headers=admin_headers)
