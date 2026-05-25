"""UC4 — Metadata Generation: end-to-end through public REST API.

Two structurally identical tests mirror the UC4 user-story arc under stub mode
and real-LLM mode. The arc: a governance operator scopes metagen to fulfillment-
tagged datasets, seeds LLM context (a fulfillment document + UC3-approved
ontology nodes), masks descriptions in DataHub to give the model something to
predict, fires a run, reviews candidates (approve / reject), verifies DataHub
changes on approval, fires the run again, and asserts approved items are skipped
while rejected items are cleared and re-generated.

Spec: spec/USE_CASE_en.md §UC4 Metadata Generation
Spec: spec/feature/BACKEND.md §UC4 Metadata Generation

Function-level coverage (conf CRUD, boundary CRUD, run-gating, candidate review
edge cases, item filters) lives in tests/integration/spot/ and is not duplicated
here.
"""
# spec: USE_CASE_en.md §UC4

import urllib.parse
import uuid
from contextlib import suppress

import httpx
import pytest
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.graph.client import DatahubClientConfig, DataHubGraph
from datahub.metadata.schema_classes import (
    DatasetPropertiesClass,
    EditableDatasetPropertiesClass,
    EditableSchemaMetadataClass,
    SchemaMetadataClass,
)
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.util.datahub import (
    _gms_url,
    get_datahub_token,
    hard_delete_document,
    seed_native_document,
)
from tests.integration.util.metagen import (
    EU_PROFILES_URN,
    FULFILLMENT_TAG,
    ORDERS_EVENTS_URN,
    delete_metagen_state_for_urn,
    delete_ontogen_node,
    load_fulfillment_doc,
    seed_approved_ontogen_node,
    seed_dataset_node_map,
)

# ── Module-level constants ─────────────────────────────────────────────────────

# Declare fixture dependencies so module_dummy_data seeds the right schemas and
# DataHub entities. spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset(
    {"customers", "orders", "shipping", "catalog"}
)
DUMMY_DATA_DATAHUB_TOPICS: frozenset[str] = frozenset({"imazon.orders.events"})

_EU_ENCODED = urllib.parse.quote(EU_PROFILES_URN, safe="")
_OE_ENCODED = urllib.parse.quote(ORDERS_EVENTS_URN, safe="")

