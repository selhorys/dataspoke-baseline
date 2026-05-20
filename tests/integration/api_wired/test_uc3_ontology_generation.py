"""UC3 — Ontology Generation: end-to-end through public REST API.

Two structurally identical tests mirror the UC3 user-story arc under stub mode
and real-LLM mode. The arc: a DG operator enables ontology generation, seeds
domain knowledge, runs real (non-dry-run) inference, inspects the concept graph
and the debate evidence behind each row, then cleans up.

Spec: spec/USE_CASE_en.md §UC3 Ontology Generation
Spec: spec/feature/BACKEND_LLM.md §Adversarial Debate Framework

Function-level branch coverage (dry-run, dependency-gate, disabled-gate, reject
verdict, document-evidence ingestion) lives in tests/integration/spot/ and is
not duplicated here.
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
async def test_uc3_ontology_generation_under_stub(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """UC3 user-story arc under stub mode (DATASPOKE_TEST_MODE=true).

    Steps mirror USE_CASE_en.md §UC3 Imazon Example:
      1. PUT singleton ontogen conf (is_enabled, schedule_tier, dataset_filter)
      2. POST Markdown seed — assert 201, capture seed_id; GET seed list and
         assert entry shows preview and updated_at
      3. POST real (non-dry-run) inference — assert OntogenRunSummary shape
      4. GET /event — find ONTOGEN.RUN_COMPLETE; assert debate fields in detail
      5. GET result/{node,edge,triple} — assert standard envelope shape for each
      6. For each persisted row, GET result/{type}/{id}/attr — assert evidence.debate
         keys (no-op under stub because stub Producer returns empty payload)
      7. Cleanup: DELETE seed, PATCH conf disabled

    Spec: USE_CASE_en.md §UC3
    Spec: BACKEND_LLM.md §Adversarial Debate Framework §Evidence shape
    Spec: BACKEND_LLM.md §Test Mode — stub Producer returns empty payload so no rows
    are persisted; step 6 per-row loop is intentionally a no-op under stub mode.
    """
    conf_url = "/api/v1/spoke/common/ontogen/attr/conf"
    seed_url = "/api/v1/spoke/common/ontogen/attr/seed"
    seed_id: str | None = None

    try:
        # ── Step 1: PUT ontogen conf ──────────────────────────────────────────
        # UC3 narrative: "The governance team enables ontology generation."
        # spec: USE_CASE_en.md §UC3 §Conf
        put_conf_resp = await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "is_enabled": True,
                "schedule_tier": "daily",
                "dataset_filter": {"origin": "DEV", "tags": ["urn:li:tag:area:catalog"]},
            },
        )
        assert put_conf_resp.status_code in (200, 201), (
            f"PUT ontogen conf failed: {put_conf_resp.status_code} {put_conf_resp.text}. "
            "spec: USE_CASE_en.md §UC3 §Conf — PUT returns 200 or 201"
        )
        conf_body = put_conf_resp.json()
        assert conf_body["is_enabled"] is True, (
            "PUT conf response must round-trip is_enabled=True. "
            "spec: USE_CASE_en.md §UC3 §Conf"
        )
        assert conf_body["schedule_tier"] == "daily", (
            f"PUT conf response must round-trip schedule_tier='daily'; "
            f"got {conf_body.get('schedule_tier')!r}. spec: USE_CASE_en.md §UC3 §Conf"
        )
        assert conf_body["dataset_filter"] == {"origin": "DEV", "tags": ["urn:li:tag:area:catalog"]}, (
            f"dataset_filter not preserved: {conf_body.get('dataset_filter')!r}. "
            "spec: USE_CASE_en.md §UC3 §Conf"
        )

        # ── Step 2: POST Markdown seed ────────────────────────────────────────
        # UC3 narrative: "They post a domain seed (Markdown) to steer the LLM
        # toward bookstore-friendly names."
        # spec: USE_CASE_en.md §UC3 §Seeds steer inference
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
            f"POST seed failed: {create_seed_resp.status_code} {create_seed_resp.text}. "
            "spec: USE_CASE_en.md §UC3 §Seeds steer inference — POST returns 201"
        )
        seed_id = create_seed_resp.json()["seed_id"]
        assert seed_id, "server must assign a non-empty seed_id"

        list_seed_resp = await api_client.get(seed_url, headers=admin_headers)
        assert list_seed_resp.status_code == 200
        seeds_by_id = {s["seed_id"]: s for s in list_seed_resp.json()["seeds"]}
        assert seed_id in seeds_by_id, (
            f"seed_id {seed_id!r} not found in seed list after POST"
        )
        seed_entry = seeds_by_id[seed_id]
        assert "preview" in seed_entry, (
            "seed list entry missing 'preview'. "
            "spec: USE_CASE_en.md §UC3 — GET attr/seed returns [{seed_id, preview, updated_at}]"
        )
        assert "updated_at" in seed_entry, (
            "seed list entry missing 'updated_at'. "
            "spec: USE_CASE_en.md §UC3 — GET attr/seed returns [{seed_id, preview, updated_at}]"
        )

        # ── Step 3: POST real (non-dry-run) inference ─────────────────────────
        # spec: USE_CASE_en.md §UC3 §Run semantics — non-dry-run persists rows
        # spec: BACKEND_LLM.md §Adversarial Debate Framework — debate runs unconditionally
        run_resp = await api_client.post(
            "/api/v1/spoke/common/ontogen/method/run",
            headers=admin_headers,
        )
        assert run_resp.status_code == 200, (
            f"POST method/run failed: {run_resp.status_code} {run_resp.text}. "
            "spec: USE_CASE_en.md §UC3 §Run semantics — method/run with is_enabled=True returns 200"
        )
        run_body = run_resp.json()
        status = run_body.get("status")
        assert isinstance(status, str) and status, (
            f"OntogenRunSummary status must be a non-empty string; got {status!r}. "
            "spec: USE_CASE_en.md §UC3 — HTTP 200 already conveys success; "
            "the status field must be a well-formed string"
        )
        assert run_body.get("dry_run") is False, (
            f"OntogenRunSummary dry_run must be False on real run; "
            f"got {run_body.get('dry_run')!r}. spec: USE_CASE_en.md §UC3"
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

        # ── Step 4: GET /event — find ONTOGEN.RUN_COMPLETE ───────────────────
        # spec: BACKEND_LLM.md §Adversarial Debate Framework §Wiring — _run_inner emits
        # debate_outcome, producer_iterations, producer_errors_dropped in event detail
        event_resp = await api_client.get(
            "/api/v1/spoke/common/ontogen/event?limit=20",
            headers=admin_headers,
        )
        assert event_resp.status_code == 200, (
            f"GET /event failed: {event_resp.status_code}"
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
        outcome = detail.get("debate_outcome")
        assert outcome in ("accept", "turns_exhausted", "cycle_detected"), (
            f"event detail debate_outcome={outcome!r} not in canonical set. "
            "spec: BACKEND_LLM.md §Adversarial Debate Framework §Termination"
        )
        prod_iter = detail.get("producer_iterations")
        assert isinstance(prod_iter, int) and prod_iter >= 1, (
            f"event detail producer_iterations must be int ≥ 1; got {prod_iter!r}. "
            "spec: BACKEND_LLM.md §Inference Loop"
        )
        prod_err = detail.get("producer_errors_dropped")
        assert isinstance(prod_err, int) and prod_err >= 0, (
            f"event detail producer_errors_dropped must be int ≥ 0; got {prod_err!r}. "
            "spec: BACKEND_LLM.md §Inference Loop"
        )

        # ── Step 5: GET result/{node,edge,triple} — assert standard envelope ──
        # spec: USE_CASE_en.md §UC3 §API Mapping — list endpoints return paginated envelopes
        # spec: spec/API.md §Standard Envelope
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
            assert list_key in list_body, (
                f"GET result/{result_type} response missing list field '{list_key}'. "
                "spec: API.md §Standard Envelope"
            )
            assert isinstance(list_body[list_key], list), (
                f"GET result/{result_type} '{list_key}' must be a list. "
                "spec: API.md §Standard Envelope"
            )
            assert list_body.get("offset") == 0, (
                f"GET result/{result_type} offset expected 0; got {list_body.get('offset')!r}. "
                "spec: API.md §Standard Envelope"
            )
            assert list_body.get("limit") == 10, (
                f"GET result/{result_type} limit expected 10; got {list_body.get('limit')!r}. "
                "spec: API.md §Standard Envelope"
            )
            total = list_body.get("total_count")
            assert isinstance(total, int) and total >= 0, (
                f"GET result/{result_type} total_count must be non-negative int; "
                f"got {total!r}. spec: API.md §Standard Envelope"
            )
            if total <= 10:
                assert len(list_body[list_key]) == total, (
                    f"GET result/{result_type}: total_count={total} but list has "
                    f"{len(list_body[list_key])} entries — envelope incoherent. "
                    "spec: API.md §Standard Envelope — total_count is the unpaginated row count"
                )

            # ── Step 6: GET result/{type}/{id}/attr — assert evidence.debate ──
            # spec: BACKEND_LLM.md §Evidence shape — debate transcript in evidence JSONB
            # Under stub mode the Producer returns an empty payload so no rows are
            # persisted; the per-row block is intentionally a no-op.
            rows = list_body[list_key]
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
                for key in ("turns_completed", "outcome", "final_reviewer_verdict",
                            "rag_anchors", "history"):
                    assert key in debate, (
                        f"evidence.debate for {result_type} {row['id']!r} missing {key!r}. "
                        "spec: BACKEND_LLM.md §Evidence shape"
                    )
                assert debate["outcome"] in ("accept", "turns_exhausted", "cycle_detected"), (
                    f"{result_type} {row['id']!r} debate.outcome invalid: "
                    f"{debate['outcome']!r}. spec: BACKEND_LLM.md §Termination"
                )
                tc = debate["turns_completed"]
                assert isinstance(tc, int) and tc >= 2, (
                    f"{result_type} {row['id']!r} turns_completed must be int ≥ 2; "
                    f"got {tc!r}. spec: BACKEND_LLM.md §Loop shape"
                )
                history = debate.get("history", [])
                assert isinstance(history, list) and len(history) >= 2, (
                    f"{result_type} {row['id']!r} history must have ≥ 2 entries "
                    f"(at least 1 Producer + 1 Reviewer); got {len(history)}. "
                    "spec: BACKEND_LLM.md §Evidence shape §history"
                )
                assert tc == len(history), (
                    f"{result_type} {row['id']!r} turns_completed={tc} must equal "
                    f"len(history)={len(history)}. "
                    "spec: BACKEND_LLM.md §Evidence shape — each turn appends one history entry"
                )
                for i, entry in enumerate(history):
                    expected_actor = "producer" if i % 2 == 0 else "reviewer"
                    actual_actor = entry.get("actor")
                    assert actual_actor == expected_actor, (
                        f"{result_type} {row['id']!r} history[{i}].actor must be "
                        f"{expected_actor!r} per Producer-then-Reviewer alternation; "
                        f"got {actual_actor!r}. spec: BACKEND_LLM.md §Loop shape"
                    )

    finally:
        # ── Step 7: Cleanup ───────────────────────────────────────────────────
        if seed_id is not None:
            await api_client.delete(f"{seed_url}/{seed_id}", headers=admin_headers)
        await api_client.patch(conf_url, headers=admin_headers, json={"is_enabled": False})


@pytest.mark.asyncio
@pytest.mark.skipif(not settings.test_llm_real, reason="requires DATASPOKE_TEST_LLM_REAL=true")
async def test_uc3_ontology_generation_with_real_llm(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """UC3 user-story arc with a real LLM (DATASPOKE_TEST_LLM_REAL=true).

    Structurally identical to test_uc3_ontology_generation_under_stub. Additional
    assertion after step 5/6: any_rows_found must be True — a real LLM run that
    persists zero rows signals a prompt/filter regression.

    Steps mirror USE_CASE_en.md §UC3 Imazon Example:
      1. PUT singleton ontogen conf (is_enabled, schedule_tier, dataset_filter)
      2. POST Markdown seed — assert 201, capture seed_id; GET seed list and
         assert entry shows preview and updated_at
      3. POST real (non-dry-run) inference — assert OntogenRunSummary shape
      4. GET /event — find ONTOGEN.RUN_COMPLETE; assert debate fields in detail
      5. GET result/{node,edge,triple} — assert standard envelope shape for each
      6. For each persisted row, GET result/{type}/{id}/attr — assert evidence.debate
         keys; track any_rows_found across all three result types
      7. Assert any_rows_found is True
      8. Cleanup: DELETE seed, PATCH conf disabled

    Spec: USE_CASE_en.md §UC3
    Spec: BACKEND_LLM.md §Adversarial Debate Framework §Evidence shape
    Spec: BACKEND_LLM.md §Test Mode — DATASPOKE_TEST_LLM_REAL=true bypasses stub;
    real LLM calls execute for Producer and Reviewer and must persist ≥1 row.
    """
    conf_url = "/api/v1/spoke/common/ontogen/attr/conf"
    seed_url = "/api/v1/spoke/common/ontogen/attr/seed"
    seed_id: str | None = None

    try:
        # ── Step 1: PUT ontogen conf ──────────────────────────────────────────
        # UC3 narrative: "The governance team enables ontology generation."
        # spec: USE_CASE_en.md §UC3 §Conf
        put_conf_resp = await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "is_enabled": True,
                "schedule_tier": "daily",
                "dataset_filter": {"origin": "DEV", "tags": ["urn:li:tag:area:catalog"]},
            },
        )
        assert put_conf_resp.status_code in (200, 201), (
            f"PUT ontogen conf failed: {put_conf_resp.status_code} {put_conf_resp.text}. "
            "spec: USE_CASE_en.md §UC3 §Conf — PUT returns 200 or 201"
        )
        conf_body = put_conf_resp.json()
        assert conf_body["is_enabled"] is True, (
            "PUT conf response must round-trip is_enabled=True. "
            "spec: USE_CASE_en.md §UC3 §Conf"
        )
        assert conf_body["schedule_tier"] == "daily", (
            f"PUT conf response must round-trip schedule_tier='daily'; "
            f"got {conf_body.get('schedule_tier')!r}. spec: USE_CASE_en.md §UC3 §Conf"
        )
        assert conf_body["dataset_filter"] == {"origin": "DEV", "tags": ["urn:li:tag:area:catalog"]}, (
            f"dataset_filter not preserved: {conf_body.get('dataset_filter')!r}. "
            "spec: USE_CASE_en.md §UC3 §Conf"
        )

        # ── Step 2: POST Markdown seed ────────────────────────────────────────
        # UC3 narrative: "They post a domain seed (Markdown) to steer the LLM
        # toward bookstore-friendly names."
        # spec: USE_CASE_en.md §UC3 §Seeds steer inference
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
            f"POST seed failed: {create_seed_resp.status_code} {create_seed_resp.text}. "
            "spec: USE_CASE_en.md §UC3 §Seeds steer inference — POST returns 201"
        )
        seed_id = create_seed_resp.json()["seed_id"]
        assert seed_id, "server must assign a non-empty seed_id"

        list_seed_resp = await api_client.get(seed_url, headers=admin_headers)
        assert list_seed_resp.status_code == 200
        seeds_by_id = {s["seed_id"]: s for s in list_seed_resp.json()["seeds"]}
        assert seed_id in seeds_by_id, (
            f"seed_id {seed_id!r} not found in seed list after POST"
        )
        seed_entry = seeds_by_id[seed_id]
        assert "preview" in seed_entry, (
            "seed list entry missing 'preview'. "
            "spec: USE_CASE_en.md §UC3 — GET attr/seed returns [{seed_id, preview, updated_at}]"
        )
        assert "updated_at" in seed_entry, (
            "seed list entry missing 'updated_at'. "
            "spec: USE_CASE_en.md §UC3 — GET attr/seed returns [{seed_id, preview, updated_at}]"
        )

        # ── Step 3: POST real (non-dry-run) inference ─────────────────────────
        # spec: USE_CASE_en.md §UC3 §Run semantics — non-dry-run persists rows
        # spec: BACKEND_LLM.md §Adversarial Debate Framework — debate runs unconditionally
        run_resp = await api_client.post(
            "/api/v1/spoke/common/ontogen/method/run",
            headers=admin_headers,
        )
        assert run_resp.status_code == 200, (
            f"POST method/run failed: {run_resp.status_code} {run_resp.text}. "
            "spec: USE_CASE_en.md §UC3 §Run semantics — method/run with is_enabled=True returns 200"
        )
        run_body = run_resp.json()
        status = run_body.get("status")
        assert isinstance(status, str) and status, (
            f"OntogenRunSummary status must be a non-empty string; got {status!r}. "
            "spec: USE_CASE_en.md §UC3 — HTTP 200 already conveys success; "
            "the status field must be a well-formed string"
        )
        assert run_body.get("dry_run") is False, (
            f"OntogenRunSummary dry_run must be False on real run; "
            f"got {run_body.get('dry_run')!r}. spec: USE_CASE_en.md §UC3"
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

        # ── Step 4: GET /event — find ONTOGEN.RUN_COMPLETE ───────────────────
        # spec: BACKEND_LLM.md §Adversarial Debate Framework §Wiring — _run_inner emits
        # debate_outcome, producer_iterations, producer_errors_dropped in event detail
        event_resp = await api_client.get(
            "/api/v1/spoke/common/ontogen/event?limit=20",
            headers=admin_headers,
        )
        assert event_resp.status_code == 200, (
            f"GET /event failed: {event_resp.status_code}"
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
        outcome = detail.get("debate_outcome")
        assert outcome in ("accept", "turns_exhausted", "cycle_detected"), (
            f"event detail debate_outcome={outcome!r} not in canonical set. "
            "spec: BACKEND_LLM.md §Adversarial Debate Framework §Termination"
        )
        prod_iter = detail.get("producer_iterations")
        assert isinstance(prod_iter, int) and prod_iter >= 1, (
            f"event detail producer_iterations must be int ≥ 1; got {prod_iter!r}. "
            "spec: BACKEND_LLM.md §Inference Loop"
        )
        prod_err = detail.get("producer_errors_dropped")
        assert isinstance(prod_err, int) and prod_err >= 0, (
            f"event detail producer_errors_dropped must be int ≥ 0; got {prod_err!r}. "
            "spec: BACKEND_LLM.md §Inference Loop"
        )

        # ── Step 5 + 6: GET result/{node,edge,triple} + per-row evidence.debate ─
        # spec: USE_CASE_en.md §UC3 §API Mapping — list endpoints return paginated envelopes
        # spec: BACKEND_LLM.md §Evidence shape — debate transcript in evidence JSONB
        # spec: API.md §Standard Envelope
        any_rows_found = False
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
            assert list_key in list_body, (
                f"GET result/{result_type} response missing list field '{list_key}'. "
                "spec: API.md §Standard Envelope"
            )
            assert isinstance(list_body[list_key], list), (
                f"GET result/{result_type} '{list_key}' must be a list. "
                "spec: API.md §Standard Envelope"
            )
            assert list_body.get("offset") == 0, (
                f"GET result/{result_type} offset expected 0; got {list_body.get('offset')!r}. "
                "spec: API.md §Standard Envelope"
            )
            assert list_body.get("limit") == 10, (
                f"GET result/{result_type} limit expected 10; got {list_body.get('limit')!r}. "
                "spec: API.md §Standard Envelope"
            )
            total = list_body.get("total_count")
            assert isinstance(total, int) and total >= 0, (
                f"GET result/{result_type} total_count must be non-negative int; "
                f"got {total!r}. spec: API.md §Standard Envelope"
            )
            if total <= 10:
                assert len(list_body[list_key]) == total, (
                    f"GET result/{result_type}: total_count={total} but list has "
                    f"{len(list_body[list_key])} entries — envelope incoherent. "
                    "spec: API.md §Standard Envelope — total_count is the unpaginated row count"
                )

            rows = list_body[list_key]
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
                for key in ("turns_completed", "outcome", "final_reviewer_verdict",
                            "rag_anchors", "history"):
                    assert key in debate, (
                        f"evidence.debate for {result_type} {row['id']!r} missing {key!r}. "
                        "spec: BACKEND_LLM.md §Evidence shape"
                    )
                assert debate["outcome"] in ("accept", "turns_exhausted", "cycle_detected"), (
                    f"{result_type} {row['id']!r} debate.outcome invalid: "
                    f"{debate['outcome']!r}. spec: BACKEND_LLM.md §Termination"
                )
                tc = debate["turns_completed"]
                assert isinstance(tc, int) and tc >= 2, (
                    f"{result_type} {row['id']!r} turns_completed must be int ≥ 2; "
                    f"got {tc!r}. spec: BACKEND_LLM.md §Loop shape"
                )
                history = debate.get("history", [])
                assert isinstance(history, list) and len(history) >= 2, (
                    f"{result_type} {row['id']!r} history must have ≥ 2 entries "
                    f"(at least 1 Producer + 1 Reviewer); got {len(history)}. "
                    "spec: BACKEND_LLM.md §Evidence shape §history"
                )
                assert tc == len(history), (
                    f"{result_type} {row['id']!r} turns_completed={tc} must equal "
                    f"len(history)={len(history)}. "
                    "spec: BACKEND_LLM.md §Evidence shape — each turn appends one history entry"
                )
                for i, entry in enumerate(history):
                    expected_actor = "producer" if i % 2 == 0 else "reviewer"
                    actual_actor = entry.get("actor")
                    assert actual_actor == expected_actor, (
                        f"{result_type} {row['id']!r} history[{i}].actor must be "
                        f"{expected_actor!r} per Producer-then-Reviewer alternation; "
                        f"got {actual_actor!r}. spec: BACKEND_LLM.md §Loop shape"
                    )

        # ── Step 7: Assert real LLM produced rows ────────────────────────────
        assert any_rows_found, (
            "Real LLM run produced zero rows — verify prompt/filter pipeline. "
            "spec: BACKEND_LLM.md §Test Mode — real LLM must persist ≥1 row"
        )

    finally:
        # ── Step 8: Cleanup ───────────────────────────────────────────────────
        if seed_id is not None:
            await api_client.delete(f"{seed_url}/{seed_id}", headers=admin_headers)
        await api_client.patch(conf_url, headers=admin_headers, json={"is_enabled": False})
