"""Spot tests for Ontology Generation — run-method and list endpoints.

Concerns covered (6 test functions):

Run-method:
  test_ontogen_run_dry_run
  test_ontogen_run_dry_run_includes_seeded_documents_in_evidence
  test_ontogen_run_is_enabled_false_non_dry_run_returns_409_ONTOGEN_DISABLED

List endpoints:
  test_ontogen_list_nodes_envelope
  test_ontogen_list_edges_envelope
  test_ontogen_list_triples_envelope

NOTE: node_embeddings sync is verified at the DAG/run-level integration test, not on
per-node REST review approval.  spec/feature/BACKEND_SCHEMA.md §node_embeddings lists
DAG and on-demand inference runs as the ONLY sync triggers — the review endpoint is
not a trigger.

NOTE: AGE graph materialisation is verified at the run-level integration test.
spec/feature/BACKEND.md L397-398 mandates AGE persistence as part of the inference
pipeline (step 9); the triple-approve REST endpoint best-effort materialises, which is
not spec-mandated per BACKEND.md L411-414.

Spec traceability:
- spec/feature/BACKEND.md §Ontology Generation Service §Inference Pipeline
- spec/feature/BACKEND.md L661 — RUN_COMPLETE emitted for dry-run and non-dry-run
- spec/USE_CASE_en.md L541 — ONTOGEN_DISABLED on non-dry run with is_enabled=False
- spec/USE_CASE_en.md §UC3 §Inputs — document evidence read path
- spec/DATAHUB_INTEGRATION.md §Document Aspects — relatedAssets discovery filter
"""

import re
import uuid

import httpx
import pytest

# Declare DataHub fixture dependency so module_dummy_data ingests catalog.title_master
# into DataHub before tests that seed NATIVE documents (evidence path tests).
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})

# UUID4 regex: version nibble = 4, variant nibble = 8-b
# spec: BACKEND_LLM.md §Observability — run_id is uuid4 from service.run()
_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


