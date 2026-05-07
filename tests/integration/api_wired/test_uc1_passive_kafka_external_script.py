"""UC1 Case 3 — Passive Kafka via external script: end-to-end through public REST API.

An external script emits a DataProcessInstance lifecycle (Properties, Relationships, Output,
STARTED, dataset aspects, COMPLETE) for the imazon.orders.events Kafka topic; DataSpoke's
passive-sync surfaces the resulting run as an INGESTION.COMPLETE event row. Steps cover PUT
passive conf, 409 INGESTION_NOT_APPLICABLE rejection, DPI emission via DataHubClient, bounded
passive-sync polling, event-row assertion, and cross-dataset overview verification.
"""
# spec: USE_CASE_en.md §UC1

import asyncio
import os
import time
import urllib.parse
import uuid

import httpx
import pytest

# ── Dataset URN constants ─────────────────────────────────────────────────────

# spec: USE_CASE_en.md §UC1 Case 3 — imazon.orders.events Kafka topic
# spec: TESTING.md §Imazon Dummy-Data Reference — Kafka topic imazon.orders.events
_PASSIVE_KAFKA_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.orders.events,DEV)"
)
_PASSIVE_KAFKA_ENCODED = urllib.parse.quote(_PASSIVE_KAFKA_URN, safe="")

# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_TOPICS: frozenset[str] = frozenset(["imazon.orders.events"])


