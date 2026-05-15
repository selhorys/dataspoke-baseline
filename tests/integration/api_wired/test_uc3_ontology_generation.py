"""UC3 — Ontology Generation: end-to-end through public REST API.

Maps `spec/USE_CASE_en.md §UC3` paragraphs to executable steps. REST-only
per `spec/TESTING.md §Api-Wired Integration Tests`. This module covers the
UC3 narrative arc and the debate-framework smoke; single-concern coverage
(dependency-gate, disabled-gate, reject verdict, document evidence ingestion)
lives in `tests/integration/spot/test_ontogen.py`.

Tests in this module:
  - test_uc3_run_and_list: Conf PUT, seed POST, dry-run inference, list node/edge/triple
    envelopes, seed DELETE.
  - test_uc3_debate_smoke_under_stub: POST real run under stub mode, assert the debate
    code path runs without error (returns 200, correct OntogenRunSummary shape). Stub
    Producer returns empty payload so no rows are persisted; per-row evidence assertions
    fire only when rows happen to be present. Full per-row transcript assertions live in
    test_uc3_debate_real_when_test_llm_real.
  - test_uc3_debate_real_when_test_llm_real: Skipped when DATASPOKE_TEST_LLM_REAL=false.
    When true, fires a real LLM run and asserts debate transcript content.
"""
# spec: USE_CASE_en.md §UC3

import httpx
import pytest

from src.shared.settings import settings

# Declare fixture dependencies so module_dummy_data seeds all schemas + topics for
# cross-dataset ontology inference. spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset(
    {"catalog", "customers", "reviews", "orders", "shipping"}
)
DUMMY_DATA_DATAHUB_TOPICS: frozenset[str] = frozenset(
    {"imazon.orders.events", "imazon.shipping.updates"}
)


