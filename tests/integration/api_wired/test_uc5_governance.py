"""UC5 — Governance: end-to-end through public REST API.

Maps `spec/USE_CASE_en.md §UC5` paragraphs to executable steps. REST-only
per `spec/TESTING.md §Api-Wired Integration Tests`.

Tests in this module:
  - test_uc5_metrics_and_overview: Register both baseline metrics (ingestion-freshness,
    validation-score) bounded to one URN, trigger immediate runs, query time-range
    trends with breakdown shape assertion, and verify GET /spoke/dg/overview returns
    all six documented keys with correct types.
"""
# spec: USE_CASE_en.md §UC5

import httpx
import pytest

# Bounded URN: keeps DataHub I/O small by scoping measurements to one known dataset
# spec: TESTING.md §Imazon Dummy-Data Reference — catalog.title_master
_BOUNDED_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
)

# Baseline metric IDs
# spec: USE_CASE_en.md §UC5 L640-L643
_METRIC_INGESTION_FRESHNESS = "ingestion-freshness"
_METRIC_VALIDATION_SCORE = "validation-score"


@pytest.mark.asyncio
async def test_uc5_metrics_and_overview(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """UC5 narrative: 'As a governance lead or CDO, I want a small set of always-on
    signals — ingestion freshness and validation score — and one overview that shows
    them at a glance, so that I can monitor health without curating dashboards by hand.'

    Steps mirror USE_CASE_en.md §UC5:
      1. PUT both baseline metric confs, bounded to one URN
      2. POST immediate runs for each metric (dry_run=false) — assert status: success
      3. GET time-range results for each metric — assert envelope + breakdown shape
      4. GET /spoke/dg/overview — assert all 6 documented keys with correct types
      5. Cleanup — DELETE both metric confs
    """
    ingestion_conf_url = (
        f"/api/v1/spoke/dg/metric/{_METRIC_INGESTION_FRESHNESS}/attr/conf"
    )
    ingestion_run_url = (
        f"/api/v1/spoke/dg/metric/{_METRIC_INGESTION_FRESHNESS}/method/run"
    )
    ingestion_results_url = (
        f"/api/v1/spoke/dg/metric/{_METRIC_INGESTION_FRESHNESS}/attr/result"
    )

    validation_conf_url = (
        f"/api/v1/spoke/dg/metric/{_METRIC_VALIDATION_SCORE}/attr/conf"
    )
    validation_run_url = (
        f"/api/v1/spoke/dg/metric/{_METRIC_VALIDATION_SCORE}/method/run"
    )
    validation_results_url = (
        f"/api/v1/spoke/dg/metric/{_METRIC_VALIDATION_SCORE}/attr/result"
    )

    try:
        # ── Step 1: PUT both baseline metric confs ────────────────────────────
        # UC5 narrative: "The CDO registers both metrics."
        # spec: USE_CASE_en.md §UC5 L677-L703

        # Register ingestion-freshness
        # spec: USE_CASE_en.md §UC5 L686 — aggregation value is "pct_fresh"
        put_ingestion_resp = await api_client.put(
            ingestion_conf_url,
            headers=admin_headers,
            json={
                "title": "Ingestion freshness",
                # spec-inconsistency: USE_CASE_en.md §UC5 L685-L703 omits 'description' from
                # the example PUT, but BACKEND_SCHEMA.md L241 has the column NOT NULL.
                # Sending it to satisfy the schema.
                "description": (
                    "Percentage of enabled ingestion configs whose latest run is within "
                    "the freshness window."
                ),
                "theme": "freshness",
                "measurement_query": {
                    "aggregation": "pct_fresh",
                    "dataset_filter": {"dataset_urns": [_BOUNDED_URN]},
                },
                "schedule_tier": "hourly",
                "is_enabled": True,
            },
        )
        assert put_ingestion_resp.status_code in (200, 201), (
            f"PUT ingestion-freshness conf failed: "
            f"{put_ingestion_resp.status_code} {put_ingestion_resp.text}"
        )
        ingestion_conf_body = put_ingestion_resp.json()
        assert ingestion_conf_body["id"] == _METRIC_INGESTION_FRESHNESS

        # Register validation-score
        # spec: USE_CASE_en.md §UC5 L699 — aggregation value is "pct_rules_passing"
        put_validation_resp = await api_client.put(
            validation_conf_url,
            headers=admin_headers,
            json={
                "title": "Validation score",
                # spec-inconsistency: USE_CASE_en.md §UC5 L685-L703 omits 'description' from
                # the example PUT, but BACKEND_SCHEMA.md L241 has the column NOT NULL.
                # Sending it to satisfy the schema.
                "description": (
                    "Percentage of validation rules passing in the latest run."
                ),
                "theme": "quality",
                "measurement_query": {
                    "aggregation": "pct_rules_passing",
                    "dataset_filter": {"dataset_urns": [_BOUNDED_URN]},
                },
                "schedule_tier": "hourly",
                "is_enabled": True,
            },
        )
        assert put_validation_resp.status_code in (200, 201), (
            f"PUT validation-score conf failed: "
            f"{put_validation_resp.status_code} {put_validation_resp.text}"
        )
        validation_conf_body = put_validation_resp.json()
        assert validation_conf_body["id"] == _METRIC_VALIDATION_SCORE

        # ── Step 2a: Dry-run — must not persist results or emit events ───────
        # UC5 narrative: "dry_run: true evaluates without persisting and without
        # emitting events."
        # spec: USE_CASE_en.md §UC5 L651-L653

        # Capture baseline counts before dry-run
        pre_dry_attr_resp = await api_client.get(
            f"{ingestion_results_url}?from=2026-01-01T00:00:00Z&to=2026-12-31T23:59:59Z",
            headers=admin_headers,
        )
        assert pre_dry_attr_resp.status_code == 200
        pre_dry_attr_count = pre_dry_attr_resp.json().get("total_count", 0)

        pre_dry_event_resp = await api_client.get(
            f"/api/v1/spoke/dg/metric/{_METRIC_INGESTION_FRESHNESS}/event?"
            "from=2026-01-01T00:00:00Z&to=2026-12-31T23:59:59Z",
            headers=admin_headers,
        )
        assert pre_dry_event_resp.status_code == 200
        pre_dry_event_count = pre_dry_event_resp.json().get("total_count", 0)

        dry_run_ingestion_resp = await api_client.post(
            ingestion_run_url,
            headers=admin_headers,
            json={"dry_run": True},
        )
        assert dry_run_ingestion_resp.status_code == 200, (
            f"POST ingestion-freshness dry-run failed: "
            f"{dry_run_ingestion_resp.status_code} {dry_run_ingestion_resp.text}"
        )
        dry_run_body = dry_run_ingestion_resp.json()
        assert dry_run_body.get("status") == "success", (
            f"ingestion-freshness dry-run status expected 'success'; "
            f"got {dry_run_body.get('status')!r}. spec: USE_CASE_en.md §UC5 L651-L653"
        )

        # attr/result total_count must not change after dry-run
        # spec: USE_CASE_en.md §UC5 L651-L653 — dry_run=true does not persist
        post_dry_attr_resp = await api_client.get(
            f"{ingestion_results_url}?from=2026-01-01T00:00:00Z&to=2026-12-31T23:59:59Z",
            headers=admin_headers,
        )
        assert post_dry_attr_resp.status_code == 200
        post_dry_attr_count = post_dry_attr_resp.json().get("total_count", 0)
        assert post_dry_attr_count == pre_dry_attr_count, (
            f"dry_run persisted a result: attr/result total_count went from "
            f"{pre_dry_attr_count} to {post_dry_attr_count}. "
            "spec: USE_CASE_en.md §UC5 L651-L653"
        )

        # event total_count must not change after dry-run (no METRIC.RUN_COMPLETE emitted)
        # spec: USE_CASE_en.md §UC5 L651-L653 — dry_run=true does not emit events
        post_dry_event_resp = await api_client.get(
            f"/api/v1/spoke/dg/metric/{_METRIC_INGESTION_FRESHNESS}/event?"
            "from=2026-01-01T00:00:00Z&to=2026-12-31T23:59:59Z",
            headers=admin_headers,
        )
        assert post_dry_event_resp.status_code == 200
        post_dry_event_count = post_dry_event_resp.json().get("total_count", 0)
        assert post_dry_event_count == pre_dry_event_count, (
            f"dry_run emitted an event: event total_count went from "
            f"{pre_dry_event_count} to {post_dry_event_count}. "
            "spec: USE_CASE_en.md §UC5 L651-L653"
        )

        # ── Step 2: Trigger immediate runs ────────────────────────────────────
        # UC5 narrative: "The CDO triggers an immediate first run rather than waiting
        # for the schedule: POST /api/v1/spoke/dg/metric/ingestion-freshness/method/run"
        # spec: USE_CASE_en.md §UC5 L705-L707
        run_ingestion_resp = await api_client.post(
            ingestion_run_url,
            headers=admin_headers,
            json={"dry_run": False},
        )
        assert run_ingestion_resp.status_code == 200, (
            f"POST ingestion-freshness run failed: "
            f"{run_ingestion_resp.status_code} {run_ingestion_resp.text}"
        )
        ingestion_run_body = run_ingestion_resp.json()
        assert ingestion_run_body.get("status") == "success", (
            f"ingestion-freshness run status: {ingestion_run_body.get('status')!r}. "
            "spec: USE_CASE_en.md §UC5 L705"
        )

        run_validation_resp = await api_client.post(
            validation_run_url,
            headers=admin_headers,
            json={"dry_run": False},
        )
        assert run_validation_resp.status_code == 200, (
            f"POST validation-score run failed: "
            f"{run_validation_resp.status_code} {run_validation_resp.text}"
        )
        validation_run_body = run_validation_resp.json()
        assert validation_run_body.get("status") == "success", (
            f"validation-score run status: {validation_run_body.get('status')!r}. "
            "spec: USE_CASE_en.md §UC5 L707"
        )

        # ── Step 3: Time-range trend queries ──────────────────────────────────
        # UC5 narrative: "A week later, trends are pulled for a board update:
        # GET .../attr/result?from=…&to=…"
        # spec: USE_CASE_en.md §UC5 L709-L713
        for results_url, metric_label in [
            (ingestion_results_url, "ingestion-freshness"),
            (validation_results_url, "validation-score"),
        ]:
            results_resp = await api_client.get(
                f"{results_url}?from=2026-01-01T00:00:00Z&to=2026-12-31T23:59:59Z"
                "&offset=0&limit=5",
                headers=admin_headers,
            )
            assert results_resp.status_code == 200, (
                f"GET {metric_label} results failed: {results_resp.status_code}"
            )
            results_body = results_resp.json()
            # spec: API.md §Standard Envelope
            assert "results" in results_body
            assert "offset" in results_body
            assert "limit" in results_body
            assert "total_count" in results_body
            assert isinstance(results_body["results"], list)

            # spec: USE_CASE_en.md §UC5 L645-L650 — after a successful non-dry-run,
            # at least one result row must be persisted
            assert results_body["results"], (
                f"{metric_label}: expected ≥1 persisted result row after a successful "
                "non-dry-run; spec USE_CASE_en.md §UC5 L645-L650"
            )
            # Verify breakdown shape on the first result row
            # spec: USE_CASE_en.md §UC5 L648-L650 — result row carries breakdown
            # spec: feature/BACKEND.md §Metrics Service L457 — breakdown shape
            result_row = results_body["results"][0]
            assert "breakdown" in result_row, (
                f"{metric_label} result row missing 'breakdown'. "
                "spec: feature/BACKEND.md §Metrics Service L457"
            )
            breakdown = result_row["breakdown"]
            assert isinstance(breakdown, dict), "breakdown must be a dict"
            assert "dataset_count" in breakdown, (
                "breakdown missing 'dataset_count'. "
                "spec: feature/BACKEND.md §Metrics Service L457"
            )
            assert isinstance(breakdown["dataset_count"], int), (
                "dataset_count must be an int"
            )
            assert "datasets" in breakdown, (
                "breakdown missing 'datasets'. "
                "spec: feature/BACKEND.md §Metrics Service L457"
            )
            assert isinstance(breakdown["datasets"], list), (
                "breakdown.datasets must be a list"
            )
            for entry in breakdown["datasets"]:
                # spec: BACKEND.md L465 — required: urn, category; L469 — detail is optional
                assert "urn" in entry, (
                    "dataset entry missing 'urn'. "
                    "spec: feature/BACKEND.md §Metrics Service L465"
                )
                assert "category" in entry, (
                    "dataset entry missing 'category'. "
                    "spec: feature/BACKEND.md §Metrics Service L465"
                )
                if "detail" in entry:
                    assert isinstance(entry["detail"], dict), (
                        "dataset entry 'detail' must be a dict when present. "
                        "spec: feature/BACKEND.md §Metrics Service L469"
                    )

        # ── Step 4: Dashboard overview view ───────────────────────────────────
        # UC5 narrative: "The dashboard view consumes the overview endpoint:
        # GET /api/v1/spoke/dg/overview … returns both metric values plus a
        # per-dataset breakdown … alongside any blind spots."
        # spec: USE_CASE_en.md §UC5 L715-L727
        overview_resp = await api_client.get(
            "/api/v1/spoke/dg/overview",
            headers=admin_headers,
        )
        assert overview_resp.status_code == 200, (
            f"GET /spoke/dg/overview failed: {overview_resp.status_code} {overview_resp.text}"
        )
        overview_body = overview_resp.json()

        # All 6 documented keys must be present with correct types
        # spec: feature/BACKEND.md §Overview Service L478-L483
        assert "metric_values" in overview_body, (
            "overview missing 'metric_values'. spec: feature/BACKEND.md §Overview L478"
        )
        assert isinstance(overview_body["metric_values"], dict), (
            "metric_values must be a dict. spec: feature/BACKEND.md §Overview L478"
        )

        assert "per_dataset_breakdown" in overview_body, (
            "overview missing 'per_dataset_breakdown'. spec: feature/BACKEND.md §Overview L479"
        )
        assert isinstance(overview_body["per_dataset_breakdown"], dict), (
            "per_dataset_breakdown must be a dict. spec: feature/BACKEND.md §Overview L479"
        )

        assert "blind_spots" in overview_body, (
            "overview missing 'blind_spots'. spec: feature/BACKEND.md §Overview L480"
        )
        assert isinstance(overview_body["blind_spots"], list), (
            "blind_spots must be a list. spec: feature/BACKEND.md §Overview L480"
        )

        assert "ontology_graph" in overview_body, (
            "overview missing 'ontology_graph'. spec: feature/BACKEND.md §Overview L481"
        )
        ontology_graph = overview_body["ontology_graph"]
        assert "nodes" in ontology_graph, (
            "ontology_graph missing 'nodes'. spec: feature/BACKEND.md §Overview L481"
        )
        assert "edges" in ontology_graph, (
            "ontology_graph missing 'edges'. spec: feature/BACKEND.md §Overview L481"
        )
        assert isinstance(ontology_graph["nodes"], list)
        assert isinstance(ontology_graph["edges"], list)

        assert "medallion" in overview_body, (
            "overview missing 'medallion'. spec: feature/BACKEND.md §Overview L482"
        )
        medallion = overview_body["medallion"]
        assert "bronze" in medallion, (
            "medallion missing 'bronze'. spec: feature/BACKEND.md §Overview L482"
        )
        assert "silver" in medallion, (
            "medallion missing 'silver'. spec: feature/BACKEND.md §Overview L482"
        )
        assert "gold" in medallion, (
            "medallion missing 'gold'. spec: feature/BACKEND.md §Overview L482"
        )
        assert isinstance(medallion["bronze"], int), (
            "medallion.bronze must be int. spec: feature/BACKEND.md §Overview L482"
        )
        assert isinstance(medallion["silver"], int), (
            "medallion.silver must be int. spec: feature/BACKEND.md §Overview L482"
        )
        assert isinstance(medallion["gold"], int), (
            "medallion.gold must be int. spec: feature/BACKEND.md §Overview L482"
        )

        assert "ownership_topology" in overview_body, (
            "overview missing 'ownership_topology'. spec: feature/BACKEND.md §Overview L483"
        )
        assert isinstance(overview_body["ownership_topology"], dict), (
            "ownership_topology must be a dict. spec: feature/BACKEND.md §Overview L483"
        )

    finally:
        # ── Step 5: Cleanup — DELETE both metric confs ────────────────────────
        await api_client.delete(ingestion_conf_url, headers=admin_headers)
        await api_client.delete(validation_conf_url, headers=admin_headers)