# ── Tests ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_uc4_metadata_generation_under_stub(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
    runtime_conf: dict,
) -> None:
    """UC4 user-story arc under stub mode (stub_llm_client=true in runtime conf).

    Steps mirror USE_CASE_en.md §UC4 Imazon Example:
      1.  Seed LLM context: fulfillment document + 5 approved ontogen nodes
          mapped to both datasets
      2.  Mask descriptions in DataHub: wipe eu_profiles dataset description and
          all column descriptions; wipe first 4 orders.events column descriptions
      3.  PUT global metagen conf (is_enabled, schedule_tier, dataset_filter, tags)
      4.  PUT per-dataset boundaries for eu_profiles and orders.events
      5.  POST method/run (first run) — assert MetagenRunResponse shape
      6.  GET global event — find METAGEN.RUN_COMPLETE; assert detail keys
      7.  GET per-dataset items for both URNs — assert item envelope shape
      8.  Review candidates: approve eu_profiles dataset.description, reject
          eu_profiles column.email.description, approve orders.events first masked col
      9.  DataHub round-trip: verify approved values emitted to editable aspects
     10.  GET per-dataset metagen events — assert CANDIDATE_APPROVE / CANDIDATE_REJECT
     11.  POST method/run (second run) — assert approved items skipped,
          rejected item cleared and re-generated
     12.  Cleanup (finally): delete metagen state, boundaries, conf, DataHub
          aspect snapshots, document, ontogen nodes

    Spec: USE_CASE_en.md §UC4
    Spec: BACKEND.md §UC4 Metadata Generation — MetagenRunResponse, event catalogue,
      approval pipeline, idempotency (approved items skipped on subsequent runs)
    """
    conf_url = "/api/v1/spoke/common/metagen/attr/conf"
    eu_boundary_url = f"/api/v1/spoke/common/data/{_EU_ENCODED}/attr/metagen/conf"
    oe_boundary_url = f"/api/v1/spoke/common/data/{_OE_ENCODED}/attr/metagen/conf"
    run_url = "/api/v1/spoke/common/metagen/method/run"
    global_event_url = "/api/v1/spoke/common/metagen/event"

    # Mutable state captured during the try block for use in finally.
    document_urn: str | None = None
    node_ids: list[str] = []
    graph: DataHubGraph | None = None
    # Aspect snapshots captured before masking — restored in cleanup.
    eu_props_snapshot: DatasetPropertiesClass | None = None
    eu_schema_snapshot: SchemaMetadataClass | None = None
    oe_schema_snapshot: SchemaMetadataClass | None = None
    # First 4 orders.events field paths selected at runtime from the seeded schema.
    masked_oe_field_paths: list[str] = []
    # Original field descriptions captured before in-place mutation so cleanup
    # can restore the correct values (not the masked-out Nones).
    eu_original_dataset_description: str | None = None
    eu_original_field_descs: dict[str, str | None] = {}
    oe_original_field_descs: dict[str, str | None] = {}

    try:
        # ── Step 1: Seed LLM context ──────────────────────────────────────────
        # 1a. Seed a fulfillment domain document so metagen evidence includes
        #     narrative context about eu_profiles and orders.events.
        # spec: USE_CASE_en.md §UC4 L565 — LLM context includes related documents
        dh_token = get_datahub_token()
        document_id = uuid.uuid4().hex[:16]
        document_urn = seed_native_document(
            document_id=document_id,
            title="Imazon Fulfillment Process Guide",
            body_markdown=load_fulfillment_doc(),
            related_dataset_urns=[EU_PROFILES_URN, ORDERS_EVENTS_URN],
            token=dh_token,
        )

        # 1b. Seed 5 approved ontogen nodes for fulfillment concepts and map them
        #     to both datasets so metagen evidence includes ontology context.
        # spec: BACKEND.md §UC4 — UC4 reads UC3-approved ontology nodes via
        # dataset_node_map.status='approved'.
        suffix = uuid.uuid4().hex[:8]
        node_names = ["Order", "OrderLine", "Customer", "ShipmentEvent", "DeliveryStatus"]
        for name in node_names:
            candidate_id = f"uc4-{name.lower()}-{suffix}"
            actual_id = await seed_approved_ontogen_node(async_session, candidate_id, name)
            node_ids.append(actual_id)
            for urn in (EU_PROFILES_URN, ORDERS_EVENTS_URN):
                await seed_dataset_node_map(async_session, dataset_urn=urn, node_id=actual_id)

        # ── Step 2: Mask descriptions in DataHub ──────────────────────────────
        # Snapshot the current aspects before mutation so cleanup can restore them.
        # spec: USE_CASE_en.md §UC4 L588 — metagen generates what is currently missing
        graph = DataHubGraph(DatahubClientConfig(server=_gms_url, token=dh_token))

        # eu_profiles: snapshot DatasetProperties + SchemaMetadata, then mask both.
        eu_props_snapshot = graph.get_aspect(
            entity_urn=EU_PROFILES_URN, aspect_type=DatasetPropertiesClass
        )
        eu_schema_snapshot = graph.get_aspect(
            entity_urn=EU_PROFILES_URN, aspect_type=SchemaMetadataClass
        )

        # Capture original values before any in-place mutation so cleanup can
        # restore them accurately.
        eu_original_dataset_description = eu_props_snapshot.description if eu_props_snapshot else None
        if eu_schema_snapshot is not None and hasattr(eu_schema_snapshot, "fields"):
            for f in eu_schema_snapshot.fields:
                eu_original_field_descs[f.fieldPath] = f.description

        if eu_props_snapshot is not None:
            # Wipe description; preserve other fields (name, qualifiedName, etc.).
            masked_eu_props = DatasetPropertiesClass(
                name=eu_props_snapshot.name,
                qualifiedName=eu_props_snapshot.qualifiedName,
                description="",
                customProperties=eu_props_snapshot.customProperties,
            )
            graph.emit_mcp(
                MetadataChangeProposalWrapper(
                    entityUrn=EU_PROFILES_URN, aspect=masked_eu_props
                )
            )

        if eu_schema_snapshot is not None and hasattr(eu_schema_snapshot, "fields"):
            for f in eu_schema_snapshot.fields:
                f.description = None
            graph.emit_mcp(
                MetadataChangeProposalWrapper(
                    entityUrn=EU_PROFILES_URN, aspect=eu_schema_snapshot
                )
            )

        # orders.events: snapshot SchemaMetadata, pick first 4 field paths to mask.
        oe_schema_snapshot = graph.get_aspect(
            entity_urn=ORDERS_EVENTS_URN, aspect_type=SchemaMetadataClass
        )

        # Capture original field descriptions before masking.
        if oe_schema_snapshot is not None and hasattr(oe_schema_snapshot, "fields"):
            for f in oe_schema_snapshot.fields:
                oe_original_field_descs[f.fieldPath] = f.description

        if oe_schema_snapshot is not None and hasattr(oe_schema_snapshot, "fields"):
            all_field_paths = [f.fieldPath for f in oe_schema_snapshot.fields]
            masked_oe_field_paths = all_field_paths[:4]
            for f in oe_schema_snapshot.fields:
                if f.fieldPath in masked_oe_field_paths:
                    f.description = None
            graph.emit_mcp(
                MetadataChangeProposalWrapper(
                    entityUrn=ORDERS_EVENTS_URN, aspect=oe_schema_snapshot
                )
            )

        # ── Step 3: PUT global metagen conf ──────────────────────────────────
        # UC4 narrative: "The governance team enables metagen globally, scoped
        # to fulfillment-tagged datasets."
        # spec: USE_CASE_en.md §UC4 L694–706
        put_conf_resp = await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "is_enabled": True,
                "schedule_tier": "daily",
                "dataset_filter": {"origin": "DEV", "tags": [FULFILLMENT_TAG]},
                "result_limit": 3,
                "overwrite_pending": True,
            },
        )
        assert put_conf_resp.status_code in (200, 201), (
            f"PUT global metagen conf failed: "
            f"{put_conf_resp.status_code} {put_conf_resp.text}. "
            "spec: USE_CASE_en.md §UC4 L694 — PUT returns 200 or 201"
        )
        conf_body = put_conf_resp.json()
        assert conf_body["is_enabled"] is True, (
            "PUT conf response must round-trip is_enabled=True. "
            "spec: USE_CASE_en.md §UC4 L604"
        )
        assert conf_body["schedule_tier"] == "daily", (
            f"schedule_tier not preserved: {conf_body.get('schedule_tier')!r}. "
            "spec: USE_CASE_en.md §UC4 L604"
        )
        assert conf_body["result_limit"] == 3, (
            f"result_limit not preserved: {conf_body.get('result_limit')!r}. "
            "spec: USE_CASE_en.md §UC4 L605"
        )
        assert conf_body["overwrite_pending"] is True, (
            f"overwrite_pending not preserved: {conf_body.get('overwrite_pending')!r}. "
            "spec: USE_CASE_en.md §UC4 L606"
        )
        assert conf_body["dataset_filter"] == {"origin": "DEV", "tags": [FULFILLMENT_TAG]}, (
            f"dataset_filter not preserved: {conf_body.get('dataset_filter')!r}. "
            "spec: USE_CASE_en.md §UC4 L604"
        )

        # ── Step 4: PUT per-dataset boundaries ───────────────────────────────
        # UC4 narrative: "The catalog team opts each dataset in."
        # spec: USE_CASE_en.md §UC4 L609–615
        put_eu_boundary_resp = await api_client.put(
            eu_boundary_url,
            headers=admin_headers,
            json={
                "is_enabled": True,
                "allowed": ["dataset.description", "column.description"],
            },
        )
        assert put_eu_boundary_resp.status_code in (200, 201), (
            f"PUT eu_profiles boundary failed: "
            f"{put_eu_boundary_resp.status_code} {put_eu_boundary_resp.text}. "
            "spec: USE_CASE_en.md §UC4 L613"
        )
        eu_boundary_body = put_eu_boundary_resp.json()
        assert eu_boundary_body["dataset_urn"] == EU_PROFILES_URN, (
            f"boundary dataset_urn not echoed: {eu_boundary_body.get('dataset_urn')!r}. "
            "spec: USE_CASE_en.md §UC4 L613"
        )
        assert set(eu_boundary_body["allowed"]) == {"dataset.description", "column.description"}, (
            f"eu_profiles boundary allowed not preserved: {eu_boundary_body.get('allowed')!r}. "
            "spec: USE_CASE_en.md §UC4 L614"
        )

        put_oe_boundary_resp = await api_client.put(
            oe_boundary_url,
            headers=admin_headers,
            json={
                "is_enabled": True,
                "allowed": ["column.description"],
            },
        )
        assert put_oe_boundary_resp.status_code in (200, 201), (
            f"PUT orders.events boundary failed: "
            f"{put_oe_boundary_resp.status_code} {put_oe_boundary_resp.text}. "
            "spec: USE_CASE_en.md §UC4 L613"
        )
        oe_boundary_body = put_oe_boundary_resp.json()
        assert oe_boundary_body["dataset_urn"] == ORDERS_EVENTS_URN, (
            f"orders.events boundary dataset_urn not echoed: "
            f"{oe_boundary_body.get('dataset_urn')!r}. spec: USE_CASE_en.md §UC4 L613"
        )
        assert "column.description" in oe_boundary_body["allowed"], (
            "orders.events boundary allowed must include 'column.description'. "
            "spec: USE_CASE_en.md §UC4 L614"
        )

        # ── Step 5: POST method/run (first run) ───────────────────────────────
        # UC4 narrative: "The daily Airflow DAG fires (or reviewer triggers an
        # immediate run)."
        # spec: USE_CASE_en.md §UC4 L720–723 — POST with no body
        # spec: BACKEND.md §UC4 — MetagenRunResponse: run_id, status, dry_run,
        #   unresolved_urns, counts, producer_iterations, debate_outcome
        run_resp = await api_client.post(run_url, headers=admin_headers, timeout=90.0)
        assert run_resp.status_code == 200, (
            f"POST method/run (first run) failed: "
            f"{run_resp.status_code} {run_resp.text}. "
            "spec: USE_CASE_en.md §UC4 L720 — method/run with is_enabled=True returns 200"
        )
        run_body = run_resp.json()
        first_run_id = run_body["run_id"]

        assert "run_id" in run_body, (
            "MetagenRunResponse must carry 'run_id'. spec: BACKEND.md §UC4"
        )
        uuid.UUID(run_body["run_id"])  # raises ValueError if malformed
        assert run_body.get("status") == "success", (
            f"MetagenRunResponse status expected 'success'; got {run_body.get('status')!r}. "
            "spec: BACKEND.md §UC4"
        )
        assert run_body.get("dry_run") is False, (
            f"MetagenRunResponse dry_run must be False for a real run; "
            f"got {run_body.get('dry_run')!r}. spec: BACKEND.md §UC4"
        )
        assert isinstance(run_body.get("unresolved_urns"), list), (
            "MetagenRunResponse unresolved_urns must be a list. spec: BACKEND.md §UC4"
        )
        assert run_body["unresolved_urns"] == [], (
            f"Expected no unresolved URNs with tag-scoped filter; "
            f"got {run_body['unresolved_urns']!r}. spec: BACKEND.md §UC4"
        )

        first_counts = run_body.get("counts")
        assert isinstance(first_counts, dict), (
            "MetagenRunResponse counts must be a dict. spec: BACKEND.md §UC4"
        )
        assert "items_considered" in first_counts, (
            "MetagenRunResponse counts must contain 'items_considered'. "
            "spec: BACKEND.md event catalogue L766"
        )
        # eu_profiles: 1 dataset.description + 8 column descriptions = 9 items
        # orders.events: ≥1 column descriptions (4 masked) = ≥1 item
        # Total items_considered ≥ 1 deterministically.
        # spec: BACKEND.md event catalogue L766 — items_considered = in-scope target items
        assert (
            isinstance(first_counts["items_considered"], int)
            and first_counts["items_considered"] >= 1
        ), (
            f"counts.items_considered must be int ≥ 1 given scoped datasets + boundaries; "
            f"got {first_counts.get('items_considered')!r}. spec: BACKEND.md event catalogue L766"
        )
        assert "candidates_added" in first_counts, (
            "MetagenRunResponse counts must contain 'candidates_added' on a real run. "
            "spec: BACKEND.md event catalogue L766"
        )
        # spec: BACKEND.md §UC4 — debate_outcome ∈ {accept, turns_exhausted, cycle_detected}
        assert run_body.get("debate_outcome") in ("accept", "turns_exhausted", "cycle_detected"), (
            f"MetagenRunResponse debate_outcome={run_body.get('debate_outcome')!r} not in "
            "canonical set. spec: BACKEND.md §UC4 — METAGEN.RUN_COMPLETE detail"
        )
        assert (
            isinstance(run_body.get("producer_iterations"), int)
            and run_body["producer_iterations"] >= 1
        ), (
            f"MetagenRunResponse producer_iterations must be int ≥ 1; "
            f"got {run_body.get('producer_iterations')!r}. spec: BACKEND.md §UC4"
        )
        # Under stub mode the stub branches ensure at least one candidate is generated.
        # spec: BACKEND_LLM.md §Test Mode — metagen Producer stub emits one candidate
        #   per target item; metagen Reviewer stub accepts.
        if runtime_conf.get("stub_llm_client"):
            assert first_counts.get("candidates_added", 0) >= 1, (
                "Stub metagen_validate branch produced zero candidates; prompt-format drift "
                "between src/backend/metagen/prompts.py and src/workflows/_stubs.py probably "
                "broke the stub parser."
            )

        # ── Step 6: GET global event — assert METAGEN.RUN_COMPLETE detail ─────
        # spec: BACKEND.md event catalogue L766 — detail keys: run_id, unresolved_urns,
        #   counts, dry_run, producer_iterations, debate_outcome
        event_resp = await api_client.get(
            f"{global_event_url}?limit=20",
            headers=admin_headers,
        )
        assert event_resp.status_code == 200, (
            f"GET global metagen event failed: {event_resp.status_code}"
        )
        event_body = event_resp.json()
        assert "events" in event_body, "spec: API.md §Standard Envelope — 'events' key required"
        assert "offset" in event_body, "spec: API.md §Standard Envelope — 'offset' key required"
        assert "limit" in event_body, "spec: API.md §Standard Envelope — 'limit' key required"
        assert "total_count" in event_body, (
            "spec: API.md §Standard Envelope — 'total_count' key required"
        )
        assert isinstance(event_body["events"], list)

        run_complete_event = next(
            (
                e
                for e in event_body["events"]
                if e["event_type"] == "METAGEN.RUN_COMPLETE"
                and e.get("detail", {}).get("run_id") == first_run_id
            ),
            None,
        )
        assert run_complete_event is not None, (
            f"No METAGEN.RUN_COMPLETE event found with run_id={first_run_id!r} after method/run. "
            "spec: BACKEND.md event catalogue L766"
        )
        rc_detail = run_complete_event["detail"]
        assert "run_id" in rc_detail, (
            "METAGEN.RUN_COMPLETE detail missing 'run_id'. spec: BACKEND.md event catalogue L766"
        )
        assert "unresolved_urns" in rc_detail and isinstance(rc_detail["unresolved_urns"], list), (
            "METAGEN.RUN_COMPLETE detail missing or wrong type for 'unresolved_urns'. "
            "spec: BACKEND.md event catalogue L766"
        )
        assert "counts" in rc_detail and isinstance(rc_detail["counts"], dict), (
            "METAGEN.RUN_COMPLETE detail missing or wrong type for 'counts'. "
            "spec: BACKEND.md event catalogue L766"
        )
        assert rc_detail.get("dry_run") is False, (
            f"METAGEN.RUN_COMPLETE detail dry_run expected False; "
            f"got {rc_detail.get('dry_run')!r}. spec: BACKEND.md event catalogue L766"
        )
        assert rc_detail.get("debate_outcome") in ("accept", "turns_exhausted", "cycle_detected"), (
            f"METAGEN.RUN_COMPLETE detail debate_outcome={rc_detail.get('debate_outcome')!r} "
            "not in canonical set. spec: BACKEND.md event catalogue L766"
        )
        assert (
            isinstance(rc_detail.get("producer_iterations"), int)
            and rc_detail["producer_iterations"] >= 1
        ), (
            f"METAGEN.RUN_COMPLETE detail producer_iterations must be int ≥ 1; "
            f"got {rc_detail.get('producer_iterations')!r}. "
            "spec: BACKEND.md event catalogue L766"
        )

        # ── Step 7: GET per-dataset items for both URNs ───────────────────────
        # UC4 narrative: "After the run, the dashboard lists items for each dataset."
        # spec: USE_CASE_en.md §UC4 L725–730
        for urn, encoded_urn in ((EU_PROFILES_URN, _EU_ENCODED), (ORDERS_EVENTS_URN, _OE_ENCODED)):
            items_resp = await api_client.get(
                f"/api/v1/spoke/common/data/{encoded_urn}/attr/metagen/item",
                headers=admin_headers,
            )
            assert items_resp.status_code == 200, (
                f"GET per-dataset items for {urn!r} failed: "
                f"{items_resp.status_code} {items_resp.text}"
            )
            items_body = items_resp.json()
            assert "items" in items_body and isinstance(items_body["items"], list), (
                "Item list response must carry 'items' list. spec: API.md §Standard Envelope"
            )
            assert "offset" in items_body
            assert "limit" in items_body
            assert "total_count" in items_body

            for item in items_body["items"]:
                assert item["kind"] in ("dataset.description", "column.description"), (
                    f"item kind {item['kind']!r} not in allowed set. "
                    "spec: USE_CASE_en.md §UC4 L617"
                )
                assert item["status"] in ("pending", "llm_approved", "approved"), (
                    f"item status {item['status']!r} not in valid set. "
                    "spec: src/api/schemas/metagen.py — MetagenItemSummary.status Literal"
                )
                assert item["dataset_urn"] == urn, (
                    f"item dataset_urn {item['dataset_urn']!r} != expected {urn!r}"
                )
                assert "composite_id" in item, (
                    "item missing 'composite_id'. spec: USE_CASE_en.md §UC4 API Mapping L684"
                )
                # spec: USE_CASE_en.md §UC4 L684 — composite_id = '{dataset_urn}::{item_id}'
                assert item["composite_id"] == f"{item['dataset_urn']}::{item['item_id']}", (
                    f"composite_id format mismatch: {item['composite_id']!r}. "
                    "spec: USE_CASE_en.md §UC4 API Mapping L684"
                )

        # ── Step 8: Review candidates ─────────────────────────────────────────
        # Approve eu_profiles dataset.description, reject eu_profiles column.email.description,
        # approve orders.events column.<first_masked_field>.description.
        # Each review fetches item-detail first to find the first llm_approved candidate.
        # spec: USE_CASE_en.md §UC4 L752–760
        # spec: BACKEND.md §UC4 — POST .../candidate/{cid}/method/review

        # Helper to fetch the first llm_approved candidate for an item.
        async def _first_llm_approved_candidate(
            encoded: str, item_id: str
        ) -> dict | None:
            encoded_item_id = urllib.parse.quote(item_id, safe="")
            detail_resp = await api_client.get(
                f"/api/v1/spoke/common/data/{encoded}/attr/metagen/item/{encoded_item_id}",
                headers=admin_headers,
            )
            if detail_resp.status_code != 200:
                return None
            detail_body = detail_resp.json()
            candidates = detail_body.get("candidates", [])
            return next((c for c in candidates if c["status"] == "llm_approved"), None)

        # 8a. Approve eu_profiles dataset.description.
        eu_desc_item_id = "dataset.description"
        eu_desc_candidate = await _first_llm_approved_candidate(_EU_ENCODED, eu_desc_item_id)
        approved_eu_desc_value: str | None = None
        if eu_desc_candidate is not None:
            eu_desc_cid = eu_desc_candidate["candidate_id"]
            approved_eu_desc_value = eu_desc_candidate["value"]
            eu_desc_encoded_item = urllib.parse.quote(eu_desc_item_id, safe="")
            review_resp = await api_client.post(
                f"/api/v1/spoke/common/data/{_EU_ENCODED}"
                f"/attr/metagen/item/{eu_desc_encoded_item}"
                f"/candidate/{eu_desc_cid}/method/review",
                headers=admin_headers,
                json={"verdict": "approve", "reason": "uc4 narrative approve"},
            )
            assert review_resp.status_code == 200, (
                f"POST approve eu_profiles dataset.description failed: "
                f"{review_resp.status_code} {review_resp.text}. "
                "spec: USE_CASE_en.md §UC4 L752–760"
            )
            review_body = review_resp.json()
            assert review_body.get("status") == "approved", (
                f"Candidate status after approve must be 'approved'; "
                f"got {review_body.get('status')!r}. spec: USE_CASE_en.md §UC4 L649–657"
            )
            assert review_body.get("candidate_id") == eu_desc_cid, (
                "candidate_id echo mismatch. spec: BACKEND.md §UC4"
            )

        # 8b. Reject eu_profiles column.email.description.
        eu_email_item_id = "column.email.description"
        eu_email_candidate = await _first_llm_approved_candidate(_EU_ENCODED, eu_email_item_id)
        rejected_eu_email_cid: str | None = None
        if eu_email_candidate is not None:
            rejected_eu_email_cid = eu_email_candidate["candidate_id"]
            eu_email_encoded_item = urllib.parse.quote(eu_email_item_id, safe="")
            reject_resp = await api_client.post(
                f"/api/v1/spoke/common/data/{_EU_ENCODED}"
                f"/attr/metagen/item/{eu_email_encoded_item}"
                f"/candidate/{rejected_eu_email_cid}/method/review",
                headers=admin_headers,
                json={"verdict": "reject", "reason": "uc4 narrative reject"},
            )
            assert reject_resp.status_code == 200, (
                f"POST reject eu_profiles column.email.description failed: "
                f"{reject_resp.status_code} {reject_resp.text}. "
                "spec: USE_CASE_en.md §UC4 L752–760"
            )
            reject_body = reject_resp.json()
            assert reject_body.get("status") == "rejected", (
                f"Candidate status after reject must be 'rejected'; "
                f"got {reject_body.get('status')!r}. spec: USE_CASE_en.md §UC4 L649–657"
            )

        # 8c. Approve orders.events first masked column description.
        approved_oe_item_id: str | None = None
        approved_oe_value: str | None = None
        oe_col_candidate: dict | None = None
        if masked_oe_field_paths:
            oe_col_item_id = f"column.{masked_oe_field_paths[0]}.description"
            approved_oe_item_id = oe_col_item_id
            oe_col_candidate = await _first_llm_approved_candidate(_OE_ENCODED, oe_col_item_id)
            if oe_col_candidate is not None:
                oe_col_cid = oe_col_candidate["candidate_id"]
                approved_oe_value = oe_col_candidate["value"]
                oe_col_encoded_item = urllib.parse.quote(oe_col_item_id, safe="")
                oe_review_resp = await api_client.post(
                    f"/api/v1/spoke/common/data/{_OE_ENCODED}"
                    f"/attr/metagen/item/{oe_col_encoded_item}"
                    f"/candidate/{oe_col_cid}/method/review",
                    headers=admin_headers,
                    json={"verdict": "approve", "reason": "uc4 narrative approve oe col"},
                )
                assert oe_review_resp.status_code == 200, (
                    f"POST approve orders.events column description failed: "
                    f"{oe_review_resp.status_code} {oe_review_resp.text}. "
                    "spec: USE_CASE_en.md §UC4 L752–760"
                )
                oe_review_body = oe_review_resp.json()
                assert oe_review_body.get("status") == "approved", (
                    f"Candidate status after approve must be 'approved'; "
                    f"got {oe_review_body.get('status')!r}. spec: USE_CASE_en.md §UC4 L649–657"
                )

        # ── Step 9: DataHub round-trip for the two approved items ─────────────
        # After approving a dataset.description candidate, DataSpoke emits the value
        # to editableDatasetProperties.description.
        # After approving a column.description candidate, DataSpoke emits the value
        # to editableSchemaMetadata[fieldPath].description.
        # spec: USE_CASE_en.md §UC4 L588 (table) — approval map
        # spec: BACKEND.md §UC4 — _emit_to_datahub writes to editable aspects
        if approved_eu_desc_value is not None:
            editable_props = graph.get_aspect(
                entity_urn=EU_PROFILES_URN,
                aspect_type=EditableDatasetPropertiesClass,
            )
            assert editable_props is not None, (
                "editableDatasetProperties aspect is None after approving dataset.description "
                "candidate — DataSpoke did not emit to DataHub. "
                "spec: USE_CASE_en.md §UC4 L762–764"
            )
            assert editable_props.description == approved_eu_desc_value, (
                f"editableDatasetProperties.description={editable_props.description!r} "
                f"does not match approved value={approved_eu_desc_value!r}. "
                "spec: USE_CASE_en.md §UC4 L762–764 — approval emits value to DataHub"
            )

        if approved_oe_item_id is not None and approved_oe_value is not None:
            approved_field_path = masked_oe_field_paths[0]
            editable_schema = graph.get_aspect(
                entity_urn=ORDERS_EVENTS_URN,
                aspect_type=EditableSchemaMetadataClass,
            )
            assert editable_schema is not None, (
                "editableSchemaMetadata aspect is None after approving column.description "
                "candidate — DataSpoke did not emit to DataHub. "
                "spec: USE_CASE_en.md §UC4 L762–764"
            )
            field_infos = editable_schema.editableSchemaFieldInfo or []
            matched_fi = next(
                (fi for fi in field_infos if fi.fieldPath == approved_field_path), None
            )
            assert matched_fi is not None, (
                f"editableSchemaMetadata has no entry for fieldPath={approved_field_path!r} "
                "after approving column description. spec: USE_CASE_en.md §UC4 L762–764"
            )
            assert matched_fi.description == approved_oe_value, (
                f"editableSchemaMetadata[{approved_field_path!r}].description="
                f"{matched_fi.description!r} does not match approved value="
                f"{approved_oe_value!r}. spec: USE_CASE_en.md §UC4 L762–764"
            )

        # ── Step 10: GET per-dataset metagen events ───────────────────────────
        # Verify CANDIDATE_APPROVE and CANDIDATE_REJECT events are recorded.
        # spec: USE_CASE_en.md §UC4 L769–770
        # spec: BACKEND.md event catalogue L767 — CANDIDATE_APPROVE detail: item_id,
        #   candidate_id, reason; CANDIDATE_REJECT detail: item_id, candidate_id, reason
        if eu_desc_candidate is not None:
            eu_event_resp = await api_client.get(
                f"/api/v1/spoke/common/data/{_EU_ENCODED}/event/metagen?limit=20",
                headers=admin_headers,
            )
            assert eu_event_resp.status_code == 200, (
                f"GET eu_profiles metagen events failed: {eu_event_resp.status_code}"
            )
            eu_event_body = eu_event_resp.json()
            assert "events" in eu_event_body
            assert "offset" in eu_event_body
            assert "limit" in eu_event_body
            assert "total_count" in eu_event_body

            approve_event = next(
                (
                    e
                    for e in eu_event_body["events"]
                    if e["event_type"] == "METAGEN.CANDIDATE_APPROVE"
                ),
                None,
            )
            assert approve_event is not None, (
                "No METAGEN.CANDIDATE_APPROVE event found after approving eu_profiles "
                "dataset.description candidate. spec: BACKEND.md event catalogue L767"
            )
            ev_detail = approve_event["detail"]
            assert "item_id" in ev_detail, (
                "METAGEN.CANDIDATE_APPROVE detail missing 'item_id'. "
                "spec: BACKEND.md event catalogue L767"
            )
            assert "candidate_id" in ev_detail, (
                "METAGEN.CANDIDATE_APPROVE detail missing 'candidate_id'. "
                "spec: BACKEND.md event catalogue L767"
            )
            assert "reason" in ev_detail, (
                "METAGEN.CANDIDATE_APPROVE detail missing 'reason'. "
                "spec: BACKEND.md event catalogue L767"
            )

        if rejected_eu_email_cid is not None:
            eu_event_resp2 = await api_client.get(
                f"/api/v1/spoke/common/data/{_EU_ENCODED}/event/metagen?limit=20",
                headers=admin_headers,
            )
            assert eu_event_resp2.status_code == 200
            reject_event = next(
                (
                    e
                    for e in eu_event_resp2.json().get("events", [])
                    if e["event_type"] == "METAGEN.CANDIDATE_REJECT"
                ),
                None,
            )
            assert reject_event is not None, (
                "No METAGEN.CANDIDATE_REJECT event found after rejecting eu_profiles "
                "column.email.description candidate. spec: BACKEND.md event catalogue L767"
            )
            rj_detail = reject_event["detail"]
            assert "item_id" in rj_detail, (
                "METAGEN.CANDIDATE_REJECT detail missing 'item_id'. "
                "spec: BACKEND.md event catalogue L767"
            )
            assert "candidate_id" in rj_detail, (
                "METAGEN.CANDIDATE_REJECT detail missing 'candidate_id'. "
                "spec: BACKEND.md event catalogue L767"
            )
            assert "reason" in rj_detail, (
                "METAGEN.CANDIDATE_REJECT detail missing 'reason'. "
                "spec: BACKEND.md event catalogue L767"
            )

        # ── Step 11: POST method/run (second run) — idempotency ───────────────
        # Approved items must be skipped; rejected items must be cleared and re-generated.
        # spec: USE_CASE_en.md §UC4 L567 — "previously approved descriptions are
        # not overwritten on subsequent runs"
        # spec: BACKEND.md §UC4 — _enumerate_target_items skips approved (dataset_urn, item_id)
        # spec: BACKEND.md §UC4 — _clear_rejected_candidates deletes rejected candidates
        #   before each run so they can be re-generated fresh
        run2_resp = await api_client.post(run_url, headers=admin_headers, timeout=90.0)
        assert run2_resp.status_code == 200, (
            f"POST method/run (second run) failed: "
            f"{run2_resp.status_code} {run2_resp.text}. "
            "spec: USE_CASE_en.md §UC4 L720"
        )
        run2_body = run2_resp.json()
        second_run_id = run2_body["run_id"]
        assert uuid.UUID(second_run_id)
        assert run2_body.get("status") == "success"
        assert run2_body.get("dry_run") is False

        second_counts = run2_body.get("counts", {})
        first_items_considered = first_counts["items_considered"]
        second_items_considered = second_counts.get("items_considered", 0)

        # Precondition: stub mode is deterministic — both approvals must have occurred upstream.
        # Asserting this separately gives a precise root-cause message when the stub regresses,
        # rather than misattributing the failure to _enumerate_target_items downstream.
        assert eu_desc_candidate is not None and oe_col_candidate is not None, (
            "Stub regressed — no llm_approved candidate to approve in step 8. "
            "Check src/workflows/_stubs.py metagen_validate branch and "
            "src/backend/metagen/prompts.py TARGET ITEMS block format."
        )

        # The two approved items must be excluded from the second run's scope.
        # spec: BACKEND.md §UC4 — _enumerate_target_items filters out (urn, item_id) pairs
        #   that already have an approved candidate.
        assert second_items_considered < first_items_considered, (
            f"Second run items_considered ({second_items_considered}) must be strictly less "
            f"than first run ({first_items_considered}) because approved items are excluded. "
            "spec: BACKEND.md §UC4 — _enumerate_target_items skips approved items"
        )

        # Verify approved items: GET item-detail and assert exactly one approved candidate,
        # no new llm_approved candidate from the second run.
        # spec: BACKEND.md §UC4 — approved candidates are not evicted or overwritten
        if eu_desc_candidate is not None:
            eu_desc_encoded_item = urllib.parse.quote(eu_desc_item_id, safe="")
            detail_resp2 = await api_client.get(
                f"/api/v1/spoke/common/data/{_EU_ENCODED}"
                f"/attr/metagen/item/{eu_desc_encoded_item}",
                headers=admin_headers,
            )
            assert detail_resp2.status_code == 200
            detail2_body = detail_resp2.json()
            candidates2 = detail2_body.get("candidates", [])
            approved_candidates = [c for c in candidates2 if c["status"] == "approved"]
            assert len(approved_candidates) == 1, (
                f"Expected exactly 1 approved candidate for eu_profiles dataset.description "
                f"after second run; got {len(approved_candidates)}. "
                "spec: BACKEND.md §UC4 — partial unique index ensures at most one approved "
                "candidate per (dataset_urn, item_id)"
            )
            assert len(candidates2) == 1, (
                f"Approved item should have only the approved candidate after second run; "
                f"got {len(candidates2)} candidates with statuses "
                f"{[c['status'] for c in candidates2]}. "
                "spec: BACKEND.md §UC4 — _enumerate_target_items skips approved items"
            )

        # Verify the rejected eu_profiles column.email.description: prior rejected candidate
        # is gone (cleared by _clear_rejected_candidates), and a new llm_approved candidate
        # was generated.
        # spec: USE_CASE_en.md §UC4 L637-638 — at the start of each run all rejected
        #   candidates are deleted
        if rejected_eu_email_cid is not None:
            eu_email_encoded_item2 = urllib.parse.quote(eu_email_item_id, safe="")
            email_detail_resp = await api_client.get(
                f"/api/v1/spoke/common/data/{_EU_ENCODED}"
                f"/attr/metagen/item/{eu_email_encoded_item2}",
                headers=admin_headers,
            )
            assert email_detail_resp.status_code == 200
            email_detail_body = email_detail_resp.json()
            email_candidates = email_detail_body.get("candidates", [])
            still_rejected = [c for c in email_candidates if c["status"] == "rejected"]
            assert len(still_rejected) == 0, (
                f"Rejected candidate for eu_profiles column.email.description should have been "
                f"cleared by the second run; found {len(still_rejected)} rejected candidates. "
                "spec: BACKEND.md §UC4 — _clear_rejected_candidates removes rejected candidates"
            )
            new_llm_approved_email = [
                c for c in email_candidates if c["status"] == "llm_approved"
            ]
            assert len(new_llm_approved_email) >= 1, (
                "After clearing the rejected candidate, the second run must produce a new "
                "llm_approved candidate for eu_profiles column.email.description. "
                "spec: BACKEND.md §UC4 — rejected items are re-generated on the next run"
            )

        # Verify second run RUN_COMPLETE event is recorded.
        # spec: BACKEND.md event catalogue L766 — RUN_COMPLETE emitted on every run
        event2_resp = await api_client.get(
            f"{global_event_url}?limit=50",
            headers=admin_headers,
        )
        assert event2_resp.status_code == 200
        events2 = event2_resp.json().get("events", [])
        run2_complete = next(
            (
                e
                for e in events2
                if e["event_type"] == "METAGEN.RUN_COMPLETE"
                and e.get("detail", {}).get("run_id") == second_run_id
            ),
            None,
        )
        assert run2_complete is not None, (
            f"No METAGEN.RUN_COMPLETE event found with run_id={second_run_id!r} "
            "after second method/run. spec: BACKEND.md event catalogue L766"
        )

    finally:
        # ── Step 12: Cleanup ──────────────────────────────────────────────────
        # Each operation is wrapped in suppress(Exception) so a single failure
        # does not abort later cleanup steps.
        # spec: TESTING.md §Api-Wired Integration Tests — teardown must not leak state

        # Delete metagen state for both datasets (embeddings → candidates → items → events).
        with suppress(Exception):
            await delete_metagen_state_for_urn(async_session, EU_PROFILES_URN)
        with suppress(Exception):
            await delete_metagen_state_for_urn(async_session, ORDERS_EVENTS_URN)

        # Delete per-dataset boundaries and global conf.
        with suppress(Exception):
            await api_client.delete(eu_boundary_url, headers=admin_headers)
        with suppress(Exception):
            await api_client.delete(oe_boundary_url, headers=admin_headers)
        with suppress(Exception):
            await api_client.delete(conf_url, headers=admin_headers)

        # Restore DataHub aspect snapshots to undo the masking performed in step 2.
        # Originals are restored into the snapshot objects before re-emitting so
        # the emitted aspect carries the pre-test values, not the masked-out Nones.
        # Uses the graph object created in the try block (still valid — token is session-lived).
        if eu_props_snapshot is not None:
            with suppress(Exception):
                eu_props_snapshot.description = eu_original_dataset_description
                graph.emit_mcp(
                    MetadataChangeProposalWrapper(
                        entityUrn=EU_PROFILES_URN, aspect=eu_props_snapshot
                    )
                )
        if eu_schema_snapshot is not None:
            with suppress(Exception):
                if hasattr(eu_schema_snapshot, "fields"):
                    for f in eu_schema_snapshot.fields:
                        f.description = eu_original_field_descs.get(f.fieldPath)
                graph.emit_mcp(
                    MetadataChangeProposalWrapper(
                        entityUrn=EU_PROFILES_URN, aspect=eu_schema_snapshot
                    )
                )
        if oe_schema_snapshot is not None:
            with suppress(Exception):
                if hasattr(oe_schema_snapshot, "fields"):
                    for f in oe_schema_snapshot.fields:
                        f.description = oe_original_field_descs.get(f.fieldPath)
                graph.emit_mcp(
                    MetadataChangeProposalWrapper(
                        entityUrn=ORDERS_EVENTS_URN, aspect=oe_schema_snapshot
                    )
                )

        # Remove editable overrides written by approve flow.
        if graph is not None:
            with suppress(Exception):
                graph.emit_mcp(
                    MetadataChangeProposalWrapper(
                        entityUrn=EU_PROFILES_URN,
                        aspect=EditableDatasetPropertiesClass(description=None),
                    )
                )
            with suppress(Exception):
                graph.emit_mcp(
                    MetadataChangeProposalWrapper(
                        entityUrn=EU_PROFILES_URN,
                        aspect=EditableSchemaMetadataClass(editableSchemaFieldInfo=[]),
                    )
                )
            with suppress(Exception):
                graph.emit_mcp(
                    MetadataChangeProposalWrapper(
                        entityUrn=ORDERS_EVENTS_URN,
                        aspect=EditableSchemaMetadataClass(editableSchemaFieldInfo=[]),
                    )
                )

        # Hard-delete the fulfillment document seeded in step 1a.
        if document_urn is not None:
            with suppress(Exception):
                hard_delete_document(document_urn=document_urn, token=dh_token)

        # Delete seeded ontogen nodes (cascade: dataset_node_map first).
        for nid in node_ids:
            with suppress(Exception):
                await delete_ontogen_node(async_session, nid)


