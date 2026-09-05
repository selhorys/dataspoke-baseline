"""UC5 — Governance: Imazon CDO story through the public REST API.

Maps spec/USE_CASE_en.md §UC5 §Imazon Example paragraphs to executable REST steps.
REST-only per spec/TESTING.md §Api-Wired Integration Tests.

User story:
  'As a governance lead or CDO, I want a small set of always-on metrics —
  ingestion freshness, validation score, and documentation health — that I
  can schedule, scope, and trend over time, so that I can monitor estate
  health without curating dashboards by hand.'

Contract exercised here:
  - Metric creation uses POST /spoke/governance/metric with metric_id in the body (→ 201).
  - PUT /{id}/attr/conf is replace-only (→ 200 on existing, 404 METRIC_NOT_FOUND when absent).
  - metric_conf.time_window_sec is the measurement window's *width* (factory default
    172800), the same for every dataset the metric scans; where that window sits is per
    type — ingestion-freshness trails the measurement instant, validation-score shifts
    back by each dataset's own declared arrival cadence. api-wired does not assert
    exact in-window counts (real-pipeline timing is nondeterministic).
  - dataset_filter covers every column class of the grammar, the boolean `is_primary`
    included; the sibling relationship the boolean reads is emitted into DataHub by
    the `seeded_sibling_pair` fixture, because the seeded estate has none.
"""

# spec: USE_CASE_en.md §UC5 §Imazon Example

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio

from tests.integration.util import datahub as datahub_util

# Declare fixture dependencies so module_dummy_data seeds catalog schema + DataHub.
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})

_TITLE_MASTER_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
)
_EDITIONS_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"