@pytest.mark.asyncio
async def test_ontogen_run_dry_run(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST /spoke/ontogen/method/run?dry_run=true returns OntogenRunSummary body
    and emits exactly one ONTOGEN.RUN_COMPLETE event with dry_run=true in detail.

    Spec: spec/feature/BACKEND.md §Ontology Generation Service §Inference Pipeline
    — ?dry_run=true evaluates steps 2–8 without persisting; returns OntogenRunSummary
    with status (str), dry_run (bool), unresolved_urns (list), counts (dict).
    Spec: spec/feature/BACKEND.md L661 — RUN_COMPLETE recorded for both dry-run and
    non-dry-run; dry_run flag in detail.
    """
    event_url = "/api/v1/spoke/ontogen/event"

    # Snapshot count of existing ONTOGEN.RUN_COMPLETE events before the POST
    pre_resp = await api_client.get(
        f"{event_url}?limit=100",
        headers=admin_headers,
    )
    assert pre_resp.status_code == 200, pre_resp.text
    pre_events = pre_resp.json()["events"]
    pre_count = sum(1 for e in pre_events if e["event_type"] == "ONTOGEN.RUN_COMPLETE")

    resp = await api_client.post(
        "/api/v1/spoke/ontogen/method/run?dry_run=true",
        headers=admin_headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    # Assert all OntogenRunSummary keys are present with correct types
    assert "status" in body and isinstance(body["status"], str)
    assert "dry_run" in body and isinstance(body["dry_run"], bool)
    assert "unresolved_urns" in body and isinstance(body["unresolved_urns"], list)
    assert "counts" in body and isinstance(body["counts"], dict)
    assert body["dry_run"] is True

    # Assert exactly one new ONTOGEN.RUN_COMPLETE event was emitted
    # spec: BACKEND.md L661 — RUN_COMPLETE recorded for dry-run; dry_run flag in detail
    post_resp = await api_client.get(
        f"{event_url}?limit=100",
        headers=admin_headers,
    )
    assert post_resp.status_code == 200, post_resp.text
    post_events = post_resp.json()["events"]
    run_complete_events = [e for e in post_events if e["event_type"] == "ONTOGEN.RUN_COMPLETE"]
    post_count = len(run_complete_events)

    assert post_count == pre_count + 1, (
        f"Expected exactly one new ONTOGEN.RUN_COMPLETE event after dry-run; "
        f"pre_count={pre_count}, post_count={post_count}. "
        "spec: BACKEND.md L661 — dry-run must emit RUN_COMPLETE"
    )

    # The newest event is first (ordered by occurred_at desc)
    new_event = run_complete_events[0]
    assert new_event["detail"].get("dry_run") is True, (
        f"ONTOGEN.RUN_COMPLETE event detail must carry dry_run=true; "
        f"got detail={new_event['detail']!r}. "
        "spec: BACKEND.md L661 — dry_run flag in detail"
    )
    # counts in event detail must match the response body's counts field exactly
    # spec: BACKEND.md L661 — RUN_COMPLETE payload carries counts
    assert new_event["detail"].get("counts") == body["counts"], (
        f"Event detail counts={new_event['detail'].get('counts')!r} must match "
        f"response counts={body['counts']!r}. "
        "spec: BACKEND.md L661 — event and response must agree on counts"
    )
    # unresolved_urns in event detail must match the response body's unresolved_urns
    # spec: BACKEND.md L661 — unresolved_urns is part of the RUN_COMPLETE payload
    assert new_event["detail"].get("unresolved_urns") == body["unresolved_urns"], (
        f"Event detail unresolved_urns={new_event['detail'].get('unresolved_urns')!r} must "
        f"match response unresolved_urns={body['unresolved_urns']!r}. "
        "spec: BACKEND.md L661 — event and response must agree on unresolved_urns"
    )
    # run_id (uuid4) must be present in detail and match the UUID4 pattern
    # spec: BACKEND_LLM.md §Observability — run_id generated in service.run(),
    # threaded through debate, recorded in ONTOGEN_RUN_COMPLETE detail.
    run_id = new_event["detail"].get("run_id")
    assert isinstance(run_id, str) and _UUID4_RE.match(run_id), (
        f"detail['run_id'] must match UUID4 pattern; got {run_id!r}. "
        "spec: BACKEND_LLM.md §Observability — run_id is uuid4 from service.run()"
    )
    # Regression: producer_iterations / producer_errors_dropped must remain in detail
    # after run_id was added (run_id must not displace prior telemetry fields).
    # spec: BACKEND_LLM.md §Inference Loop
    assert "producer_iterations" in new_event["detail"], (
        f"ONTOGEN.RUN_COMPLETE detail must contain 'producer_iterations'; "
        f"got keys {list(new_event['detail'].keys())!r}. "
        "Regression: BACKEND_LLM.md §Inference Loop"
    )
    assert "producer_errors_dropped" in new_event["detail"], (
        f"ONTOGEN.RUN_COMPLETE detail must contain 'producer_errors_dropped'; "
        f"got keys {list(new_event['detail'].keys())!r}. "
        "Regression: BACKEND_LLM.md §Inference Loop"
    )


@pytest.mark.asyncio
async def test_ontogen_run_dry_run_includes_seeded_documents_in_evidence(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """A dry-run inference reads documents whose relatedAssets reference an in-scope dataset.

    Spec: USE_CASE_en.md §UC3 §Inputs — 'documentInfo.contents.text on document entities
    whose relatedAssets reference an in-scope dataset (Markdown body by convention)'
    Spec: DATAHUB_INTEGRATION.md §Document Aspects — Discovery via searchAcrossEntities
    filtering on relatedAssets; cap at DOCUMENT_EVIDENCE_CAP_PER_DATASET.

    Steps:
      1. Seed two NATIVE documents whose relatedAssets include a known dev-env dataset URN.
      2. PUT ontogen conf with dataset_filter narrowed to that dataset URN.
      3. POST ?dry_run=true — assert the run completes without error (200, dry_run=True).
      4. Assert our dataset URN does NOT appear in unresolved_urns (evidence-gathering
         succeeded for that dataset, i.e. _fetch_documents_for_dataset did not raise).
      5. Cleanup: hard-delete both documents, restore conf to disabled.

    The stub LLM (stub_llm_client=true) returns no nodes/edges/triples, so we cannot
    assert ontology output.  The absence of our dataset URN from unresolved_urns is the
    proxy proof that the evidence path reached the document-fetch step without error.
    """
    from tests.integration.util.datahub import (
        get_datahub_token,
        hard_delete_document,
        seed_native_document,
    )

    # ── Dataset URN — catalog.title_master is the canonical UC3 test dataset ──
    # spec: TESTING.md §Imazon Dummy-Data Reference — catalog.title_master is UC1, UC3
    dataset_urn = (
        "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
    )

    suffix = uuid.uuid4().hex[:12]
    doc1_id = f"spot-doc1-{suffix}"
    doc2_id = f"spot-doc2-{suffix}"
    doc1_urn: str | None = None
    doc2_urn: str | None = None

    conf_url = "/api/v1/spoke/ontogen/attr/conf"

    try:
        # ── Step 1: Seed two NATIVE documents whose relatedAssets include dataset_urn ──
        # spec: DATAHUB_INTEGRATION.md §Document Aspects — NATIVE source, relatedAssets shape
        token = get_datahub_token()
        doc1_urn = seed_native_document(
            document_id=doc1_id,
            title="Spot test doc 1 — catalog context",
            body_markdown="# Catalog Context\n\nDataspoke spot test seed document 1.",
            related_dataset_urns=[dataset_urn],
            token=token,
        )
        doc2_urn = seed_native_document(
            document_id=doc2_id,
            title="Spot test doc 2 — title master notes",
            body_markdown="# Title Master Notes\n\nDataspoke spot test seed document 2.",
            related_dataset_urns=[dataset_urn],
            token=token,
        )

        # ── Step 2: PUT ontogen conf narrowed to our dataset URN ─────────────────
        # spec: USE_CASE_en.md §UC3 L392-L398 — dataset_filter.dataset_urns narrows scope
        conf_resp = await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "is_enabled": True,
                "schedule_tier": "daily",
                "dataset_filter": {"dataset_urns": [dataset_urn]},
            },
        )
        assert conf_resp.status_code in (200, 201), (
            f"PUT ontogen conf failed: {conf_resp.status_code} {conf_resp.text}"
        )

        # ── Step 3: POST dry-run — must complete without error ─────────────────────
        # spec: USE_CASE_en.md §UC3 L415-L416 — dry_run=true evaluates without persisting
        # spec: DATAHUB_INTEGRATION.md §Document Aspects — relatedAssets discovery path
        dry_run_resp = await api_client.post(
            "/api/v1/spoke/ontogen/method/run?dry_run=true",
            headers=admin_headers,
        )
        assert dry_run_resp.status_code == 200, (
            f"POST dry-run failed: {dry_run_resp.status_code} {dry_run_resp.text}. "
            "spec: USE_CASE_en.md §UC3 §Run semantics — dry-run must succeed when document "
            "evidence is present (regression guard for _fetch_documents_for_dataset)"
        )
        body = dry_run_resp.json()

        # ── Step 4: Assert OntogenRunSummary shape and evidence success ───────────
        # spec: USE_CASE_en.md §UC3 — OntogenRunSummary: status, dry_run, unresolved_urns, counts
        assert "status" in body and isinstance(body["status"], str), (
            "OntogenRunSummary missing 'status'. spec: USE_CASE_en.md §UC3 §Run semantics"
        )
        assert body.get("dry_run") is True, (
            "OntogenRunSummary dry_run must be True. spec: USE_CASE_en.md §UC3 §Run semantics"
        )
        assert isinstance(body.get("unresolved_urns"), list), (
            "OntogenRunSummary missing 'unresolved_urns'. spec: USE_CASE_en.md §UC3"
        )
        assert isinstance(body.get("counts"), dict), (
            "OntogenRunSummary missing 'counts'. spec: USE_CASE_en.md §UC3"
        )
        # The dataset_filter pins to our dataset_urn; if evidence-gathering succeeded it
        # must NOT appear in unresolved_urns (which lists URNs that were skipped).
        # spec: USE_CASE_en.md §UC3 L396 — "entries that don't resolve … are skipped and
        # reported in the run-complete event's unresolved_urns field"
        assert dataset_urn not in body["unresolved_urns"], (
            f"dataset_urn {dataset_urn!r} found in unresolved_urns — evidence-gathering failed. "
            "spec: USE_CASE_en.md §UC3 §dataset_filter — seeded documents with matching "
            "relatedAssets must not cause the dataset to be unresolvable. "
            "Regression: _fetch_documents_for_dataset may have raised or returned wrong filter."
        )

    finally:
        # ── Step 5: Cleanup — hard-delete documents, restore conf ─────────────────
        if doc1_urn is not None:
            try:
                hard_delete_document(document_urn=doc1_urn, token=token)
            except Exception:
                pass
        if doc2_urn is not None:
            try:
                hard_delete_document(document_urn=doc2_urn, token=token)
            except Exception:
                pass
        await api_client.patch(conf_url, headers=admin_headers, json={"is_enabled": False})


@pytest.mark.asyncio
async def test_ontogen_run_is_enabled_false_non_dry_run_returns_409_ONTOGEN_DISABLED(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """POST /method/run (no dry_run) with is_enabled=False returns 409 ONTOGEN_DISABLED;
    dry-run with the same conf returns 200; no ONTOGEN.RUN_COMPLETE event on rejected run.

    spec: USE_CASE_en.md L541 — 'When is_enabled=false, non-dry-run calls to
    method/run return 409 ONTOGEN_DISABLED. Dry-run (?dry_run=true) is always
    permitted regardless of is_enabled.'
    spec: BACKEND.md L661 — RUN_COMPLETE is emitted only when the run completes;
    a rejected (409) call must not emit it.
    """
    conf_url = "/api/v1/spoke/ontogen/attr/conf"
    event_url = "/api/v1/spoke/ontogen/event"

    try:
        put_resp = await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "is_enabled": False,
                "schedule_tier": "daily",
                "dataset_filter": {},
            },
        )
        assert put_resp.status_code in (200, 201), (
            f"PUT ontogen conf failed: {put_resp.status_code} {put_resp.text}"
        )

        # Snapshot ONTOGEN.RUN_COMPLETE count before the rejected call
        pre_resp = await api_client.get(f"{event_url}?limit=100", headers=admin_headers)
        assert pre_resp.status_code == 200, pre_resp.text
        pre_run_complete_count = sum(
            1 for e in pre_resp.json()["events"]
            if e["event_type"] == "ONTOGEN.RUN_COMPLETE"
        )

        run_resp = await api_client.post(
            "/api/v1/spoke/ontogen/method/run",
            headers=admin_headers,
        )
        assert run_resp.status_code == 409, (
            f"Expected 409 ONTOGEN_DISABLED when is_enabled=False; "
            f"got {run_resp.status_code}: {run_resp.text}. "
            "spec: USE_CASE_en.md L541"
        )
        body = run_resp.json()
        assert body.get("error_code") == "ONTOGEN_DISABLED", (
            f"Expected error_code 'ONTOGEN_DISABLED'; got: {body}. "
            "spec: USE_CASE_en.md L541"
        )

        # Negative-parity: no ONTOGEN.RUN_COMPLETE event must have been emitted
        # spec: BACKEND.md L661 — event is for completed runs only; rejected calls are not runs
        post_resp = await api_client.get(f"{event_url}?limit=100", headers=admin_headers)
        assert post_resp.status_code == 200, post_resp.text
        post_run_complete_count = sum(
            1 for e in post_resp.json()["events"]
            if e["event_type"] == "ONTOGEN.RUN_COMPLETE"
        )
        assert post_run_complete_count == pre_run_complete_count, (
            f"No new ONTOGEN.RUN_COMPLETE event must be emitted after a 409-rejected run; "
            f"pre={pre_run_complete_count}, post={post_run_complete_count}. "
            "spec: BACKEND.md L661"
        )

        # Dry-run must still succeed when is_enabled=False — disabled gate is
        # scoped to non-dry-run only. spec: USE_CASE_en.md L541
        dry_resp = await api_client.post(
            "/api/v1/spoke/ontogen/method/run?dry_run=true",
            headers=admin_headers,
        )
        assert dry_resp.status_code == 200, (
            f"Dry-run must succeed even when is_enabled=False; "
            f"got {dry_resp.status_code}: {dry_resp.text}. "
            "spec: USE_CASE_en.md L541"
        )
    finally:
        await api_client.patch(conf_url, headers=admin_headers, json={"is_enabled": False})


@pytest.mark.asyncio
async def test_ontogen_list_nodes_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/ontogen/result/node returns paginated node list."""
    resp = await api_client.get(
        "/api/v1/spoke/ontogen/result/node?offset=0&limit=10",
        headers=admin_headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "nodes" in body
    assert "offset" in body
    assert "limit" in body
    assert "total_count" in body
    assert isinstance(body["nodes"], list)


@pytest.mark.asyncio
async def test_ontogen_list_edges_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/ontogen/result/edge returns paginated edge list."""
    resp = await api_client.get(
        "/api/v1/spoke/ontogen/result/edge?offset=0&limit=10",
        headers=admin_headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "edges" in body
    assert "offset" in body
    assert "limit" in body
    assert "total_count" in body
    assert isinstance(body["edges"], list)


@pytest.mark.asyncio
async def test_ontogen_list_triples_envelope(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """GET /spoke/ontogen/result/triple returns paginated triple list."""
    resp = await api_client.get(
        "/api/v1/spoke/ontogen/result/triple?offset=0&limit=10",
        headers=admin_headers,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert "triples" in body
    assert "offset" in body
    assert "limit" in body
    assert "total_count" in body
    assert isinstance(body["triples"], list)


@pytest.mark.asyncio
async def test_ontogen_dry_run_with_origin_filter_does_not_raise(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Dry-run with dataset_filter={"origin": "DEV"} completes without error.

    When no DataHub datasets match the origin, the resolver returns an empty scope
    cleanly (swallow_enumerate_errors=True for UC3). The run must succeed with
    empty scope, not raise a 500.

    spec: spec/feature/BACKEND.md §UC3 dataset_filter — resolver returns empty scope
          cleanly when no datasets match the origin filter.
    spec: USE_CASE_en.md §UC3 §Run semantics — dry_run=true with empty scope completes.
    """
    conf_url = "/api/v1/spoke/ontogen/attr/conf"

    try:
        put_resp = await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "is_enabled": True,
                "schedule_tier": "daily",
                "dataset_filter": {"origin": "DEV"},
            },
        )
        assert put_resp.status_code in (200, 201), (
            f"PUT conf with origin filter failed: {put_resp.status_code} {put_resp.text}"
        )

        run_resp = await api_client.post(
            "/api/v1/spoke/ontogen/method/run?dry_run=true",
            headers=admin_headers,
        )
        assert run_resp.status_code == 200, (
            f"Dry-run with origin=DEV filter must return 200; "
            f"got {run_resp.status_code}: {run_resp.text}. "
            "spec: BACKEND.md §UC3 dataset_filter — empty scope must not raise"
        )
        body = run_resp.json()
        assert "status" in body and isinstance(body["status"], str), (
            "Run response must have status field. spec: USE_CASE_en.md §UC3"
        )
        assert "unresolved_urns" in body and isinstance(body["unresolved_urns"], list), (
            "Run response must have unresolved_urns list. spec: USE_CASE_en.md §UC3"
        )

    finally:
        from contextlib import suppress
        with suppress(Exception):
            await api_client.patch(
                conf_url,
                headers=admin_headers,
                json={"is_enabled": False, "dataset_filter": {}},
            )
