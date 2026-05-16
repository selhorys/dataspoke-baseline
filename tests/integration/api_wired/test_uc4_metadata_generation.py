"""UC4 — Metadata Generation: end-to-end through public REST API.

Maps `spec/USE_CASE_en.md §UC4` (lines 552–776) paragraphs to executable steps.
REST-only per `spec/TESTING.md §Api-Wired Integration Tests`.

This module covers:
  - test_uc4_narrative_run_review_emit_with_datahub_roundtrip:
      Full UC4 narrative arc — global conf PUT, per-dataset boundary PUT,
      method/run, event query, item browse, candidate review (approve), DataHub
      editableDatasetProperties round-trip verify, per-dataset event query.
  - test_uc4_reads_uc3_approved_nodes_via_global_run:
      UC3 → UC4 coupling — seed approved ontogen_nodes + dataset_node_map rows,
      run global metagen, assert RUN_COMPLETE event recorded without error.

Single-concern tests (conf CRUD, boundary CRUD, run-gating, candidate review edge
cases, item filters) live in `tests/integration/spot/test_metagen.py`.

spec: USE_CASE_en.md §UC4 L552–776
spec: BACKEND.md §UC4 Metadata Generation (event catalogue table)
spec: TESTING.md §Api-Wired Integration Tests
"""
# spec: USE_CASE_en.md §UC4

import json
import urllib.parse
import uuid

import httpx
import pytest
from datahub.ingestion.graph.client import DatahubClientConfig, DataHubGraph
from datahub.metadata.schema_classes import EditableDatasetPropertiesClass
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.util.datahub import _gms_url, get_datahub_token

# ── Raw-SQL seeding helpers for metagen tables ─────────────────────────────────
# spec: TESTING.md §Api-Wired Integration Tests — setup/teardown may use raw SQL;
# the test body stays REST-only.

# Declare fixture dependencies so module_dummy_data seeds catalog schema + DataHub.
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})

# UC4 dataset: catalog.title_master — Imazon primary catalog table.
# spec: TESTING.md §Imazon Dummy-Data Reference — catalog.title_master is the UC4 table.
_TEST_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_ENCODED_URN = urllib.parse.quote(_TEST_URN, safe="")

# ── Raw-SQL helpers for DB seeding (setup/teardown only — not in test bodies) ──
# spec: TESTING.md §Api-Wired Integration Tests — "Setup/teardown fixtures may use
# tests.integration.util … the test itself stays REST-only."
#
# metagen_items PK: composite (dataset_urn TEXT, item_id TEXT) — no surrogate UUID.
# metagen_candidates PK: candidate_id UUID (as_uuid=True).
# spec: src/shared/db/models.py — MetagenItem, MetagenCandidate column layout.


async def _seed_approved_ontogen_node(
    session: AsyncSession,
    node_id: str,
    name: str,
) -> None:
    """Insert an approved ontogen_nodes row via raw SQL (UC3 → UC4 coupling setup).

    spec: BACKEND.md §UC4 — UC4 reads UC3-approved nodes via
    dataset_node_map.status='approved'.
    """
    from sqlalchemy import text

    await session.execute(
        text(
            "INSERT INTO dataspoke.ontogen_nodes"
            " (id, name, description, confidence_score, status, evidence)"
            " VALUES (:id, :name, :desc, :conf, 'approved', CAST(:ev AS jsonb))"
        ),
        {
            "id": node_id,
            "name": name,
            "desc": "UC4 coupling test approved node",
            "conf": 0.90,
            "ev": json.dumps({"source": "uc4-coupling-test"}),
        },
    )
    await session.commit()


async def _seed_dataset_node_map(
    session: AsyncSession,
    *,
    dataset_urn: str,
    node_id: str,
    status: str = "approved",
) -> None:
    """Insert a dataset_node_map row via raw SQL (composite PK: dataset_urn, node_id).

    spec: BACKEND.md §UC4 — UC4 reads rows with status='approved'.
    spec: src/shared/db/models.py — DatasetNodeMap schema.
    """
    from sqlalchemy import text

    await session.execute(
        text(
            "INSERT INTO dataspoke.dataset_node_map"
            " (dataset_urn, node_id, confidence_score, status, is_primary)"
            " VALUES (:dataset_urn, :node_id, :conf, :status, false)"
            " ON CONFLICT (dataset_urn, node_id) DO UPDATE SET status = EXCLUDED.status"
        ),
        {
            "dataset_urn": dataset_urn,
            "node_id": node_id,
            "conf": 0.90,
            "status": status,
        },
    )
    await session.commit()