@pytest.mark.asyncio
async def test_uc4_metadata_generation_with_real_llm(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
    runtime_conf: dict,
) -> None:
    """UC4 user-story arc with a real LLM (stub_llm_client=false in runtime conf).

    Skipped when stub_llm_client is true — a stub LLM cannot satisfy the
    real-LLM contract assertions (candidates_added ≥ 1).

    Structurally identical to test_uc4_metadata_generation_under_stub. Additional
    assertion after step 5: candidates_added ≥ 1 — a real LLM run that persists
    zero candidates signals a prompt/filter regression.

    Steps mirror USE_CASE_en.md §UC4 Imazon Example:
      1.  Seed LLM context: fulfillment document + 5 approved ontogen nodes
      2.  Mask descriptions in DataHub (eu_profiles + orders.events)
      3.  PUT global metagen conf (is_enabled, schedule_tier, dataset_filter, tags)
      4.  PUT per-dataset boundaries
      5.  POST method/run (first run) — assert MetagenRunResponse shape;
          assert candidates_added ≥ 1 (real-LLM invariant)
      6.  GET global event — find METAGEN.RUN_COMPLETE
      7.  GET per-dataset items for both URNs
      8.  Review candidates: approve eu_profiles dataset.description,
          reject eu_profiles column.email.description,
          approve orders.events first masked column
      9.  DataHub round-trip verify
     10.  GET per-dataset metagen events
     11.  POST method/run (second run) — assert approved items skipped,
          rejected item cleared and re-generated
     12.  Cleanup (finally)

    Spec: USE_CASE_en.md §UC4
    Spec: BACKEND.md §UC4 Metadata Generation
    Spec: USE_CASE_en.md §UC4 — real-LLM contract: candidates_added ≥ 1.
    """
    if runtime_conf.get("stub_llm_client"):
        pytest.skip("stub_llm_client=true; set stub_llm_client=false via PATCH /admin/conf to run real-LLM tests")

    conf_url = "/api/v1/spoke/common/metagen/attr/conf"
    eu_boundary_url = f"/api/v1/spoke/common/data/{_EU_ENCODED}/attr/metagen/conf"
    oe_boundary_url = f"/api/v1/spoke/common/data/{_OE_ENCODED}/attr/metagen/conf"
    run_url = "/api/v1/spoke/common/metagen/method/run"
    global_event_url = "/api/v1/spoke/common/metagen/event"

    document_urn: str | None = None
    node_ids: list[str] = []
    graph: DataHubGraph | None = None
    eu_props_snapshot: DatasetPropertiesClass | None = None
    eu_schema_snapshot: SchemaMetadataClass | None = None
    oe_schema_snapshot: SchemaMetadataClass | None = None
    masked_oe_field_paths: list[str] = []
    # Original field descriptions captured before in-place mutation so cleanup
    # can restore the correct values (not the masked-out Nones).
    eu_original_dataset_description: str | None = None
    eu_original_field_descs: dict[str, str | None] = {}
    oe_original_field_descs: dict[str, str | None] = {}

    try:
        # ── Step 1: Seed LLM context ──────────────────────────────────────────
        dh_token = get_datahub_token()
        document_id = uuid.uuid4().hex[:16]
        document_urn = seed_native_document(
            document_id=document_id,
            title="Imazon Fulfillment Process Guide",
            body_markdown=load_fulfillment_doc(),
            related_dataset_urns=[EU_PROFILES_URN, ORDERS_EVENTS_URN],
            token=dh_token,
        )

        suffix = uuid.uuid4().hex[:8]
        node_names = ["Order", "OrderLine", "Customer", "ShipmentEvent", "DeliveryStatus"]
        for name in node_names:
            candidate_id = f"uc4-{name.lower()}-{suffix}"
            actual_id = await seed_approved_ontogen_node(async_session, candidate_id, name)
            node_ids.append(actual_id)
            for urn in (EU_PROFILES_URN, ORDERS_EVENTS_URN):
                await seed_dataset_node_map(async_session, dataset_urn=urn, node_id=actual_id)

        # ── Step 2: Mask descriptions in DataHub ──────────────────────────────
        graph = DataHubGraph(DatahubClientConfig(server=_gms_url, token=dh_token))

        eu_props_snapshot = graph.get_aspect(
            entity_urn=EU_PROFILES_URN, aspect_type=DatasetPropertiesClass
        )
        eu_schema_snapshot = graph.get_aspect(
            entity_urn=EU_PROFILES_URN, aspect_type=SchemaMetadataClass
        )

        # Capture original values before any in-place mutation so cleanup can
        # restore them accurately.
        eu_original_dataset_description = eu_props_snapshot.description if eu_props_snapshot else None
        if eu_schema_snapshot is not None and hasattr(eu_schema_snapshot, "fields"):
            for f in eu_schema_snapshot.fields:
                eu_original_field_descs[f.fieldPath] = f.description

        if eu_props_snapshot is not None:
            masked_eu_props = DatasetPropertiesClass(
                name=eu_props_snapshot.name,
                qualifiedName=eu_props_snapshot.qualifiedName,
                description="",
                customProperties=eu_props_snapshot.customProperties,
            )
            graph.emit_mcp(
                MetadataChangeProposalWrapper(
                    entityUrn=EU_PROFILES_URN, aspect=masked_eu_props
                )
            )

        if eu_schema_snapshot is not None and hasattr(eu_schema_snapshot, "fields"):
            for f in eu_schema_snapshot.fields:
                f.description = None
            graph.emit_mcp(
                MetadataChangeProposalWrapper(
                    entityUrn=EU_PROFILES_URN, aspect=eu_schema_snapshot
                )
            )

        oe_schema_snapshot = graph.get_aspect(
            entity_urn=ORDERS_EVENTS_URN, aspect_type=SchemaMetadataClass
        )

        # Capture original field descriptions before masking.
        if oe_schema_snapshot is not None and hasattr(oe_schema_snapshot, "fields"):
            for f in oe_schema_snapshot.fields:
                oe_original_field_descs[f.fieldPath] = f.description

        if oe_schema_snapshot is not None and hasattr(oe_schema_snapshot, "fields"):
            all_field_paths = [f.fieldPath for f in oe_schema_snapshot.fields]
            masked_oe_field_paths = all_field_paths[:4]
            for f in oe_schema_snapshot.fields:
                if f.fieldPath in masked_oe_field_paths:
                    f.description = None
            graph.emit_mcp(
                MetadataChangeProposalWrapper(
                    entityUrn=ORDERS_EVENTS_URN, aspect=oe_schema_snapshot
                )
            )

        # ── Step 3: PUT global metagen conf ──────────────────────────────────
        put_conf_resp = await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "is_enabled": True,
                "schedule_tier": "daily",
                "dataset_filter": {"origin": "DEV", "tags": [FULFILLMENT_TAG]},
                "result_limit": 3,
                "overwrite_pending": True,
            },
        )
        assert put_conf_resp.status_code in (200, 201), (
            f"PUT global metagen conf failed: "
            f"{put_conf_resp.status_code} {put_conf_resp.text}. "
            "spec: USE_CASE_en.md §UC4 L694 — PUT returns 200 or 201"
        )
        conf_body = put_conf_resp.json()
        assert conf_body["is_enabled"] is True, (
            "PUT conf response must round-trip is_enabled=True. "
            "spec: USE_CASE_en.md §UC4 L604"
        )
        assert conf_body["schedule_tier"] == "daily", (
            f"schedule_tier not preserved: {conf_body.get('schedule_tier')!r}. "
            "spec: USE_CASE_en.md §UC4 L604"
        )
        assert conf_body["result_limit"] == 3, (
            f"result_limit not preserved: {conf_body.get('result_limit')!r}. "
            "spec: USE_CASE_en.md §UC4 L605"
        )
        assert conf_body["overwrite_pending"] is True, (
            f"overwrite_pending not preserved: {conf_body.get('overwrite_pending')!r}. "
            "spec: USE_CASE_en.md §UC4 L606"
        )
        assert conf_body["dataset_filter"] == {"origin": "DEV", "tags": [FULFILLMENT_TAG]}, (
            f"dataset_filter not preserved: {conf_body.get('dataset_filter')!r}. "
            "spec: USE_CASE_en.md §UC4 L604"
        )

        # ── Step 4: PUT per-dataset boundaries ───────────────────────────────
        put_eu_boundary_resp = await api_client.put(
            eu_boundary_url,
            headers=admin_headers,
            json={
                "is_enabled": True,
                "allowed": ["dataset.description", "column.description"],
            },
        )
        assert put_eu_boundary_resp.status_code in (200, 201), (
            f"PUT eu_profiles boundary failed: "
            f"{put_eu_boundary_resp.status_code} {put_eu_boundary_resp.text}. "
            "spec: USE_CASE_en.md §UC4 L613"
        )
        eu_boundary_body = put_eu_boundary_resp.json()
        assert eu_boundary_body["dataset_urn"] == EU_PROFILES_URN, (
            f"boundary dataset_urn not echoed: {eu_boundary_body.get('dataset_urn')!r}. "
            "spec: USE_CASE_en.md §UC4 L613"
        )
        assert set(eu_boundary_body["allowed"]) == {"dataset.description", "column.description"}, (
            f"eu_profiles boundary allowed not preserved: {eu_boundary_body.get('allowed')!r}. "
            "spec: USE_CASE_en.md §UC4 L614"
        )

        put_oe_boundary_resp = await api_client.put(
            oe_boundary_url,
            headers=admin_headers,
            json={
                "is_enabled": True,
                "allowed": ["column.description"],
            },
        )
        assert put_oe_boundary_resp.status_code in (200, 201), (
            f"PUT orders.events boundary failed: "
            f"{put_oe_boundary_resp.status_code} {put_oe_boundary_resp.text}. "
            "spec: USE_CASE_en.md §UC4 L613"
        )
        oe_boundary_body = put_oe_boundary_resp.json()
        assert oe_boundary_body["dataset_urn"] == ORDERS_EVENTS_URN, (
            f"orders.events boundary dataset_urn not echoed: "
            f"{oe_boundary_body.get('dataset_urn')!r}. spec: USE_CASE_en.md §UC4 L613"
        )
        assert "column.description" in oe_boundary_body["allowed"], (
            "orders.events boundary allowed must include 'column.description'. "
            "spec: USE_CASE_en.md §UC4 L614"
        )

        # ── Step 5: POST method/run (first run) ───────────────────────────────
        run_resp = await api_client.post(run_url, headers=admin_headers, timeout=90.0)
        assert run_resp.status_code == 200, (
            f"POST method/run (first run) failed: "
            f"{run_resp.status_code} {run_resp.text}. "
            "spec: USE_CASE_en.md §UC4 L720"
        )
        run_body = run_resp.json()
        first_run_id = run_body["run_id"]

        uuid.UUID(run_body["run_id"])
        assert run_body.get("status") == "success", (
            f"MetagenRunResponse status expected 'success'; got {run_body.get('status')!r}. "
            "spec: BACKEND.md §UC4"
        )
        assert run_body.get("dry_run") is False, (
            f"MetagenRunResponse dry_run must be False; got {run_body.get('dry_run')!r}. "
            "spec: BACKEND.md §UC4"
        )
        assert isinstance(run_body.get("unresolved_urns"), list)
        assert run_body["unresolved_urns"] == [], (
            f"Expected no unresolved URNs; got {run_body['unresolved_urns']!r}. "
            "spec: BACKEND.md §UC4"
        )
        first_counts = run_body.get("counts", {})
        assert isinstance(first_counts, dict)
        assert "items_considered" in first_counts
        assert isinstance(first_counts["items_considered"], int) and first_counts["items_considered"] >= 1, (
            f"items_considered must be int ≥ 1; got {first_counts.get('items_considered')!r}. "
            "spec: BACKEND.md event catalogue L766"
        )
        assert "candidates_added" in first_counts
        assert run_body.get("debate_outcome") in ("accept", "turns_exhausted", "cycle_detected"), (
            f"debate_outcome={run_body.get('debate_outcome')!r} not in canonical set. "
            "spec: BACKEND.md §UC4"
        )
        assert isinstance(run_body.get("producer_iterations"), int) and run_body["producer_iterations"] >= 1

        # Real-LLM invariant: must persist ≥1 candidate.
        # spec: BACKEND_LLM.md §Test Mode — real LLM must produce non-zero candidates
        assert first_counts.get("candidates_added", 0) >= 1, (
            "Real LLM produced zero candidates — verify prompt/filter pipeline. "
            "spec: BACKEND_LLM.md §Test Mode"
        )

        # ── Step 6: GET global event — assert METAGEN.RUN_COMPLETE detail ─────
        event_resp = await api_client.get(
            f"{global_event_url}?limit=20",
            headers=admin_headers,
        )
        assert event_resp.status_code == 200
        event_body = event_resp.json()
        assert "events" in event_body
        assert "offset" in event_body
        assert "limit" in event_body
        assert "total_count" in event_body

        run_complete_event = next(
            (
                e
                for e in event_body["events"]
                if e["event_type"] == "METAGEN.RUN_COMPLETE"
                and e.get("detail", {}).get("run_id") == first_run_id
            ),
            None,
        )
        assert run_complete_event is not None, (
            f"No METAGEN.RUN_COMPLETE event found with run_id={first_run_id!r}. "
            "spec: BACKEND.md event catalogue L766"
        )
        rc_detail = run_complete_event["detail"]
        assert "run_id" in rc_detail
        assert "unresolved_urns" in rc_detail and isinstance(rc_detail["unresolved_urns"], list)
        assert "counts" in rc_detail and isinstance(rc_detail["counts"], dict)
        assert rc_detail.get("dry_run") is False
        assert rc_detail.get("debate_outcome") in ("accept", "turns_exhausted", "cycle_detected"), (
            f"event detail debate_outcome={rc_detail.get('debate_outcome')!r} not in canonical set. "
            "spec: BACKEND.md event catalogue L766"
        )
        assert isinstance(rc_detail.get("producer_iterations"), int) and rc_detail["producer_iterations"] >= 1

        # ── Step 7: GET per-dataset items for both URNs ───────────────────────
        for urn, encoded_urn in ((EU_PROFILES_URN, _EU_ENCODED), (ORDERS_EVENTS_URN, _OE_ENCODED)):
            items_resp = await api_client.get(
                f"/api/v1/spoke/common/data/{encoded_urn}/attr/metagen/item",
                headers=admin_headers,
            )
            assert items_resp.status_code == 200, (
                f"GET per-dataset items for {urn!r} failed: {items_resp.status_code}"
            )
            items_body = items_resp.json()
            assert "items" in items_body and isinstance(items_body["items"], list)
            assert "offset" in items_body
            assert "limit" in items_body
            assert "total_count" in items_body

            for item in items_body["items"]:
                assert item["kind"] in ("dataset.description", "column.description"), (
                    f"item kind {item['kind']!r} not in allowed set. "
                    "spec: USE_CASE_en.md §UC4 L617"
                )
                assert item["status"] in ("pending", "llm_approved", "approved"), (
                    f"item status {item['status']!r} not in valid set. "
                    "spec: src/api/schemas/metagen.py — MetagenItemSummary.status Literal"
                )
                assert item["dataset_urn"] == urn
                assert "composite_id" in item
                assert item["composite_id"] == f"{item['dataset_urn']}::{item['item_id']}", (
                    "composite_id format mismatch. spec: USE_CASE_en.md §UC4 API Mapping L684"
                )

        # ── Step 8: Review candidates ─────────────────────────────────────────
        async def _first_llm_approved_candidate(
            encoded: str, item_id: str
        ) -> dict | None:
            encoded_item_id = urllib.parse.quote(item_id, safe="")
            detail_resp = await api_client.get(
                f"/api/v1/spoke/common/data/{encoded}/attr/metagen/item/{encoded_item_id}",
                headers=admin_headers,
            )
            if detail_resp.status_code != 200:
                return None
            return next(
                (c for c in detail_resp.json().get("candidates", []) if c["status"] == "llm_approved"),
                None,
            )

        # 8a. Approve eu_profiles dataset.description.
        eu_desc_item_id = "dataset.description"
        eu_desc_candidate = await _first_llm_approved_candidate(_EU_ENCODED, eu_desc_item_id)
        approved_eu_desc_value: str | None = None
        if eu_desc_candidate is not None:
            eu_desc_cid = eu_desc_candidate["candidate_id"]
            approved_eu_desc_value = eu_desc_candidate["value"]
            eu_desc_encoded_item = urllib.parse.quote(eu_desc_item_id, safe="")
            review_resp = await api_client.post(
                f"/api/v1/spoke/common/data/{_EU_ENCODED}"
                f"/attr/metagen/item/{eu_desc_encoded_item}"
                f"/candidate/{eu_desc_cid}/method/review",
                headers=admin_headers,
                json={"verdict": "approve", "reason": "uc4 narrative approve"},
            )
            assert review_resp.status_code == 200, (
                f"POST approve eu_profiles dataset.description failed: "
                f"{review_resp.status_code} {review_resp.text}. "
                "spec: USE_CASE_en.md §UC4 L752–760"
            )
            assert review_resp.json().get("status") == "approved", (
                "Candidate status after approve must be 'approved'. "
                "spec: USE_CASE_en.md §UC4 L649–657"
            )

        # 8b. Reject eu_profiles column.email.description.
        eu_email_item_id = "column.email.description"
        eu_email_candidate = await _first_llm_approved_candidate(_EU_ENCODED, eu_email_item_id)
        rejected_eu_email_cid: str | None = None
        if eu_email_candidate is not None:
            rejected_eu_email_cid = eu_email_candidate["candidate_id"]
            eu_email_encoded_item = urllib.parse.quote(eu_email_item_id, safe="")
            reject_resp = await api_client.post(
                f"/api/v1/spoke/common/data/{_EU_ENCODED}"
                f"/attr/metagen/item/{eu_email_encoded_item}"
                f"/candidate/{rejected_eu_email_cid}/method/review",
                headers=admin_headers,
                json={"verdict": "reject", "reason": "uc4 narrative reject"},
            )
            assert reject_resp.status_code == 200, (
                f"POST reject eu_profiles column.email.description failed: "
                f"{reject_resp.status_code} {reject_resp.text}. "
                "spec: USE_CASE_en.md §UC4 L752–760"
            )
            assert reject_resp.json().get("status") == "rejected", (
                "Candidate status after reject must be 'rejected'. "
                "spec: USE_CASE_en.md §UC4 L649–657"
            )

        # 8c. Approve orders.events first masked column description.
        approved_oe_item_id: str | None = None
        approved_oe_value: str | None = None
        oe_col_candidate = None
        if masked_oe_field_paths:
            oe_col_item_id = f"column.{masked_oe_field_paths[0]}.description"
            approved_oe_item_id = oe_col_item_id
            oe_col_candidate = await _first_llm_approved_candidate(_OE_ENCODED, oe_col_item_id)
            if oe_col_candidate is not None:
                oe_col_cid = oe_col_candidate["candidate_id"]
                approved_oe_value = oe_col_candidate["value"]
                oe_col_encoded_item = urllib.parse.quote(oe_col_item_id, safe="")
                oe_review_resp = await api_client.post(
                    f"/api/v1/spoke/common/data/{_OE_ENCODED}"
                    f"/attr/metagen/item/{oe_col_encoded_item}"
                    f"/candidate/{oe_col_cid}/method/review",
                    headers=admin_headers,
                    json={"verdict": "approve", "reason": "uc4 narrative approve oe col"},
                )
                assert oe_review_resp.status_code == 200, (
                    f"POST approve orders.events column description failed: "
                    f"{oe_review_resp.status_code} {oe_review_resp.text}. "
                    "spec: USE_CASE_en.md §UC4 L752–760"
                )
                assert oe_review_resp.json().get("status") == "approved", (
                    "Candidate status after approve must be 'approved'. "
                    "spec: USE_CASE_en.md §UC4 L649–657"
                )

        # ── Step 9: DataHub round-trip verify ─────────────────────────────────
        if approved_eu_desc_value is not None:
            editable_props = graph.get_aspect(
                entity_urn=EU_PROFILES_URN, aspect_type=EditableDatasetPropertiesClass
            )
            assert editable_props is not None, (
                "editableDatasetProperties is None after approving dataset.description. "
                "spec: USE_CASE_en.md §UC4 L762–764"
            )
            assert editable_props.description == approved_eu_desc_value, (
                f"editableDatasetProperties.description={editable_props.description!r} "
                f"!= approved value={approved_eu_desc_value!r}. "
                "spec: USE_CASE_en.md §UC4 L762–764"
            )

        if approved_oe_item_id is not None and approved_oe_value is not None:
            approved_field_path = masked_oe_field_paths[0]
            editable_schema = graph.get_aspect(
                entity_urn=ORDERS_EVENTS_URN, aspect_type=EditableSchemaMetadataClass
            )
            assert editable_schema is not None, (
                "editableSchemaMetadata is None after approving column description. "
                "spec: USE_CASE_en.md §UC4 L762–764"
            )
            field_infos = editable_schema.editableSchemaFieldInfo or []
            matched_fi = next(
                (fi for fi in field_infos if fi.fieldPath == approved_field_path), None
            )
            assert matched_fi is not None, (
                f"editableSchemaMetadata has no entry for fieldPath={approved_field_path!r}. "
                "spec: USE_CASE_en.md §UC4 L762–764"
            )
            assert matched_fi.description == approved_oe_value, (
                f"editableSchemaMetadata[{approved_field_path!r}].description="
                f"{matched_fi.description!r} != approved value={approved_oe_value!r}. "
                "spec: USE_CASE_en.md §UC4 L762–764"
            )

        # ── Step 10: GET per-dataset metagen events ───────────────────────────
        if eu_desc_candidate is not None:
            eu_event_resp = await api_client.get(
                f"/api/v1/spoke/common/data/{_EU_ENCODED}/event/metagen?limit=20",
                headers=admin_headers,
            )
            assert eu_event_resp.status_code == 200
            eu_event_body = eu_event_resp.json()
            assert "events" in eu_event_body
            assert "offset" in eu_event_body
            assert "limit" in eu_event_body
            assert "total_count" in eu_event_body

            approve_event = next(
                (
                    e
                    for e in eu_event_body["events"]
                    if e["event_type"] == "METAGEN.CANDIDATE_APPROVE"
                ),
                None,
            )
            assert approve_event is not None, (
                "No METAGEN.CANDIDATE_APPROVE event found after approving. "
                "spec: BACKEND.md event catalogue L767"
            )
            ev_detail = approve_event["detail"]
            assert "item_id" in ev_detail, (
                "METAGEN.CANDIDATE_APPROVE detail missing 'item_id'. "
                "spec: BACKEND.md event catalogue L767"
            )
            assert "candidate_id" in ev_detail, (
                "METAGEN.CANDIDATE_APPROVE detail missing 'candidate_id'. "
                "spec: BACKEND.md event catalogue L767"
            )
            assert "reason" in ev_detail, (
                "METAGEN.CANDIDATE_APPROVE detail missing 'reason'. "
                "spec: BACKEND.md event catalogue L767"
            )

        if rejected_eu_email_cid is not None:
            eu_event_resp2 = await api_client.get(
                f"/api/v1/spoke/common/data/{_EU_ENCODED}/event/metagen?limit=20",
                headers=admin_headers,
            )
            assert eu_event_resp2.status_code == 200
            reject_event = next(
                (
                    e
                    for e in eu_event_resp2.json().get("events", [])
                    if e["event_type"] == "METAGEN.CANDIDATE_REJECT"
                ),
                None,
            )
            assert reject_event is not None, (
                "No METAGEN.CANDIDATE_REJECT event found after rejecting. "
                "spec: BACKEND.md event catalogue L767"
            )
            rj_detail = reject_event["detail"]
            assert "item_id" in rj_detail
            assert "candidate_id" in rj_detail
            assert "reason" in rj_detail

        # ── Step 11: POST method/run (second run) — idempotency ───────────────
        run2_resp = await api_client.post(run_url, headers=admin_headers, timeout=90.0)
        assert run2_resp.status_code == 200, (
            f"POST method/run (second run) failed: "
            f"{run2_resp.status_code} {run2_resp.text}. "
            "spec: USE_CASE_en.md §UC4 L720"
        )
        run2_body = run2_resp.json()
        second_run_id = run2_body["run_id"]
        assert uuid.UUID(second_run_id)
        assert run2_body.get("status") == "success"
        assert run2_body.get("dry_run") is False

        second_counts = run2_body.get("counts", {})
        first_items_considered = first_counts["items_considered"]
        second_items_considered = second_counts.get("items_considered", 0)

        approved_items_count = sum(
            [
                eu_desc_candidate is not None,
                oe_col_candidate is not None if approved_oe_item_id is not None else False,
            ]
        )
        if approved_items_count >= 1:
            assert second_items_considered < first_items_considered, (
                f"Second run items_considered ({second_items_considered}) must be strictly less "
                f"than first run ({first_items_considered}) because approved items are excluded. "
                "spec: BACKEND.md §UC4 — _enumerate_target_items skips approved items"
            )

        if eu_desc_candidate is not None:
            eu_desc_encoded_item = urllib.parse.quote(eu_desc_item_id, safe="")
            detail_resp2 = await api_client.get(
                f"/api/v1/spoke/common/data/{_EU_ENCODED}"
                f"/attr/metagen/item/{eu_desc_encoded_item}",
                headers=admin_headers,
            )
            assert detail_resp2.status_code == 200
            candidates2 = detail_resp2.json().get("candidates", [])
            approved_candidates = [c for c in candidates2 if c["status"] == "approved"]
            assert len(approved_candidates) == 1, (
                f"Expected exactly 1 approved candidate for eu_profiles dataset.description "
                f"after second run; got {len(approved_candidates)}. "
                "spec: BACKEND.md §UC4 — partial unique index ensures at most one approved"
            )
            assert len(candidates2) == 1, (
                f"Approved item should have only the approved candidate after second run; "
                f"got {len(candidates2)} candidates with statuses "
                f"{[c['status'] for c in candidates2]}. "
                "spec: BACKEND.md §UC4 — _enumerate_target_items skips approved items"
            )

        if rejected_eu_email_cid is not None:
            eu_email_encoded_item2 = urllib.parse.quote(eu_email_item_id, safe="")
            email_detail_resp = await api_client.get(
                f"/api/v1/spoke/common/data/{_EU_ENCODED}"
                f"/attr/metagen/item/{eu_email_encoded_item2}",
                headers=admin_headers,
            )
            assert email_detail_resp.status_code == 200
            email_candidates = email_detail_resp.json().get("candidates", [])
            still_rejected = [c for c in email_candidates if c["status"] == "rejected"]
            assert len(still_rejected) == 0, (
                "Rejected candidate should have been cleared by second run. "
                "spec: BACKEND.md §UC4 — _clear_rejected_candidates"
            )
            new_llm_approved_email = [c for c in email_candidates if c["status"] == "llm_approved"]
            assert len(new_llm_approved_email) >= 1, (
                "Second run must produce a new llm_approved candidate for rejected item. "
                "spec: BACKEND.md §UC4 — rejected items re-generated on next run"
            )

        event2_resp = await api_client.get(
            f"{global_event_url}?limit=50",
            headers=admin_headers,
        )
        assert event2_resp.status_code == 200
        events2 = event2_resp.json().get("events", [])
        run2_complete = next(
            (
                e
                for e in events2
                if e["event_type"] == "METAGEN.RUN_COMPLETE"
                and e.get("detail", {}).get("run_id") == second_run_id
            ),
            None,
        )
        assert run2_complete is not None, (
            f"No METAGEN.RUN_COMPLETE event for second run_id={second_run_id!r}. "
            "spec: BACKEND.md event catalogue L766"
        )

    finally:
        # ── Step 12: Cleanup ──────────────────────────────────────────────────
        with suppress(Exception):
            await delete_metagen_state_for_urn(async_session, EU_PROFILES_URN)
        with suppress(Exception):
            await delete_metagen_state_for_urn(async_session, ORDERS_EVENTS_URN)

        with suppress(Exception):
            await api_client.delete(eu_boundary_url, headers=admin_headers)
        with suppress(Exception):
            await api_client.delete(oe_boundary_url, headers=admin_headers)
        with suppress(Exception):
            await api_client.delete(conf_url, headers=admin_headers)

        # Restore DataHub aspect snapshots to undo the masking performed in step 2.
        # Originals are restored into the snapshot objects before re-emitting so
        # the emitted aspect carries the pre-test values, not the masked-out Nones.
        if eu_props_snapshot is not None:
            with suppress(Exception):
                eu_props_snapshot.description = eu_original_dataset_description
                graph.emit_mcp(
                    MetadataChangeProposalWrapper(
                        entityUrn=EU_PROFILES_URN, aspect=eu_props_snapshot
                    )
                )
        if eu_schema_snapshot is not None:
            with suppress(Exception):
                if hasattr(eu_schema_snapshot, "fields"):
                    for f in eu_schema_snapshot.fields:
                        f.description = eu_original_field_descs.get(f.fieldPath)
                graph.emit_mcp(
                    MetadataChangeProposalWrapper(
                        entityUrn=EU_PROFILES_URN, aspect=eu_schema_snapshot
                    )
                )
        if oe_schema_snapshot is not None:
            with suppress(Exception):
                if hasattr(oe_schema_snapshot, "fields"):
                    for f in oe_schema_snapshot.fields:
                        f.description = oe_original_field_descs.get(f.fieldPath)
                graph.emit_mcp(
                    MetadataChangeProposalWrapper(
                        entityUrn=ORDERS_EVENTS_URN, aspect=oe_schema_snapshot
                    )
                )

        # Remove editable overrides written by approve flow.
        if graph is not None:
            with suppress(Exception):
                graph.emit_mcp(
                    MetadataChangeProposalWrapper(
                        entityUrn=EU_PROFILES_URN,
                        aspect=EditableDatasetPropertiesClass(description=None),
                    )
                )
            with suppress(Exception):
                graph.emit_mcp(
                    MetadataChangeProposalWrapper(
                        entityUrn=EU_PROFILES_URN,
                        aspect=EditableSchemaMetadataClass(editableSchemaFieldInfo=[]),
                    )
                )
            with suppress(Exception):
                graph.emit_mcp(
                    MetadataChangeProposalWrapper(
                        entityUrn=ORDERS_EVENTS_URN,
                        aspect=EditableSchemaMetadataClass(editableSchemaFieldInfo=[]),
                    )
                )

        if document_urn is not None:
            with suppress(Exception):
                hard_delete_document(document_urn=document_urn, token=dh_token)

        for nid in node_ids:
            with suppress(Exception):
                await delete_ontogen_node(async_session, nid)