@pytest_asyncio.fixture
async def seeded_sibling_pair(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> AsyncIterator[None]:
    """Make `catalog.editions` a non-leading sibling of `catalog.title_master`.

    Setup only — the test body stays REST-only (spec/TESTING.md §Api-wired
    integration tests: "Setup/teardown fixtures may use `tests.integration.util` to
    reset/ingest data; the test itself stays REST-only").

    The seeded Imazon estate carries no `siblings` aspect at all, so without this the
    whole estate reads `is_primary = true` and the `false` branch of the clause under
    test is unreachable — the assertions would pass against an empty set and prove
    nothing.

    Snapshot → mutate → verified restore, on both stores this touches: the `siblings`
    aspect in DataHub is read before it is written and restored to what was read, and
    the mirrored `dataset_registry.is_primary` is swept back to `true` and read back
    through the API. Neither restore is assumed — a restore that did not take fails
    this test rather than the next one to look at `is_primary`
    (spec/TESTING.md §Integration Lifecycle & Isolation).
    """
    snapshot = datahub_util.read_siblings(_EDITIONS_URN)
    datahub_util.emit_siblings(_EDITIONS_URN, [_TITLE_MASTER_URN], primary=False)
    try:
        yield
    finally:
        # Restore the aspect to what was there. Absent is not re-creatable through the
        # emitter, but an empty sibling set with the leader flag is the same "no
        # sibling information" reading — the baseline the rest of the suite assumes.
        restored_urns, restored_primary = snapshot if snapshot is not None else ([], True)
        datahub_util.emit_siblings(_EDITIONS_URN, restored_urns, primary=restored_primary)
        assert datahub_util.read_siblings(_EDITIONS_URN) == (restored_urns, restored_primary), (
            "the siblings aspect must read back as the snapshot this fixture took; a "
            "silent restore failure leaves the shared estate mis-marked for every later "
            "test. spec: TESTING.md §Integration Lifecycle & Isolation."
        )

        # The registry column is a mirror, so restoring the aspect is not enough: until
        # a sweep runs, `dataset_registry.is_primary` for editions stays `false` for the
        # rest of the session. Sweep and read the mirror back through the API — the
        # scroll the sweep reads is eventually consistent, so poll rather than sleep.
        probe_id = "uc5-sibling-restore-probe"
        probe_conf_url = f"/api/v1/spoke/governance/metric/{probe_id}/attr/conf"
        probe_dataset_url = f"/api/v1/spoke/governance/metric/{probe_id}/dataset"
        await api_client.delete(probe_conf_url, headers=admin_headers)
        probe_resp = await api_client.post(
            "/api/v1/spoke/governance/metric",
            headers=admin_headers,
            json={
                "metric_id": probe_id,
                "mode": "active",
                "is_enabled": False,
                "metric_type": "doc-health",
                "title": "Sibling restore probe",
                "description": "Reads back the non-leading side of the registry mirror",
                "metrics": [{"name": "total", "color": "#2563EB", "idx": 1}],
                "metric_conf": {},
                "schedule_tier": None,
                "dataset_filter": "is_primary = false",
            },
        )
        assert probe_resp.status_code == 201, (
            f"the restore probe metric must be creatable, got {probe_resp.status_code}: "
            f"{probe_resp.text}."
        )
        try:
            deadline = datetime.now(tz=UTC) + timedelta(seconds=180)
            non_leading: set[str] = {_EDITIONS_URN}
            while datetime.now(tz=UTC) < deadline:
                sync_resp = await api_client.post(
                    "/internal/activities/ingestion/sync",
                    headers=internal_headers,
                    timeout=300.0,
                )
                assert sync_resp.status_code == 200, (
                    f"POST /internal/activities/ingestion/sync expected 200, got "
                    f"{sync_resp.status_code}: {sync_resp.text}."
                )
                probe_view = await api_client.get(
                    f"{probe_dataset_url}?limit=1000", headers=admin_headers
                )
                assert probe_view.status_code == 200, probe_view.text
                non_leading = {row["dataset_urn"] for row in probe_view.json()["datasets"]}
                if _EDITIONS_URN not in non_leading:
                    break
            assert _EDITIONS_URN not in non_leading, (
                f"`{_EDITIONS_URN}` must read `is_primary = true` again once the sibling "
                f"aspect is restored and swept; it is still in the non-leading set "
                f"{sorted(non_leading)}. spec: TESTING.md §Integration Lifecycle & "
                "Isolation — the restore is asserted, not assumed."
            )
        finally:
            await api_client.delete(probe_conf_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_uc5_governance_imazon_example(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """UC5 Imazon Example: CDO creates, runs, and reviews DEV-scoped daily metrics.

    USE_CASE narrative shows doc-health; the test exercises all three built-in
    active metric types in parallel to cover the full loop.

    Steps:
      1   — CDO creates three DEV-scoped daily metrics via POST (metric_id in body) → 201
      1b  — Re-POST same metric_id (collision) → 409 METRIC_EXISTS
      1c  — PUT replace-only: existing id → 200 + change reflected; absent id → 404 METRIC_NOT_FOUND
      2   — CDO triggers immediate first run for each metric → 200 + non-empty run_id
      3   — Trends pulled over a one-week window → at least one result row per metric;
            values is a dict whose keys match the metric's declared metrics list
      4   — Cleanup: DELETE each created metric (204 or 404 both acceptable)
    """
    # The three built-in active metric types — created DEV-scoped, daily, enabled.
    # spec: USE_CASE_en.md §UC5 §Built-in active metric types
    #
    # metric_conf.time_window_sec=172800 is the measurement window (factory default).
    # spec: USE_CASE_en.md §UC5 §Built-in active metric types — "**the** measurement
    # window (positive int seconds … factory default 172800)".
    metrics_to_create = [
        {
            "metric_id": "ingestion-freshness-dev",
            "type": "ingestion-freshness",
            "title": "Ingestion Freshness (DEV)",
            "description": "Daily count of datasets ingested within the configured time window "
            "across DEV",
            "metrics": [
                {"name": "total", "color": "#64748B", "idx": 1},
                {"name": "ingested_in_time", "color": "#22C55E", "idx": 2},
            ],
            "metric_conf": {"time_window_sec": 172800},
        },
        {
            "metric_id": "validation-score-dev",
            "type": "validation-score",
            "title": "Validation Score (DEV)",
            # Counts, not a score sum: `valid_confd` is how many of the scoped datasets
            # carry a validation config, `valid_in_time` how many of those pass their
            # cadence-anchored window test.
            # spec: feature/BACKEND.md §Metrics Service — "`validation-score` counts and
            # the unconfigured set".
            # Worded to start "Daily count of validated ..." rather than "Daily count of
            # DEV ...": the latter collided with the ingestion-freshness description
            # below at the `D`/`d` boundary of "DEV"/"datasets" under Postgres's actual
            # collation, which orders that boundary differently from Python's
            # case-sensitive `sorted()` used to compute this test's expected order.
            "description": "Daily count of validated DEV datasets inside their own "
            "cadence-anchored window",
            "metrics": [
                {"name": "valid_confd", "color": "#3B82F6", "idx": 1},
                {"name": "valid_in_time", "color": "#22C55E", "idx": 2},
            ],
            "metric_conf": {"time_window_sec": 172800},
        },
        {
            "metric_id": "doc-health-dev",
            "type": "doc-health",
            "title": "Doc Health (DEV)",
            "description": "Daily documentation-completeness check across DEV datasets",
            "metrics": [
                {"name": "total", "color": "#64748B", "idx": 1},
                {"name": "doc_health", "color": "#A855F7", "idx": 2},
            ],
            "metric_conf": {},
        },
    ]

    # Throwaway id used in Step 1c(b) to test absent-id 404.
    _THROWAWAY_ID = "uc5-put-absent-test"
    _throwaway_conf_url = f"/api/v1/spoke/governance/metric/{_THROWAWAY_ID}/attr/conf"

    try:
        # ── Step 1: CDO creates three DEV-scoped daily metrics ────────────────
        # UC5 Imazon Example: "The CDO adds the doc-health metric with a
        # DEV-scoped daily run, supplying the metric_id in the create body."
        # Mirrored across all three built-in active types.
        # spec: USE_CASE_en.md §UC5 §Imazon Example — POST /spoke/governance/metric,
        #       metric_id supplied in body, returns 201.
        # spec: API.md §Metric — POST /spoke/governance/metric creates; 409 METRIC_EXISTS on
        # collision.
        for cfg in metrics_to_create:
            post_resp = await api_client.post(
                "/api/v1/spoke/governance/metric",
                headers=admin_headers,
                json={
                    "metric_id": cfg["metric_id"],
                    "mode": "active",
                    "is_enabled": True,
                    "metric_type": cfg["type"],
                    "title": cfg["title"],
                    "description": cfg["description"],
                    "metrics": cfg["metrics"],
                    "metric_conf": cfg["metric_conf"],
                    "schedule_tier": "daily",
                    "dataset_filter": "origin = 'DEV'",
                },
            )
            assert post_resp.status_code == 201, (
                f"POST /spoke/governance/metric for '{cfg['metric_id']}' expected 201, "
                f"got {post_resp.status_code}: {post_resp.text}. "
                "spec: USE_CASE_en.md §UC5 §Imazon Example."
            )

        # ── Step 1a: the list route orders by description ─────────────────────
        # `description` is one of the four sortable keys of the list route, and
        # the only non-timestamp one besides `title`, so it is the sharpest probe
        # that the server-side sort map is wired past the timestamp default.
        # spec: API.md §Metric — GET /spoke/governance/metric "sortable by
        #       created_at/updated_at/title/description".
        by_description = await api_client.get(
            "/api/v1/spoke/governance/metric",
            headers=admin_headers,
            params={"sort": "description_asc", "limit": 1000},
        )
        assert by_description.status_code == 200, (
            f"GET /spoke/governance/metric?sort=description_asc expected 200, "
            f"got {by_description.status_code}: {by_description.text}. "
            "spec: API.md §Metric."
        )
        created_ids = {cfg["metric_id"] for cfg in metrics_to_create}
        created_descriptions = [
            m["description"] for m in by_description.json()["metrics"] if m["id"] in created_ids
        ]
        assert created_descriptions == sorted(cfg["description"] for cfg in metrics_to_create), (
            "sort=description_asc must return the three created metrics in ascending "
            f"description order; got {created_descriptions}. spec: API.md §Metric."
        )

        # ── Step 1b: Collision rejection ──────────────────────────────────────
        # Re-POSTing with the same metric_id must return 409 METRIC_EXISTS.
        # spec: API.md §Metric — colliding id returns 409 METRIC_EXISTS.
        # spec: API.md §Error Catalogue — error envelope: top-level error_code field.
        collision_cfg = metrics_to_create[0]  # use ingestion-freshness-dev
        collision_resp = await api_client.post(
            "/api/v1/spoke/governance/metric",
            headers=admin_headers,
            json={
                "metric_id": collision_cfg["metric_id"],
                "mode": "active",
                "is_enabled": True,
                "metric_type": collision_cfg["type"],
                "title": collision_cfg["title"],
                "description": collision_cfg["description"],
                "metrics": collision_cfg["metrics"],
                "metric_conf": collision_cfg["metric_conf"],
                "schedule_tier": "daily",
                "dataset_filter": "origin = 'DEV'",
            },
        )
        assert collision_resp.status_code == 409, (
            f"Re-POST of existing metric_id '{collision_cfg['metric_id']}' expected 409, "
            f"got {collision_resp.status_code}: {collision_resp.text}. "
            "spec: API.md §Metric — colliding id returns 409 METRIC_EXISTS."
        )
        assert collision_resp.json().get("error_code") == "METRIC_EXISTS", (
            f"Expected error_code='METRIC_EXISTS' on collision; "
            f"got {collision_resp.json().get('error_code')!r}. "
            "spec: API.md §Error Catalogue."
        )

        # ── Step 1c: PUT replace-only semantics ───────────────────────────────
        # (a) PUT on an existing id → 200, change is reflected on GET.
        # spec: API.md §Metric — PUT .../attr/conf replaces existing definition, returns 200.
        replace_cfg = metrics_to_create[2]  # use doc-health-dev
        replace_url = f"/api/v1/spoke/governance/metric/{replace_cfg['metric_id']}/attr/conf"
        replace_resp = await api_client.put(
            replace_url,
            headers=admin_headers,
            json={
                "mode": "active",
                "is_enabled": True,
                "metric_type": replace_cfg["type"],
                "title": replace_cfg["title"],
                "description": "Updated description for replace-only test",
                "metrics": replace_cfg["metrics"],
                "metric_conf": replace_cfg["metric_conf"],
                "schedule_tier": "daily",
                "dataset_filter": "origin = 'DEV'",
            },
        )
        assert replace_resp.status_code == 200, (
            f"PUT on existing '{replace_cfg['metric_id']}' expected 200, "
            f"got {replace_resp.status_code}: {replace_resp.text}. "
            "spec: API.md §Metric — PUT replaces existing definition, returns 200."
        )
        get_after_replace = await api_client.get(replace_url, headers=admin_headers)
        assert get_after_replace.status_code == 200
        assert (
            get_after_replace.json()["description"] == "Updated description for replace-only test"
        ), (
            "PUT change to 'description' must be reflected on GET. "
            "spec: API.md §Metric."
        )

        # (b) PUT on an absent id (never created) → 404 METRIC_NOT_FOUND.
        # spec: API.md §Metric — PUT returns 404 METRIC_NOT_FOUND when the id is absent
        #       (use POST /spoke/governance/metric to create).
        # Ensure throwaway does not exist before testing.
        await api_client.delete(_throwaway_conf_url, headers=admin_headers)
        absent_put_resp = await api_client.put(
            _throwaway_conf_url,
            headers=admin_headers,
            json={
                "mode": "active",
                "is_enabled": False,
                "metric_type": "doc-health",
                "title": "Should Fail",
                "description": "PUT on absent id must return 404",
                "metrics": [
                    {"name": "total", "color": "#64748B", "idx": 1},
                    {"name": "doc_health", "color": "#A855F7", "idx": 2},
                ],
                "metric_conf": {},
                "schedule_tier": "daily",
                "dataset_filter": "",
            },
        )
        assert absent_put_resp.status_code == 404, (
            f"PUT on absent '{_THROWAWAY_ID}' expected 404 METRIC_NOT_FOUND, "
            f"got {absent_put_resp.status_code}: {absent_put_resp.text}. "
            "spec: API.md §Metric — PUT returns 404 METRIC_NOT_FOUND when id is absent."
        )
        assert absent_put_resp.json().get("error_code") == "METRIC_NOT_FOUND", (
            f"Expected error_code='METRIC_NOT_FOUND'; "
            f"got {absent_put_resp.json().get('error_code')!r}. "
            "spec: API.md §Error Catalogue."
        )

        # ── Step 2: CDO triggers an immediate first run for each metric ───────
        # UC5 Imazon Example: "The CDO triggers an immediate first run rather
        # than waiting for the schedule."
        # spec: USE_CASE_en.md §UC5 §Imazon Example
        # spec: API.md §Metric — POST .../method/run returns 200 with run_id.
        for cfg in metrics_to_create:
            run_resp = await api_client.post(
                f"/api/v1/spoke/governance/metric/{cfg['metric_id']}/method/run",
                headers=admin_headers,
            )
            assert run_resp.status_code == 200, (
                f"POST method/run for '{cfg['metric_id']}' expected 200, "
                f"got {run_resp.status_code}: {run_resp.text}. "
                "spec: USE_CASE_en.md §UC5 §API Mapping."
            )
            assert run_resp.json().get("run_id"), (
                f"Run response for '{cfg['metric_id']}' must carry a non-empty run_id. "
                "spec: USE_CASE_en.md §UC5 §API Mapping."
            )

        # ── Step 3: A week later, trends are pulled for a board update ────────
        # UC5 Imazon Example: "A week later, trends are pulled for a board update"
        # with from=2026-04-19T00:00:00Z&to=2026-04-25T23:59:59Z (one week span).
        # spec: USE_CASE_en.md §UC5 §Imazon Example
        #
        # No exact in-window count assertions — how much of the estate happens to fall
        # inside the declared window is nondeterministic against real-pipeline timing.
        # The windowing contract itself is covered by the spot suite, which seeds
        # controlled timestamps.
        # spec: TESTING.md §Spot vs Api-Wired Integration Tests.
        now = datetime.now(tz=UTC)
        from_ts = (now - timedelta(days=7)).isoformat()
        # +1 day padding to include the run just triggered
        to_ts = (now + timedelta(days=1)).isoformat()
        for cfg in metrics_to_create:
            results_resp = await api_client.get(
                f"/api/v1/spoke/governance/metric/{cfg['metric_id']}/attr/result",
                params={"from": from_ts, "to": to_ts},
                headers=admin_headers,
            )
            assert results_resp.status_code == 200, (
                f"GET attr/result for '{cfg['metric_id']}' expected 200, "
                f"got {results_resp.status_code}: {results_resp.text}."
            )
            results = results_resp.json().get("results", [])
            assert results, (
                f"Expected at least one result row for '{cfg['metric_id']}' after a successful "
                f"run. "
                "spec: USE_CASE_en.md §UC5."
            )
            assert isinstance(results[0]["values"], dict), (
                "result.values must be a dict. "
                "spec: USE_CASE_en.md §UC5 §Built-in active metric types."
            )
            declared_names = {series["name"] for series in cfg["metrics"]}
            assert set(results[0]["values"].keys()) == declared_names, (
                f"'{cfg['metric_id']}' values keys must equal the declared series names "
                f"{declared_names}. "
                "spec: feature/BACKEND.md §Metrics Service — 'the service filters the "
                "dict to the names declared by attr/conf.metrics[] before persisting'."
            )

    finally:
        # ── Step 4: Cleanup ───────────────────────────────────────────────────
        # 204 = deleted; 404 = metric was never created (POST failed) or already gone.
        # Both are acceptable for idempotent teardown.
        for cfg in metrics_to_create:
            del_resp = await api_client.delete(
                f"/api/v1/spoke/governance/metric/{cfg['metric_id']}/attr/conf",
                headers=admin_headers,
            )
            assert del_resp.status_code in (204, 404), (
                f"DELETE '{cfg['metric_id']}' expected 204 or 404, "
                f"got {del_resp.status_code}: {del_resp.text}."
            )
        # Also clean up throwaway id from Step 1c(b) pre-flight delete.
        await api_client.delete(_throwaway_conf_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_uc5_dataset_filter_worked_examples_and_dataset_view(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """UC5 narrative continued: the CDO scopes a metric with the documented clause
    forms, runs it, and opens the metric's Datasets panel to see which datasets met
    the criterion.

    Story steps:
      1. Create a metric whose `dataset_filter` is the tag-membership clause UC3's
         Imazon example uses — the simplest of the two documented forms.
      2. Replace it with a composite clause in the shape the grammar's worked example
         prints — an origin equality AND-ed with a parenthesised tag/glossary-term OR,
         here scoped to the seeded DEV estate.
      3. A clause that names a column outside the grammar is rejected with the
         character position, so the editor can point at the error.
      4. Run the metric, then read `GET .../dataset`: every covered dataset carries a
         `met` verdict, a `last_check_at`, and the envelope reports how fresh the
         scope's attributes are.
      5. The `met` filter narrows that page.

    spec: USE_CASE_en.md §UC5 §Imazon Example — POST /spoke/governance/metric with a
          `dataset_filter`, then POST method/run for an immediate first run.
    spec: API.md §`dataset_filter` grammar — the productions, the depth-1 parenthesised
          AND/OR composition, and the 422 INVALID_DATASET_FILTER position.
    spec: API.md §Metric — GET /spoke/governance/metric/{metric_id}/dataset.
    """
    _METRIC_ID = "uc5-filter-worked-examples"
    conf_url = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/conf"
    run_url = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/method/run"
    dataset_url = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/dataset"

    # Pre-flight: a leftover from an aborted run must not turn step 1 into a 409.
    await api_client.delete(conf_url, headers=admin_headers)

    try:
        # ── Step 1: create with the tag-membership clause ─────────────────────
        # spec: USE_CASE_en.md §UC3 §Imazon Example prints this exact clause for the
        # ontogen conf; §`dataset_filter` grammar states UC5 uses the same grammar.
        create_resp = await api_client.post(
            "/api/v1/spoke/governance/metric",
            headers=admin_headers,
            json={
                "metric_id": _METRIC_ID,
                "mode": "active",
                "is_enabled": True,
                "metric_type": "doc-health",
                "title": "Catalog documentation health",
                "description": "Documentation completeness across the catalog-tagged estate",
                "metrics": [
                    {"name": "total", "color": "#2563EB", "idx": 1},
                    {"name": "doc_health", "color": "#16A34A", "idx": 2},
                ],
                "metric_conf": {},
                "schedule_tier": "daily",
                "dataset_filter": "'urn:li:tag:area:catalog' IN tag_urns",
            },
        )
        assert create_resp.status_code == 201, (
            f"POST /spoke/governance/metric expected 201, got "
            f"{create_resp.status_code}: {create_resp.text}. "
            "spec: USE_CASE_en.md §UC5 §Imazon Example."
        )
        assert create_resp.json()["dataset_filter"] == "'urn:li:tag:area:catalog' IN tag_urns", (
            "the clause must round-trip verbatim — the backend owns the grammar, so no "
            "route rewrites or normalises it. spec: API.md §`dataset_filter` grammar."
        )
        assert [series["idx"] for series in create_resp.json()["metrics"]] == [1, 2], (
            "series descriptors round-trip with their display order. "
            "spec: API.md §Metric — Definition body."
        )

        # ── Step 2: replace with a composite clause in the example's shape ────
        # spec: API.md §`dataset_filter` grammar — `expr := term { (AND|OR) term }`
        # with `term := '(' expr ')'`, the composition its worked example prints.
        composite = (
            "origin = 'DEV' AND ('urn:li:tag:area:catalog' IN tag_urns"
            " OR 'urn:li:glossaryTerm:pii.gdpr' IN glossary_term_urns)"
        )
        put_resp = await api_client.put(
            conf_url,
            headers=admin_headers,
            json={
                "mode": "active",
                "is_enabled": True,
                "metric_type": "doc-health",
                "title": "Catalog documentation health",
                "description": "Documentation completeness across catalog and GDPR datasets",
                "metrics": [
                    {"name": "total", "color": "#2563EB", "idx": 1},
                    {"name": "doc_health", "color": "#16A34A", "idx": 2},
                ],
                "metric_conf": {},
                "schedule_tier": "daily",
                "dataset_filter": composite,
            },
        )
        assert put_resp.status_code == 200, (
            f"PUT attr/conf with the composite clause expected 200, got "
            f"{put_resp.status_code}: {put_resp.text}. "
            "spec: API.md §`dataset_filter` grammar — depth-1 parentheses are accepted."
        )
        assert put_resp.json()["dataset_filter"] == composite

        get_conf = await api_client.get(conf_url, headers=admin_headers)
        assert get_conf.status_code == 200, get_conf.text
        assert get_conf.json()["dataset_filter"] == composite, (
            "the stored clause must read back byte-identical. "
            "spec: API.md §`dataset_filter` grammar."
        )

        # ── Step 3: a clause outside the grammar is refused with its position ──
        # spec: API.md §Error Catalogue — INVALID_DATASET_FILTER, 422, "`detail` carries
        # the character position of the error".
        bad_resp = await api_client.patch(
            conf_url,
            headers=admin_headers,
            json={"dataset_filter": "owner = 'catalog-team'"},
        )
        assert bad_resp.status_code == 422, (
            f"PATCH with an unknown column expected 422, got "
            f"{bad_resp.status_code}: {bad_resp.text}."
        )
        bad_body = bad_resp.json()
        assert bad_body.get("error_code") == "INVALID_DATASET_FILTER", bad_body
        assert "position" in (bad_body.get("detail") or {}), (
            f"the 422 must carry the character position so the editor can point at the "
            f"error; got {bad_body!r}. spec: API.md §Error Catalogue."
        )

        still = await api_client.get(conf_url, headers=admin_headers)
        assert still.json()["dataset_filter"] == composite, (
            "a rejected PATCH must not have altered the stored clause"
        )

        # ── Step 4: run, then open the Datasets panel ─────────────────────────
        run_resp = await api_client.post(run_url, headers=admin_headers)
        assert run_resp.status_code == 200, (
            f"POST method/run expected 200, got {run_resp.status_code}: {run_resp.text}. "
            "spec: USE_CASE_en.md §UC5 §Imazon Example — immediate first run."
        )
        scanned = int(run_resp.json()["detail"]["values"]["total"])

        view_resp = await api_client.get(f"{dataset_url}?limit=1000", headers=admin_headers)
        assert view_resp.status_code == 200, (
            f"GET .../dataset expected 200, got {view_resp.status_code}: {view_resp.text}. "
            "spec: API.md §Metric — GET /spoke/governance/metric/{metric_id}/dataset."
        )
        view = view_resp.json()
        assert view["total_count"] == scanned, (
            f"the Datasets panel must cover the same scope the run measured "
            f"({scanned}); got {view['total_count']}. "
            "spec: feature/BACKEND.md §Metrics Service — one resolver for both."
        )
        assert scanned > 0, (
            "backstop: the composite clause must match at least one seeded dataset, or "
            "the row assertions below are vacuous. A zero here means the attribute sweep "
            "has not run — see spec/TESTING.md §Prerequisites."
        )
        for row in view["datasets"]:
            assert set(row) == {"dataset_urn", "met", "last_check_at", "detail"}, (
                f"row inventory must be exactly the four documented fields; got "
                f"{sorted(row)}. spec: API.md §Metric."
            )
            assert row["met"] in ("true", "false"), (
                f"{row['dataset_urn']} was in scope for the run just made, so it must "
                f"carry a verdict rather than 'unknown'; got {row['met']!r}. "
                "spec: API.md §Metric — 'unknown' = in scope but never evaluated."
            )
            assert row["last_check_at"] is not None, (
                "doc-health has no per-dataset timestamp, so last_check_at falls back to "
                f"the run's measured_at; got null for {row['dataset_urn']}. "
                "spec: API.md §Metric — last_check_at fallback."
            )
        assert "attrs_synced_at" in view, (
            "the envelope must state how fresh the scope's attributes are, so a filter "
            "matching nothing is distinguishable from one whose attributes have not "
            "synced. spec: API.md §Metric."
        )

        # ── Step 5: the met filter narrows the panel ──────────────────────────
        # spec: API.md §Metric — "Repeatable `met` query param (default: all three)".
        served = {row["dataset_urn"]: row["met"] for row in view["datasets"]}
        for state in ("true", "false"):
            filtered = await api_client.get(
                f"{dataset_url}?met={state}&limit=1000", headers=admin_headers
            )
            assert filtered.status_code == 200, filtered.text
            body = filtered.json()
            assert {row["met"] for row in body["datasets"]} <= {state}, (
                f"met={state} must return that state alone; got "
                f"{sorted({row['met'] for row in body['datasets']})}."
            )
            assert body["total_count"] == sum(1 for met in served.values() if met == state), (
                f"met={state}: total_count must count the filtered set, not the scope."
            )
            assert body["attrs_synced_at"] == view["attrs_synced_at"], (
                "attrs_synced_at is scope-relative and must not move with the met "
                "filter. spec: API.md §Metric."
            )

        unknown_page = await api_client.get(
            f"{dataset_url}?met=unknown&limit=1000", headers=admin_headers
        )
        assert unknown_page.status_code == 200, unknown_page.text
        assert unknown_page.json()["datasets"] == [], (
            "every dataset in scope was measured by the run just made, so no dataset "
            "may read 'unknown'. spec: API.md §Metric."
        )
    finally:
        del_resp = await api_client.delete(conf_url, headers=admin_headers)
        assert del_resp.status_code in (204, 404), (
            f"DELETE '{_METRIC_ID}' expected 204 or 404, got "
            f"{del_resp.status_code}: {del_resp.text}."
        )


@pytest.mark.asyncio
async def test_uc5_metric_scoped_to_primary_siblings(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    internal_headers: dict[str, str],
    seeded_sibling_pair: None,
) -> None:
    """UC5 narrative continued: the CDO scopes a metric to one row per logical asset.

    Imazon's catalog exists twice in DataHub — the warehouse table and its modelling
    counterpart are one logical dataset in two platforms, related by DataHub's
    `siblings` aspect. Counted naively, every estate metric double-counts the pair.
    The CDO writes `is_primary = true` so each logical asset is scored once.

    Story steps:
      1. [API-fired setup] The sibling relationship is mirrored into
         `dataset_registry` by the same `datahub-sync` sweep that mirrors tags.
      2. The CDO scopes a metric to the non-leading siblings (`is_primary = false`)
         to see what the naive count was double-counting.
      3. Flipping the clause to `is_primary = true` yields the complement — the
         leader is in scope and its non-leading sibling is not.
      4. A quoted boolean is refused with its character position rather than
         silently matching nothing.

    spec: USE_CASE_en.md §UC5 §Imazon Example — POST /spoke/governance/metric with a
          `dataset_filter`.
    spec: API.md §`dataset_filter` grammar — "`is_primary` | bool | `true` when the
          dataset is the primary member of its DataHub sibling set, or has no
          siblings. `is_primary = true` scopes a filter to one row per logical asset,
          so a metric, ontogen run, or metagen conf counts a dbt model and its
          warehouse table once"; "`is_primary = 'true'` is a syntax error
          (`422 INVALID_DATASET_FILTER`)".
    spec: DATAHUB_INTEGRATION.md §Dataset attribute sync — `is_primary` derives from
          the `siblings` aspect on the sweep's scroll.
    """
    _METRIC_ID = "uc5-sibling-scoped"
    conf_url = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/attr/conf"
    dataset_url = f"/api/v1/spoke/governance/metric/{_METRIC_ID}/dataset"

    await api_client.delete(conf_url, headers=admin_headers)

    try:
        # ── Step 2: scope the metric to the non-leading siblings ──────────────
        create_resp = await api_client.post(
            "/api/v1/spoke/governance/metric",
            headers=admin_headers,
            json={
                "metric_id": _METRIC_ID,
                "mode": "active",
                "is_enabled": False,
                "metric_type": "doc-health",
                "title": "Documentation health — sibling duplicates",
                "description": "What a naive estate count double-counts",
                "metrics": [
                    {"name": "total", "color": "#2563EB", "idx": 1},
                    {"name": "doc_health", "color": "#16A34A", "idx": 2},
                ],
                "metric_conf": {},
                "schedule_tier": None,
                "dataset_filter": "is_primary = false",
            },
        )
        assert create_resp.status_code == 201, (
            f"POST /spoke/governance/metric expected 201, got "
            f"{create_resp.status_code}: {create_resp.text}. "
            "spec: API.md §`dataset_filter` grammar — `bool_col '=' bool` is a "
            "production of the grammar, so the clause is writable."
        )
        assert create_resp.json()["dataset_filter"] == "is_primary = false", (
            "the clause must round-trip verbatim. spec: API.md §`dataset_filter` grammar."
        )

        # ── Step 1 (deferred): the sweep mirrors the aspect into the registry ──
        # [API-fired] The sweep is the DAG's own task; UC5's story has no gesture for
        # it. Poll rather than sleep: the aspect was emitted seconds ago and the
        # scroll the sweep reads is eventually consistent.
        # spec: feature/BACKEND.md §Sync + mapping sweep — step 3 "Dataset attribute
        #       sync"; TESTING.md §Airflow Integration Test Pitfalls — direct activity
        #       call over DAG orchestration.
        deadline = datetime.now(tz=UTC) + timedelta(seconds=180)
        false_urns: set[str] = set()
        while datetime.now(tz=UTC) < deadline:
            sync_resp = await api_client.post(
                "/internal/activities/ingestion/sync", headers=internal_headers, timeout=300.0
            )
            assert sync_resp.status_code == 200, (
                f"POST /internal/activities/ingestion/sync expected 200, got "
                f"{sync_resp.status_code}: {sync_resp.text}."
            )
            view_resp = await api_client.get(f"{dataset_url}?limit=1000", headers=admin_headers)
            assert view_resp.status_code == 200, view_resp.text
            false_urns = {row["dataset_urn"] for row in view_resp.json()["datasets"]}
            if false_urns:
                break

        assert false_urns == {_EDITIONS_URN}, (
            f"`is_primary = false` must select exactly the dataset DataHub marks as a "
            f"non-leading sibling; got {sorted(false_urns)}. A zero here means the "
            "sweep never mirrored the emitted `siblings` aspect. "
            "spec: DATAHUB_INTEGRATION.md §Dataset attribute sync."
        )

        # ── Step 3: the complement — one row per logical asset ────────────────
        patch_resp = await api_client.patch(
            conf_url,
            headers=admin_headers,
            json={"dataset_filter": "is_primary = true"},
        )
        assert patch_resp.status_code == 200, (
            f"PATCH attr/conf with the true-side clause expected 200, got "
            f"{patch_resp.status_code}: {patch_resp.text}."
        )

        true_view = await api_client.get(f"{dataset_url}?limit=1000", headers=admin_headers)
        assert true_view.status_code == 200, true_view.text
        true_urns = {row["dataset_urn"] for row in true_view.json()["datasets"]}
        assert _TITLE_MASTER_URN in true_urns, (
            f"the sibling leader must stay in scope; got {sorted(true_urns)}. "
            "spec: API.md §`dataset_filter` grammar."
        )
        assert _EDITIONS_URN not in true_urns, (
            "the non-leading sibling must drop out — otherwise the clause is a no-op "
            "and the double-count it exists to prevent survives. "
            "spec: API.md §`dataset_filter` grammar."
        )

        # ── Step 4: a quoted boolean is refused with its position ─────────────
        quoted = await api_client.patch(
            conf_url,
            headers=admin_headers,
            json={"dataset_filter": "is_primary = 'true'"},
        )
        assert quoted.status_code == 422, (
            f"a quoted boolean must be a syntax error, not a silently different "
            f"filter; got {quoted.status_code}: {quoted.text}. "
            "spec: API.md §`dataset_filter` grammar."
        )
        assert quoted.json().get("error_code") == "INVALID_DATASET_FILTER", quoted.text
        assert "position" in (quoted.json().get("detail") or {}), (
            f"the 422 must carry the character position so the editor can point at "
            f"the quote; got {quoted.text}. spec: API.md §Error Catalogue."
        )

        unchanged = await api_client.get(conf_url, headers=admin_headers)
        assert unchanged.json()["dataset_filter"] == "is_primary = true", (
            "a rejected PATCH must not have altered the stored clause"
        )
    finally:
        del_resp = await api_client.delete(conf_url, headers=admin_headers)
        assert del_resp.status_code in (204, 404), (
            f"DELETE '{_METRIC_ID}' expected 204 or 404, got "
            f"{del_resp.status_code}: {del_resp.text}."
        )
