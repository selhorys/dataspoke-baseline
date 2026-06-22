"""UC4 — Metadata Generation: end-to-end through public REST API.

Two structurally identical tests mirror the UC4 user-story arc under stub mode
and real-LLM mode. The arc: a governance operator scopes metagen to fulfillment-
tagged datasets, seeds LLM context (a fulfillment document + UC3-approved
ontology nodes), masks descriptions in DataHub to give the model something to
predict, fires a run, reviews candidates (approve / reject), verifies DataHub
changes on approval, fires the run again, and asserts approved items are skipped
while rejected items are cleared and re-generated.

Spec: spec/USE_CASE_en.md §UC4: Metadata Generation
Spec: spec/feature/BACKEND.md §Metadata Generation Service

Function-level coverage (conf CRUD, boundary CRUD, run-gating, candidate review
edge cases, item filters) lives in tests/integration/spot/ and is not duplicated
here.
"""
# spec: USE_CASE_en.md §UC4

import urllib.parse
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from datahub.emitter.mcp import MetadataChangeProposalWrapper
from datahub.ingestion.graph.client import DatahubClientConfig, DataHubGraph
from datahub.metadata.schema_classes import (
    DatasetPropertiesClass,
    EditableDatasetPropertiesClass,
    EditableSchemaFieldInfoClass,
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
    delete_metagen_conf,
    delete_metagen_state_for_urn,
    delete_ontogen_node,
    load_fulfillment_doc,
    seed_approved_ontogen_node,
    seed_dataset_node_map,
    seed_metagen_boundary,
    seed_metagen_candidate,
    seed_metagen_conf,
    seed_metagen_item,
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

    The metagen conf is a managed COLLECTION: this arc uses TWO confs over
    DIFFERENT dataset groups (conf EU scoped to eu_profiles, conf OE scoped to
    orders.events) plus a third conf RIVAL also targeting eu_profiles to prove
    cross-conf approval exclusivity. Steps mirror USE_CASE_en.md §UC4:
      1.  Seed LLM context: fulfillment document + 5 approved ontogen nodes
          mapped to both datasets
      2.  Mask descriptions in DataHub: wipe eu_profiles dataset description and
          all column descriptions; wipe first 4 orders.events column descriptions
      3.  POST two confs — conf EU (dataset_urns=[eu_profiles]) and conf OE
          (dataset_urns=[orders.events]) — each with its own budget/events
      4.  PUT per-dataset boundaries for eu_profiles and orders.events
      5.  POST conf EU run + conf OE run — assert per-conf MetagenRunResponse
          (each run_id carries its conf_id; events isolated per conf)
      6.  GET each conf's event feed + cross-conf /event union
      7.  GET per-dataset items for both URNs — candidates carry conf_id/conf_name
      8.  Review candidates: approve eu_profiles dataset.description (conf EU),
          reject eu_profiles column.email.description, approve orders.events col
      8b. Cross-conf exclusivity: a RIVAL conf also scopes eu_profiles and runs,
          so a shared column item holds llm_approved candidates from BOTH confs;
          approving conf EU's then conf RIVAL's candidate on that SAME item demotes
          conf EU's back to llm_approved (one-approved-per-item holds globally
          across confs), verified by GET item-detail read-back
      9.  DataHub round-trip: verify approved values emitted to editable aspects
     10.  GET per-dataset metagen events — assert CANDIDATE_APPROVE / CANDIDATE_REJECT
     11.  POST conf EU run (second run) — approved items skipped, rejected cleared
     12.  Cleanup (finally): delete metagen state, boundaries, confs, DataHub
          aspect snapshots, document, ontogen nodes

    Spec: USE_CASE_en.md §UC4
    Spec: BACKEND.md §Metadata Generation Service — conf collection, per-conf run +
      events, cross-conf approval exclusivity, idempotency
    """
    conf_url = "/api/v1/spoke/metagen/conf"
    eu_boundary_url = f"/api/v1/spoke/common/data/{_EU_ENCODED}/attr/metagen/boundary"
    oe_boundary_url = f"/api/v1/spoke/common/data/{_OE_ENCODED}/attr/metagen/boundary"
    global_event_url = "/api/v1/spoke/metagen/event"

    # Conf ids created in step 3 / 8b, used for per-conf run + event routes + cleanup.
    conf_eu_id: str | None = None
    conf_oe_id: str | None = None
    conf_rival_id: str | None = None

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
        # spec: USE_CASE_en.md §UC4: Metadata Generation — LLM context includes related documents
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
        # spec: BACKEND.md §Generation Pipeline — reads UC3-approved ontology nodes via
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
        # spec: USE_CASE_en.md §UC4: Metadata Generation — metagen generates what is missing
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
        eu_original_dataset_description = (
            eu_props_snapshot.description if eu_props_snapshot else None
        )
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

        # ── Step 3: POST two confs over DIFFERENT dataset groups ──────────────
        # The conf collection lets teams run different documentation policies over
        # different dataset groups. Conf EU documents eu_profiles; conf OE documents
        # orders.events. Each carries its own budget and event feed.
        # spec: feature/BACKEND.md §Metadata Generation Service — conf collection;
        #   API.md §Metadata Generation — POST /metagen/conf → 201.
        conf_suffix = uuid.uuid4().hex[:8]
        post_conf_eu = await api_client.post(
            conf_url,
            headers=admin_headers,
            json={
                "name": f"uc4-eu-{conf_suffix}",
                "is_enabled": True,
                "schedule_tier": "daily",
                "dataset_filter": {"dataset_urns": [EU_PROFILES_URN]},
                "result_limit": 3,
                "overwrite_pending": True,
            },
        )
        assert post_conf_eu.status_code == 201, (
            f"POST conf EU failed: {post_conf_eu.status_code} {post_conf_eu.text}. "
            "spec: API.md §Metadata Generation — POST /metagen/conf → 201"
        )
        conf_eu_body = post_conf_eu.json()
        conf_eu_id = conf_eu_body["id"]
        assert conf_eu_body["is_enabled"] is True
        assert conf_eu_body["dataset_filter"] == {"dataset_urns": [EU_PROFILES_URN]}

        post_conf_oe = await api_client.post(
            conf_url,
            headers=admin_headers,
            json={
                "name": f"uc4-oe-{conf_suffix}",
                "is_enabled": True,
                "schedule_tier": "daily",
                "dataset_filter": {"dataset_urns": [ORDERS_EVENTS_URN]},
                "result_limit": 3,
                "overwrite_pending": True,
            },
        )
        assert post_conf_oe.status_code == 201, (
            f"POST conf OE failed: {post_conf_oe.status_code} {post_conf_oe.text}. "
            "spec: API.md §Metadata Generation — POST /metagen/conf → 201"
        )
        conf_oe_id = post_conf_oe.json()["id"]

        # ── Step 4: PUT per-dataset boundaries ───────────────────────────────
        # UC4 narrative: "The catalog team opts each dataset in."
        # spec: USE_CASE_en.md §UC4 — API Mapping — per-dataset boundary opt-in
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
            "spec: USE_CASE_en.md §UC4 — API Mapping"
        )
        eu_boundary_body = put_eu_boundary_resp.json()
        assert eu_boundary_body["dataset_urn"] == EU_PROFILES_URN, (
            f"boundary dataset_urn not echoed: {eu_boundary_body.get('dataset_urn')!r}. "
            "spec: USE_CASE_en.md §UC4 — API Mapping"
        )
        assert set(eu_boundary_body["allowed"]) == {"dataset.description", "column.description"}, (
            f"eu_profiles boundary allowed not preserved: {eu_boundary_body.get('allowed')!r}. "
            "spec: USE_CASE_en.md §UC4 — API Mapping"
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
            "spec: USE_CASE_en.md §UC4 — API Mapping"
        )
        oe_boundary_body = put_oe_boundary_resp.json()
        assert oe_boundary_body["dataset_urn"] == ORDERS_EVENTS_URN, (
            f"orders.events boundary dataset_urn not echoed: "
            f"{oe_boundary_body.get('dataset_urn')!r}. spec: USE_CASE_en.md §UC4 — API Mapping"
        )
        assert "column.description" in oe_boundary_body["allowed"], (
            "orders.events boundary allowed must include 'column.description'. "
            "spec: USE_CASE_en.md §UC4 — API Mapping"
        )

        # ── Step 5: POST per-conf runs (conf EU + conf OE) ────────────────────
        # Each conf runs independently under its own per-conf lock; the run
        # response carries the conf_id it ran. We run conf OE first, then conf EU
        # (the EU run is the one we assert on in detail).
        # spec: API.md §Metadata Generation — POST /metagen/conf/{conf_id}/method/run
        # spec: BACKEND.md §Event Catalogue — MetagenRunResponse: run_id, conf_id, status,
        #   dry_run, unresolved_urns, counts, producer_iterations, debate_outcome
        oe_run_resp = await api_client.post(
            f"{conf_url}/{conf_oe_id}/method/run", headers=admin_headers, timeout=90.0
        )
        assert oe_run_resp.status_code == 200, (
            f"POST conf OE run failed: {oe_run_resp.status_code} {oe_run_resp.text}. "
            "spec: API.md §Metadata Generation — per-conf run returns 200"
        )
        oe_run_id = oe_run_resp.json()["run_id"]
        assert oe_run_resp.json()["conf_id"] == conf_oe_id, (
            "conf OE run response must carry its own conf_id. "
            "spec: BACKEND.md §Event Catalogue — RUN_COMPLETE detail conf_id"
        )

        run_resp = await api_client.post(
            f"{conf_url}/{conf_eu_id}/method/run", headers=admin_headers, timeout=90.0
        )
        assert run_resp.status_code == 200, (
            f"POST conf EU run (first run) failed: "
            f"{run_resp.status_code} {run_resp.text}. "
            "spec: API.md §Metadata Generation — per-conf run returns 200"
        )
        run_body = run_resp.json()
        first_run_id = run_body["run_id"]

        assert "run_id" in run_body, (
            "MetagenRunResponse must carry 'run_id'. spec: BACKEND.md §Event Catalogue"
        )
        uuid.UUID(run_body["run_id"])  # raises ValueError if malformed
        assert run_body["conf_id"] == conf_eu_id, (
            "conf EU run response must carry conf_eu_id. "
            "spec: BACKEND.md §Event Catalogue — RUN_COMPLETE detail conf_id"
        )
        assert run_body.get("status") == "success", (
            f"MetagenRunResponse status expected 'success'; got {run_body.get('status')!r}. "
            "spec: BACKEND.md §Event Catalogue"
        )
        assert run_body.get("dry_run") is False, (
            f"MetagenRunResponse dry_run must be False for a real run; "
            f"got {run_body.get('dry_run')!r}. spec: BACKEND.md §Event Catalogue"
        )
        assert isinstance(run_body.get("unresolved_urns"), list), (
            "MetagenRunResponse unresolved_urns must be a list. spec: BACKEND.md §Event Catalogue"
        )
        assert run_body["unresolved_urns"] == [], (
            f"Expected no unresolved URNs with tag-scoped filter; "
            f"got {run_body['unresolved_urns']!r}. spec: BACKEND.md §Event Catalogue"
        )

        first_counts = run_body.get("counts")
        assert isinstance(first_counts, dict), (
            "MetagenRunResponse counts must be a dict. spec: BACKEND.md §Event Catalogue"
        )
        assert "items_considered" in first_counts, (
            "MetagenRunResponse counts must contain 'items_considered'. "
            "spec: BACKEND.md §Event Catalogue"
        )
        # eu_profiles: 1 dataset.description + 8 column descriptions = 9 items
        # orders.events: ≥1 column descriptions (4 masked) = ≥1 item
        # Total items_considered ≥ 1 deterministically.
        # spec: BACKEND.md §Event Catalogue — items_considered = in-scope target items
        assert (
            isinstance(first_counts["items_considered"], int)
            and first_counts["items_considered"] >= 1
        ), (
            f"counts.items_considered must be int ≥ 1 given scoped datasets + boundaries; "
            f"got {first_counts.get('items_considered')!r}. spec: BACKEND.md §Event Catalogue"
        )
        assert "candidates_added" in first_counts, (
            "MetagenRunResponse counts must contain 'candidates_added' on a real run. "
            "spec: BACKEND.md §Event Catalogue"
        )
        # spec: BACKEND.md §Event Catalogue — debate_outcome canonical set
        assert run_body.get("debate_outcome") in ("accept", "turns_exhausted", "cycle_detected"), (
            f"MetagenRunResponse debate_outcome={run_body.get('debate_outcome')!r} not in "
            "canonical set. spec: BACKEND.md §Event Catalogue — METAGEN.RUN_COMPLETE detail"
        )
        assert (
            isinstance(run_body.get("producer_iterations"), int)
            and run_body["producer_iterations"] >= 1
        ), (
            f"MetagenRunResponse producer_iterations must be int ≥ 1; "
            f"got {run_body.get('producer_iterations')!r}. spec: BACKEND.md §Event Catalogue"
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

        # ── Step 6: per-conf event isolation + cross-conf union ───────────────
        # Conf EU's event feed contains its own run, not conf OE's; the cross-conf
        # /event union contains both.
        # spec: API.md §Metadata Generation — /conf/{conf_id}/event per-conf;
        #   /metagen/event cross-conf union.
        eu_conf_ev = await api_client.get(
            f"{conf_url}/{conf_eu_id}/event?limit=50", headers=admin_headers
        )
        assert eu_conf_ev.status_code == 200, (
            f"GET conf EU event failed: {eu_conf_ev.status_code} {eu_conf_ev.text}"
        )
        eu_conf_run_ids = {e["detail"].get("run_id") for e in eu_conf_ev.json()["events"]}
        assert first_run_id in eu_conf_run_ids, "conf EU feed must include conf EU's run"
        assert oe_run_id not in eu_conf_run_ids, (
            "conf EU feed must not include conf OE's run. "
            "spec: API.md §Metadata Generation — per-conf event isolation"
        )

        union_ev = await api_client.get(f"{global_event_url}?limit=100", headers=admin_headers)
        assert union_ev.status_code == 200
        union_run_ids = {e["detail"].get("run_id") for e in union_ev.json()["events"]}
        assert {first_run_id, oe_run_id} <= union_run_ids, (
            "cross-conf /event union must include both confs' runs. "
            "spec: API.md §Metadata Generation — /metagen/event union"
        )

        # ── Step 6b: GET global event — assert METAGEN.RUN_COMPLETE detail ────
        # spec: BACKEND.md §Event Catalogue — detail keys: run_id, conf_id,
        #   unresolved_urns, counts, dry_run, producer_iterations, debate_outcome
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
            "spec: BACKEND.md §Event Catalogue"
        )
        rc_detail = run_complete_event["detail"]
        assert "run_id" in rc_detail, (
            "METAGEN.RUN_COMPLETE detail missing 'run_id'. spec: BACKEND.md §Event Catalogue"
        )
        assert "unresolved_urns" in rc_detail and isinstance(rc_detail["unresolved_urns"], list), (
            "METAGEN.RUN_COMPLETE detail missing or wrong type for 'unresolved_urns'. "
            "spec: BACKEND.md §Event Catalogue"
        )
        assert "counts" in rc_detail and isinstance(rc_detail["counts"], dict), (
            "METAGEN.RUN_COMPLETE detail missing or wrong type for 'counts'. "
            "spec: BACKEND.md §Event Catalogue"
        )
        assert rc_detail.get("dry_run") is False, (
            f"METAGEN.RUN_COMPLETE detail dry_run expected False; "
            f"got {rc_detail.get('dry_run')!r}. spec: BACKEND.md §Event Catalogue"
        )
        assert rc_detail.get("debate_outcome") in ("accept", "turns_exhausted", "cycle_detected"), (
            f"METAGEN.RUN_COMPLETE detail debate_outcome={rc_detail.get('debate_outcome')!r} "
            "not in canonical set. spec: BACKEND.md §Event Catalogue"
        )
        assert (
            isinstance(rc_detail.get("producer_iterations"), int)
            and rc_detail["producer_iterations"] >= 1
        ), (
            f"METAGEN.RUN_COMPLETE detail producer_iterations must be int ≥ 1; "
            f"got {rc_detail.get('producer_iterations')!r}. "
            "spec: BACKEND.md §Event Catalogue"
        )

        # ── Step 7: GET per-dataset items for both URNs ───────────────────────
        # UC4 narrative: "After the run, the dashboard lists items for each dataset."
        # spec: USE_CASE_en.md §UC4 — API Mapping
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
                    "spec: USE_CASE_en.md §UC4 — API Mapping"
                )
                assert item["status"] in ("pending", "llm_approved", "approved"), (
                    f"item status {item['status']!r} not in valid set. "
                    "spec: BACKEND.md §Item status — pending / llm_approved / approved"
                )
                assert item["dataset_urn"] == urn, (
                    f"item dataset_urn {item['dataset_urn']!r} != expected {urn!r}"
                )
                assert "composite_id" in item, (
                    "item missing 'composite_id'. spec: USE_CASE_en.md §UC4 — API Mapping"
                )
                # spec: USE_CASE_en.md §UC4 — API Mapping — composite_id format
                assert item["composite_id"] == f"{item['dataset_urn']}::{item['item_id']}", (
                    f"composite_id format mismatch: {item['composite_id']!r}. "
                    "spec: USE_CASE_en.md §UC4 — API Mapping"
                )

        # ── Step 8: Review candidates ─────────────────────────────────────────
        # Approve eu_profiles dataset.description, reject eu_profiles column.email.description,
        # approve orders.events column.<first_masked_field>.description.
        # Each review fetches item-detail first to find the first llm_approved candidate.
        # spec: USE_CASE_en.md §UC4 — Review
        # spec: BACKEND.md §Approval flow — POST .../candidate/{cid}/method/review

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
                "spec: USE_CASE_en.md §UC4 — Review"
            )
            review_body = review_resp.json()
            assert review_body.get("status") == "approved", (
                f"Candidate status after approve must be 'approved'; "
                f"got {review_body.get('status')!r}. spec: USE_CASE_en.md §UC4 — Review"
            )
            assert review_body.get("candidate_id") == eu_desc_cid, (
                "candidate_id echo mismatch. spec: BACKEND.md §Approval flow"
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
                "spec: USE_CASE_en.md §UC4 — Review"
            )
            reject_body = reject_resp.json()
            assert reject_body.get("status") == "rejected", (
                f"Candidate status after reject must be 'rejected'; "
                f"got {reject_body.get('status')!r}. spec: USE_CASE_en.md §UC4 — Review"
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
                    "spec: USE_CASE_en.md §UC4 — Review"
                )
                oe_review_body = oe_review_resp.json()
                assert oe_review_body.get("status") == "approved", (
                    f"Candidate status after approve must be 'approved'; "
                    f"got {oe_review_body.get('status')!r}. spec: USE_CASE_en.md §UC4 — Review"
                )

        # ── Step 8d: Cross-conf approval exclusivity on a SHARED item ─────────
        # A RIVAL conf also scopes eu_profiles, so it produces its own candidates
        # — each stamped with its own conf_id — on the SAME items as conf EU. On a
        # shared, not-yet-approved column item we approve conf EU's candidate first,
        # then approve conf RIVAL's candidate on that SAME item. The second approval
        # must atomically demote conf EU's just-approved sibling back to
        # llm_approved: the partial unique index UNIQUE (dataset_urn, item_id)
        # WHERE status='approved' holds GLOBALLY across confs, so an item can hold
        # at most one approved candidate regardless of which conf produced it.
        # spec: feature/BACKEND.md §Approval flow — approving a candidate flips any
        #   previously-approved sibling (possibly from a different conf) back to
        #   llm_approved in the same transaction.
        # spec: feature/BACKEND_SCHEMA.md §metagen_candidates — partial unique index
        #   UNIQUE (dataset_urn, item_id) WHERE status='approved' (global one-
        #   approved-per-item, across all confs).
        #
        # Generation skips items that already hold an approved candidate from ANY
        # conf (§Generation Pipeline step 4). The shared item used here is a column
        # that was NOT approved in step 8 and NOT email (rejected in 8b above), so
        # both confs accumulate llm_approved siblings on it.
        post_conf_rival = await api_client.post(
            conf_url,
            headers=admin_headers,
            json={
                "name": f"uc4-rival-{conf_suffix}",
                "is_enabled": True,
                "dataset_filter": {"dataset_urns": [EU_PROFILES_URN]},
                "result_limit": 3,
                "overwrite_pending": True,
            },
        )
        assert post_conf_rival.status_code == 201, (
            f"POST conf RIVAL failed: {post_conf_rival.status_code} {post_conf_rival.text}. "
            "spec: API.md §Metadata Generation — POST /metagen/conf → 201"
        )
        conf_rival_id = post_conf_rival.json()["id"]

        # Run conf RIVAL over eu_profiles. eu_profiles' boundary is enabled and its
        # dataset.description is approved (skipped), but its column items are still
        # open, so RIVAL produces RIVAL-stamped candidates on the same column items
        # conf EU already populated.
        rival_run_resp = await api_client.post(
            f"{conf_url}/{conf_rival_id}/method/run", headers=admin_headers, timeout=90.0
        )
        assert rival_run_resp.status_code == 200, (
            f"POST conf RIVAL run failed: {rival_run_resp.status_code} {rival_run_resp.text}. "
            "spec: API.md §Metadata Generation — per-conf run returns 200"
        )
        assert rival_run_resp.json()["conf_id"] == conf_rival_id, (
            "conf RIVAL run response must carry its own conf_id. "
            "spec: feature/BACKEND.md §Event Catalogue — RUN_COMPLETE detail conf_id"
        )

        # Find a SHARED column item that now holds llm_approved candidates from BOTH
        # conf EU and conf RIVAL (distinct conf_id). dataset.description is already
        # approved (single approved candidate) and email was rejected, so we scan the
        # column items via the per-dataset item list and probe each for a two-conf
        # candidate set.
        items_for_shared = await api_client.get(
            f"/api/v1/spoke/common/data/{_EU_ENCODED}/attr/metagen/item",
            headers=admin_headers,
        )
        assert items_for_shared.status_code == 200, (
            f"GET eu_profiles items for cross-conf probe failed: "
            f"{items_for_shared.status_code} {items_for_shared.text}"
        )
        shared_item_id: str | None = None
        eu_cand_on_shared: dict | None = None
        rival_cand_on_shared: dict | None = None
        for item in items_for_shared.json().get("items", []):
            item_id = item["item_id"]
            if item_id in ("dataset.description", eu_email_item_id):
                continue
            encoded_item = urllib.parse.quote(item_id, safe="")
            detail = await api_client.get(
                f"/api/v1/spoke/common/data/{_EU_ENCODED}/attr/metagen/item/{encoded_item}",
                headers=admin_headers,
            )
            if detail.status_code != 200:
                continue
            cands = detail.json().get("candidates", [])
            eu_c = next(
                (
                    c
                    for c in cands
                    if c["status"] == "llm_approved" and c["conf_id"] == conf_eu_id
                ),
                None,
            )
            rival_c = next(
                (
                    c
                    for c in cands
                    if c["status"] == "llm_approved" and c["conf_id"] == conf_rival_id
                ),
                None,
            )
            if eu_c is not None and rival_c is not None:
                shared_item_id = item_id
                eu_cand_on_shared = eu_c
                rival_cand_on_shared = rival_c
                break

        assert shared_item_id is not None, (
            "Cross-conf demotion is unreachable: no eu_profiles column item carried "
            "llm_approved candidates from BOTH conf EU and conf RIVAL after running "
            "both confs over the shared dataset. Under stub mode each conf emits one "
            "candidate per open column item, so a shared two-conf item must exist. "
            "spec: feature/BACKEND.md §Generation Pipeline — items shared across confs; "
            "spec: src/workflows/_stubs.py metagen_validate stub branch."
        )
        assert eu_cand_on_shared is not None and rival_cand_on_shared is not None
        shared_encoded_item = urllib.parse.quote(shared_item_id, safe="")

        # Approve conf EU's candidate on the shared item first.
        approve_eu_shared = await api_client.post(
            f"/api/v1/spoke/common/data/{_EU_ENCODED}"
            f"/attr/metagen/item/{shared_encoded_item}"
            f"/candidate/{eu_cand_on_shared['candidate_id']}/method/review",
            headers=admin_headers,
            json={"verdict": "approve", "reason": "cross-conf: approve EU first"},
        )
        assert approve_eu_shared.status_code == 200, (
            f"Approving conf EU's shared-item candidate failed: "
            f"{approve_eu_shared.status_code} {approve_eu_shared.text}. "
            "spec: feature/BACKEND.md §Approval flow"
        )

        # Now approve conf RIVAL's candidate on the SAME item. This must demote
        # conf EU's just-approved sibling back to llm_approved.
        approve_rival_shared = await api_client.post(
            f"/api/v1/spoke/common/data/{_EU_ENCODED}"
            f"/attr/metagen/item/{shared_encoded_item}"
            f"/candidate/{rival_cand_on_shared['candidate_id']}/method/review",
            headers=admin_headers,
            json={"verdict": "approve", "reason": "cross-conf: approve RIVAL demotes EU"},
        )
        assert approve_rival_shared.status_code == 200, (
            f"Approving conf RIVAL's shared-item candidate failed: "
            f"{approve_rival_shared.status_code} {approve_rival_shared.text}. "
            "spec: feature/BACKEND.md §Approval flow — cross-conf approval"
        )

        # Read the truth back from the backend via GET item-detail (not the POST
        # echo): conf RIVAL's candidate is now the sole approved candidate, and conf
        # EU's was atomically flipped back to llm_approved. The global one-approved-
        # per-item invariant holds across confs.
        detail_after = await api_client.get(
            f"/api/v1/spoke/common/data/{_EU_ENCODED}"
            f"/attr/metagen/item/{shared_encoded_item}",
            headers=admin_headers,
        )
        assert detail_after.status_code == 200, (
            f"GET shared item detail after cross-conf approval failed: "
            f"{detail_after.status_code} {detail_after.text}"
        )
        cands_after = detail_after.json().get("candidates", [])
        approved_after = [c for c in cands_after if c["status"] == "approved"]
        assert len(approved_after) == 1, (
            "Exactly one approved candidate may exist per item, globally across "
            f"confs; found {len(approved_after)}. "
            "spec: feature/BACKEND_SCHEMA.md §metagen_candidates — partial unique "
            "index UNIQUE (dataset_urn, item_id) WHERE status='approved'"
        )
        assert approved_after[0]["candidate_id"] == rival_cand_on_shared["candidate_id"], (
            "conf RIVAL's candidate must be the sole approved candidate after its "
            f"approval; got {approved_after[0]['candidate_id']!r}. "
            "spec: feature/BACKEND.md §Approval flow — newly approved candidate wins"
        )
        assert approved_after[0]["conf_id"] == conf_rival_id, (
            "The sole approved candidate must belong to conf RIVAL. "
            "spec: feature/BACKEND.md §Approval flow — cross-conf approval"
        )
        eu_after = next(
            (c for c in cands_after if c["candidate_id"] == eu_cand_on_shared["candidate_id"]),
            None,
        )
        assert eu_after is not None and eu_after["status"] == "llm_approved", (
            "conf EU's previously-approved candidate must be demoted back to "
            f"'llm_approved' when conf RIVAL's sibling is approved; got "
            f"{eu_after['status'] if eu_after else 'missing'!r}. "
            "spec: feature/BACKEND.md §Approval flow — approving a candidate flips "
            "the previously-approved sibling (possibly from a different conf) back "
            "to llm_approved in the same transaction"
        )

        # ── Step 9: DataHub round-trip for the two approved items ─────────────
        # After approving a dataset.description candidate, DataSpoke emits the value
        # to editableDatasetProperties.description.
        # After approving a column.description candidate, DataSpoke emits the value
        # to editableSchemaMetadata[fieldPath].description.
        # spec: USE_CASE_en.md §UC4 — Review — approval map
        # spec: BACKEND.md §Approval flow — approval writes to editable DataHub aspects
        if approved_eu_desc_value is not None:
            editable_props = graph.get_aspect(
                entity_urn=EU_PROFILES_URN,
                aspect_type=EditableDatasetPropertiesClass,
            )
            assert editable_props is not None, (
                "editableDatasetProperties aspect is None after approving dataset.description "
                "candidate — DataSpoke did not emit to DataHub. "
                "spec: USE_CASE_en.md §UC4 — Review"
            )
            assert editable_props.description == approved_eu_desc_value, (
                f"editableDatasetProperties.description={editable_props.description!r} "
                f"does not match approved value={approved_eu_desc_value!r}. "
                "spec: USE_CASE_en.md §UC4 — Review — approval emits value to DataHub"
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
                "spec: USE_CASE_en.md §UC4 — Review"
            )
            field_infos = editable_schema.editableSchemaFieldInfo or []
            matched_fi = next(
                (fi for fi in field_infos if fi.fieldPath == approved_field_path), None
            )
            assert matched_fi is not None, (
                f"editableSchemaMetadata has no entry for fieldPath={approved_field_path!r} "
                "after approving column description. spec: USE_CASE_en.md §UC4 — Review"
            )
            assert matched_fi.description == approved_oe_value, (
                f"editableSchemaMetadata[{approved_field_path!r}].description="
                f"{matched_fi.description!r} does not match approved value="
                f"{approved_oe_value!r}. spec: USE_CASE_en.md §UC4 — Review"
            )

        # ── Step 10: GET per-dataset metagen events ───────────────────────────
        # Verify CANDIDATE_APPROVE and CANDIDATE_REJECT events are recorded.
        # spec: USE_CASE_en.md §UC4 — Review
        # spec: BACKEND.md §Event Catalogue — CANDIDATE_APPROVE detail: item_id,
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
                "dataset.description candidate. spec: BACKEND.md §Event Catalogue"
            )
            ev_detail = approve_event["detail"]
            assert "item_id" in ev_detail, (
                "METAGEN.CANDIDATE_APPROVE detail missing 'item_id'. "
                "spec: BACKEND.md §Event Catalogue"
            )
            assert "candidate_id" in ev_detail, (
                "METAGEN.CANDIDATE_APPROVE detail missing 'candidate_id'. "
                "spec: BACKEND.md §Event Catalogue"
            )
            assert "reason" in ev_detail, (
                "METAGEN.CANDIDATE_APPROVE detail missing 'reason'. "
                "spec: BACKEND.md §Event Catalogue"
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
                "column.email.description candidate. spec: BACKEND.md §Event Catalogue"
            )
            rj_detail = reject_event["detail"]
            assert "item_id" in rj_detail, (
                "METAGEN.CANDIDATE_REJECT detail missing 'item_id'. "
                "spec: BACKEND.md §Event Catalogue"
            )
            assert "candidate_id" in rj_detail, (
                "METAGEN.CANDIDATE_REJECT detail missing 'candidate_id'. "
                "spec: BACKEND.md §Event Catalogue"
            )
            assert "reason" in rj_detail, (
                "METAGEN.CANDIDATE_REJECT detail missing 'reason'. "
                "spec: BACKEND.md §Event Catalogue"
            )

        # ── Step 11: POST method/run (second run) — idempotency ───────────────
        # Approved items must be skipped; rejected items must be cleared and re-generated.
        # spec: USE_CASE_en.md §UC4 — Review — "previously approved descriptions are
        # not overwritten on subsequent runs"
        # spec: BACKEND.md §Generation Pipeline — enumeration skips approved items
        # spec: BACKEND.md §Generation Pipeline — rejected candidates cleared at run start
        #   before each run so they can be re-generated fresh
        run2_resp = await api_client.post(
            f"{conf_url}/{conf_eu_id}/method/run", headers=admin_headers, timeout=90.0
        )
        assert run2_resp.status_code == 200, (
            f"POST conf EU run (second run) failed: "
            f"{run2_resp.status_code} {run2_resp.text}. "
            "spec: API.md §Metadata Generation — per-conf run"
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
        # spec: BACKEND.md §Generation Pipeline — enumeration filters out approved (urn, item_id)
        #   that already have an approved candidate.
        assert second_items_considered < first_items_considered, (
            f"Second run items_considered ({second_items_considered}) must be strictly less "
            f"than first run ({first_items_considered}) because approved items are excluded. "
            "spec: BACKEND.md §Generation Pipeline — target enumeration skips approved items"
        )

        # Verify approved items: GET item-detail and assert exactly one approved candidate,
        # no new llm_approved candidate from the second run.
        # spec: BACKEND.md §Approval flow — approved candidates are not evicted or overwritten
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
                "spec: BACKEND.md §Approval flow — partial unique index: at most one "
                "approved candidate per (dataset_urn, item_id)"
            )
            assert len(candidates2) == 1, (
                f"Approved item should have only the approved candidate after second run; "
                f"got {len(candidates2)} candidates with statuses "
                f"{[c['status'] for c in candidates2]}. "
                "spec: BACKEND.md §Generation Pipeline — target enumeration skips approved items"
            )

        # Verify the rejected eu_profiles column.email.description: prior rejected candidate
        # is gone (cleared by _clear_rejected_candidates), and a new llm_approved candidate
        # was generated.
        # spec: USE_CASE_en.md §UC4 — Review — at the start of each run all rejected
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
                "spec: BACKEND.md §Generation Pipeline — rejected candidates removed at run start"
            )
            new_llm_approved_email = [
                c for c in email_candidates if c["status"] == "llm_approved"
            ]
            assert len(new_llm_approved_email) >= 1, (
                "After clearing the rejected candidate, the second run must produce a new "
                "llm_approved candidate for eu_profiles column.email.description. "
                "spec: BACKEND.md §Generation Pipeline — rejected items re-generated next run"
            )

        # Verify second run RUN_COMPLETE event is recorded.
        # spec: BACKEND.md §Event Catalogue — RUN_COMPLETE emitted on every run
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
            "after second method/run. spec: BACKEND.md §Event Catalogue"
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

        # Delete per-dataset boundaries and the three confs (EU, OE, RIVAL).
        with suppress(Exception):
            await api_client.delete(eu_boundary_url, headers=admin_headers)
        with suppress(Exception):
            await api_client.delete(oe_boundary_url, headers=admin_headers)
        for cid in (conf_eu_id, conf_oe_id, conf_rival_id):
            if cid is not None:
                with suppress(Exception):
                    await api_client.delete(f"{conf_url}/{cid}", headers=admin_headers)

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


# ── Focused: covered-datasets view + reject-approved + run_id/created_at ─────────
#
# These cases assert backend invariants that the UC4 narrative arc above does not
# naturally reach. They seed metagen state via raw SQL (boundary states, an
# already-emitted approved candidate) because the concern under test is the
# query/review behaviour, not the LLM run pipeline that would produce the data.
# They run in stub mode (no LLM dependency); real-LLM-only assertions are not
# needed here. spec: TESTING.md §Spot vs Api-Wired — raw-SQL seeding when the
# concern is review/query behaviour, not the run pipeline.


@pytest.mark.asyncio
async def test_uc4_covered_datasets_view(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """GET /spoke/metagen/conf/{conf_id}/dataset — covered datasets boundary view.

    A conf scoped (via an explicit dataset_urns filter) to two datasets:
      - eu_profiles: writable boundary (is_enabled=true, non-empty allowed) → not blocked
      - orders.events: blocked boundary (is_enabled=false) → boundary-blocked

    Asserts the spec invariants for the covered view:
      1. Default (include_disallowed omitted): only the writable covered dataset is
         returned; the boundary-blocked one is hidden.
      2. ?include_disallowed=true: both appear; the blocked one carries blocked=true
         with a reason; the writable one carries blocked=false.
      3. Each row's is_enabled / allowed / owner boundary summary is correct.
      4. Unknown conf_id → 404 METAGEN_CONF_NOT_FOUND.

    Spec: API.md §Metadata Generation — GET /spoke/metagen/conf/{conf_id}/dataset
    Spec: feature/BACKEND.md §Covered-datasets view
    """
    conf_id: str | None = None
    owner_label = "uc4-covered-owner@imazon.test"
    try:
        # Seed a conf scoped to exactly the two fulfillment datasets via dataset_urns.
        # spec: feature/BACKEND.md §Covered-datasets view — resolution reuses
        #   resolve_dataset_scope for the conf's dataset_filter.
        conf_id = await seed_metagen_conf(
            async_session,
            name=f"uc4-covered-{uuid.uuid4().hex[:8]}",
            is_enabled=True,
            schedule_tier="daily",
            dataset_filter={"dataset_urns": [EU_PROFILES_URN, ORDERS_EVENTS_URN]},
        )

        # eu_profiles: writable boundary (enabled + non-empty allowed) → blocked=false.
        await seed_metagen_boundary(
            async_session,
            dataset_urn=EU_PROFILES_URN,
            is_enabled=True,
            allowed=["dataset.description", "column.description"],
            owner=owner_label,
        )
        # orders.events: disabled boundary → boundary-blocked.
        await seed_metagen_boundary(
            async_session,
            dataset_urn=ORDERS_EVENTS_URN,
            is_enabled=False,
            allowed=["column.description"],
            owner=None,
        )

        covered_url = f"/api/v1/spoke/metagen/conf/{conf_id}/dataset"

        # ── 1. Default response excludes the boundary-blocked covered dataset ──
        # spec: feature/BACKEND.md §Covered-datasets view — default returns only
        #   writable (non-blocked) covered datasets.
        default_resp = await api_client.get(covered_url, headers=admin_headers)
        assert default_resp.status_code == 200, (
            f"GET covered datasets (default) failed: "
            f"{default_resp.status_code} {default_resp.text}. "
            "spec: API.md §Metadata Generation — GET /conf/{conf_id}/dataset"
        )
        default_body = default_resp.json()
        # Standard envelope. spec: API.md §Standard Envelope
        for key in ("offset", "limit", "total_count"):
            assert key in default_body, (
                f"covered-datasets response missing '{key}'. spec: API.md §Standard Envelope"
            )
        # The covered view mirrors /uncovered, whose rows live under 'datasets'.
        # spec: API.md §Metadata Generation — /conf/{conf_id}/dataset mirrors /uncovered
        assert "datasets" in default_body and isinstance(default_body["datasets"], list), (
            "covered-datasets response must carry a 'datasets' list of rows. "
            "spec: API.md §Metadata Generation — mirrors /uncovered"
        )
        default_urns = {r["dataset_urn"] for r in default_body["datasets"]}
        assert EU_PROFILES_URN in default_urns, (
            "Writable covered dataset eu_profiles must appear in the default covered view. "
            "spec: feature/BACKEND.md §Covered-datasets view — writable datasets returned"
        )
        assert ORDERS_EVENTS_URN not in default_urns, (
            "Boundary-blocked covered dataset orders.events must be hidden by default. "
            "spec: feature/BACKEND.md §Covered-datasets view — default hides blocked"
        )
        for r in default_body["datasets"]:
            assert r["blocked"] is False, (
                f"Default covered view must only contain non-blocked rows; got "
                f"blocked={r['blocked']!r} for {r['dataset_urn']!r}. "
                "spec: feature/BACKEND.md §Covered-datasets view"
            )

        # ── 2. ?include_disallowed=true reveals the blocked covered dataset ───
        # spec: feature/BACKEND.md §Covered-datasets view — include_disallowed adds
        #   boundary-blocked covered datasets flagged with a reason.
        all_resp = await api_client.get(
            f"{covered_url}?include_disallowed=true", headers=admin_headers
        )
        assert all_resp.status_code == 200, (
            f"GET covered datasets (include_disallowed) failed: "
            f"{all_resp.status_code} {all_resp.text}"
        )
        all_rows = all_resp.json()["datasets"]
        by_urn = {r["dataset_urn"]: r for r in all_rows}
        assert EU_PROFILES_URN in by_urn and ORDERS_EVENTS_URN in by_urn, (
            "include_disallowed=true must reveal both the writable and the blocked "
            f"covered dataset; got {sorted(by_urn)!r}. "
            "spec: feature/BACKEND.md §Covered-datasets view"
        )

        # ── 3. Per-row boundary summary correctness ──────────────────────────
        eu_row = by_urn[EU_PROFILES_URN]
        assert eu_row["blocked"] is False, (
            "eu_profiles has an enabled, non-empty-allowed boundary → blocked=false. "
            "spec: feature/BACKEND.md §Covered-datasets view"
        )
        assert eu_row["is_enabled"] is True, (
            f"eu_profiles boundary is_enabled must echo true; got {eu_row['is_enabled']!r}. "
            "spec: API.md §Metadata Generation — covered row carries is_enabled"
        )
        assert set(eu_row["allowed"]) == {"dataset.description", "column.description"}, (
            f"eu_profiles allowed not echoed: {eu_row['allowed']!r}. "
            "spec: API.md §Metadata Generation — covered row carries allowed"
        )
        assert eu_row["owner"] == owner_label, (
            f"eu_profiles owner not echoed: {eu_row['owner']!r} != {owner_label!r}. "
            "spec: API.md §Metadata Generation — covered row carries owner"
        )

        oe_row = by_urn[ORDERS_EVENTS_URN]
        assert oe_row["blocked"] is True, (
            "orders.events has a disabled boundary → blocked=true under include_disallowed. "
            "spec: feature/BACKEND.md §Covered-datasets view — disabled boundary blocks"
        )
        assert oe_row["is_enabled"] is False, (
            f"orders.events boundary is_enabled must echo false; got {oe_row['is_enabled']!r}. "
            "spec: feature/BACKEND.md §Covered-datasets view"
        )
        assert oe_row.get("reason"), (
            "A boundary-blocked covered row must carry a non-empty 'reason'. "
            "spec: feature/BACKEND.md §Covered-datasets view — boundary_blocked reason"
        )

        # ── 4. Unknown conf → 404 METAGEN_CONF_NOT_FOUND ─────────────────────
        # spec: API.md §Metadata Generation — 404 METAGEN_CONF_NOT_FOUND when absent
        # spec: API.md §Error Codes — METAGEN_CONF_NOT_FOUND
        missing_resp = await api_client.get(
            f"/api/v1/spoke/metagen/conf/{uuid.uuid4()}/dataset", headers=admin_headers
        )
        assert missing_resp.status_code == 404, (
            f"Unknown conf_id must return 404; got {missing_resp.status_code} {missing_resp.text}. "
            "spec: API.md §Metadata Generation — 404 METAGEN_CONF_NOT_FOUND"
        )
        assert missing_resp.json().get("error_code") == "METAGEN_CONF_NOT_FOUND", (
            f"Unknown conf error code must be METAGEN_CONF_NOT_FOUND; got "
            f"{missing_resp.json().get('error_code')!r}. spec: API.md §Error Codes"
        )

    finally:
        with suppress(Exception):
            await delete_metagen_state_for_urn(async_session, EU_PROFILES_URN)
        with suppress(Exception):
            await delete_metagen_state_for_urn(async_session, ORDERS_EVENTS_URN)
        if conf_id is not None:
            with suppress(Exception):
                await delete_metagen_conf(async_session, conf_id)


@pytest.mark.asyncio
async def test_uc4_reject_approved_clears_datahub_description(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """Rejecting an approved candidate flips it to rejected AND clears DataHub.

    Mirrors plan #7. Two parallel cases on eu_profiles:
      A. dataset.description: seed an llm_approved candidate → approve via REST
         (writes EditableDatasetProperties.description in DataHub, GMS read-back
         confirms) → reject via REST → status becomes 'rejected', the editable
         DataHub description is removed (GMS read-back is empty/None), and a
         METAGEN.CANDIDATE_REJECT event is recorded.
      B. control: an llm_approved candidate (never approved, no DataHub write) is
         rejected → status 'rejected' and a pre-existing sentinel editable aspect
         is left UNTOUCHED (the llm_approved reject does not write to DataHub).

    Spec: API.md §Metadata Generation — reject valid on approved; removes editable aspect
    Spec: feature/BACKEND.md §Approval flow — reject branch (approved vs llm_approved)
    """
    boundary_url = f"/api/v1/spoke/common/data/{_EU_ENCODED}/attr/metagen/boundary"
    graph: DataHubGraph | None = None
    approved_item_id = "dataset.description"
    control_item_id = "column.email.description"
    approved_value = "Imazon EU customer profile dataset (uc4 reject-approved test)."
    sentinel_value = "uc4 sentinel — llm_approved reject must not touch DataHub"
    try:
        dh_token = get_datahub_token()
        graph = DataHubGraph(DatahubClientConfig(server=_gms_url, token=dh_token))

        # Enabled boundary so review passes the boundary guard.
        # spec: feature/BACKEND.md §Boundary guard — review needs is_enabled=true boundary.
        put_boundary = await api_client.put(
            boundary_url,
            headers=admin_headers,
            json={
                "is_enabled": True,
                "allowed": ["dataset.description", "column.description"],
            },
        )
        assert put_boundary.status_code in (200, 201), (
            f"PUT eu_profiles boundary failed: {put_boundary.status_code} {put_boundary.text}"
        )

        # ── Case A: seed an llm_approved dataset.description candidate ────────
        await seed_metagen_item(
            async_session,
            dataset_urn=EU_PROFILES_URN,
            item_id=approved_item_id,
            kind="dataset.description",
        )
        approved_cid = await seed_metagen_candidate(
            async_session,
            dataset_urn=EU_PROFILES_URN,
            item_id=approved_item_id,
            value=approved_value,
            status="llm_approved",
            item_kind="dataset.description",
        )

        approved_encoded = urllib.parse.quote(approved_item_id, safe="")
        review_base = (
            f"/api/v1/spoke/common/data/{_EU_ENCODED}"
            f"/attr/metagen/item/{approved_encoded}/candidate/{approved_cid}/method/review"
        )

        # Approve → writes editable DataHub aspect.
        # spec: API.md §Metadata Generation — approve writes value to editable aspect.
        approve_resp = await api_client.post(
            review_base,
            headers=admin_headers,
            json={"verdict": "approve", "reason": "uc4 reject-approved: approve first"},
        )
        assert approve_resp.status_code == 200, (
            f"Approve dataset.description failed: {approve_resp.status_code} {approve_resp.text}"
        )
        assert approve_resp.json().get("status") == "approved", (
            "Candidate status after approve must be 'approved'. spec: USE_CASE_en.md §UC4 — Review"
        )

        # GMS read-back: editable description now holds the approved value.
        # spec: API.md §Metadata Generation — approve emits to editableDatasetProperties.
        editable_after_approve = graph.get_aspect(
            entity_urn=EU_PROFILES_URN, aspect_type=EditableDatasetPropertiesClass
        )
        assert editable_after_approve is not None, (
            "editableDatasetProperties is None after approve — DataSpoke did not emit. "
            "spec: API.md §Metadata Generation — approve writes editable aspect"
        )
        assert editable_after_approve.description == approved_value, (
            f"editableDatasetProperties.description={editable_after_approve.description!r} "
            f"!= approved value={approved_value!r} after approve. "
            "spec: API.md §Metadata Generation — approve emits value to DataHub"
        )

        # Now REJECT the approved candidate.
        # spec: API.md §Metadata Generation — reject valid on approved candidate.
        reject_resp = await api_client.post(
            review_base,
            headers=admin_headers,
            json={"verdict": "reject", "reason": "uc4 reject-approved: reject after approve"},
        )
        assert reject_resp.status_code == 200, (
            f"Reject of approved candidate must succeed (200); got "
            f"{reject_resp.status_code} {reject_resp.text}. "
            "spec: API.md §Metadata Generation — reject valid on approved candidate"
        )
        assert reject_resp.json().get("status") == "rejected", (
            f"Candidate status after rejecting an approved candidate must be 'rejected'; "
            f"got {reject_resp.json().get('status')!r}. "
            "spec: feature/BACKEND.md §Approval flow — reject flips to rejected"
        )

        # GMS read-back: the editable description it had written is removed.
        # spec: feature/BACKEND.md §Approval flow — reject of approved sets
        #   EditableDatasetProperties.description="" (falls back to non-editable).
        editable_after_reject = graph.get_aspect(
            entity_urn=EU_PROFILES_URN, aspect_type=EditableDatasetPropertiesClass
        )
        cleared_desc = (
            editable_after_reject.description if editable_after_reject is not None else None
        )
        assert cleared_desc in (None, ""), (
            f"Rejecting an approved dataset.description candidate must remove the editable "
            f"DataHub description (expected ''/None); got {cleared_desc!r}. "
            "spec: feature/BACKEND.md §Approval flow — reject of approved removes editable aspect"
        )

        # The DB row is now rejected (read-back via item detail).
        detail_resp = await api_client.get(
            f"/api/v1/spoke/common/data/{_EU_ENCODED}/attr/metagen/item/{approved_encoded}",
            headers=admin_headers,
        )
        assert detail_resp.status_code == 200
        detail_cands = detail_resp.json().get("candidates", [])
        target = next((c for c in detail_cands if c["candidate_id"] == approved_cid), None)
        assert target is not None and target["status"] == "rejected", (
            f"Item-detail read-back must show the candidate as 'rejected'; got "
            f"{target['status'] if target else 'missing'!r}. "
            "spec: feature/BACKEND.md §Approval flow"
        )

        # A METAGEN.CANDIDATE_REJECT event is recorded for this candidate.
        # spec: feature/BACKEND.md §Event Catalogue — reject emits METAGEN.CANDIDATE_REJECT.
        events_resp = await api_client.get(
            f"/api/v1/spoke/common/data/{_EU_ENCODED}/event/metagen?limit=50",
            headers=admin_headers,
        )
        assert events_resp.status_code == 200
        reject_event = next(
            (
                e
                for e in events_resp.json().get("events", [])
                if e["event_type"] == "METAGEN.CANDIDATE_REJECT"
                and e.get("detail", {}).get("candidate_id") == approved_cid
            ),
            None,
        )
        assert reject_event is not None, (
            "No METAGEN.CANDIDATE_REJECT event found for the rejected approved candidate. "
            "spec: feature/BACKEND.md §Event Catalogue — CANDIDATE_REJECT"
        )

        # ── Case B: llm_approved reject must NOT touch DataHub ────────────────
        # Pre-write a sentinel editable column aspect for the control field, then
        # reject the llm_approved candidate. The sentinel must survive.
        # spec: feature/BACKEND.md §Approval flow — rejecting an llm_approved
        #   candidate writes nothing to DataHub.
        graph.emit_mcp(
            MetadataChangeProposalWrapper(
                entityUrn=EU_PROFILES_URN,
                aspect=EditableSchemaMetadataClass(
                    editableSchemaFieldInfo=[
                        EditableSchemaFieldInfoClass(
                            fieldPath="email", description=sentinel_value
                        )
                    ]
                ),
            )
        )

        await seed_metagen_item(
            async_session,
            dataset_urn=EU_PROFILES_URN,
            item_id=control_item_id,
            kind="column.description",
            field_path="email",
        )
        control_cid = await seed_metagen_candidate(
            async_session,
            dataset_urn=EU_PROFILES_URN,
            item_id=control_item_id,
            value="never-emitted column description",
            status="llm_approved",
            item_kind="column.description",
        )
        control_encoded = urllib.parse.quote(control_item_id, safe="")
        control_reject = await api_client.post(
            f"/api/v1/spoke/common/data/{_EU_ENCODED}"
            f"/attr/metagen/item/{control_encoded}/candidate/{control_cid}/method/review",
            headers=admin_headers,
            json={"verdict": "reject", "reason": "uc4 reject-approved: llm_approved control"},
        )
        assert control_reject.status_code == 200, (
            f"Reject of llm_approved candidate failed: "
            f"{control_reject.status_code} {control_reject.text}"
        )
        assert control_reject.json().get("status") == "rejected", (
            "llm_approved candidate must flip to 'rejected'. "
            "spec: feature/BACKEND.md §Approval flow"
        )

        # Sentinel editable aspect is untouched (no DataHub write on llm_approved reject).
        editable_control = graph.get_aspect(
            entity_urn=EU_PROFILES_URN, aspect_type=EditableSchemaMetadataClass
        )
        control_fi = next(
            (
                fi
                for fi in (editable_control.editableSchemaFieldInfo or [])
                if fi.fieldPath == "email"
            ),
            None,
        ) if editable_control is not None else None
        assert control_fi is not None and control_fi.description == sentinel_value, (
            "Rejecting an llm_approved candidate must NOT alter DataHub: the sentinel "
            f"editable description must survive; got "
            f"{control_fi.description if control_fi else 'missing'!r}. "
            "spec: feature/BACKEND.md §Approval flow — llm_approved reject writes nothing"
        )

    finally:
        with suppress(Exception):
            await delete_metagen_state_for_urn(async_session, EU_PROFILES_URN)
        with suppress(Exception):
            await api_client.delete(boundary_url, headers=admin_headers)
        # Remove the editable overrides written during the test (approve + sentinel).
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


@pytest.mark.asyncio
async def test_uc4_run_id_and_created_at_exposed(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """Candidate responses carry run_id; item-list rows carry created_at.

    Mirrors plan #6 (evidence link from candidate run_id; result table Created At
    column). Seeds one item + candidate and reads:
      - the per-dataset item LIST rows → each carries non-null created_at
      - the item DETAIL candidate → carries run_id and created_at

    Spec: API.md §Metadata Generation — item-list row carries created_at;
      item-detail candidate carries run_id, created_at.
    """
    boundary_url = f"/api/v1/spoke/common/data/{_EU_ENCODED}/attr/metagen/boundary"
    item_id = "dataset.description"
    try:
        await seed_metagen_boundary(
            async_session,
            dataset_urn=EU_PROFILES_URN,
            is_enabled=True,
            allowed=["dataset.description", "column.description"],
        )
        await seed_metagen_item(
            async_session,
            dataset_urn=EU_PROFILES_URN,
            item_id=item_id,
            kind="dataset.description",
        )
        cid = await seed_metagen_candidate(
            async_session,
            dataset_urn=EU_PROFILES_URN,
            item_id=item_id,
            value="run_id/created_at exposure probe",
            status="llm_approved",
            item_kind="dataset.description",
        )

        # ── Item LIST rows carry created_at ──────────────────────────────────
        # spec: API.md §Metadata Generation — item row carries created_at.
        list_resp = await api_client.get(
            f"/api/v1/spoke/common/data/{_EU_ENCODED}/attr/metagen/item",
            headers=admin_headers,
        )
        assert list_resp.status_code == 200, (
            f"GET item list failed: {list_resp.status_code} {list_resp.text}"
        )
        list_rows = list_resp.json().get("items", [])
        target_row = next((r for r in list_rows if r["item_id"] == item_id), None)
        assert target_row is not None, (
            "Seeded item must appear in the per-dataset item list."
        )
        assert "created_at" in target_row and target_row["created_at"], (
            f"Item-list row must carry a non-empty 'created_at'; got "
            f"{target_row.get('created_at')!r}. "
            "spec: API.md §Metadata Generation — item row carries created_at"
        )

        # ── Item DETAIL candidate carries run_id + created_at ────────────────
        # spec: API.md §Metadata Generation — item-detail candidate carries run_id, created_at.
        encoded_item = urllib.parse.quote(item_id, safe="")
        detail_resp = await api_client.get(
            f"/api/v1/spoke/common/data/{_EU_ENCODED}/attr/metagen/item/{encoded_item}",
            headers=admin_headers,
        )
        assert detail_resp.status_code == 200, (
            f"GET item detail failed: {detail_resp.status_code} {detail_resp.text}"
        )
        cands = detail_resp.json().get("candidates", [])
        cand = next((c for c in cands if c["candidate_id"] == cid), None)
        assert cand is not None, "Seeded candidate must appear in item detail."
        assert "run_id" in cand and cand["run_id"], (
            f"Candidate response must carry a non-empty 'run_id'; got {cand.get('run_id')!r}. "
            "spec: API.md §Metadata Generation — item-detail candidate carries run_id"
        )
        uuid.UUID(str(cand["run_id"]))  # raises ValueError if malformed
        assert "created_at" in cand and cand["created_at"], (
            f"Candidate response must carry a non-empty 'created_at'; got "
            f"{cand.get('created_at')!r}. "
            "spec: API.md §Metadata Generation — item-detail candidate carries created_at"
        )

    finally:
        with suppress(Exception):
            await delete_metagen_state_for_urn(async_session, EU_PROFILES_URN)
        with suppress(Exception):
            await api_client.delete(boundary_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_uc4_dataset_rollup_view(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """GET /spoke/metagen/dataset — per-dataset rollup of generation results.

    Seeds a deterministic candidate set across the two fulfillment datasets via raw
    SQL (the concern is the aggregation query, not the LLM run pipeline that would
    produce the data; the run pipeline cannot deterministically yield a fixed mix of
    approved / rejected / multi-conf candidates). Two confs (EU, RIVAL) and the
    following items:

      eu_profiles  (boundary enabled, allowed=[dataset.description, column.description]):
        - dataset.description       : conf EU approved  + conf EU rejected      (2 cands)
        - column.email.description  : conf EU llm_approved + conf RIVAL llm_approved (2 cands)
      orders.events  (NO boundary):
        - column.foo.description    : conf RIVAL rejected                       (1 cand)

    Asserts the spec invariants of API.md §Metadata Generation — GET /spoke/metagen/dataset:
      1. Unfiltered: one row per dataset; item_count = distinct items; candidate-level
         approved_count / rejected_count / candidate_count (candidate_count counts ALL
         candidates incl. rejected); boundary is_enabled / allowed via LEFT JOIN
         (is_enabled=false, allowed=[] when no boundary); last_modified_at equals the
         max item created_at of the dataset.
      1b. Default sort is last_modified_at_desc; ?sort=last_modified_at_asc reverses it.
      2. dataset_urn is a substring filter.
      3. conf_id restricts rows to datasets holding a candidate from that conf AND
         scopes every count to that conf's candidates.
      4. Malformed conf_id → 404 metagen_conf.

    Spec: API.md §Metadata Generation — GET /spoke/metagen/dataset
    Spec: feature/BACKEND.md §Per-dataset rollup view
    """
    dataset_url = "/api/v1/spoke/metagen/dataset"
    conf_eu_id: str | None = None
    conf_rival_id: str | None = None
    # Pin item created_at so the rollup's last_modified_at (= max item created_at
    # per dataset) is deterministic and the two datasets order distinctly.
    # spec: feature/BACKEND.md §Per-dataset rollup view — last_modified_at = max item created_at;
    # spec: API.md §Metadata Generation — default sort last_modified_at_desc.
    base_ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    OE_ITEM_TS = base_ts  # orders.events: oldest → sorts last under _desc
    EU_OLD_ITEM_TS = base_ts + timedelta(hours=1)
    EU_NEW_ITEM_TS = base_ts + timedelta(hours=2)  # eu_profiles max → sorts first under _desc
    try:
        # ── Seed two confs ────────────────────────────────────────────────────
        # spec: feature/BACKEND.md §Metadata Generation Service — conf collection.
        suffix = uuid.uuid4().hex[:8]
        conf_eu_id = await seed_metagen_conf(
            async_session,
            name=f"uc4-rollup-eu-{suffix}",
            is_enabled=True,
            dataset_filter={"dataset_urns": [EU_PROFILES_URN]},
        )
        conf_rival_id = await seed_metagen_conf(
            async_session,
            name=f"uc4-rollup-rival-{suffix}",
            is_enabled=True,
            dataset_filter={"dataset_urns": [EU_PROFILES_URN, ORDERS_EVENTS_URN]},
        )

        # ── eu_profiles: enabled boundary + two items ─────────────────────────
        # spec: feature/BACKEND.md §Per-dataset rollup view — LEFT JOIN boundary.
        await seed_metagen_boundary(
            async_session,
            dataset_urn=EU_PROFILES_URN,
            is_enabled=True,
            allowed=["dataset.description", "column.description"],
            owner="uc4-rollup-owner@imazon.test",
        )
        # dataset.description: conf EU approved + conf EU rejected.
        await seed_metagen_item(
            async_session,
            dataset_urn=EU_PROFILES_URN,
            item_id="dataset.description",
            kind="dataset.description",
            created_at=EU_OLD_ITEM_TS,
        )
        await seed_metagen_candidate(
            async_session,
            dataset_urn=EU_PROFILES_URN,
            item_id="dataset.description",
            value="approved dataset desc (rollup)",
            status="approved",
            conf_id=conf_eu_id,
            item_kind="dataset.description",
        )
        await seed_metagen_candidate(
            async_session,
            dataset_urn=EU_PROFILES_URN,
            item_id="dataset.description",
            value="rejected dataset desc (rollup)",
            status="rejected",
            conf_id=conf_eu_id,
            item_kind="dataset.description",
        )
        # column.email.description: conf EU llm_approved + conf RIVAL llm_approved.
        await seed_metagen_item(
            async_session,
            dataset_urn=EU_PROFILES_URN,
            item_id="column.email.description",
            kind="column.description",
            field_path="email",
            created_at=EU_NEW_ITEM_TS,
        )
        await seed_metagen_candidate(
            async_session,
            dataset_urn=EU_PROFILES_URN,
            item_id="column.email.description",
            value="EU email desc (rollup)",
            status="llm_approved",
            conf_id=conf_eu_id,
            item_kind="column.description",
        )
        await seed_metagen_candidate(
            async_session,
            dataset_urn=EU_PROFILES_URN,
            item_id="column.email.description",
            value="RIVAL email desc (rollup)",
            status="llm_approved",
            conf_id=conf_rival_id,
            item_kind="column.description",
        )

        # ── orders.events: NO boundary + one item (conf RIVAL rejected) ───────
        await seed_metagen_item(
            async_session,
            dataset_urn=ORDERS_EVENTS_URN,
            item_id="column.foo.description",
            kind="column.description",
            field_path="foo",
            created_at=OE_ITEM_TS,
        )
        await seed_metagen_candidate(
            async_session,
            dataset_urn=ORDERS_EVENTS_URN,
            item_id="column.foo.description",
            value="OE foo desc (rollup)",
            status="rejected",
            conf_id=conf_rival_id,
            item_kind="column.description",
        )

        # ── 1. Unfiltered rollup: per-dataset rows + candidate-level counts ───
        # spec: API.md §Metadata Generation — GET /spoke/metagen/dataset row shape.
        resp = await api_client.get(f"{dataset_url}?limit=100", headers=admin_headers)
        assert resp.status_code == 200, (
            f"GET /spoke/metagen/dataset failed: {resp.status_code} {resp.text}. "
            "spec: API.md §Metadata Generation — GET /spoke/metagen/dataset"
        )
        body = resp.json()
        # Standard pagination envelope. spec: API.md §Standard Envelope.
        for key in ("offset", "limit", "total_count"):
            assert key in body, (
                f"rollup response missing '{key}'. spec: API.md §Standard Envelope"
            )
        assert "datasets" in body and isinstance(body["datasets"], list), (
            "rollup response must carry a 'datasets' list. "
            "spec: API.md §Metadata Generation — GET /spoke/metagen/dataset"
        )
        by_urn = {r["dataset_urn"]: r for r in body["datasets"]}
        assert EU_PROFILES_URN in by_urn and ORDERS_EVENTS_URN in by_urn, (
            "Both seeded datasets must appear as rollup rows; got "
            f"{sorted(by_urn)!r}. spec: API.md §Metadata Generation — one row per dataset"
        )

        eu = by_urn[EU_PROFILES_URN]
        # item_count = distinct items (2: dataset.description, column.email.description).
        # spec: feature/BACKEND.md §Per-dataset rollup view — item_count distinct items.
        assert eu["item_count"] == 2, (
            f"eu_profiles item_count must be 2 (distinct items); got {eu['item_count']!r}. "
            "spec: feature/BACKEND.md §Per-dataset rollup view — item_count distinct items"
        )
        # candidate_count counts ALL candidates incl. rejected (4: 2 on each item).
        # spec: API.md §Metadata Generation — candidate-level candidate_count (total).
        assert eu["candidate_count"] == 4, (
            f"eu_profiles candidate_count must be 4 (all candidates); got "
            f"{eu['candidate_count']!r}. spec: API.md §Metadata Generation — candidate_count total"
        )
        assert eu["approved_count"] == 1, (
            f"eu_profiles approved_count must be 1; got {eu['approved_count']!r}. "
            "spec: API.md §Metadata Generation — candidate-level approved_count"
        )
        assert eu["rejected_count"] == 1, (
            f"eu_profiles rejected_count must be 1; got {eu['rejected_count']!r}. "
            "spec: API.md §Metadata Generation — candidate-level rejected_count"
        )
        # Boundary surfaced via LEFT JOIN.
        # spec: API.md §Metadata Generation — row carries is_enabled / allowed boundary.
        assert eu["is_enabled"] is True, (
            f"eu_profiles is_enabled must echo the enabled boundary; got {eu['is_enabled']!r}. "
            "spec: API.md §Metadata Generation — row carries boundary is_enabled"
        )
        assert set(eu["allowed"]) == {"dataset.description", "column.description"}, (
            f"eu_profiles allowed must echo the boundary; got {eu['allowed']!r}. "
            "spec: API.md §Metadata Generation — row carries boundary allowed"
        )
        # last_modified_at = the MAX created_at of the dataset's items.
        # eu_profiles has two items (EU_OLD_ITEM_TS, EU_NEW_ITEM_TS) → the newer wins.
        # spec: feature/BACKEND.md §Per-dataset rollup view (max item created_at).
        assert eu["last_modified_at"] is not None, (
            "eu_profiles last_modified_at must be present when items exist. "
            "spec: API.md §Metadata Generation — row carries last_modified_at"
        )
        eu_lm = datetime.fromisoformat(eu["last_modified_at"])
        assert eu_lm == EU_NEW_ITEM_TS, (
            "eu_profiles last_modified_at must equal the MAX of its items' created_at "
            f"({EU_NEW_ITEM_TS.isoformat()}), not the older item's; "
            f"got {eu['last_modified_at']!r}. spec: feature/BACKEND.md "
            "§Per-dataset rollup view — last_modified_at = max item created_at"
        )

        oe = by_urn[ORDERS_EVENTS_URN]
        # orders.events has NO boundary → is_enabled=false, allowed=[] (LEFT JOIN default).
        # spec: API.md §Metadata Generation — is_enabled=false/allowed=[] when no boundary.
        assert oe["is_enabled"] is False, (
            f"orders.events has no boundary → is_enabled must be false; got {oe['is_enabled']!r}. "
            "spec: API.md §Metadata Generation — is_enabled=false when no boundary"
        )
        assert oe["allowed"] == [], (
            f"orders.events has no boundary → allowed must be []; got {oe['allowed']!r}. "
            "spec: API.md §Metadata Generation — allowed=[] when no boundary"
        )
        assert oe["item_count"] == 1 and oe["candidate_count"] == 1, (
            f"orders.events must show item_count=1 candidate_count=1; got "
            f"item_count={oe['item_count']!r} candidate_count={oe['candidate_count']!r}. "
            "spec: feature/BACKEND.md §Per-dataset rollup view"
        )
        assert oe["rejected_count"] == 1 and oe["approved_count"] == 0, (
            f"orders.events must show rejected_count=1 approved_count=0; got "
            f"rejected_count={oe['rejected_count']!r} approved_count={oe['approved_count']!r}. "
            "spec: API.md §Metadata Generation — candidate-level counts"
        )
        # orders.events has a single item → last_modified_at is exactly its created_at.
        # spec: feature/BACKEND.md §Per-dataset rollup view (max item created_at).
        assert oe["last_modified_at"] is not None, (
            "orders.events last_modified_at must be present when an item exists. "
            "spec: API.md §Metadata Generation — row carries last_modified_at"
        )
        oe_lm = datetime.fromisoformat(oe["last_modified_at"])
        assert oe_lm == OE_ITEM_TS, (
            "orders.events last_modified_at must equal its single item's created_at "
            f"({OE_ITEM_TS.isoformat()}); got {oe['last_modified_at']!r}. "
            "spec: feature/BACKEND.md §Per-dataset rollup view (max item created_at)"
        )

        # ── 1b. Default sort is last_modified_at_desc; ?sort=..._asc reverses ──
        # eu_profiles (max item created_at = base+2h) is newer than orders.events
        # (base); under the default desc sort eu_profiles precedes orders.events.
        # spec: API.md §Metadata Generation — default sort last_modified_at_desc;
        #   sortable by last_modified_at (last_modified_at_asc reverses).
        # Restrict to the two seeded URNs so unrelated rows don't perturb ordering.
        seeded = {EU_PROFILES_URN, ORDERS_EVENTS_URN}
        default_order = [
            r["dataset_urn"]
            for r in body["datasets"]
            if r["dataset_urn"] in seeded
        ]
        assert default_order == [EU_PROFILES_URN, ORDERS_EVENTS_URN], (
            "Default rollup order must be last_modified_at-descending: eu_profiles "
            f"(newer) before orders.events (older); got {default_order!r}. "
            "spec: API.md §Metadata Generation — default sort last_modified_at_desc"
        )

        asc_resp = await api_client.get(
            f"{dataset_url}?sort=last_modified_at_asc&limit=100", headers=admin_headers
        )
        assert asc_resp.status_code == 200, (
            f"GET rollup ?sort=last_modified_at_asc failed: "
            f"{asc_resp.status_code} {asc_resp.text}. "
            "spec: API.md §Metadata Generation — sortable by last_modified_at"
        )
        asc_order = [
            r["dataset_urn"]
            for r in asc_resp.json()["datasets"]
            if r["dataset_urn"] in seeded
        ]
        assert asc_order == [ORDERS_EVENTS_URN, EU_PROFILES_URN], (
            "?sort=last_modified_at_asc must reverse the default: orders.events "
            f"(older) before eu_profiles (newer); got {asc_order!r}. "
            "spec: API.md §Metadata Generation — last_modified_at_asc"
        )

        # ── 2. dataset_urn is a substring filter ──────────────────────────────
        # spec: API.md §Metadata Generation — filterable by dataset_urn text.
        text_resp = await api_client.get(
            f"{dataset_url}?dataset_urn=eu_profiles&limit=100", headers=admin_headers
        )
        assert text_resp.status_code == 200, (
            f"GET rollup with dataset_urn filter failed: {text_resp.status_code} {text_resp.text}"
        )
        text_urns = {r["dataset_urn"] for r in text_resp.json()["datasets"]}
        assert EU_PROFILES_URN in text_urns, (
            "dataset_urn substring 'eu_profiles' must match the eu_profiles row. "
            "spec: API.md §Metadata Generation — dataset_urn text filter"
        )
        assert ORDERS_EVENTS_URN not in text_urns, (
            "dataset_urn substring 'eu_profiles' must NOT match orders.events. "
            "spec: API.md §Metadata Generation — dataset_urn text filter"
        )

        # ── 3. conf_id scopes membership AND counts ───────────────────────────
        # conf EU has candidates only on eu_profiles, so orders.events drops out;
        # counts scope to conf EU's candidates only.
        #   eu_profiles under conf EU: dataset.description (approved + rejected) +
        #   column.email.description (1 EU llm_approved) = 3 EU candidates over 2 items;
        #   the RIVAL email candidate is excluded from the count.
        # spec: API.md §Metadata Generation — conf_id restricts rows + scopes counts.
        eu_scoped_resp = await api_client.get(
            f"{dataset_url}?conf_id={conf_eu_id}&limit=100", headers=admin_headers
        )
        assert eu_scoped_resp.status_code == 200, (
            f"GET rollup conf_id={conf_eu_id} failed: "
            f"{eu_scoped_resp.status_code} {eu_scoped_resp.text}"
        )
        eu_scoped = {r["dataset_urn"]: r for r in eu_scoped_resp.json()["datasets"]}
        assert ORDERS_EVENTS_URN not in eu_scoped, (
            "conf EU holds no candidate on orders.events → it must be excluded under "
            f"conf_id=conf_eu. got {sorted(eu_scoped)!r}. "
            "spec: API.md §Metadata Generation — conf_id restricts rows to that conf's datasets"
        )
        assert EU_PROFILES_URN in eu_scoped, (
            "eu_profiles holds conf EU candidates → it must appear under conf_id=conf_eu. "
            "spec: API.md §Metadata Generation — conf_id row membership"
        )
        eu_s = eu_scoped[EU_PROFILES_URN]
        assert eu_s["candidate_count"] == 3, (
            f"Under conf_id=conf_eu, eu_profiles candidate_count must scope to conf EU's 3 "
            f"candidates (RIVAL's email candidate excluded); got {eu_s['candidate_count']!r}. "
            "spec: API.md §Metadata Generation — conf_id scopes counts"
        )
        assert eu_s["approved_count"] == 1 and eu_s["rejected_count"] == 1, (
            f"Under conf_id=conf_eu, eu_profiles approved_count/rejected_count must be 1/1; "
            f"got {eu_s['approved_count']!r}/{eu_s['rejected_count']!r}. "
            "spec: API.md §Metadata Generation — conf_id scopes candidate-level counts"
        )

        # conf RIVAL has candidates on BOTH datasets → both rows present; the
        # orders.events count scopes to RIVAL's single rejected candidate.
        rival_scoped_resp = await api_client.get(
            f"{dataset_url}?conf_id={conf_rival_id}&limit=100", headers=admin_headers
        )
        assert rival_scoped_resp.status_code == 200
        rival_scoped = {r["dataset_urn"]: r for r in rival_scoped_resp.json()["datasets"]}
        assert EU_PROFILES_URN in rival_scoped and ORDERS_EVENTS_URN in rival_scoped, (
            "conf RIVAL holds candidates on both datasets → both rows must appear under "
            f"conf_id=conf_rival. got {sorted(rival_scoped)!r}. "
            "spec: API.md §Metadata Generation — conf_id row membership"
        )
        # eu_profiles under conf RIVAL: only the single RIVAL email llm_approved candidate.
        assert rival_scoped[EU_PROFILES_URN]["candidate_count"] == 1, (
            "Under conf_id=conf_rival, eu_profiles candidate_count must scope to RIVAL's 1 "
            f"candidate; got {rival_scoped[EU_PROFILES_URN]['candidate_count']!r}. "
            "spec: API.md §Metadata Generation — conf_id scopes counts"
        )
        assert rival_scoped[ORDERS_EVENTS_URN]["rejected_count"] == 1, (
            "Under conf_id=conf_rival, orders.events rejected_count must be 1; got "
            f"{rival_scoped[ORDERS_EVENTS_URN]['rejected_count']!r}. "
            "spec: API.md §Metadata Generation — conf_id scopes counts"
        )

        # ── 4. Malformed conf_id → 404 metagen_conf ──────────────────────────
        # spec: feature/BACKEND.md §Per-dataset rollup view — conf_id validated UUID,
        #   404 metagen_conf when absent (mirrors list_items).
        bad_resp = await api_client.get(
            f"{dataset_url}?conf_id=not-a-uuid", headers=admin_headers
        )
        assert bad_resp.status_code == 404, (
            f"Malformed conf_id must return 404; got {bad_resp.status_code} {bad_resp.text}. "
            "spec: feature/BACKEND.md §Per-dataset rollup view — conf_id 404 on bad/absent"
        )

    finally:
        with suppress(Exception):
            await delete_metagen_state_for_urn(async_session, EU_PROFILES_URN)
        with suppress(Exception):
            await delete_metagen_state_for_urn(async_session, ORDERS_EVENTS_URN)
        for cid in (conf_eu_id, conf_rival_id):
            if cid is not None:
                with suppress(Exception):
                    await delete_metagen_conf(async_session, cid)


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
    Spec: BACKEND.md §Metadata Generation Service
    Spec: USE_CASE_en.md §UC4 — real-LLM contract: candidates_added ≥ 1.
    """
    if runtime_conf.get("stub_llm_client"):
        pytest.skip(
            "stub_llm_client=true; PATCH /admin/conf {stub_llm_client:false} to run real-LLM"
        )

    conf_url = "/api/v1/spoke/metagen/conf"
    eu_boundary_url = f"/api/v1/spoke/common/data/{_EU_ENCODED}/attr/metagen/boundary"
    oe_boundary_url = f"/api/v1/spoke/common/data/{_OE_ENCODED}/attr/metagen/boundary"
    global_event_url = "/api/v1/spoke/metagen/event"

    # Conf id created in step 3; used for the per-conf run route + cleanup.
    conf_id: str | None = None

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
        eu_original_dataset_description = (
            eu_props_snapshot.description if eu_props_snapshot else None
        )
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

        # ── Step 3: POST a tag-scoped metagen conf (collection) ───────────────
        # The real-LLM mirror uses a single tag-scoped conf covering both
        # fulfillment datasets; the cross-conf exclusivity concern is exercised by
        # the stub test. spec: API.md §Metadata Generation — POST /metagen/conf → 201.
        post_conf_resp = await api_client.post(
            conf_url,
            headers=admin_headers,
            json={
                "name": f"uc4-real-{uuid.uuid4().hex[:8]}",
                "is_enabled": True,
                "schedule_tier": "daily",
                "dataset_filter": {"origin": "DEV", "tags": [FULFILLMENT_TAG]},
                "result_limit": 3,
                "overwrite_pending": True,
            },
        )
        assert post_conf_resp.status_code == 201, (
            f"POST metagen conf failed: "
            f"{post_conf_resp.status_code} {post_conf_resp.text}. "
            "spec: API.md §Metadata Generation — POST /metagen/conf → 201"
        )
        conf_body = post_conf_resp.json()
        conf_id = conf_body["id"]
        assert conf_body["is_enabled"] is True
        assert conf_body["schedule_tier"] == "daily"
        assert conf_body["result_limit"] == 3
        assert conf_body["overwrite_pending"] is True
        assert conf_body["dataset_filter"] == {"origin": "DEV", "tags": [FULFILLMENT_TAG]}

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
            "spec: USE_CASE_en.md §UC4 — API Mapping"
        )
        eu_boundary_body = put_eu_boundary_resp.json()
        assert eu_boundary_body["dataset_urn"] == EU_PROFILES_URN, (
            f"boundary dataset_urn not echoed: {eu_boundary_body.get('dataset_urn')!r}. "
            "spec: USE_CASE_en.md §UC4 — API Mapping"
        )
        assert set(eu_boundary_body["allowed"]) == {"dataset.description", "column.description"}, (
            f"eu_profiles boundary allowed not preserved: {eu_boundary_body.get('allowed')!r}. "
            "spec: USE_CASE_en.md §UC4 — API Mapping"
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
            "spec: USE_CASE_en.md §UC4 — API Mapping"
        )
        oe_boundary_body = put_oe_boundary_resp.json()
        assert oe_boundary_body["dataset_urn"] == ORDERS_EVENTS_URN, (
            f"orders.events boundary dataset_urn not echoed: "
            f"{oe_boundary_body.get('dataset_urn')!r}. spec: USE_CASE_en.md §UC4 — API Mapping"
        )
        assert "column.description" in oe_boundary_body["allowed"], (
            "orders.events boundary allowed must include 'column.description'. "
            "spec: USE_CASE_en.md §UC4 — API Mapping"
        )

        # ── Step 5: POST per-conf run (first run) ─────────────────────────────
        run_resp = await api_client.post(
            f"{conf_url}/{conf_id}/method/run", headers=admin_headers, timeout=90.0
        )
        assert run_resp.status_code == 200, (
            f"POST per-conf run (first run) failed: "
            f"{run_resp.status_code} {run_resp.text}. "
            "spec: API.md §Metadata Generation — per-conf run"
        )
        run_body = run_resp.json()
        first_run_id = run_body["run_id"]

        uuid.UUID(run_body["run_id"])
        assert run_body.get("status") == "success", (
            f"MetagenRunResponse status expected 'success'; got {run_body.get('status')!r}. "
            "spec: BACKEND.md §Event Catalogue"
        )
        assert run_body.get("dry_run") is False, (
            f"MetagenRunResponse dry_run must be False; got {run_body.get('dry_run')!r}. "
            "spec: BACKEND.md §Event Catalogue"
        )
        assert isinstance(run_body.get("unresolved_urns"), list)
        assert run_body["unresolved_urns"] == [], (
            f"Expected no unresolved URNs; got {run_body['unresolved_urns']!r}. "
            "spec: BACKEND.md §Event Catalogue"
        )
        first_counts = run_body.get("counts", {})
        assert isinstance(first_counts, dict)
        assert "items_considered" in first_counts
        assert (
            isinstance(first_counts["items_considered"], int)
            and first_counts["items_considered"] >= 1
        ), (
            f"items_considered must be int ≥ 1; got {first_counts.get('items_considered')!r}. "
            "spec: BACKEND.md §Event Catalogue"
        )
        assert "candidates_added" in first_counts
        assert run_body.get("debate_outcome") in ("accept", "turns_exhausted", "cycle_detected"), (
            f"debate_outcome={run_body.get('debate_outcome')!r} not in canonical set. "
            "spec: BACKEND.md §Event Catalogue"
        )
        assert (
            isinstance(run_body.get("producer_iterations"), int)
            and run_body["producer_iterations"] >= 1
        )
        assert run_body["conf_id"] == conf_id, (
            "Real-LLM run response must carry the conf_id it ran. "
            "spec: BACKEND.md §Event Catalogue — RUN_COMPLETE detail conf_id"
        )

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
            "spec: BACKEND.md §Event Catalogue"
        )
        rc_detail = run_complete_event["detail"]
        assert "run_id" in rc_detail
        assert "unresolved_urns" in rc_detail and isinstance(rc_detail["unresolved_urns"], list)
        assert "counts" in rc_detail and isinstance(rc_detail["counts"], dict)
        assert rc_detail.get("dry_run") is False
        assert rc_detail.get("debate_outcome") in ("accept", "turns_exhausted", "cycle_detected"), (
            f"event detail debate_outcome={rc_detail.get('debate_outcome')!r} not canonical. "
            "spec: BACKEND.md §Event Catalogue"
        )
        assert (
            isinstance(rc_detail.get("producer_iterations"), int)
            and rc_detail["producer_iterations"] >= 1
        )

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
                    "spec: USE_CASE_en.md §UC4 — API Mapping"
                )
                assert item["status"] in ("pending", "llm_approved", "approved"), (
                    f"item status {item['status']!r} not in valid set. "
                    "spec: BACKEND.md §Item status — pending / llm_approved / approved"
                )
                assert item["dataset_urn"] == urn
                assert "composite_id" in item
                assert item["composite_id"] == f"{item['dataset_urn']}::{item['item_id']}", (
                    "composite_id format mismatch. spec: USE_CASE_en.md §UC4 — API Mapping"
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
            cands = detail_resp.json().get("candidates", [])
            return next((c for c in cands if c["status"] == "llm_approved"), None)

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
                "spec: USE_CASE_en.md §UC4 — Review"
            )
            assert review_resp.json().get("status") == "approved", (
                "Candidate status after approve must be 'approved'. "
                "spec: USE_CASE_en.md §UC4 — Review"
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
                "spec: USE_CASE_en.md §UC4 — Review"
            )
            assert reject_resp.json().get("status") == "rejected", (
                "Candidate status after reject must be 'rejected'. "
                "spec: USE_CASE_en.md §UC4 — Review"
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
                    "spec: USE_CASE_en.md §UC4 — Review"
                )
                assert oe_review_resp.json().get("status") == "approved", (
                    "Candidate status after approve must be 'approved'. "
                    "spec: USE_CASE_en.md §UC4 — Review"
                )

        # ── Step 9: DataHub round-trip verify ─────────────────────────────────
        if approved_eu_desc_value is not None:
            editable_props = graph.get_aspect(
                entity_urn=EU_PROFILES_URN, aspect_type=EditableDatasetPropertiesClass
            )
            assert editable_props is not None, (
                "editableDatasetProperties is None after approving dataset.description. "
                "spec: USE_CASE_en.md §UC4 — Review"
            )
            assert editable_props.description == approved_eu_desc_value, (
                f"editableDatasetProperties.description={editable_props.description!r} "
                f"!= approved value={approved_eu_desc_value!r}. "
                "spec: USE_CASE_en.md §UC4 — Review"
            )

        if approved_oe_item_id is not None and approved_oe_value is not None:
            approved_field_path = masked_oe_field_paths[0]
            editable_schema = graph.get_aspect(
                entity_urn=ORDERS_EVENTS_URN, aspect_type=EditableSchemaMetadataClass
            )
            assert editable_schema is not None, (
                "editableSchemaMetadata is None after approving column description. "
                "spec: USE_CASE_en.md §UC4 — Review"
            )
            field_infos = editable_schema.editableSchemaFieldInfo or []
            matched_fi = next(
                (fi for fi in field_infos if fi.fieldPath == approved_field_path), None
            )
            assert matched_fi is not None, (
                f"editableSchemaMetadata has no entry for fieldPath={approved_field_path!r}. "
                "spec: USE_CASE_en.md §UC4 — Review"
            )
            assert matched_fi.description == approved_oe_value, (
                f"editableSchemaMetadata[{approved_field_path!r}].description="
                f"{matched_fi.description!r} != approved value={approved_oe_value!r}. "
                "spec: USE_CASE_en.md §UC4 — Review"
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
                "spec: BACKEND.md §Event Catalogue"
            )
            ev_detail = approve_event["detail"]
            assert "item_id" in ev_detail, (
                "METAGEN.CANDIDATE_APPROVE detail missing 'item_id'. "
                "spec: BACKEND.md §Event Catalogue"
            )
            assert "candidate_id" in ev_detail, (
                "METAGEN.CANDIDATE_APPROVE detail missing 'candidate_id'. "
                "spec: BACKEND.md §Event Catalogue"
            )
            assert "reason" in ev_detail, (
                "METAGEN.CANDIDATE_APPROVE detail missing 'reason'. "
                "spec: BACKEND.md §Event Catalogue"
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
                "spec: BACKEND.md §Event Catalogue"
            )
            rj_detail = reject_event["detail"]
            assert "item_id" in rj_detail
            assert "candidate_id" in rj_detail
            assert "reason" in rj_detail

        # ── Step 11: POST per-conf run (second run) — idempotency ─────────────
        run2_resp = await api_client.post(
            f"{conf_url}/{conf_id}/method/run", headers=admin_headers, timeout=90.0
        )
        assert run2_resp.status_code == 200, (
            f"POST per-conf run (second run) failed: "
            f"{run2_resp.status_code} {run2_resp.text}. "
            "spec: USE_CASE_en.md §UC4 — API Mapping"
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
                "spec: BACKEND.md §Generation Pipeline — target enumeration skips approved items"
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
                "spec: BACKEND.md §Approval flow — partial unique index: one approved per item"
            )
            assert len(candidates2) == 1, (
                f"Approved item should have only the approved candidate after second run; "
                f"got {len(candidates2)} candidates with statuses "
                f"{[c['status'] for c in candidates2]}. "
                "spec: BACKEND.md §Generation Pipeline — target enumeration skips approved items"
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
                "spec: BACKEND.md §Generation Pipeline — rejected candidates cleared at run start"
            )
            new_llm_approved_email = [c for c in email_candidates if c["status"] == "llm_approved"]
            assert len(new_llm_approved_email) >= 1, (
                "Second run must produce a new llm_approved candidate for rejected item. "
                "spec: BACKEND.md §Generation Pipeline — rejected items re-generated on next run"
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
            "spec: BACKEND.md §Event Catalogue"
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
        if conf_id is not None:
            with suppress(Exception):
                await api_client.delete(f"{conf_url}/{conf_id}", headers=admin_headers)

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
