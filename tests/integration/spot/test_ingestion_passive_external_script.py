"""Spot tests — Ingestion Control: passive Kafka (external-script bare DPI).

Concerns covered:
- PUT passive conf for imazon.orders.events; response carries mode='passive', no schedule_tier
- POST method/ingestion/run returns 409 INGESTION_NOT_APPLICABLE
- Emit bare DPI lifecycle (no ingestionSource attribution) via DataHubClient; trigger
  passive-sync; assert INGESTION.COMPLETE event row with source='passive' within 30s.

The bare-DPI emission (no ingestionSource property) simulates an external script following
the Custom Ingestor Authoring Contract (USE_CASE_en.md §UC1 Case 3).
"""
# spec: USE_CASE_en.md §UC1 Case 3 — passive Kafka via external script
# spec: API.md §Ingestion routes — passive mode for Kafka
# spec: BACKEND.md §Passive status-sync pipeline — sync_passive_status()

import asyncio
import os
import time
import urllib.parse
import uuid

import httpx
import pytest

from datahub.metadata.schema_classes import (
    AuditStampClass,
    DataProcessInstanceOutputClass,
    DataProcessInstancePropertiesClass,
    DataProcessInstanceRelationshipsClass,
    DataProcessInstanceRunEventClass,
    DataProcessInstanceRunResultClass,
    DataProcessRunStatusClass,
    DataProcessTypeClass,
    DatasetPropertiesClass,
    OtherSchemaClass,
    RunResultTypeClass,
    SchemaMetadataClass,
    StatusClass,
)

from src.shared.datahub.client import DataHubClient

# Per-module dummy-data seed — Kafka topic triggers DataHub ingest.
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_TOPICS: frozenset[str] = frozenset({"imazon.orders.events"})

_KAFKA_INSTANCE = os.environ.get("DATASPOKE_DEV_DUMMY_DATA_KAFKA_INSTANCE", "example_kafka")
_TOPIC = "imazon.orders.events"
_DATAHUB_GMS_URL = os.environ.get("DATASPOKE_DEV_DATAHUB_GMS_URL", "")
_DATAHUB_TOKEN = os.environ.get("DATASPOKE_DEV_DATAHUB_TOKEN", "")

# spec: TESTING.md §Imazon Dummy-Data Reference — Kafka topic imazon.orders.events
_PASSIVE_KAFKA_URN = (
    f"urn:li:dataset:(urn:li:dataPlatform:kafka,{_KAFKA_INSTANCE}.{_TOPIC},DEV)"
)
_PASSIVE_KAFKA_ENCODED = urllib.parse.quote(_PASSIVE_KAFKA_URN, safe="")