async def _delete_ontogen_node(session: AsyncSession, node_id: str) -> None:
    """Delete an ontogen_nodes row. Removes dataset_node_map rows first (FK)."""
    from sqlalchemy import text

    await session.execute(
        text("DELETE FROM dataspoke.dataset_node_map WHERE node_id = :node_id"),
        {"node_id": node_id},
    )
    await session.execute(
        text("DELETE FROM dataspoke.ontogen_nodes WHERE id = :id"),
        {"id": node_id},
    )
    await session.commit()


async def _seed_llm_approved_candidate(
    session: AsyncSession,
    *,
    dataset_urn: str,
    item_id: str,
    value: str,
    run_id: str,
    confidence: float = 0.92,
) -> str:
    """Insert an llm_approved metagen_candidates row; ensure the parent item exists.

    The parent metagen_items row (dataset_urn, item_id) is inserted with
    kind='dataset.description' if it does not already exist (ON CONFLICT DO NOTHING).
    The new candidate row is inserted with status='llm_approved' and the supplied
    value/confidence.  Returns the new candidate_id as a str (UUID hex).

    spec: src/shared/db/models.py — MetagenItem composite PK (dataset_urn, item_id);
      MetagenCandidate PK candidate_id UUID; FK (dataset_urn, item_id) → metagen_items.
    spec: TESTING.md §Api-Wired Integration Tests — raw SQL allowed in setup/teardown.
    """
    from sqlalchemy import text

    # Ensure parent item row exists (no-op if already created by the run).
    await session.execute(
        text(
            "INSERT INTO dataspoke.metagen_items"
            " (dataset_urn, item_id, kind)"
            " VALUES (:dataset_urn, :item_id, 'dataset.description')"
            " ON CONFLICT (dataset_urn, item_id) DO NOTHING"
        ),
        {"dataset_urn": dataset_urn, "item_id": item_id},
    )
    candidate_id = uuid.uuid4()
    run_uuid = uuid.UUID(run_id)
    await session.execute(
        text(
            "INSERT INTO dataspoke.metagen_candidates"
            " (candidate_id, dataset_urn, item_id, run_id, value,"
            "  confidence_score, status, evidence)"
            " VALUES (:candidate_id, :dataset_urn, :item_id, :run_id, :value,"
            "         :confidence, 'llm_approved', '{}'::jsonb)"
        ),
        {
            "candidate_id": candidate_id,
            "dataset_urn": dataset_urn,
            "item_id": item_id,
            "run_id": run_uuid,
            "value": value,
            "confidence": confidence,
        },
    )
    await session.commit()
    return str(candidate_id)