@pytest.mark.asyncio
async def test_uc1_passive_kafka_via_external_script(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """UC1 Case 3 — External script emits a DPI; DataSpoke passive sync surfaces it.

    Narrative: "Imazon needs to load metadata for a Kafka topic from a one-off context:
    a developer runs a Python script using the acryl-datahub SDK that emits Status,
    SchemaMetadata, and a DataProcessInstance per invocation."
    spec: USE_CASE_en.md §UC1 Case 3

    Steps:
      1. PUT passive-mode conf for imazon.orders.events Kafka dataset
      2. Verify POST method/ingestion/run returns 409 INGESTION_NOT_APPLICABLE
      3. GET event/ingestion — assert initially empty for this URN
      4. Simulate external script: emit DPI lifecycle via DataHub client
         (Properties + Output + STARTED + dataset aspects + COMPLETE with SUCCESS)
      5. Trigger sync_passive_status via internal activity endpoint
      6. GET event/ingestion — assert one INGESTION.COMPLETE row with source='passive'
      7. Cross-dataset overview includes passive Kafka URN
      8. Cleanup: delete DataSpoke conf (reset-all handles DPI cleanup in subsequent runs)
    """
    datahub_gms_url = os.environ.get("DATASPOKE_DATAHUB_GMS_URL", "")
    datahub_token = os.environ.get("DATASPOKE_DATAHUB_TOKEN", "")

    if not datahub_gms_url:
        pytest.skip("DATASPOKE_DATAHUB_GMS_URL not set; skipping passive Kafka test")

    passive_conf_url = f"/api/v1/spoke/common/data/{_PASSIVE_KAFKA_ENCODED}/attr/ingestion/conf"
    passive_run_url = f"/api/v1/spoke/common/data/{_PASSIVE_KAFKA_ENCODED}/method/ingestion/run"
    passive_events_url = f"/api/v1/spoke/common/data/{_PASSIVE_KAFKA_ENCODED}/event/ingestion"

    dpi_urn: str | None = None

    try:
        # ── Step 1: PUT passive conf for Kafka dataset ────────────────────────
        # spec: USE_CASE_en.md §UC1 Case 3 — PUT passive conf (no locator/auth/schedule_tier)
        put_resp = await api_client.put(
            passive_conf_url,
            headers=admin_headers,
            json={
                "mode": "passive",
                "platform": "kafka",
                "identifier": {
                    "topic": "imazon.orders.events",
                    "cluster": "example_kafka",
                },
                "is_enabled": True,
            },
        )
        assert put_resp.status_code in (200, 201), (
            f"PUT passive Kafka conf failed: {put_resp.status_code} {put_resp.text}"
        )
        passive_body = put_resp.json()
        assert passive_body["mode"] == "passive"
        assert passive_body["platform"] == "kafka"
        assert passive_body.get("schedule_tier") is None, (
            "Passive conf must not carry schedule_tier. spec: USE_CASE_en.md §UC1 Case 3"
        )
        assert passive_body["is_enabled"] is True

        # ── Step 2: Verify method/ingestion/run returns 409 ───────────────────
        # spec: USE_CASE_en.md §UC1 API Mapping —
        #     "passive configs return 409 INGESTION_NOT_APPLICABLE"
        reject_resp = await api_client.post(
            passive_run_url,
            headers=admin_headers,
            json={"dry_run": False},
        )
        assert reject_resp.status_code == 409, (
            f"Expected 409 INGESTION_NOT_APPLICABLE; "
            f"got {reject_resp.status_code}: {reject_resp.text}"
        )
        assert reject_resp.json().get("error_code") == "INGESTION_NOT_APPLICABLE", (
            f"Expected error_code='INGESTION_NOT_APPLICABLE'; "
            f"got {reject_resp.json().get('error_code')!r}. "
            "spec: USE_CASE_en.md §UC1 API Mapping"
        )

        # ── Step 3: Event history initially empty for this URN ────────────────
        events_before_resp = await api_client.get(
            passive_events_url + "?from=2026-01-01T00:00:00Z&to=2026-12-31T23:59:59Z",
            headers=admin_headers,
        )
        assert events_before_resp.status_code == 200
        events_before = events_before_resp.json()
        assert isinstance(events_before.get("events"), list)
        ingestion_events_before = [
            e
            for e in events_before["events"]
            if e.get("event_type") in ("INGESTION.COMPLETE", "INGESTION.FAIL")
            and (e.get("detail") or {}).get("source") == "passive"
        ]

        # ── Step 4: Simulate external script — emit DPI lifecycle ─────────────
        # Mimics what a script following the Custom Ingestor Authoring Contract does.
        # spec: BACKEND.md §Custom Ingestor Authoring Contract — Required aspects per run:
        #   1. DataProcessInstanceProperties
        #   2. DataProcessInstanceRelationships
        #   3. DataProcessInstanceRunEvent(STARTED)
        #   4. Dataset aspect work (Status + DatasetProperties + SchemaMetadata)
        #   5. DataProcessInstanceRunEvent(COMPLETE/SUCCESS)
        from src.shared.datahub.client import DataHubClient

        dh_client = DataHubClient(gms_url=datahub_gms_url, token=datahub_token)

        test_run_uuid = uuid.uuid4().hex[:12]
        # spec: BACKEND.md §Custom Ingestor Authoring Contract §DPI URN convention:
        #     "urn:li:dataProcessInstance:<deterministic-id>. Recommend <platform>-<run_id>"
        dpi_urn = f"urn:li:dataProcessInstance:external-kafka-{test_run_uuid}"
        now_ms = int(time.time() * 1000)

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

        # 1. Properties
        await dh_client.emit_aspect(
            dpi_urn,
            DataProcessInstancePropertiesClass(
                name=f"external-kafka-ingestor-{test_run_uuid}",
                type=DataProcessTypeClass.BATCH_SCHEDULED,
                created=AuditStampClass(time=now_ms, actor="urn:li:corpuser:external-script"),
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
        # 2b. Output relationship (dataset this run produced)
        await dh_client.emit_aspect(
            dpi_urn,
            DataProcessInstanceOutputClass(outputs=[_PASSIVE_KAFKA_URN]),
        )
        # 3. STARTED — before any dataset aspect work
        await dh_client.emit_aspect(
            dpi_urn,
            DataProcessInstanceRunEventClass(
                status=DataProcessRunStatusClass.STARTED,
                timestampMillis=now_ms,
            ),
        )
        # 4. Dataset aspects (Status + DatasetProperties + SchemaMetadata)
        await dh_client.emit_aspect(_PASSIVE_KAFKA_URN, StatusClass(removed=False))
        await dh_client.emit_aspect(
            _PASSIVE_KAFKA_URN,
            DatasetPropertiesClass(
                name="imazon.orders.events",
                description="Order state-change events from external Kafka topic",
                customProperties={"source": "uc1-external-script-test"},
            ),
        )
        await dh_client.emit_aspect(
            _PASSIVE_KAFKA_URN,
            SchemaMetadataClass(
                schemaName="imazon.orders.events",
                platform="urn:li:dataPlatform:kafka",
                version=0,
                hash="",
                platformSchema=OtherSchemaClass(rawSchema=""),
                fields=[],
            ),
        )
        # 5. COMPLETE/SUCCESS — after all aspect work
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

        # ── Step 5: Trigger sync_passive_status (with bounded retry) ────────
        # spec: BACKEND.md §Passive status-sync pipeline — hourly DAG calls this
        # DataHub indexing of emitted DPI aspects may take a moment. Poll sync +
        # event check rather than sleeping a fixed duration.
        # spec: feedback_no_increase_timeout — bounded polls with explicit failure.
        internal_token = os.environ.get("DATASPOKE_INTERNAL_TOKEN", "")
        internal_headers_dict = {"X-Internal-Token": internal_token} if internal_token else {}

        # ── Step 6: GET event/ingestion — assert one INGESTION.COMPLETE row ───
        # spec: USE_CASE_en.md §UC1 Case 3 — "the next hourly poll surfaces a row
        # in event/ingestion exactly as in Case 2"
        events_after: dict = {}
        ingestion_events_after: list = []
        deadline = time.time() + 30.0
        while time.time() < deadline:
            sync_resp = await api_client.post(
                "/internal/activities/ingestion/passive-sync",
                headers=internal_headers_dict,
            )
            assert sync_resp.status_code in (200, 204), (
                f"Internal passive-sync activity failed: {sync_resp.status_code}: {sync_resp.text}"
            )
            events_after_resp = await api_client.get(
                passive_events_url + "?from=2026-01-01T00:00:00Z&to=2026-12-31T23:59:59Z",
                headers=admin_headers,
            )
            assert events_after_resp.status_code == 200
            events_after = events_after_resp.json()
            # The DPI emitted in Step 4 carries RunResultTypeClass.SUCCESS, so per
            # BACKEND.md §Custom Ingestor Authoring Contract the polled row MUST be
            # INGESTION.COMPLETE — a regression that mapped SUCCESS→FAIL during the
            # passive sync should fail this assertion.
            ingestion_events_after = [
                e
                for e in events_after.get("events", [])
                if e.get("event_type") == "INGESTION.COMPLETE"
                and (e.get("detail") or {}).get("source") == "passive"
            ]
            if len(ingestion_events_after) > len(ingestion_events_before):
                break
            await asyncio.sleep(1.0)

        assert len(ingestion_events_after) > len(ingestion_events_before), (
            f"Expected at least one new INGESTION.COMPLETE event with "
            f"source='passive' within 30s; before={len(ingestion_events_before)}, "
            f"after={len(ingestion_events_after)}. "
            f"Events: {events_after.get('events', [])}. "
            "spec: USE_CASE_en.md §UC1 Case 3 + BACKEND.md §Custom Ingestor Authoring Contract"
        )

        # spec: USE_CASE_en.md §UC1 Case 2 — INGESTION.COMPLETE rows carry status="success"
        new_evt = ingestion_events_after[-1]
        assert new_evt.get("status") == "success", (
            f"INGESTION.COMPLETE event must carry status='success'; got {new_evt.get('status')!r}"
        )

        # ── Step 7: Cross-dataset overview includes passive Kafka URN ─────────
        overview_resp = await api_client.get(
            "/api/v1/spoke/common/ingestion?limit=100",
            headers=admin_headers,
        )
        assert overview_resp.status_code == 200
        configs_by_urn = {c["dataset_urn"]: c for c in overview_resp.json().get("configs", [])}
        assert _PASSIVE_KAFKA_URN in configs_by_urn, (
            f"Passive Kafka URN {_PASSIVE_KAFKA_URN!r} not found in "
            "GET /spoke/common/ingestion. spec: USE_CASE_en.md §UC1 §Cross-dataset overview"
        )
        assert configs_by_urn[_PASSIVE_KAFKA_URN]["mode"] == "passive"

    finally:
        # ── Step 8: Cleanup ──────────────────────────────────────────────────
        # Delete the simulated DPI from DataHub to leave a clean state.
        # spec: BACKEND.md §Custom Ingestor Authoring Contract — DPI URN is deterministic;
        # delete it explicitly so subsequent runs don't observe stale DPI records.
        if datahub_gms_url and dpi_urn:
            dh_headers = (
                {"Authorization": f"Bearer {datahub_token}", "Content-Type": "application/json"}
                if datahub_token
                else {"Content-Type": "application/json"}
            )
            delete_entity_mutation = """
            mutation deleteEntity($urn: String!, $soft: Boolean!) {
                deleteEntity(urn: $urn, soft: $soft)
            }
            """
            try:
                httpx.post(
                    f"{datahub_gms_url}/api/graphql",
                    headers=dh_headers,
                    json={
                        "query": delete_entity_mutation,
                        "variables": {"urn": dpi_urn, "soft": False},
                    },
                    timeout=10.0,
                )
            except Exception:
                pass

        # Delete DataSpoke passive conf
        await api_client.delete(passive_conf_url, headers=admin_headers)
