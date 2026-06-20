"""UC3 — Ontology Generation: end-to-end through public REST API.

Two structurally identical tests mirror the UC3 user-story arc under stub mode
and real-LLM mode. The arc: a DG operator enables ontology generation, seeds
domain knowledge, runs real (non-dry-run) inference, inspects the concept graph,
opens the run's Langfuse session (each row links to its creating run via run_id),
then cleans up.

Spec: spec/USE_CASE_en.md §UC3 Ontology Generation
Spec: spec/feature/BACKEND_LLM.md §Adversarial Debate Framework

Function-level branch coverage (dry-run, dependency-gate, disabled-gate, reject
verdict, document-evidence ingestion) lives in tests/integration/spot/ and is
not duplicated here.
"""
# spec: USE_CASE_en.md §UC3

import httpx
import pytest

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
    """UC3 user-story arc under stub mode (stub_llm_client=true in runtime conf).

    Steps mirror USE_CASE_en.md §UC3 Imazon Example:
      1. PUT singleton ontogen conf (is_enabled, schedule_tier, dataset_filter)
      2. POST Markdown seed — assert 201, capture seed_id; GET seed list and
         assert entry shows preview, updated_at, and is_enabled=false (ships disabled)
      2c. PATCH attr/seed/{id}/attr/enabled {is_enabled: true} — the seed joins
          inference; the list now shows is_enabled=true
      3. POST real (non-dry-run) inference — assert OntogenRunSummary shape
      4. GET /event — find ONTOGEN.RUN_COMPLETE; assert debate fields + run_id in detail
      5. GET result/{node,edge,triple} — assert standard envelope shape for each, and
         that every persisted row carries run_id == the RUN_COMPLETE event's run_id
         (the row's link to its creating run's Langfuse session)
      6. Cleanup: DELETE seed (hard delete — gone from list), PATCH conf disabled

    Spec: USE_CASE_en.md §UC3 — open the run's Langfuse session via run_id
    Spec: BACKEND_LLM.md §Evidence shape — debate transcript lives in Langfuse,
    addressed by session_id = run_id; the row persists run_id, not the transcript.
    Spec: BACKEND_LLM.md §Test Mode — stub Producer returns empty payload so no rows
    are persisted; the per-row run_id check is intentionally a no-op under stub mode.
    """
    conf_url = "/api/v1/spoke/ontogen/attr/conf"
    seed_url = "/api/v1/spoke/ontogen/attr/seed"
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
        assert conf_body["dataset_filter"] == {
            "origin": "DEV",
            "tags": ["urn:li:tag:area:catalog"],
        }, (
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
            "spec: USE_CASE_en.md §UC3 — GET attr/seed returns "
            "[{seed_id, preview, updated_at, is_enabled}]"
        )
        assert "updated_at" in seed_entry, (
            "seed list entry missing 'updated_at'. "
            "spec: USE_CASE_en.md §UC3 — GET attr/seed returns "
            "[{seed_id, preview, updated_at, is_enabled}]"
        )
        # A new seed ships disabled — it does not participate in inference until enabled.
        # spec: USE_CASE_en.md §UC3 — POST attr/seed creates the seed disabled.
        assert seed_entry.get("is_enabled") is False, (
            "a new seed must be created disabled; got "
            f"is_enabled={seed_entry.get('is_enabled')!r}. "
            "spec: USE_CASE_en.md §UC3 — seed ships disabled"
        )

        # ── Step 2c: Enable the seed so it joins the inference run ─────────────
        # UC3 narrative: "The seed is created disabled; the steward reviews it, then
        # enables it via PATCH .../attr/seed/{seed_id}/attr/enabled so it joins the
        # next inference run."
        # spec: API.md §PATCH attr/seed/{seed_id}/attr/enabled — JSON {is_enabled: bool}.
        enable_resp = await api_client.patch(
            f"{seed_url}/{seed_id}/attr/enabled",
            headers=admin_headers,
            json={"is_enabled": True},
        )
        assert enable_resp.status_code == 200, (
            f"PATCH attr/enabled failed: {enable_resp.status_code} {enable_resp.text}. "
            "spec: API.md §PATCH attr/seed/{seed_id}/attr/enabled"
        )
        assert enable_resp.json().get("is_enabled") is True, (
            "enable response must round-trip is_enabled=true"
        )
        # The list now reflects the enabled state (disabled seeds stay visible too).
        relist_resp = await api_client.get(seed_url, headers=admin_headers)
        assert relist_resp.status_code == 200
        relisted = {s["seed_id"]: s for s in relist_resp.json()["seeds"]}
        assert relisted[seed_id]["is_enabled"] is True, (
            "after enabling, the seed list must show is_enabled=true. "
            "spec: USE_CASE_en.md §UC3 — GET attr/seed lists all seeds with is_enabled"
        )

        # ── Step 3: POST real (non-dry-run) inference ─────────────────────────
        # spec: USE_CASE_en.md §UC3 §Run semantics — non-dry-run persists rows
        # spec: BACKEND_LLM.md §Adversarial Debate Framework — debate runs unconditionally
        run_resp = await api_client.post(
            "/api/v1/spoke/ontogen/method/run",
            headers=admin_headers,
            # method/run is synchronous; a real (non-stub) LLM inference takes minutes,
            # so override the 30s api_client default to avoid a ReadTimeout. Harmless
            # under stub mode (the stub Producer returns immediately).
            timeout=300.0,
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
            "/api/v1/spoke/ontogen/event?limit=20",
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
        # run_id identifies the Langfuse session this run traced under; every row
        # this run persists carries it (see Step 5).
        # spec: BACKEND_LLM.md §Evidence shape — session_id = run_id
        run_id = detail.get("run_id")
        assert isinstance(run_id, str) and run_id, (
            f"event detail run_id must be a non-empty string; got {run_id!r}. "
            "spec: BACKEND_LLM.md §Evidence shape — run_id = Langfuse session id"
        )

        # ── Step 5: GET result/{node,edge,triple} — envelope + per-row run_id ──
        # spec: USE_CASE_en.md §UC3 §API Mapping — list endpoints return paginated envelopes
        # spec: spec/API.md §Standard Envelope
        for result_type, list_key in [
            ("node", "nodes"),
            ("edge", "edges"),
            ("triple", "triples"),
        ]:
            list_resp = await api_client.get(
                f"/api/v1/spoke/ontogen/result/{result_type}?offset=0&limit=10",
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

            # Every result row exposes a run_id field — its link to the creating run's
            # Langfuse session. The value is that row's creating-run id; rows from other
            # runs / seeded fixtures carry a different id or NULL, so the equality to
            # THIS run's id is only meaningful for rows this run produced.
            # spec: BACKEND_LLM.md §Evidence shape — row.run_id = session_id; the
            # transcript lives in Langfuse, not in the row.
            # Under stub mode the Producer returns an empty payload, so this run persists
            # no new rows; the schema contract is asserted on whatever rows exist.
            for row in list_body[list_key]:
                assert "run_id" in row, (
                    f"{result_type} {row['id']!r} missing run_id field. "
                    "spec: BACKEND_LLM.md §Evidence shape — every result row carries run_id"
                )

        # ── Step 5b: DELETE the seed is a hard delete — gone from the list ────
        # UC3 narrative: "DELETE removes the seed outright."
        # spec: USE_CASE_en.md §UC3 — DELETE attr/seed/{id} hard-deletes the seed.
        del_seed_resp = await api_client.delete(
            f"{seed_url}/{seed_id}", headers=admin_headers
        )
        assert del_seed_resp.status_code == 204, (
            f"DELETE seed expected 204, got {del_seed_resp.status_code}: {del_seed_resp.text}"
        )
        post_delete_list = await api_client.get(seed_url, headers=admin_headers)
        assert post_delete_list.status_code == 200
        post_delete_ids = {s["seed_id"] for s in post_delete_list.json()["seeds"]}
        assert seed_id not in post_delete_ids, (
            f"hard-deleted seed {seed_id!r} must be absent from the list; "
            f"got: {post_delete_ids}"
        )
        seed_id = None  # already deleted — skip the finally cleanup

    finally:
        # ── Step 6: Cleanup ───────────────────────────────────────────────────
        if seed_id is not None:
            await api_client.delete(f"{seed_url}/{seed_id}", headers=admin_headers)
        await api_client.patch(conf_url, headers=admin_headers, json={"is_enabled": False})


@pytest.mark.asyncio
async def test_uc3_ontology_generation_with_real_llm(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    runtime_conf: dict,
) -> None:
    """UC3 user-story arc with a real LLM (stub_llm_client=false in runtime conf).

    Skipped when stub_llm_client is true — a stub LLM cannot satisfy the
    real-LLM contract assertions (non-zero persisted rows).

    Structurally identical to test_uc3_ontology_generation_under_stub. Additional
    assertion after step 5: any_rows_found must be True — a real LLM run that
    persists zero rows signals a prompt/filter regression.

    Steps mirror USE_CASE_en.md §UC3 Imazon Example:
      1. PUT singleton ontogen conf (is_enabled, schedule_tier, dataset_filter)
      2. POST Markdown seed — assert 201, capture seed_id; GET seed list and
         assert entry shows preview, updated_at, and is_enabled=false (ships disabled)
      2c. PATCH attr/seed/{id}/attr/enabled {is_enabled: true} — the seed joins inference
      3. POST real (non-dry-run) inference — assert OntogenRunSummary shape
      4. GET /event — find ONTOGEN.RUN_COMPLETE; assert debate fields + run_id in detail
      5. GET result/{node,edge,triple} — assert standard envelope shape for each, and
         that every persisted row carries run_id == the RUN_COMPLETE event's run_id;
         track any_rows_found across all three result types
      6. Assert any_rows_found is True
      7. Cleanup: DELETE seed (hard delete), PATCH conf disabled

    Spec: USE_CASE_en.md §UC3 — open the run's Langfuse session via run_id
    Spec: BACKEND_LLM.md §Evidence shape — debate transcript lives in Langfuse,
    addressed by session_id = run_id; the row persists run_id, not the transcript.
    """
    if runtime_conf.get("stub_llm_client"):
        pytest.skip(
            "stub_llm_client=true; set stub_llm_client=false via PATCH /admin/conf "
            "to run real-LLM tests"
        )

    conf_url = "/api/v1/spoke/ontogen/attr/conf"
    seed_url = "/api/v1/spoke/ontogen/attr/seed"
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
        assert conf_body["dataset_filter"] == {
            "origin": "DEV",
            "tags": ["urn:li:tag:area:catalog"],
        }, (
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
            "spec: USE_CASE_en.md §UC3 — GET attr/seed returns "
            "[{seed_id, preview, updated_at, is_enabled}]"
        )
        assert "updated_at" in seed_entry, (
            "seed list entry missing 'updated_at'. "
            "spec: USE_CASE_en.md §UC3 — GET attr/seed returns "
            "[{seed_id, preview, updated_at, is_enabled}]"
        )
        # A new seed ships disabled.
        # spec: USE_CASE_en.md §UC3 — POST attr/seed creates the seed disabled.
        assert seed_entry.get("is_enabled") is False, (
            "a new seed must be created disabled; got "
            f"is_enabled={seed_entry.get('is_enabled')!r}. "
            "spec: USE_CASE_en.md §UC3 — seed ships disabled"
        )

        # ── Step 2c: Enable the seed so it joins the inference run ─────────────
        # UC3 narrative: "The steward enables the seed via PATCH .../attr/enabled so
        # it joins the next inference run."
        # spec: API.md §PATCH attr/seed/{seed_id}/attr/enabled — JSON {is_enabled: bool}.
        enable_resp = await api_client.patch(
            f"{seed_url}/{seed_id}/attr/enabled",
            headers=admin_headers,
            json={"is_enabled": True},
        )
        assert enable_resp.status_code == 200, (
            f"PATCH attr/enabled failed: {enable_resp.status_code} {enable_resp.text}. "
            "spec: API.md §PATCH attr/seed/{seed_id}/attr/enabled"
        )
        assert enable_resp.json().get("is_enabled") is True, (
            "enable response must round-trip is_enabled=true"
        )

        # ── Step 3: POST real (non-dry-run) inference ─────────────────────────
        # spec: USE_CASE_en.md §UC3 §Run semantics — non-dry-run persists rows
        # spec: BACKEND_LLM.md §Adversarial Debate Framework — debate runs unconditionally
        run_resp = await api_client.post(
            "/api/v1/spoke/ontogen/method/run",
            headers=admin_headers,
            # method/run is synchronous; a real (non-stub) LLM inference takes minutes,
            # so override the 30s api_client default to avoid a ReadTimeout. Harmless
            # under stub mode (the stub Producer returns immediately).
            timeout=300.0,
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
            "/api/v1/spoke/ontogen/event?limit=20",
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
        # run_id identifies the Langfuse session this run traced under; every row
        # this run persists carries it (see Step 5).
        # spec: BACKEND_LLM.md §Evidence shape — session_id = run_id
        run_id = detail.get("run_id")
        assert isinstance(run_id, str) and run_id, (
            f"event detail run_id must be a non-empty string; got {run_id!r}. "
            "spec: BACKEND_LLM.md §Evidence shape — run_id = Langfuse session id"
        )

        # ── Step 5: GET result/{node,edge,triple} + per-row run_id ─────────────
        # spec: USE_CASE_en.md §UC3 §API Mapping — list endpoints return paginated envelopes
        # spec: BACKEND_LLM.md §Evidence shape — row.run_id = Langfuse session id
        # spec: API.md §Standard Envelope
        any_rows_found = False
        for result_type, list_key in [
            ("node", "nodes"),
            ("edge", "edges"),
            ("triple", "triples"),
        ]:
            list_resp = await api_client.get(
                f"/api/v1/spoke/ontogen/result/{result_type}?offset=0&limit=10",
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

            # Every result row exposes a run_id field. Rows this run produced carry
            # run_id == the RUN_COMPLETE event's run_id (their link to the run's Langfuse
            # session, where the debate transcript lives); rows from prior runs / seeded
            # fixtures carry a different id or NULL. any_rows_found is the discriminating
            # signal — a NULL or swapped run_id on the new rows leaves no row matching
            # this run's id and fails the "produced ≥1 row" assertion below.
            # spec: BACKEND_LLM.md §Evidence shape — row.run_id = session_id
            for row in list_body[list_key]:
                assert "run_id" in row, (
                    f"{result_type} {row['id']!r} missing run_id field. "
                    "spec: BACKEND_LLM.md §Evidence shape"
                )
            if any(r.get("run_id") == run_id for r in list_body[list_key]):
                any_rows_found = True

        # ── Step 6: Assert real LLM produced rows for this run ───────────────
        assert any_rows_found, (
            "Real LLM run produced zero rows carrying this run's run_id — verify "
            "prompt/filter pipeline. spec: BACKEND_LLM.md §Test Mode — real LLM "
            "must persist ≥1 row stamped with the run's id"
        )

        # ── Step 6b: DELETE the seed is a hard delete — gone from the list ───
        # spec: USE_CASE_en.md §UC3 — DELETE attr/seed/{id} hard-deletes the seed.
        del_seed_resp = await api_client.delete(
            f"{seed_url}/{seed_id}", headers=admin_headers
        )
        assert del_seed_resp.status_code == 204, (
            f"DELETE seed expected 204, got {del_seed_resp.status_code}: {del_seed_resp.text}"
        )
        post_delete_list = await api_client.get(seed_url, headers=admin_headers)
        assert post_delete_list.status_code == 200
        post_delete_ids = {s["seed_id"] for s in post_delete_list.json()["seeds"]}
        assert seed_id not in post_delete_ids, (
            f"hard-deleted seed {seed_id!r} must be absent from the list; "
            f"got: {post_delete_ids}"
        )
        seed_id = None  # already deleted — skip the finally cleanup

    finally:
        # ── Step 7: Cleanup ───────────────────────────────────────────────────
        if seed_id is not None:
            await api_client.delete(f"{seed_url}/{seed_id}", headers=admin_headers)
        await api_client.patch(conf_url, headers=admin_headers, json={"is_enabled": False})