# ── Tests ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_uc3_run_and_list(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """UC3 narrative: 'DataSpoke autonomously infer the business concepts, the
    relationship types, and the specific facts that connect them across datasets,
    so that I can navigate datasets by concept.'

    Steps mirror USE_CASE_en.md §UC3:
      1. PUT singleton ontogen conf (is_enabled, schedule_tier, dataset_filter, seed params)
      2. POST a Markdown seed to steer inference
      3. POST dry-run — returns OntogenRunSummary (status, dry_run=true, unresolved_urns, counts)
      4. GET result/node, result/edge, result/triple — assert paginated envelopes
      5. DELETE the seed
      6. Cleanup — PATCH conf to disabled
    """
    conf_url = "/api/v1/spoke/common/ontogen/attr/conf"
    seed_url = "/api/v1/spoke/common/ontogen/attr/seed"

    seed_id: str | None = None

    try:
        # ── Step 1: PUT ontogen conf ──────────────────────────────────────────
        # UC3 narrative: "The governance team enables ontology generation."
        # spec: USE_CASE_en.md §UC3 L385-L398
        put_conf_resp = await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "is_enabled": True,
                "schedule_tier": "daily",
                "dataset_filter": {"tags": ["urn:li:tag:area:catalog"]},
            },
        )
        # spec: USE_CASE_en.md §UC3 L309-L317 — PUT conf returns 200 or 201
        assert put_conf_resp.status_code in (200, 201), (
            f"PUT ontogen conf failed: {put_conf_resp.status_code} {put_conf_resp.text}"
        )
        conf_body = put_conf_resp.json()
        assert conf_body["is_enabled"] is True
        assert conf_body["schedule_tier"] == "daily"
        # spec: USE_CASE_en.md §UC3 L309-L317 — round-trip must preserve dataset_filter
        assert conf_body["dataset_filter"] == {"tags": ["urn:li:tag:area:catalog"]}, (
            f"dataset_filter not preserved: {conf_body.get('dataset_filter')!r}. "
            "spec: USE_CASE_en.md §UC3 L309-L317"
        )
        # ── Step 2: POST a Markdown seed ──────────────────────────────────────
        # UC3 narrative: "They post a domain seed (Markdown) to steer the LLM toward
        # bookstore-friendly names."
        # spec: USE_CASE_en.md §UC3 L400-L409
        seed_md = (
            "# Imazon Bookstore Domain\n\n"
            "Imazon is an online retailer specialising in books. The storefront sells "
            "individual titles, identified by ISBN-13, in multiple physical and digital "
            "formats — Hardcover, Paperback, eBook, and Audiobook. Each title carries "
            "marketing copy, cover artwork, and a genre code from a two-level taxonomy "
            "(for example `FIC-THR` for Fiction → Thriller).\n\n"
            "Customers place *orders* that bundle one or more *order lines*; each line "
            "is a single edition at a single quantity. Treat *order* as the header concept "
            "and *order line* as the per-book row — never confuse the two, and prefer "
            "those business-friendly names over the underlying table names.\n\n"
            "Customers may submit ratings and reviews tied to a specific edition, and we "
            "track whether the rating came from a verified purchase. Editorial metadata "
            "(blurbs, cover sourcing, genre decisions) is curated by the catalog team and "
            "lives separately from operational sales data. Prefer business-domain language "
            "over warehouse schema names whenever both are available."
        )
        create_seed_resp = await api_client.post(
            seed_url,
            headers={**admin_headers, "content-type": "text/markdown"},
            content=seed_md.encode(),
        )
        assert create_seed_resp.status_code == 201, (
            f"POST seed failed: {create_seed_resp.status_code} {create_seed_resp.text}"
        )
        seed_id = create_seed_resp.json()["seed_id"]
        assert seed_id, "server must assign a seed_id"

        # List seeds — our seed_id must appear with preview and updated_at
        # spec: USE_CASE_en.md §UC3 L362 — seed list returns [{seed_id, preview, updated_at}]
        list_seed_resp = await api_client.get(seed_url, headers=admin_headers)
        assert list_seed_resp.status_code == 200
        seeds_by_id = {s["seed_id"]: s for s in list_seed_resp.json()["seeds"]}
        assert seed_id in seeds_by_id, f"seed_id {seed_id!r} not found in seed list after POST"
        seed_entry = seeds_by_id[seed_id]
        assert "preview" in seed_entry, (
            "seed list entry missing 'preview'. spec: USE_CASE_en.md §UC3 L362"
        )
        assert "updated_at" in seed_entry, (
            "seed list entry missing 'updated_at'. spec: USE_CASE_en.md §UC3 L362"
        )

        # ── Step 3: POST dry-run ──────────────────────────────────────────────
        # UC3 narrative: "?dry_run=true evaluates the inference and returns the
        # would-be node / edge / triple set without persisting changes."
        # spec: USE_CASE_en.md §UC3 L328-L330
        dry_run_resp = await api_client.post(
            "/api/v1/spoke/common/ontogen/method/run?dry_run=true",
            headers=admin_headers,
        )
        assert dry_run_resp.status_code == 200, (
            f"POST dry-run failed: {dry_run_resp.status_code} {dry_run_resp.text}"
        )
        dry_body = dry_run_resp.json()
        # spec: USE_CASE_en.md §UC3 — OntogenRunSummary shape
        assert "status" in dry_body and isinstance(dry_body["status"], str)
        assert "dry_run" in dry_body and isinstance(dry_body["dry_run"], bool)
        assert dry_body["dry_run"] is True
        assert "unresolved_urns" in dry_body and isinstance(dry_body["unresolved_urns"], list)
        assert "counts" in dry_body and isinstance(dry_body["counts"], dict)

        # ── Step 4: List envelopes for node, edge, triple ────────────────────
        # UC3 narrative: "Three nodes, two edges, two triples — all llm_pending or llm_approved."
        # spec: USE_CASE_en.md §UC3 L421-L437
        for result_type, list_key in [
            ("node", "nodes"),
            ("edge", "edges"),
            ("triple", "triples"),
        ]:
            list_resp = await api_client.get(
                f"/api/v1/spoke/common/ontogen/result/{result_type}?offset=0&limit=10",
                headers=admin_headers,
            )
            assert list_resp.status_code == 200, (
                f"GET result/{result_type} failed: {list_resp.status_code}"
            )
            list_body = list_resp.json()
            # spec: API.md §Standard Envelope
            assert list_key in list_body
            assert "offset" in list_body
            assert "limit" in list_body
            assert "total_count" in list_body
            assert isinstance(list_body[list_key], list)
            # spec: API.md §Standard Envelope — offset and limit echo the request params
            assert list_body["offset"] == 0, (
                f"GET result/{result_type} offset expected 0; got {list_body['offset']!r}"
            )
            assert list_body["limit"] == 10, (
                f"GET result/{result_type} limit expected 10; got {list_body['limit']!r}"
            )
            assert isinstance(list_body["total_count"], int) and list_body["total_count"] >= 0, (
                f"GET result/{result_type} total_count must be non-negative int; "
                f"got {list_body['total_count']!r}"
            )

    finally:
        # ── Step 5: DELETE the seed ───────────────────────────────────────────
        if seed_id is not None:
            await api_client.delete(f"{seed_url}/{seed_id}", headers=admin_headers)
        # ── Step 6: Patch conf back to disabled ──────────────────────────────
        await api_client.patch(conf_url, headers=admin_headers, json={"is_enabled": False})


# ── Adversarial debate transcript tests ───────────────────────────────────────


@pytest.mark.asyncio
async def test_uc3_debate_smoke_under_stub(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """Debate code path runs without error under stub mode; OntogenRunSummary shape is correct.

    Under stub mode (DATASPOKE_TEST_MODE=true, DATASPOKE_TEST_LLM_REAL=false):
    - Stub Producer returns an empty payload (nodes/edges/triples=[]).
    - Stub Reviewer returns overall_verdict='accept' on turn 1.
    - Debate terminates with outcome='accept' and turns_completed=2.
    - No rows are persisted because the stub Producer emits an empty payload.

    This test verifies the debate code path is wired correctly and returns 200 with the
    correct OntogenRunSummary shape. Per-row evidence.debate assertions serve as
    defense-in-depth — they fire correctly when rows happen to be present, but the stub
    Producer's empty payload prevents new row persistence under default test mode.

    NOTE: Per-row transcript content (evidence.debate keys and values for real LLM output)
    is asserted in test_uc3_debate_real_when_test_llm_real, not here, because the stub
    Producer's empty payload prevents new row persistence under default test mode.

    Steps mirror USE_CASE_en.md §UC3 §Debate transcript and stay structurally
    symmetric with test_uc3_debate_real_when_test_llm_real:
      1. PUT conf (is_enabled=True, dataset_filter)
      2. POST seed (Markdown)
      3. POST run (no dry_run) — assert OntogenRunSummary status/dry_run/counts/unresolved_urns
      4. GET event/ontogen — assert ONTOGEN.RUN_COMPLETE detail (debate_outcome,
         producer_iterations, producer_errors_dropped)
      5. GET result/{node,edge,triple} — for each persisted row, verify evidence.debate
         shape (zero rows is expected under stub; per-row block is a no-op then)
      6. Cleanup: DELETE seed, PATCH conf disabled

    Spec: BACKEND_LLM.md §Adversarial Debate Framework §Evidence shape
    — debate transcript stored in evidence JSONB with keys:
      turns_completed, outcome, final_reviewer_verdict, rag_anchors, history.
    Spec: BACKEND_LLM.md §Test Mode — stub Reviewer accepts on turn 1; stub Producer
    returns empty output so no rows are persisted under default test mode.
    """
    conf_url = "/api/v1/spoke/common/ontogen/attr/conf"
    seed_url = "/api/v1/spoke/common/ontogen/attr/seed"
    seed_id: str | None = None

    try:
        # ── Step 1: PUT conf ──────────────────────────────────────────────────
        # spec: USE_CASE_en.md §UC3 L385-L398
        put_conf_resp = await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "is_enabled": True,
                "schedule_tier": "daily",
                "dataset_filter": {"tags": ["urn:li:tag:area:catalog"]},
            },
        )
        assert put_conf_resp.status_code in (200, 201), (
            f"PUT conf failed: {put_conf_resp.status_code} {put_conf_resp.text}"
        )

        # ── Step 2: POST seed ─────────────────────────────────────────────────
        # spec: USE_CASE_en.md §UC3 L400-L409
        seed_md = (
            "# Imazon Bookstore Domain\n\n"
            "Imazon sells books. Key concepts: Title, Edition, Order, Customer."
        )
        create_seed_resp = await api_client.post(
            seed_url,
            headers={**admin_headers, "content-type": "text/markdown"},
            content=seed_md.encode(),
        )
        assert create_seed_resp.status_code == 201, (
            f"POST seed failed: {create_seed_resp.status_code} {create_seed_resp.text}"
        )
        seed_id = create_seed_resp.json()["seed_id"]

        # ── Step 3: POST real run ─────────────────────────────────────────────
        # spec: USE_CASE_en.md §UC3 — non-dry-run persists rows
        # spec: BACKEND_LLM.md §Adversarial Debate Framework — debate runs unconditionally
        run_resp = await api_client.post(
            "/api/v1/spoke/common/ontogen/method/run",
            headers=admin_headers,
        )
        assert run_resp.status_code == 200, (
            f"POST run failed: {run_resp.status_code} {run_resp.text}. "
            "spec: USE_CASE_en.md §UC3 — method/run with is_enabled=True returns 200"
        )
        run_body = run_resp.json()

        # OntogenRunSummary shape must be present regardless of LLM mode
        # spec: USE_CASE_en.md §UC3 — OntogenRunSummary: status, dry_run, counts, unresolved_urns
        assert run_body.get("status") == "success", (
            f"OntogenRunSummary status must be 'success' on healthy run; "
            f"got {run_body.get('status')!r}. spec: USE_CASE_en.md §UC3"
        )
        actual_dry_run = run_body.get("dry_run")
        assert actual_dry_run is False, (
            f"OntogenRunSummary dry_run must be False on real run; got {actual_dry_run!r}. "
            "spec: USE_CASE_en.md §UC3"
        )
        assert isinstance(run_body.get("unresolved_urns"), list), (
            f"OntogenRunSummary unresolved_urns must be a list; "
            f"got {type(run_body.get('unresolved_urns')).__name__}. spec: USE_CASE_en.md §UC3"
        )
        counts = run_body.get("counts")
        assert isinstance(counts, dict), (
            "OntogenRunSummary missing 'counts' (dict). spec: USE_CASE_en.md §UC3"
        )
        for key in ("nodes_added", "edges_added", "triples_added"):
            assert key in counts and isinstance(counts[key], int) and counts[key] >= 0, (
                f"OntogenRunSummary counts.{key} must be non-negative int; "
                f"got {counts.get(key)!r}. spec: USE_CASE_en.md §UC3"
            )

        # ── Step 4: GET /event/ontogen — ONTOGEN.RUN_COMPLETE detail must carry debate fields ──
        # spec: BACKEND_LLM.md §Adversarial Debate Framework §Wiring — _run_inner emits
        # debate_outcome, producer_iterations, producer_errors_dropped in event detail
        event_resp = await api_client.get(
            "/api/v1/spoke/common/ontogen/event?limit=20",
            headers=admin_headers,
        )
        assert event_resp.status_code == 200, (
            f"GET /event/ontogen failed: {event_resp.status_code}"
        )
        events = event_resp.json().get("events", [])
        run_complete = next(
            (e for e in events if e["event_type"] == "ONTOGEN.RUN_COMPLETE"), None
        )
        assert run_complete is not None, (
            "No ONTOGEN.RUN_COMPLETE event found after method/run. "
            "spec: BACKEND_LLM.md §Wiring — RUN_COMPLETE must follow run_debate"
        )
        detail = run_complete["detail"]
        # spec: §Termination — outcome ∈ {accept, turns_exhausted, cycle_detected}
        outcome = detail.get("debate_outcome")
        assert outcome in ("accept", "turns_exhausted", "cycle_detected"), (
            f"event detail debate_outcome={outcome!r} not in canonical set. "
            "spec: BACKEND_LLM.md §Adversarial Debate Framework §Termination"
        )
        # spec: §Inference Loop — producer_iterations is 1..max
        prod_iter = detail.get("producer_iterations")
        assert isinstance(prod_iter, int) and prod_iter >= 1, (
            f"event detail producer_iterations must be int ≥ 1; got {prod_iter!r}. "
            "spec: BACKEND_LLM.md §Inference Loop"
        )
        # spec: §Inference Loop — producer_errors_dropped is non-negative row count
        prod_err = detail.get("producer_errors_dropped")
        assert isinstance(prod_err, int) and prod_err >= 0, (
            f"event detail producer_errors_dropped must be int ≥ 0; got {prod_err!r}. "
            "spec: BACKEND_LLM.md §Inference Loop"
        )

        # ── Step 5: GET result/{node,edge,triple} — assert debate shape on any persisted rows ──
        # spec: BACKEND_LLM.md §Evidence shape — debate transcript in evidence JSONB
        # Symmetric with test_uc3_debate_real_when_test_llm_real: iterate all three
        # result types. Under stub mode the Producer returns an empty payload so no rows
        # are persisted; the per-row block accepts zero rows by design. The real-LLM
        # variant additionally asserts any_rows_found == True (impossible under stub).
        for result_type, list_key in [
            ("node", "nodes"),
            ("edge", "edges"),
            ("triple", "triples"),
        ]:
            list_resp = await api_client.get(
                f"/api/v1/spoke/common/ontogen/result/{result_type}?offset=0&limit=10",
                headers=admin_headers,
            )
            assert list_resp.status_code == 200, (
                f"GET result/{result_type} failed: {list_resp.status_code}"
            )
            rows = list_resp.json().get(list_key, [])
            for row in rows:
                attr_resp = await api_client.get(
                    f"/api/v1/spoke/common/ontogen/result/{result_type}/{row['id']}/attr",
                    headers=admin_headers,
                )
                assert attr_resp.status_code == 200, (
                    f"GET result/{result_type}/{row['id']}/attr failed: "
                    f"{attr_resp.status_code}"
                )
                evidence = attr_resp.json().get("evidence") or {}
                debate = evidence.get("debate")
                assert debate is not None, (
                    f"{result_type} {row['id']!r} evidence missing 'debate'. "
                    "spec: BACKEND_LLM.md §Evidence shape"
                )
                # spec: §Evidence shape — required top-level keys
                for key in ("turns_completed", "outcome", "final_reviewer_verdict",
                            "rag_anchors", "history"):
                    assert key in debate, (
                        f"evidence.debate for {result_type} {row['id']!r} missing {key!r}. "
                        "spec: BACKEND_LLM.md §Evidence shape"
                    )
                # spec: §Termination — outcome must be one of the three canonical values
                assert debate["outcome"] in ("accept", "turns_exhausted", "cycle_detected"), (
                    f"{result_type} {row['id']!r} debate.outcome invalid: "
                    f"{debate['outcome']!r}. spec: BACKEND_LLM.md §Termination"
                )
                # spec: §Loop shape — at least 1 Producer + 1 Reviewer turn
                tc = debate["turns_completed"]
                assert isinstance(tc, int) and tc >= 2, (
                    f"{result_type} {row['id']!r} turns_completed must be int ≥ 2; "
                    f"got {tc!r}. spec: BACKEND_LLM.md §Loop shape"
                )
                # spec: §Evidence shape — history non-empty with both actor entries
                history = debate.get("history", [])
                assert isinstance(history, list) and len(history) >= 2, (
                    f"{result_type} {row['id']!r} history must have ≥ 2 entries "
                    f"(at least 1 Producer + 1 Reviewer); got {len(history)}. "
                    "spec: BACKEND_LLM.md §Evidence shape §history"
                )
                actors = {h.get("actor") for h in history}
                assert "producer" in actors, (
                    f"{result_type} {row['id']!r} history must include a producer turn. "
                    "spec: BACKEND_LLM.md §Evidence shape"
                )
                assert "reviewer" in actors, (
                    f"{result_type} {row['id']!r} history must include a reviewer turn. "
                    "spec: BACKEND_LLM.md §Evidence shape"
                )

    finally:
        # ── Step 5: Cleanup ───────────────────────────────────────────────────
        if seed_id is not None:
            await api_client.delete(f"{seed_url}/{seed_id}", headers=admin_headers)
        await api_client.patch(conf_url, headers=admin_headers, json={"is_enabled": False})


@pytest.mark.asyncio
@pytest.mark.skipif(not settings.test_llm_real, reason="requires DATASPOKE_TEST_LLM_REAL=true")
async def test_uc3_debate_real_when_test_llm_real(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """When DATASPOKE_TEST_LLM_REAL=true, a real LLM run fires and transcript is non-trivial.

    Skipped automatically when DATASPOKE_TEST_LLM_REAL=false (default CI behaviour);
    surfaces in pytest --collect-only before test setup begins.

    Steps mirror USE_CASE_en.md §UC3 §Debate transcript (real-LLM variant):
      1. PUT conf (is_enabled=True)
      2. POST seed
      3. POST run (no dry_run)
      4. GET result/{node,edge,triple} — assert for each persisted row:
         - evidence.debate.outcome ∈ {accept, turns_exhausted, cycle_detected}
         - evidence.debate.turns_completed ≥ 2
         - evidence.debate.history is a non-empty list with actor entries
      5. Cleanup

    Spec: BACKEND_LLM.md §Adversarial Debate Framework §Evidence shape
    Spec: BACKEND_LLM.md §Test Mode — DATASPOKE_TEST_LLM_REAL=true bypasses stub;
    real Gemini calls execute for Producer and Reviewer.
    """

    conf_url = "/api/v1/spoke/common/ontogen/attr/conf"
    seed_url = "/api/v1/spoke/common/ontogen/attr/seed"
    seed_id: str | None = None

    try:
        # ── Step 1: PUT conf ──────────────────────────────────────────────────
        put_conf_resp = await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "is_enabled": True,
                "schedule_tier": "daily",
                "dataset_filter": {"tags": ["urn:li:tag:area:catalog"]},
            },
        )
        assert put_conf_resp.status_code in (200, 201), (
            f"PUT conf failed: {put_conf_resp.status_code} {put_conf_resp.text}"
        )

        # ── Step 2: POST seed ─────────────────────────────────────────────────
        seed_md = (
            "# Imazon Bookstore Domain\n\n"
            "Imazon sells books online. Key concepts: Title (keyed by ISBN-13), "
            "Edition (format variant of a Title), Order, OrderLine, Customer, "
            "Rating, CarrierEvent. Prefer business-domain names over table names."
        )
        create_seed_resp = await api_client.post(
            seed_url,
            headers={**admin_headers, "content-type": "text/markdown"},
            content=seed_md.encode(),
        )
        assert create_seed_resp.status_code == 201, (
            f"POST seed failed: {create_seed_resp.status_code} {create_seed_resp.text}"
        )
        seed_id = create_seed_resp.json()["seed_id"]

        # ── Step 3: POST real run ─────────────────────────────────────────────
        run_resp = await api_client.post(
            "/api/v1/spoke/common/ontogen/method/run",
            headers=admin_headers,
        )
        assert run_resp.status_code == 200, (
            f"POST real run failed: {run_resp.status_code} {run_resp.text}. "
            "spec: USE_CASE_en.md §UC3"
        )

        # ── Step 4: Verify debate transcript on every persisted row ───────────
        # spec: BACKEND_LLM.md §Evidence shape — debate key required on each row
        # The list endpoints return summary rows without evidence; fetch each row's
        # /attr endpoint to read the full evidence JSONB including the debate sub-tree.
        any_rows_found = False
        for result_type, list_key in [("node", "nodes"), ("edge", "edges"), ("triple", "triples")]:
            list_resp = await api_client.get(
                f"/api/v1/spoke/common/ontogen/result/{result_type}?offset=0&limit=20",
                headers=admin_headers,
            )
            assert list_resp.status_code == 200, (
                f"GET result/{result_type} failed: {list_resp.status_code}"
            )
            rows = list_resp.json().get(list_key, [])
            for row in rows:
                any_rows_found = True
                attr_resp = await api_client.get(
                    f"/api/v1/spoke/common/ontogen/result/{result_type}/{row['id']}/attr",
                    headers=admin_headers,
                )
                assert attr_resp.status_code == 200, (
                    f"GET result/{result_type}/{row['id']}/attr failed: "
                    f"{attr_resp.status_code}"
                )
                evidence = attr_resp.json().get("evidence") or {}
                debate = evidence.get("debate")
                assert debate is not None, (
                    f"{result_type} {row['id']!r} evidence missing 'debate'. "
                    "spec: BACKEND_LLM.md §Evidence shape"
                )
                # spec: §Termination — outcome is one of the canonical values
                assert debate["outcome"] in ("accept", "turns_exhausted", "cycle_detected"), (
                    f"{result_type} {row['id']!r} debate.outcome invalid: "
                    f"{debate['outcome']!r}. "
                    "spec: BACKEND_LLM.md §Termination"
                )
                # spec: §Loop shape — at least 1 Producer + 1 Reviewer turn
                tc = debate["turns_completed"]
                assert isinstance(tc, int) and tc >= 2, (
                    f"{result_type} {row['id']!r} turns_completed must be ≥ 2; "
                    f"got {tc!r}. spec: BACKEND_LLM.md §Loop shape"
                )
                # spec: §Evidence shape — history non-empty with actor entries
                history = debate.get("history", [])
                assert len(history) >= 2, (
                    f"{result_type} {row['id']!r} history must have ≥ 2 entries "
                    f"(at least 1 Producer + 1 Reviewer); got {len(history)}. "
                    "spec: BACKEND_LLM.md §Evidence shape §history"
                )
                actors = {e.get("actor") for e in history}
                assert "producer" in actors, (
                    f"{result_type} {row.get('id')!r} history must include a producer turn. "
                    "spec: BACKEND_LLM.md §Evidence shape"
                )
                assert "reviewer" in actors, (
                    f"{result_type} {row.get('id')!r} history must include a reviewer turn. "
                    "spec: BACKEND_LLM.md §Evidence shape"
                )

        # Real LLM must produce at least some rows — empty output signals a prompt/filter
        # regression that must surface as FAILED, not SKIPPED.
        # spec: BACKEND_LLM.md §Test Mode — DATASPOKE_TEST_LLM_REAL=true implies real output
        assert any_rows_found, (
            "Real LLM run produced zero rows — verify prompt/filter pipeline. "
            "spec: BACKEND_LLM.md §Test Mode — real LLM must persist ≥1 row"
        )

    finally:
        # ── Step 5: Cleanup ───────────────────────────────────────────────────
        if seed_id is not None:
            await api_client.delete(f"{seed_url}/{seed_id}", headers=admin_headers)
        await api_client.patch(conf_url, headers=admin_headers, json={"is_enabled": False})