@pytest.mark.asyncio
async def test_ingestion_passive_kafka_put_shape(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """PUT passive Kafka conf returns mode='passive', no schedule_tier.

    spec: USE_CASE_en.md §UC1 Case 3 — passive conf has no locator/auth/schedule_tier
    """
    base = f"/api/v1/spoke/common/data/{_PASSIVE_KAFKA_ENCODED}/attr/ingestion/conf"

    put_resp = await api_client.put(
        base,
        headers=admin_headers,
        json={
            "mode": "passive",
            "platform": "kafka",
            "identifier": {
                "topic": _TOPIC,
                "cluster": _KAFKA_INSTANCE,
            },
            "is_enabled": True,
        },
    )
    assert put_resp.status_code in (200, 201), put_resp.text
    body = put_resp.json()
    assert body["mode"] == "passive"
    assert body["platform"] == "kafka"
    assert body.get("schedule_tier") is None, (
        "Passive conf must not carry schedule_tier. spec: USE_CASE_en.md §UC1 Case 3"
    )
    assert body["is_enabled"] is True

    await api_client.delete(base, headers=admin_headers)


@pytest.mark.asyncio
async def test_ingestion_passive_kafka_run_returns_409_not_applicable(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST method/ingestion/run on a passive Kafka conf returns 409 INGESTION_NOT_APPLICABLE.

    spec: USE_CASE_en.md §UC1 API Mapping — passive configs return 409 INGESTION_NOT_APPLICABLE
    spec: API.md §Application Error Codes — INGESTION_NOT_APPLICABLE
    """
    base_conf = f"/api/v1/spoke/common/data/{_PASSIVE_KAFKA_ENCODED}/attr/ingestion/conf"
    base_run = f"/api/v1/spoke/common/data/{_PASSIVE_KAFKA_ENCODED}/method/ingestion/run"

    await api_client.put(
        base_conf,
        headers=admin_headers,
        json={
            "mode": "passive",
            "platform": "kafka",
            "identifier": {"topic": _TOPIC, "cluster": _KAFKA_INSTANCE},
            "is_enabled": True,
        },
    )

    reject_resp = await api_client.post(
        base_run,
        headers=admin_headers,
        json={"dry_run": False},
    )
    assert reject_resp.status_code == 409, (
        f"Expected 409 INGESTION_NOT_APPLICABLE; "
        f"got {reject_resp.status_code}: {reject_resp.text}"
    )
    body = reject_resp.json()
    assert body.get("error_code") == "INGESTION_NOT_APPLICABLE", (
        f"Expected error_code='INGESTION_NOT_APPLICABLE'; got {body}. "
        "spec: USE_CASE_en.md §UC1 API Mapping"
    )

    await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_ingestion_passive_kafka_external_script_sync_emits_event(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    """Bare DPI emission (no ingestionSource) → passive-sync → INGESTION.COMPLETE event.

    Simulates an external script emitting a DataProcessInstance lifecycle without any
    ingestionSource attribution: Properties + Relationships + Output + STARTED + dataset
    aspects + COMPLETE/SUCCESS.

    The new event is identified by filtering on run_id matching the emitted dpi_urn,
    not by list ordering, to avoid ordering-assumption failures.

    spec: USE_CASE_en.md §UC1 Case 3 — external script DPI, passive sync surfaces the run
    spec: BACKEND.md §Custom Ingestor Authoring Contract — Required aspects per run
    """
    if not _DATAHUB_GMS_URL:
        pytest.skip("DATASPOKE_DEV_DATAHUB_GMS_URL not set; skipping passive sync test")

    base_conf = f"/api/v1/spoke/common/data/{_PASSIVE_KAFKA_ENCODED}/attr/ingestion/conf"
    base_events = f"/api/v1/spoke/common/data/{_PASSIVE_KAFKA_ENCODED}/event/ingestion"

    dpi_urn: str | None = None

    try:
        # PUT passive conf
        put_resp = await api_client.put(
            base_conf,
            headers=admin_headers,
            json={
                "mode": "passive",
                "platform": "kafka",
                "identifier": {"topic": _TOPIC, "cluster": _KAFKA_INSTANCE},
                "is_enabled": True,
            },
        )
        assert put_resp.status_code in (200, 201), put_resp.text

        # Emit bare DPI lifecycle (no ingestionSource property)
        # spec: BACKEND.md §Custom Ingestor Authoring Contract — Required aspects per run:
        #   1. DataProcessInstanceProperties (NO ingestionSource — distinguishes from Managed)
        #   2. DataProcessInstanceRelationships
        #   3. DataProcessInstanceOutput
        #   4. DataProcessInstanceRunEvent(STARTED)
        #   5. Dataset aspects (Status + DatasetProperties + SchemaMetadata)
        #   6. DataProcessInstanceRunEvent(COMPLETE/SUCCESS)
        dh_client = DataHubClient(gms_url=_DATAHUB_GMS_URL, token=_DATAHUB_TOKEN)
        test_run_uuid = uuid.uuid4().hex[:12]
        dpi_urn = f"urn:li:dataProcessInstance:external-kafka-{test_run_uuid}"
        now_ms = int(time.time() * 1000)

        # 1. Properties — no ingestionSource (bare external-script shape)
        await dh_client.emit_aspect(
            dpi_urn,
            DataProcessInstancePropertiesClass(
                name=f"external-kafka-ingestor-{test_run_uuid}",
                type=DataProcessTypeClass.BATCH_SCHEDULED,
                created=AuditStampClass(
                    time=now_ms, actor="urn:li:corpuser:external-script"
                ),
            ),
        )
        # 2. Relationships
        await dh_client.emit_aspect(
            dpi_urn,
            DataProcessInstanceRelationshipsClass(
                upstreamInstances=[],
                parentTemplate=None,
            ),
        )
        # 3. Output relationship
        await dh_client.emit_aspect(
            dpi_urn,
            DataProcessInstanceOutputClass(outputs=[_PASSIVE_KAFKA_URN]),
        )
        # 4. STARTED
        await dh_client.emit_aspect(
            dpi_urn,
            DataProcessInstanceRunEventClass(
                status=DataProcessRunStatusClass.STARTED,
                timestampMillis=now_ms,
            ),
        )
        # 5. Dataset aspects
        await dh_client.emit_aspect(_PASSIVE_KAFKA_URN, StatusClass(removed=False))
        await dh_client.emit_aspect(
            _PASSIVE_KAFKA_URN,
            DatasetPropertiesClass(
                name=_TOPIC,
                description="Order state-change events from external Kafka topic",
                customProperties={"source": "spot-external-script-test"},
            ),
        )
        await dh_client.emit_aspect(
            _PASSIVE_KAFKA_URN,
            SchemaMetadataClass(
                schemaName=_TOPIC,
                platform="urn:li:dataPlatform:kafka",
                version=0,
                hash="",
                platformSchema=OtherSchemaClass(rawSchema=""),
                fields=[],
            ),
        )
        # 6. COMPLETE/SUCCESS
        end_ms = int(time.time() * 1000)
        await dh_client.emit_aspect(
            dpi_urn,
            DataProcessInstanceRunEventClass(
                status=DataProcessRunStatusClass.COMPLETE,
                timestampMillis=end_ms,
                result=DataProcessInstanceRunResultClass(
                    type=RunResultTypeClass.SUCCESS,
                    nativeResultType="external-script",
                ),
                durationMillis=end_ms - now_ms,
            ),
        )

        # Poll passive-sync + event check (cap 30s)
        # Filter by run_id == dpi_urn to avoid ordering assumptions (F9)
        # spec: feedback_no_increase_timeout — bounded polls
        events_body: dict = {}
        matching_events: list = []
        deadline = time.time() + 30.0
        while time.time() < deadline:
            try:
                sync_resp = await api_client.post(
                    "/internal/activities/ingestion/passive-sync",
                    headers=internal_headers,
                )
            except httpx.ReadTimeout:
                await asyncio.sleep(1.0)
                continue
            assert sync_resp.status_code in (200, 204), (
                f"Internal passive-sync activity failed: {sync_resp.status_code}: {sync_resp.text}"
            )
            events_resp = await api_client.get(
                base_events + "?from=2026-01-01T00:00:00Z&to=2026-12-31T23:59:59Z",
                headers=admin_headers,
            )
            assert events_resp.status_code == 200
            events_body = events_resp.json()
            # Filter by run_id == dpi_urn rather than relying on list ordering
            matching_events = [
                e
                for e in events_body.get("events", [])
                if e.get("event_type") == "INGESTION.COMPLETE"
                and (e.get("detail") or {}).get("source") == "passive"
                and (e.get("detail") or {}).get("run_id") == dpi_urn
            ]
            if matching_events:
                break
            await asyncio.sleep(1.0)

        assert matching_events, (
            f"Expected INGESTION.COMPLETE event with run_id={dpi_urn!r} and source='passive' "
            f"within 30s. Events: {events_body.get('events', [])}. "
            "spec: USE_CASE_en.md §UC1 Case 3 — passive sync writes event rows"
        )

        # Verify status='success' on the matched event (case-insensitive for enum consistency)
        # spec: USE_CASE_en.md §UC1 — INGESTION.COMPLETE rows carry status='success'
        matched_evt = matching_events[0]
        assert (matched_evt.get("status") or "").lower() == "success", (
            f"INGESTION.COMPLETE event must carry status='success'; "
            f"got {matched_evt.get('status')!r}"
        )

    finally:
        if _DATAHUB_GMS_URL and dpi_urn:
            gql_headers = (
                {"Authorization": f"Bearer {_DATAHUB_TOKEN}", "Content-Type": "application/json"}
                if _DATAHUB_TOKEN
                else {"Content-Type": "application/json"}
            )
            try:
                httpx.post(
                    f"{_DATAHUB_GMS_URL}/api/graphql",
                    headers=gql_headers,
                    json={
                        "query": "mutation D($urn:String!,$soft:Boolean!){deleteEntity(urn:$urn,soft:$soft)}",
                        "variables": {"urn": dpi_urn, "soft": False},
                    },
                    timeout=10.0,
                )
            except httpx.HTTPError as e:
                print(f"cleanup warning: DPI delete failed for {dpi_urn!r}: {e}")

        await api_client.delete(base_conf, headers=admin_headers)