# ── Tests ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_uc4_narrative_run_review_emit_with_datahub_roundtrip(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """Full UC4 narrative arc: conf → boundary → run → browse → approve → DataHub verify → events.

    Steps mirror USE_CASE_en.md §UC4 Imazon Example (L691–776):
      1. PUT global conf (is_enabled=true, schedule_tier, dataset_filter, result_limit,
         overwrite_pending)
      2. PUT per-dataset boundary (is_enabled=true, allowed)
      3. POST /spoke/common/metagen/method/run
      4. GET /spoke/common/metagen/event — assert METAGEN.RUN_COMPLETE detail shape
      5. GET /spoke/common/data/{urn}/attr/metagen/item — assert ≥1 item, correct envelope
      6. Raw-SQL seed one llm_approved candidate; GET item detail — seeded candidate visible
      7. POST .../candidate/{cid}/method/review {verdict: "approve"} — response status=approved
      8. DataHub round-trip: fetch editableDatasetProperties aspect via DataHubGraph.get_aspect;
         assert description equals approved candidate's value
      9. GET /spoke/common/data/{urn}/event/metagen — assert METAGEN.CANDIDATE_APPROVE detail
     10. Cleanup (finally): delete seeded rows, DELETE boundary, DELETE global conf

    spec: USE_CASE_en.md §UC4 L552–776
    spec: BACKEND.md §UC4 Metadata Generation — METAGEN.RUN_COMPLETE detail keys,
      METAGEN.CANDIDATE_APPROVE detail keys, mutable approval → DataHub emit
    spec: BACKEND.md event catalogue table L766–767
    """
    conf_url = "/api/v1/spoke/common/metagen/attr/conf"
    boundary_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"
    run_url = "/api/v1/spoke/common/metagen/method/run"
    global_event_url = "/api/v1/spoke/common/metagen/event"
    item_list_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/item"
    dataset_event_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/event/metagen"

    # seeded_candidate_id is set during the try block; used in finally for cleanup.
    seeded_candidate_id: str | None = None

    try:
        # ── Step 1: PUT global conf ───────────────────────────────────────────
        # UC4 narrative: "The governance team enables metagen globally."
        # spec: USE_CASE_en.md §UC4 L694–706
        put_conf_resp = await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "is_enabled": True,
                "schedule_tier": "daily",
                "dataset_filter": {"dataset_urns": [_TEST_URN]},
                "result_limit": 3,
                "overwrite_pending": True,
            },
        )
        assert put_conf_resp.status_code in (200, 201), (
            f"PUT global conf failed: {put_conf_resp.status_code} {put_conf_resp.text}"
        )
        conf_body = put_conf_resp.json()
        # spec: USE_CASE_en.md §UC4 — conf round-trip preserves all fields
        assert conf_body["is_enabled"] is True, (
            f"is_enabled not preserved: {conf_body.get('is_enabled')!r}. "
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
        # spec: USE_CASE_en.md §UC4 L604 — dataset_filter round-trip preserves scoped URN list
        assert conf_body["dataset_filter"] == {"dataset_urns": [_TEST_URN]}, (
            f"dataset_filter not preserved: {conf_body.get('dataset_filter')!r}. "
            "spec: USE_CASE_en.md §UC4 L604"
        )

        # ── Step 2: PUT per-dataset boundary ──────────────────────────────────
        # UC4 narrative: "The catalog team opts catalog.title_master in for both kinds."
        # spec: USE_CASE_en.md §UC4 L709–717
        put_boundary_resp = await api_client.put(
            boundary_url,
            headers=admin_headers,
            json={
                "is_enabled": True,
                "allowed": ["dataset.description", "column.description"],
            },
        )
        assert put_boundary_resp.status_code in (200, 201), (
            f"PUT boundary failed: {put_boundary_resp.status_code} {put_boundary_resp.text}"
        )
        boundary_body = put_boundary_resp.json()
        assert boundary_body["is_enabled"] is True, (
            f"boundary is_enabled not preserved: {boundary_body.get('is_enabled')!r}. "
            "spec: USE_CASE_en.md §UC4 L613"
        )
        assert set(boundary_body["allowed"]) == {"dataset.description", "column.description"}, (
            f"boundary allowed not preserved: {boundary_body.get('allowed')!r}. "
            "spec: USE_CASE_en.md §UC4 L614"
        )
        # spec: USE_CASE_en.md §UC4 L613 — boundary response echoes dataset_urn
        assert boundary_body["dataset_urn"] == _TEST_URN, (
            f"boundary dataset_urn not echoed: {boundary_body.get('dataset_urn')!r}. "
            "spec: USE_CASE_en.md §UC4 L613"
        )

        # ── Step 3: POST method/run ───────────────────────────────────────────
        # UC4 narrative: "The daily Airflow DAG fires, or a reviewer triggers an immediate run."
        # spec: USE_CASE_en.md §UC4 L720–723 — POST with no body
        # spec: BACKEND.md §UC4 — MetagenRunResponse shape: run_id, status, dry_run,
        #   unresolved_urns, counts, producer_iterations, debate_outcome
        run_resp = await api_client.post(run_url, headers=admin_headers)
        assert run_resp.status_code == 200, (
            f"POST method/run failed: {run_resp.status_code} {run_resp.text}. "
            "spec: USE_CASE_en.md §UC4 L720 — method/run with is_enabled=True returns 200"
        )
        run_body = run_resp.json()

        # MetagenRunResponse structural assertions — no LLM-output content assertions.
        # spec: BACKEND.md §UC4 — MetagenRunResponse: run_id, status, dry_run,
        #   unresolved_urns, counts, producer_iterations, debate_outcome
        assert "run_id" in run_body, (
            "MetagenRunResponse must carry 'run_id'. spec: BACKEND.md §UC4"
        )
        # spec: USE_CASE_en.md §UC4 L720 — run_id must be a valid UUID
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
        counts = run_body.get("counts")
        assert isinstance(counts, dict), (
            "MetagenRunResponse counts must be a dict. spec: BACKEND.md §UC4"
        )
        assert "items_considered" in counts, (
            "MetagenRunResponse counts must contain 'items_considered'. "
            "spec: BACKEND.md event catalogue L766"
        )
        # dataset_filter scoped to _TEST_URN with allowed=['dataset.description','column.description']:
        # URN resolves to catalog.title_master (17 cols); items_considered ≥ 1 deterministically.
        # Guard unresolved_urns first to rule out URN non-resolution as the cause of a zero count.
        # spec: BACKEND.md §UC4 generation pipeline — items_considered counts (dataset, kind) pairs
        assert run_body["unresolved_urns"] == [], (
            f"Expected no unresolved URNs with _TEST_URN in scope; "
            f"got {run_body['unresolved_urns']!r}. spec: BACKEND.md §UC4"
        )
        assert isinstance(counts["items_considered"], int) and counts["items_considered"] >= 1, (
            f"counts.items_considered must be int ≥ 1 given scoped URN + boundary; "
            f"got {counts.get('items_considered')!r}. spec: BACKEND.md event catalogue L766"
        )
        # candidates_added is present on a non-dry real run
        # spec: BACKEND.md event catalogue L766 — real-run shape: items_considered,
        #   candidates_added, candidates_evicted, rejected_cleared
        assert "candidates_added" in counts, (
            "MetagenRunResponse counts must contain 'candidates_added' on a real run. "
            "spec: BACKEND.md event catalogue L766"
        )
        # spec: BACKEND.md §UC4 — debate_outcome ∈ {accept, turns_exhausted, cycle_detected};
        #   producer_iterations int ≥ 1; both populated when in-scope set is non-empty.
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

        # ── Step 4: GET global event — assert METAGEN.RUN_COMPLETE detail ─────
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
        # spec: API.md §Standard Envelope
        assert "events" in event_body
        assert "offset" in event_body
        assert "limit" in event_body
        assert "total_count" in event_body
        assert isinstance(event_body["events"], list)

        run_complete_event = next(
            (e for e in event_body["events"] if e["event_type"] == "METAGEN.RUN_COMPLETE"),
            None,
        )
        assert run_complete_event is not None, (
            "No METAGEN.RUN_COMPLETE event found after method/run. "
            "spec: BACKEND.md event catalogue L766"
        )
        detail = run_complete_event["detail"]
        assert "run_id" in detail, (
            "METAGEN.RUN_COMPLETE detail missing 'run_id'. spec: BACKEND.md event catalogue L766"
        )
        assert "unresolved_urns" in detail and isinstance(detail["unresolved_urns"], list), (
            "METAGEN.RUN_COMPLETE detail missing or wrong type for 'unresolved_urns'. "
            "spec: BACKEND.md event catalogue L766"
        )
        assert "counts" in detail and isinstance(detail["counts"], dict), (
            "METAGEN.RUN_COMPLETE detail missing or wrong type for 'counts'. "
            "spec: BACKEND.md event catalogue L766"
        )
        assert detail.get("dry_run") is False, (
            f"METAGEN.RUN_COMPLETE detail dry_run expected False; "
            f"got {detail.get('dry_run')!r}. spec: BACKEND.md event catalogue L766"
        )
        # spec: BACKEND.md event catalogue L766 — debate_outcome ∈ {accept, turns_exhausted,
        #   cycle_detected}; producer_iterations int ≥ 1; both populated when in-scope set
        #   is non-empty (dataset_filter scoped to _TEST_URN + boundary allowed set).
        assert detail.get("debate_outcome") in ("accept", "turns_exhausted", "cycle_detected"), (
            f"METAGEN.RUN_COMPLETE detail debate_outcome={detail.get('debate_outcome')!r} not in "
            "canonical set. spec: BACKEND.md event catalogue L766"
        )
        assert (
            isinstance(detail.get("producer_iterations"), int)
            and detail["producer_iterations"] >= 1
        ), (
            f"METAGEN.RUN_COMPLETE detail producer_iterations must be int ≥ 1; "
            f"got {detail.get('producer_iterations')!r}. "
            "spec: BACKEND.md event catalogue L766"
        )

        # ── Step 5: GET per-dataset items ─────────────────────────────────────
        # UC4 narrative: "After the run, the catalog dashboard lists the dataset's items."
        # spec: USE_CASE_en.md §UC4 L725–730
        items_resp = await api_client.get(item_list_url, headers=admin_headers)
        assert items_resp.status_code == 200, (
            f"GET per-dataset items failed: {items_resp.status_code} {items_resp.text}"
        )
        items_body = items_resp.json()
        # spec: API.md §Standard Envelope
        assert "items" in items_body
        assert "offset" in items_body
        assert "limit" in items_body
        assert "total_count" in items_body
        assert isinstance(items_body["items"], list)

        # Under test-mode stub with is_enabled+allowed boundary, candidates_added ≥ 1
        # means at least one item has candidates. Under stub LLM (empty producer), items
        # may exist as pending with zero candidates; assert structural envelope only.
        # spec: BACKEND.md §UC4 — item kind ∈ {dataset.description, column.description}
        for item in items_body["items"]:
            assert item["kind"] in ("dataset.description", "column.description"), (
                f"item kind {item['kind']!r} not in allowed set. "
                "spec: USE_CASE_en.md §UC4 L617"
            )
            assert item["status"] in ("pending", "llm_approved", "approved"), (
                f"item status {item['status']!r} not in valid set. "
                "spec: USE_CASE_en.md §UC4 L622–631"
            )
            assert item["dataset_urn"] == _TEST_URN, (
                f"item dataset_urn {item['dataset_urn']!r} != expected {_TEST_URN!r}"
            )
            assert "composite_id" in item, (
                "item missing 'composite_id'. spec: USE_CASE_en.md §UC4 API Mapping L684"
            )
            # spec: USE_CASE_en.md §UC4 L684 — composite_id = '{dataset_urn}::{item_id}'
            assert item["composite_id"] == f"{item['dataset_urn']}::{item['item_id']}", (
                f"composite_id format mismatch: {item['composite_id']!r} != "
                f"'{item['dataset_urn']}::{item['item_id']}'. "
                "spec: USE_CASE_en.md §UC4 API Mapping L684"
            )

        # ── Step 6: GET item detail for dataset.description ──────────────────
        # UC4 narrative: "Inspecting the dataset description item."
        # spec: USE_CASE_en.md §UC4 L731–751 — item detail with candidate list
        # spec: BACKEND.md §UC4 — candidate status: llm_approved | approved | rejected
        #
        # Raw-SQL seed: under stub mode the LLM producer returns candidates=[], so no
        # llm_approved candidate is persisted by the run. Seed one deterministically so
        # steps 7–9 (approve → DataHub round-trip → CANDIDATE_APPROVE event) always run
        # in CI without depending on LLM output.
        # spec: TESTING.md §Api-Wired Integration Tests — raw SQL allowed in setup/teardown
        dataset_desc_item_id = "dataset.description"
        seeded_value = "Imazon master catalog of all book titles and editions."
        seeded_candidate_id = await _seed_llm_approved_candidate(
            async_session,
            dataset_urn=_TEST_URN,
            item_id=dataset_desc_item_id,
            value=seeded_value,
            run_id=run_body["run_id"],
        )

        item_detail_url = (
            f"/api/v1/spoke/common/data/{_ENCODED_URN}"
            f"/attr/metagen/item/{dataset_desc_item_id}"
        )
        detail_resp = await api_client.get(item_detail_url, headers=admin_headers)
        assert detail_resp.status_code == 200, (
            f"GET item detail failed: {detail_resp.status_code} {detail_resp.text}"
        )
        detail_body = detail_resp.json()
        assert "candidates" in detail_body and isinstance(detail_body["candidates"], list), (
            "item detail missing 'candidates' list. "
            "spec: USE_CASE_en.md §UC4 L617-631 — MetagenItemDetailResponse"
        )
        assert "composite_id" in detail_body, (
            "item detail missing 'composite_id'. spec: USE_CASE_en.md §UC4 API Mapping L684"
        )
        # Locate the seeded llm_approved candidate; it must appear in the detail response.
        seeded_candidate = next(
            (c for c in detail_body["candidates"] if c["candidate_id"] == seeded_candidate_id),
            None,
        )
        assert seeded_candidate is not None, (
            f"Seeded candidate {seeded_candidate_id!r} not found in item detail response. "
            "spec: USE_CASE_en.md §UC4 L731–751 — candidates list includes llm_approved rows"
        )
        assert seeded_candidate["status"] == "llm_approved", (
            f"Seeded candidate status expected 'llm_approved'; "
            f"got {seeded_candidate.get('status')!r}. spec: BACKEND.md §UC4"
        )

        # ── Step 7: Approve the candidate ────────────────────────────────────
        # UC4 narrative: "The reviewer approves c1."
        # spec: USE_CASE_en.md §UC4 L752–760
        review_url = (
            f"/api/v1/spoke/common/data/{_ENCODED_URN}"
            f"/attr/metagen/item/{dataset_desc_item_id}"
            f"/candidate/{seeded_candidate_id}/method/review"
        )
        review_resp = await api_client.post(
            review_url,
            headers=admin_headers,
            json={"verdict": "approve", "reason": "narrative test"},
        )
        assert review_resp.status_code == 200, (
            f"POST candidate review (approve) failed: "
            f"{review_resp.status_code} {review_resp.text}. "
            "spec: USE_CASE_en.md §UC4 L752–760"
        )
        review_body = review_resp.json()
        # spec: BACKEND.md §UC4 — review response is MetagenCandidate with
        #   status=approved after approve verdict
        assert review_body.get("status") == "approved", (
            f"reviewed candidate status expected 'approved'; "
            f"got {review_body.get('status')!r}. spec: USE_CASE_en.md §UC4 L649–657"
        )
        assert review_body.get("candidate_id") == seeded_candidate_id, (
            "reviewed candidate_id mismatch. spec: BACKEND.md §UC4"
        )

        # ── Step 8: DataHub round-trip verify ─────────────────────────────────
        # After approving a dataset.description candidate, DataSpoke emits the value
        # to editableDatasetProperties.description on the dataset.
        # spec: USE_CASE_en.md §UC4 L588 (table) — dataset.description →
        #   editableDatasetProperties.description
        # spec: USE_CASE_en.md §UC4 L762–764 — "DataSpoke writes the value to
        #   editableDatasetProperties.description on the dataset"
        # Uses DataHubGraph.get_aspect (acryl-datahub SDK) via
        # tests/integration/util/datahub.py helpers.
        dh_token = get_datahub_token()
        graph = DataHubGraph(DatahubClientConfig(server=_gms_url, token=dh_token))
        editable_props = graph.get_aspect(
            entity_urn=_TEST_URN,
            aspect_type=EditableDatasetPropertiesClass,
        )
        assert editable_props is not None, (
            "editableDatasetProperties aspect is None after approving dataset.description "
            "candidate — DataSpoke did not emit to DataHub. "
            "spec: USE_CASE_en.md §UC4 L762–764"
        )
        assert editable_props.description == seeded_value, (
            f"editableDatasetProperties.description={editable_props.description!r} "
            f"does not match approved candidate value={seeded_value!r}. "
            "spec: USE_CASE_en.md §UC4 L762–764 — approval emits value to DataHub"
        )

        # ── Step 9: GET per-dataset metagen events ────────────────────────────
        # UC4 narrative: "GET .../event/metagen — METAGEN.CANDIDATE_APPROVE"
        # spec: USE_CASE_en.md §UC4 L769–770
        # spec: BACKEND.md event catalogue L767 — CANDIDATE_APPROVE detail: item_id,
        #   candidate_id, reason
        dataset_event_resp = await api_client.get(
            f"{dataset_event_url}?limit=20",
            headers=admin_headers,
        )
        assert dataset_event_resp.status_code == 200, (
            f"GET per-dataset metagen events failed: "
            f"{dataset_event_resp.status_code} {dataset_event_resp.text}"
        )
        dataset_event_body = dataset_event_resp.json()
        # spec: API.md §Standard Envelope
        assert "events" in dataset_event_body
        assert "offset" in dataset_event_body
        assert "limit" in dataset_event_body
        assert "total_count" in dataset_event_body

        # Approval was performed — CANDIDATE_APPROVE event must be recorded.
        # spec: BACKEND.md event catalogue L767 — METAGEN.CANDIDATE_APPROVE detail keys
        approve_event = next(
            (
                e
                for e in dataset_event_body["events"]
                if e["event_type"] == "METAGEN.CANDIDATE_APPROVE"
            ),
            None,
        )
        assert approve_event is not None, (
            "No METAGEN.CANDIDATE_APPROVE event found after approving candidate. "
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

    finally:
        # ── Step 10: Cleanup ──────────────────────────────────────────────────
        # Deletion order (FK chain): embeddings → candidates → item → boundary → conf.
        # Each step is wrapped independently so a single failure does NOT skip later steps.
        # metagen_candidate_embeddings.candidate_id → metagen_candidates.candidate_id (FK).
        # metagen_candidates.(dataset_urn, item_id) → metagen_items (FK).
        # spec: TESTING.md §Api-Wired Integration Tests — teardown must not leak state.
        from contextlib import suppress

        from sqlalchemy import text as _text

        # 1. Delete embeddings for all candidates on this (dataset_urn, item_id) pair first
        #    (FK: metagen_candidate_embeddings → metagen_candidates).
        with suppress(Exception):
            await async_session.execute(
                _text(
                    "DELETE FROM dataspoke.metagen_candidate_embeddings"
                    " WHERE candidate_id IN ("
                    "   SELECT candidate_id FROM dataspoke.metagen_candidates"
                    "   WHERE dataset_urn = :urn AND item_id = :iid"
                    " )"
                ),
                {"urn": _TEST_URN, "iid": "dataset.description"},
            )
            await async_session.commit()

        # 2. Delete all candidates for this (dataset_urn, item_id) pair — catches any
        #    extra rows inserted by real-LLM mode or sibling code paths, not just the
        #    seeded candidate_id.
        with suppress(Exception):
            await async_session.execute(
                _text(
                    "DELETE FROM dataspoke.metagen_candidates"
                    " WHERE dataset_urn = :urn AND item_id = :iid"
                ),
                {"urn": _TEST_URN, "iid": "dataset.description"},
            )
            await async_session.commit()

        # 3. Delete the item row (no longer FK-blocked; all candidates removed above).
        with suppress(Exception):
            await async_session.execute(
                _text(
                    "DELETE FROM dataspoke.metagen_items"
                    " WHERE dataset_urn = :urn AND item_id = :item_id"
                ),
                {"urn": _TEST_URN, "item_id": "dataset.description"},
            )
            await async_session.commit()

        # 4. Delete per-dataset boundary, then global conf.
        with suppress(Exception):
            await api_client.delete(boundary_url, headers=admin_headers)
        with suppress(Exception):
            await api_client.delete(conf_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_uc4_reads_uc3_approved_nodes_via_global_run(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """UC3 → UC4 coupling: global metagen run completes when approved ontogen nodes exist.

    Setup (raw SQL helpers — preserved from original file against unchanged tables):
      1. Insert an approved ontogen_nodes row.
      2. Insert a dataset_node_map row linking that node to _TEST_URN with status='approved'.
      3. PUT global conf (is_enabled=true, dataset_filter scoped to _TEST_URN).
      4. PUT per-dataset boundary (is_enabled=true, allowed=['dataset.description']).
      5. POST /spoke/common/metagen/method/run (global singleton, no body).
    Assert:
      - HTTP 200 with MetagenRunResponse shape (run_id, status, dry_run, unresolved_urns,
        counts) — proves the dataset_node_map-status join doesn't blow up when approved
        UC3 nodes exist.
      - METAGEN.RUN_COMPLETE event recorded in global event history.
    Cleanup (finally):
      - DELETE boundary, DELETE global conf, delete seeded UC3 rows.

    spec: USE_CASE_en.md §UC4 L565–570 — "UC4 reads UC3-approved ontology nodes …
      filtered to status='approved' via dataset_node_map"
    spec: BACKEND.md §UC4 — dataset_node_map.status join is part of run context gathering
    spec: TESTING.md §Api-Wired Integration Tests — raw SQL allowed in setup/teardown only
    """
    conf_url = "/api/v1/spoke/common/metagen/attr/conf"
    boundary_url = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"
    run_url = "/api/v1/spoke/common/metagen/method/run"
    global_event_url = "/api/v1/spoke/common/metagen/event"

    suffix = uuid.uuid4().hex[:8]
    node_id = f"uc4-coupling-{suffix}"

    try:
        # ── Steps 1–2: Seed approved UC3 node + dataset_node_map row ─────────
        # spec: TESTING.md §Api-Wired Integration Tests — setup may use raw SQL
        await _seed_approved_ontogen_node(
            async_session, node_id, f"Uc4CouplingNode-{suffix}"
        )
        await _seed_dataset_node_map(
            async_session,
            dataset_urn=_TEST_URN,
            node_id=node_id,
            status="approved",
        )

        # ── Step 3: PUT global conf ───────────────────────────────────────────
        # spec: USE_CASE_en.md §UC4 L694–706
        put_conf_resp = await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "is_enabled": True,
                "schedule_tier": "daily",
                "dataset_filter": {"dataset_urns": [_TEST_URN]},
                "result_limit": 3,
                "overwrite_pending": True,
            },
        )
        assert put_conf_resp.status_code in (200, 201), (
            f"PUT global conf failed: {put_conf_resp.status_code} {put_conf_resp.text}"
        )

        # ── Step 4: PUT per-dataset boundary ──────────────────────────────────
        # spec: USE_CASE_en.md §UC4 L609–615 — boundary is_enabled + allowed
        put_boundary_resp = await api_client.put(
            boundary_url,
            headers=admin_headers,
            json={
                "is_enabled": True,
                "allowed": ["dataset.description"],
            },
        )
        assert put_boundary_resp.status_code in (200, 201), (
            f"PUT boundary failed: {put_boundary_resp.status_code} {put_boundary_resp.text}"
        )

        # ── Step 5: POST global method/run ────────────────────────────────────
        # spec: USE_CASE_en.md §UC4 L681 — POST /spoke/common/metagen/method/run
        run_resp = await api_client.post(run_url, headers=admin_headers)
        assert run_resp.status_code == 200, (
            f"POST global method/run failed: {run_resp.status_code} {run_resp.text}. "
            "UC3 → UC4 coupling: dataset_node_map join must not blow up when approved "
            "rows exist. spec: USE_CASE_en.md §UC4 L565–570"
        )
        run_body = run_resp.json()

        # MetagenRunResponse structural assertions — no LLM content assertions.
        # spec: BACKEND.md §UC4 — MetagenRunResponse: run_id, status, dry_run,
        #   unresolved_urns, counts, producer_iterations, debate_outcome
        assert "run_id" in run_body, (
            "MetagenRunResponse must carry 'run_id'. spec: BACKEND.md §UC4"
        )
        # spec: BACKEND.md event catalogue — run_id is uuid4 for both real and dry runs
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
        counts = run_body.get("counts")
        assert isinstance(counts, dict), (
            "MetagenRunResponse counts must be a dict. spec: BACKEND.md §UC4"
        )
        assert "items_considered" in counts, (
            "MetagenRunResponse counts must contain 'items_considered'. "
            "spec: BACKEND.md event catalogue L766"
        )

        # ── Assert: METAGEN.RUN_COMPLETE event recorded ───────────────────────
        # Proves the run completed end-to-end and emitted its event.
        # spec: BACKEND.md event catalogue L766 — RUN_COMPLETE emitted on every run
        event_resp = await api_client.get(
            f"{global_event_url}?limit=20",
            headers=admin_headers,
        )
        assert event_resp.status_code == 200, (
            f"GET global metagen event failed: {event_resp.status_code}"
        )
        events = event_resp.json().get("events", [])
        run_complete_event = next(
            (e for e in events if e["event_type"] == "METAGEN.RUN_COMPLETE"),
            None,
        )
        assert run_complete_event is not None, (
            "No METAGEN.RUN_COMPLETE event found after method/run with UC3 approved nodes. "
            "spec: BACKEND.md event catalogue L766 — RUN_COMPLETE emitted unconditionally"
        )

    finally:
        # ── Cleanup ───────────────────────────────────────────────────────────
        # Each step is wrapped independently so a single failure does NOT skip later steps.
        # spec: TESTING.md §Api-Wired Integration Tests — teardown must not leak state.
        from contextlib import suppress

        with suppress(Exception):
            await api_client.delete(boundary_url, headers=admin_headers)
        with suppress(Exception):
            await api_client.delete(conf_url, headers=admin_headers)
        # _delete_ontogen_node removes dataset_node_map rows first (FK constraint)
        with suppress(Exception):
            await _delete_ontogen_node(async_session, node_id)
