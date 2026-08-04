"""Spot tests for internal activity endpoints.

Concerns covered — for each domain (ingestion, validation, metagen, metrics, ontogen):
- POST /internal/activities/ingestion/list-active — returns source IDs for given tier
- POST /internal/activities/ingestion/run — executes for a source_id (per-source model)
- POST /internal/activities/ingestion/sync — reconcile all sources against DataHub;
  three REST tests cover the step-2 coverage-outcome table and the summary counter
  semantics reachable from outside: zero coverage reported (with both exclusion gates
  seeded), the second-sweep no-op invariant, and the prune invariant across the
  not-evaluated / matched-nothing outcomes
- POST /internal/activities/{domain}/list-active — returns list of IDs for given tier
- POST /internal/activities/{domain}/run — executes for a metric_id

The last section drives ``IngestionService.sync()`` — the exact call the sync activity
endpoint wraps — directly against a real DB session with a stubbed DataHub client, for
the summary rows a REST caller cannot reach: the platform-absent gate, the CLI-wrapper
no-double-count rule, the positive prune case, the five state-change counters, and the
bounded/escaped degradation log record. Handcrafting the URN list is what buys exact
equalities there instead of ``>=`` deltas; running in-process is what puts the sweep's
own log records within reach of ``caplog``.
spec: TESTING.md §Spot integration tests §Boundary — 'a spot test may call dataspoke
Python directly (e.g., a backend service or a workflow stub) **or** call the API over HTTP'.

Auth: X-Internal-Token header (internal_headers fixture).
Internal routes are mounted WITHOUT the /api/v1 prefix (see src/api/main.py line 271).
"""
# spec: BACKEND.md §Tier-DAG selection
# spec: BACKEND.md §Ingestion Service §Active-custom run pipeline
# spec: BACKEND.md §Sync + mapping sweep (step 2 outcome table, §Sweep summary)
# spec: BACKEND.md §Validation Service / §Metrics Service

import json
import logging
import os
import uuid
from contextlib import suppress
from typing import Any

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.backend.ingestion.service import IngestionService
from src.shared.models.ingestion import MAX_REASON_CHARS, build_matcher_checked
from tests.integration.util import dataspoke_db

# The sweep's own logger — the record the writer-supplied degradation reason reaches.
_SYNC_LOGGER = "src.backend.ingestion.service"

_FAIL_TAIL: frozenset[str] = frozenset({"fail", "failed", "failure", "error", "errored"})

# This module drives IngestionService.sync() in-process but does not own the
# `datahub-api` peripheral_health row the sweep writes as a side effect — see the
# fixture docstring in tests/integration/spot/conftest.py.
pytestmark = pytest.mark.usefixtures("silence_api_health_report")

# In-cluster cluster-DNS address of the dummy-data postgres (resolvable inside
# the cluster; mode-independent — recipes are consumed in-cluster). Populated by
# install.sh; required (no default) so an unset env fails loud rather than
# guessing a namespace.
# spec: TESTING.md — example_db on the dev-env host.
_PG_HOST_PORT = os.environ["DATASPOKE_DEV_DUMMY_DATA_POSTGRES_HOST_PORT"]
# Secret reference: provisioned K8s Secret dataspoke-source-cred-dummy-data-pg, key 'password'.
# spec: SECRET_RESOLUTION.md §Name prefix policy — DNS-label-safe (hyphens, no underscores).
_SECRET_REF_HOURLY = "${dummy-data-pg__password}"
_SECRET_REF_DAILY = "${dummy-data-pg__password}"

# Per-module dummy-data seed: re-seed catalog schema in PG and ingest into DataHub
# before this module's tests run (autoused by tests/integration/conftest.py).
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})

# Imazon dataset URN — guaranteed to exist in DataHub after reset-seed
_TEST_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"


@pytest.mark.asyncio
async def test_ingestion_list_active_hourly(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    """POST /internal/activities/ingestion/list-active returns source IDs for the given
    tier and excludes source IDs assigned to a different tier.

    Ingestion is per-source (per-source model). The activity returns a list of source IDs
    (UUIDs) whose ACTIVE_CUSTOM_MANAGED sources have schedule_tier matching the requested tier.

    spec: BACKEND.md §Tier-DAG selection — "the periodic DAG that runs at a given tier
    fetches only the configs whose schedule_tier matches the DAG's tier"
    spec: API.md §Ingestion — POST /spoke/ingestion/sources (ACTIVE_CUSTOM_MANAGED)
    """
    # Create two sources: one hourly (target tier), one daily (must be excluded).
    create_hourly_resp = await api_client.post(
        "/api/v1/spoke/ingestion/sources",
        headers=admin_headers,
        json={
            "mode": "ACTIVE_CUSTOM_MANAGED",
            "name": "spot-test-hourly-source",
            "schedule": "0 * * * *",
            "recipe": {
                "source": {
                    "type": "postgres",
                    "config": {
                        "host_port": _PG_HOST_PORT,
                        "database": "example_db",
                        "username": "postgres",
                        "password": _SECRET_REF_HOURLY,
                        "env": "DEV",
                        "schema_pattern": {"allow": ["^catalog$"]},
                    },
                }
            },
        },
    )
    assert create_hourly_resp.status_code == 201, (
        f"Create hourly source failed: {create_hourly_resp.status_code} {create_hourly_resp.text}"
    )
    hourly_source_id = create_hourly_resp.json()["id"]

    create_daily_resp = await api_client.post(
        "/api/v1/spoke/ingestion/sources",
        headers=admin_headers,
        json={
            "mode": "ACTIVE_CUSTOM_MANAGED",
            "name": "spot-test-daily-source",
            "schedule": "0 0 * * *",
            "recipe": {
                "source": {
                    "type": "postgres",
                    "config": {
                        "host_port": _PG_HOST_PORT,
                        "database": "example_db",
                        "username": "postgres",
                        "password": _SECRET_REF_DAILY,
                        "env": "DEV",
                        "schema_pattern": {"allow": ["^orders$"]},
                    },
                }
            },
        },
    )
    assert create_daily_resp.status_code == 201, (
        f"Create daily source failed: {create_daily_resp.status_code} {create_daily_resp.text}"
    )
    daily_source_id = create_daily_resp.json()["id"]

    try:
        resp = await api_client.post(
            "/internal/activities/ingestion/list-active",
            headers=internal_headers,
            json={"tier": "hourly"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert isinstance(body, list)
        # Hourly-tier source ID must appear in the list
        assert hourly_source_id in body, (
            f"Expected hourly source {hourly_source_id!r} in list-active tier=hourly, got: {body}. "
            "spec: BACKEND.md §Tier-DAG selection"
        )
        # Daily-tier source ID must NOT appear in hourly results
        assert daily_source_id not in body, (
            f"Tier isolation violated: daily source {daily_source_id!r} appeared in hourly list. "
            "spec: BACKEND.md §Tier-DAG selection"
        )
    finally:
        await api_client.delete(
            f"/api/v1/spoke/ingestion/sources/{hourly_source_id}", headers=admin_headers
        )
        await api_client.delete(
            f"/api/v1/spoke/ingestion/sources/{daily_source_id}", headers=admin_headers
        )


@pytest.mark.asyncio
async def test_ingestion_run_activity(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    """POST /internal/activities/ingestion/run executes ingestion for a source_id.

    Pre-condition: an ACTIVE_CUSTOM_MANAGED source must exist. Creates one, runs the
    activity with dry_run=True (no DataHub emission), then cleans up.

    spec: BACKEND.md §Active-custom run pipeline — response shape: {run_id, status, ...}
    spec: API.md §Ingestion — POST /internal/activities/ingestion/run takes {source_id, dry_run}
    """
    # Create an ACTIVE_CUSTOM_MANAGED source for the catalog schema
    create_resp = await api_client.post(
        "/api/v1/spoke/ingestion/sources",
        headers=admin_headers,
        json={
            "mode": "ACTIVE_CUSTOM_MANAGED",
            "name": "spot-test-run-activity-source",
            "schedule": "0 0 * * *",
            "recipe": {
                "source": {
                    "type": "postgres",
                    "config": {
                        "host_port": _PG_HOST_PORT,
                        "database": "example_db",
                        "username": "postgres",
                        "password": "${dummy-data-pg__password}",
                        "env": "DEV",
                        "schema_pattern": {"allow": ["^catalog$"]},
                    },
                }
            },
        },
    )
    assert create_resp.status_code == 201, (
        f"Create source failed: {create_resp.status_code} {create_resp.text}"
    )
    source_id = create_resp.json()["id"]

    try:
        resp = await api_client.post(
            "/internal/activities/ingestion/run",
            headers=internal_headers,
            json={"source_id": source_id, "dry_run": True},
        )
        assert resp.status_code == 200, (
            f"ingestion/run expected 200, got {resp.status_code}: {resp.text}"
        )
        body = resp.json()
        # spec: BACKEND.md §Active-custom run pipeline — response carries run_id and status
        assert "run_id" in body and "status" in body, (
            f"Expected both 'run_id' and 'status' in ingestion run response, "
            f"got: {list(body.keys())}. spec: BACKEND.md §Active-custom run pipeline"
        )
        assert body["status"].lower() not in _FAIL_TAIL, (
            f"run unexpectedly returned fail-tail status {body['status']!r} — "
            "secret resolution or downstream connectivity may be broken"
        )
    finally:
        await api_client.delete(
            f"/api/v1/spoke/ingestion/sources/{source_id}", headers=admin_headers
        )


@pytest.mark.asyncio
async def test_metagen_run_activity_dry(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
) -> None:
    """POST /internal/activities/metagen/run executes the global metagen pipeline (dry_run).

    spec: BACKEND.md §Metadata Generation Service — singleton run, response carries status.
    spec: USE_CASE_en.md §UC4: Metadata Generation — dry_run permitted regardless of is_enabled.
    """
    resp = await api_client.post(
        "/internal/activities/metagen/run",
        headers=internal_headers,
        json={"dry_run": True},
    )

    assert resp.status_code == 200
    body = resp.json()
    # spec: BACKEND.md §Metadata Generation Service — MetagenRunResponse carries status
    assert "status" in body, (
        f"Expected 'status' in metagen run response, got: {list(body.keys())}"
    )


@pytest.mark.asyncio
async def test_metrics_list_active_hourly(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    """POST /internal/activities/metrics/list-active returns metric IDs for the requested
    tier and excludes metric IDs assigned to a different tier.

    spec: BACKEND.md §Tier-DAG selection — tier filter applies to metric_definitions
    """
    # spec: BACKEND.md §Tier-DAG selection
    # spec: API.md §Metric (/spoke/governance/metric) — POST /spoke/governance/metric is the
    # explicit create (metric_id in body); PUT /metric/{id}/attr/conf replaces an existing one.
    metric_id_hourly = "spot-list-freshness-hourly"
    metric_id_daily = "spot-list-freshness-daily"
    conf_hourly = f"/api/v1/spoke/governance/metric/{metric_id_hourly}/attr/conf"
    conf_daily = f"/api/v1/spoke/governance/metric/{metric_id_daily}/attr/conf"

    _common_conf = {
        "mode": "active",
        "is_enabled": True,
        "metric_type": "ingestion-freshness",
        "title": "Spot List Freshness",
        "description": "Spot test metric for tier-selection.",
        "metrics": ["total", "ingested_in_time"],
        "metric_conf": {"time_window_sec": 86400},
        "dataset_filter": {"dataset_urns": [_TEST_URN]},
    }

    # Create an enabled metric in the target tier (hourly).
    await api_client.post(
        "/api/v1/spoke/governance/metric",
        headers=admin_headers,
        json={"metric_id": metric_id_hourly, **_common_conf, "schedule_tier": "hourly"},
    )

    # Create an enabled metric in a DIFFERENT tier (daily) — must NOT appear in hourly results.
    await api_client.post(
        "/api/v1/spoke/governance/metric",
        headers=admin_headers,
        json={"metric_id": metric_id_daily, **_common_conf, "schedule_tier": "daily"},
    )

    resp = await api_client.post(
        "/internal/activities/metrics/list-active",
        headers=internal_headers,
        json={"tier": "hourly"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    # Hourly-tier metric must appear
    assert metric_id_hourly in body, (
        f"Expected metric '{metric_id_hourly}' in hourly metrics list, got: {body}"
    )
    # Daily-tier metric must NOT appear in hourly results
    assert metric_id_daily not in body, (
        f"Tier isolation violated: '{metric_id_daily}' (daily tier) appeared in hourly list"
    )

    # Cleanup
    await api_client.delete(conf_hourly, headers=admin_headers)
    await api_client.delete(conf_daily, headers=admin_headers)


@pytest.mark.asyncio
async def test_metrics_run_activity(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    """POST /internal/activities/metrics/run executes measurement for a metric_id.

    spec: BACKEND.md §Metrics Service — response carries run_id + status.
    """
    # spec: BACKEND.md §Metrics Service
    # spec: API.md §Metric (/spoke/governance/metric) — POST /spoke/governance/metric is the
    # explicit create (metric_id in body); PUT /metric/{id}/attr/conf replaces an existing one.
    metric_id = "spot-activity-freshness"
    conf_url = f"/api/v1/spoke/governance/metric/{metric_id}/attr/conf"

    # Create the metric config (explicit create via POST per UC5 flow).
    await api_client.post(
        "/api/v1/spoke/governance/metric",
        headers=admin_headers,
        json={
            "metric_id": metric_id,
            "mode": "active",
            "is_enabled": True,
            "metric_type": "ingestion-freshness",
            "title": "Spot Activity Freshness",
            "description": "Spot test metric description.",
            "metrics": ["total", "ingested_in_time"],
            "metric_conf": {"time_window_sec": 86400},
            "schedule_tier": "hourly",
            "dataset_filter": {"dataset_urns": [_TEST_URN]},
        },
    )

    resp = await api_client.post(
        "/internal/activities/metrics/run",
        headers=internal_headers,
        json={"metric_id": metric_id},
    )

    assert resp.status_code == 200
    body = resp.json()
    # spec: BACKEND.md §Metrics Service — response must carry both run_id and status
    assert "run_id" in body and "status" in body, (
        f"Expected both 'run_id' and 'status' in metrics run response, got: {list(body.keys())}"
    )
    assert body["status"].lower() not in _FAIL_TAIL, (
        f"run unexpectedly returned fail-tail status {body['status']!r} — "
        "secret resolution or downstream connectivity may be broken"
    )

    # Cleanup
    await api_client.delete(conf_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_ontogen_run_activity_dry(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
) -> None:
    """POST /internal/activities/ontogen/run executes ontogen inference (dry_run).

    spec: BACKEND.md §Ontology Generation Service — response carries status.
    """
    # spec: BACKEND.md §Ontology Generation Service
    resp = await api_client.post(
        "/internal/activities/ontogen/run",
        headers=internal_headers,
        json={"dry_run": True},
    )

    assert resp.status_code == 200
    body = resp.json()
    # spec: BACKEND.md §Ontology Generation Service — response must carry status
    assert "status" in body, (
        f"Expected 'status' in ontogen run response, got: {list(body.keys())}"
    )


# ── POST /internal/activities/ingestion/sync ──────────────────────────────────
#
# The mapping sweep's step-2 outcome table and the sweep-summary counter semantics.
# Every counter here is estate-wide, so each test reads the counter before its own
# mutation and asserts the *delta* it caused — a global equality (e.g.
# sources_zero_coverage == 0) is not a property of a shared dev cluster, where an
# unrelated registered source may legitimately be in any of the three outcomes.
#
# spec: BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes and prune invariant
# spec: BACKEND.md §Sync + mapping sweep §Sweep summary


@pytest.mark.asyncio
async def test_ingestion_sync_reports_zero_coverage_for_derivable_no_match_source(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    """A derivable, well-formed pattern set that matches nothing is counted + reported —
    and each of the two exclusion gates keeps its own case out of the same counter.

    Seeds every side of the classification, all on REST-reachable sources:

    - **counted** — a covering source (schema_pattern ``^catalog$``, which the seeded Imazon
      catalog datasets satisfy) and a non-covering one (a schema name no dataset carries).
      The covering source is the backstop: it proves DataHub did hold postgres datasets for
      this sweep and that the matcher ran, so the non-covering source's empty match set is
      the reported condition rather than an empty estate.
    - **not counted, ``has_selection_patterns`` gate** — a well-formed postgres source
      carrying none of the four selection-pattern keys. It legitimately covers nothing, so
      it is the 'Evaluated, no derivable patterns' row, whose Signal is 'none'.
    - **not counted, platform-has-datasets gate** — a ``snowflake`` source with a
      well-formed ``dataset_pattern`` while the estate holds no snowflake dataset. It was
      never offered a candidate name, so its empty match set is not a defect signal.

    Without the two negative cases an implementation that dropped either gate still passes:
    both would simply be counted alongside the non-covering source.

    spec: BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes and prune invariant —
        'Evaluated, derivable, matched nothing | the patterns ran and no dataset matched |
        pruned | warning naming the source and its platform, and sources_zero_coverage,
        **when DataHub holds datasets for that platform**'.
    spec: BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes and prune invariant —
        'Evaluated, no derivable patterns | the recipe is well-formed and carries none of
        the four selection-pattern keys | pruned | none'.
    spec: BACKEND.md §Sync + mapping sweep §Sweep summary — 'sources_zero_coverage …
        report a condition … so each stays non-zero for as long as the affected sources do'.
    (The platform gate's rationale is spelled out in the build_matcher docstring §Caller
        contract — 'a source.type naming no platform present in the estate is offered no
        candidate name at all, and that case is reported nowhere'; the binding rule is the
        spec table cell cited above.)
    spec: API.md §Ingestion — GET /spoke/ingestion/sources/{id}/datasets returns the
        current mapping for a source.
    """
    suffix = uuid.uuid4().hex[:8]

    # Baseline reading taken before this test's sources exist, so the assertion below
    # measures only the condition this test introduces.
    baseline_resp = await api_client.post(
        "/internal/activities/ingestion/sync",
        headers=internal_headers,
        json={},
    )
    assert baseline_resp.status_code == 200, (
        f"baseline sync expected 200, got {baseline_resp.status_code}: {baseline_resp.text}"
    )
    baseline = baseline_resp.json()
    assert "sources_zero_coverage" in baseline, (
        f"sync summary must carry sources_zero_coverage; got: {sorted(baseline)}. "
        "spec: BACKEND.md §Sync + mapping sweep §Sweep summary."
    )

    covering_resp = await api_client.post(
        "/api/v1/spoke/ingestion/sources",
        headers=admin_headers,
        json={
            "mode": "ACTIVE_CUSTOM_MANAGED",
            "name": f"spot-sync-covering-{suffix}",
            "schedule": "0 0 * * *",
            "recipe": {
                "source": {
                    "type": "postgres",
                    "config": {
                        "host_port": _PG_HOST_PORT,
                        "database": "example_db",
                        "username": "postgres",
                        "password": "${dummy-data-pg__password}",
                        "env": "DEV",
                        "schema_pattern": {"allow": ["^catalog$"]},
                    },
                }
            },
        },
    )
    assert covering_resp.status_code == 201, (
        f"Create covering source failed: {covering_resp.status_code} {covering_resp.text}"
    )
    covering_id = covering_resp.json()["id"]

    empty_schema = f"spot_no_such_schema_{suffix}"
    non_covering_id: str | None = None
    # Ids created inside the try block, torn down in the finally regardless of where a
    # failure lands.
    negative_case_ids: list[str] = []

    try:
        non_covering_resp = await api_client.post(
            "/api/v1/spoke/ingestion/sources",
            headers=admin_headers,
            json={
                "mode": "ACTIVE_CUSTOM_MANAGED",
                "name": f"spot-sync-zero-coverage-{suffix}",
                "schedule": "0 0 * * *",
                "recipe": {
                    "source": {
                        "type": "postgres",
                        "config": {
                            "host_port": _PG_HOST_PORT,
                            "database": "example_db",
                            "username": "postgres",
                            "password": "${dummy-data-pg__password}",
                            "env": "DEV",
                            # Well-formed and derivable, but no dataset carries this schema.
                            "schema_pattern": {"allow": [f"^{empty_schema}$"]},
                        },
                    }
                },
            },
        )
        assert non_covering_resp.status_code == 201, (
            f"Create zero-coverage source failed: "
            f"{non_covering_resp.status_code} {non_covering_resp.text}"
        )
        non_covering_id = non_covering_resp.json()["id"]

        sync_resp = await api_client.post(
            "/internal/activities/ingestion/sync",
            headers=internal_headers,
            json={},
        )
        assert sync_resp.status_code == 200, (
            f"sync expected 200, got {sync_resp.status_code}: {sync_resp.text}"
        )
        summary = sync_resp.json()

        # Backstop: the covering source mapped real datasets this sweep, so postgres
        # datasets were enumerated and the matcher ran over them.
        covering_datasets = await api_client.get(
            f"/api/v1/spoke/ingestion/sources/{covering_id}/datasets",
            headers=admin_headers,
            params={"limit": 100},
        )
        assert covering_datasets.status_code == 200, covering_datasets.text
        covering_body = covering_datasets.json()
        assert covering_body["total_count"] > 0, (
            "The covering source (schema_pattern ^catalog$) mapped no dataset, so this "
            "sweep saw no postgres datasets at all and the zero-coverage assertion below "
            "would be meaningless. spec: BACKEND.md §Sync + mapping sweep step 2 — "
            "'derivation = matched (authority medium)'."
        )
        assert {d["derivation"] for d in covering_body["datasets"]} == {"matched"}, (
            f"Recipe-derived mappings carry derivation 'matched'; got "
            f"{sorted({d['derivation'] for d in covering_body['datasets']})}. "
            "spec: BACKEND.md §Sync + mapping sweep step 2."
        )

        # The other side of the matcher: the non-covering source mapped nothing.
        non_covering_datasets = await api_client.get(
            f"/api/v1/spoke/ingestion/sources/{non_covering_id}/datasets",
            headers=admin_headers,
            params={"limit": 100},
        )
        assert non_covering_datasets.status_code == 200, non_covering_datasets.text
        assert non_covering_datasets.json()["total_count"] == 0, (
            f"A source whose schema_pattern allows only {empty_schema!r} must map no "
            f"dataset; got {non_covering_datasets.json()['total_count']}."
        )

        # The condition is reported on the wire. Scoped as a delta over the baseline:
        # the estate-wide counter also reports any pre-existing zero-coverage source.
        assert summary["sources_zero_coverage"] >= baseline["sources_zero_coverage"] + 1, (
            f"sources_zero_coverage must count the derivable-but-matched-nothing source "
            f"introduced by this test: baseline={baseline['sources_zero_coverage']}, "
            f"after={summary['sources_zero_coverage']}. "
            "spec: BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes — "
            "'Evaluated, derivable, matched nothing … sources_zero_coverage'."
        )
        # It is a coverage signal, not a degradation: the pattern set was readable.
        assert summary["sources_pattern_degraded"] <= baseline["sources_pattern_degraded"], (
            f"A well-formed pattern set that matched nothing is the Evaluated outcome and "
            f"must not be counted as degraded: baseline="
            f"{baseline['sources_pattern_degraded']}, after="
            f"{summary['sources_pattern_degraded']}. "
            "spec: BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes — the "
            "Not-evaluated row is reached only when the pattern set could not be read."
        )

        # ── The two exclusion gates: neither case may move the same counter ────
        # Both are added between two consecutive readings of an otherwise unchanged
        # estate, so the equality below isolates their contribution.
        no_patterns_resp = await api_client.post(
            "/api/v1/spoke/ingestion/sources",
            headers=admin_headers,
            json={
                "mode": "ACTIVE_CUSTOM_MANAGED",
                "name": f"spot-sync-no-patterns-{suffix}",
                "schedule": "0 0 * * *",
                "recipe": {
                    "source": {
                        "type": "postgres",
                        "config": {
                            # Well-formed, and carries none of the four selection-pattern
                            # keys — the 'Evaluated, no derivable patterns' row.
                            "host_port": _PG_HOST_PORT,
                            "database": "example_db",
                            "username": "postgres",
                            "password": "${dummy-data-pg__password}",
                            "env": "DEV",
                        },
                    }
                },
            },
        )
        assert no_patterns_resp.status_code == 201, (
            f"Create pattern-less source failed: "
            f"{no_patterns_resp.status_code} {no_patterns_resp.text}"
        )
        negative_case_ids.append(no_patterns_resp.json()["id"])

        absent_platform_resp = await api_client.post(
            "/api/v1/spoke/ingestion/sources",
            headers=admin_headers,
            json={
                "mode": "ACTIVE_CUSTOM_MANAGED",
                "name": f"spot-sync-absent-platform-{suffix}",
                "schedule": "0 0 * * *",
                "recipe": {
                    "source": {
                        # snowflake: the Imazon estate holds postgres and kafka datasets
                        # only, so this source is offered no candidate name at all.
                        "type": "snowflake",
                        "config": {
                            "account_id": "spot-not-a-real-account",
                            # Well-formed and derivable — the gate under test is the
                            # platform's absence from the estate, not the pattern's shape.
                            "dataset_pattern": {"allow": [f"^{empty_schema}\\..*$"]},
                        },
                    }
                },
            },
        )
        assert absent_platform_resp.status_code == 201, (
            f"Create absent-platform source failed: "
            f"{absent_platform_resp.status_code} {absent_platform_resp.text}"
        )
        negative_case_ids.append(absent_platform_resp.json()["id"])

        negatives_resp = await api_client.post(
            "/internal/activities/ingestion/sync",
            headers=internal_headers,
            json={},
        )
        assert negatives_resp.status_code == 200, (
            f"sync expected 200, got {negatives_resp.status_code}: {negatives_resp.text}"
        )
        negatives = negatives_resp.json()

        # Assumption: between the `summary` sweep and this one, the only ingestion-source
        # changes are the two sources created just above — nothing else on the shared
        # cluster entered or left a coverage outcome in that window.
        assert negatives["sources_zero_coverage"] == summary["sources_zero_coverage"], (
            f"Neither a source with no selection-pattern key nor a source whose platform "
            f"the estate does not hold may be counted as zero coverage: "
            f"before={summary['sources_zero_coverage']}, "
            f"after={negatives['sources_zero_coverage']}. "
            "spec: BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes — 'Evaluated, "
            "no derivable patterns | … | none' and 'sources_zero_coverage, **when DataHub "
            "holds datasets for that platform**'."
        )
        # Neither is a degradation either: both recipes parse and both pattern sets (where
        # one exists) are well-shaped.
        assert negatives["sources_pattern_degraded"] == summary["sources_pattern_degraded"], (
            f"A well-formed recipe is never the Not-evaluated outcome, with or without "
            f"selection patterns: before={summary['sources_pattern_degraded']}, "
            f"after={negatives['sources_pattern_degraded']}. "
            "spec: BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes."
        )
    finally:
        await api_client.delete(
            f"/api/v1/spoke/ingestion/sources/{covering_id}", headers=admin_headers
        )
        if non_covering_id is not None:
            await api_client.delete(
                f"/api/v1/spoke/ingestion/sources/{non_covering_id}", headers=admin_headers
            )
        for negative_id in negative_case_ids:
            await api_client.delete(
                f"/api/v1/spoke/ingestion/sources/{negative_id}", headers=admin_headers
            )


@pytest.mark.asyncio
async def test_ingestion_sync_second_consecutive_sweep_is_a_zero_no_op(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    """``datasets_mapped`` returns 0 on a second consecutive sweep over an unchanged estate.

    A source covering the seeded Imazon catalog datasets is created first, so the first
    sweep genuinely writes rows — the per-counter backstop (``first[counter] > 0``) that
    makes the zero on the second sweep evidence of "nothing changed" rather than of a
    counter that is never non-zero here.

    **Scope of the loop.** Only counters this REST-reachable estate can drive non-zero are
    asserted; the other three state-change counters are proven at the same-file stub tier
    (``test_sync_summary_counts_state_changes_not_rows_examined``) instead of being left as
    green no-ops here:

    - ``pipeline_links`` — needs a dataset carrying ``systemMetadata.pipelineName``. The
      only REST way to stamp one is a real ``ACTIVE_CUSTOM_MANAGED`` run, and that same run
      records ``derivation='emitted'`` rows for exactly the URNs it stamped; the step-3
      upsert's ``derivation != 'emitted'`` guard then filters every one of them, so the
      counter cannot be driven off zero from outside.
    - ``registry_inserted`` — needs a dataset URN DataHub holds and ``dataset_registry``
      does not; every seeded URN is already registered by the first sweep of the run.
    - ``events_mirrored`` — needs a DataHub execution request, which the dev DataHub has no
      executor to produce.

    spec: BACKEND.md §Sync + mapping sweep §Sweep summary — 'Most counters report state
        changes, not rows examined: datasets_mapped, pipeline_links, events_mirrored,
        sources_removed and the registry_* counters increment only on an insert, a removal
        or a genuine transition … A second consecutive sweep over an unchanged estate
        returns zero for all of those.'
    """
    suffix = uuid.uuid4().hex[:8]

    create_resp = await api_client.post(
        "/api/v1/spoke/ingestion/sources",
        headers=admin_headers,
        json={
            "mode": "ACTIVE_CUSTOM_MANAGED",
            "name": f"spot-sync-noop-{suffix}",
            "schedule": "0 0 * * *",
            "recipe": {
                "source": {
                    "type": "postgres",
                    "config": {
                        "host_port": _PG_HOST_PORT,
                        "database": "example_db",
                        "username": "postgres",
                        "password": "${dummy-data-pg__password}",
                        "env": "DEV",
                        "schema_pattern": {"allow": ["^catalog$"]},
                    },
                }
            },
        },
    )
    assert create_resp.status_code == 201, (
        f"Create source failed: {create_resp.status_code} {create_resp.text}"
    )
    source_id = create_resp.json()["id"]

    try:
        first_resp = await api_client.post(
            "/internal/activities/ingestion/sync",
            headers=internal_headers,
            json={},
        )
        assert first_resp.status_code == 200, (
            f"first sync expected 200, got {first_resp.status_code}: {first_resp.text}"
        )
        first = first_resp.json()

        second_resp = await api_client.post(
            "/internal/activities/ingestion/sync",
            headers=internal_headers,
            json={},
        )
        assert second_resp.status_code == 200, (
            f"second sync expected 200, got {second_resp.status_code}: {second_resp.text}"
        )
        second = second_resp.json()

        for counter in ("datasets_mapped",):
            # Per-counter backstop: this counter genuinely moved on the first sweep, so
            # the zero below is a no-op reading rather than a counter that is never
            # non-zero in this estate.
            assert first[counter] > 0, (
                f"The first sweep after creating a source covering the catalog schema "
                f"must move {counter}; got {first[counter]}. Without this the zero "
                "asserted next passes against an implementation that never counts. "
                "spec: BACKEND.md §Sync + mapping sweep step 2."
            )
            assert second[counter] == 0, (
                f"{counter} must be 0 on a second consecutive sweep over an unchanged "
                f"estate; got {second[counter]} (first sweep reported {first[counter]}). "
                "spec: BACKEND.md §Sync + mapping sweep §Sweep summary — 'A second "
                "consecutive sweep over an unchanged estate returns zero for all of those.'"
            )
    finally:
        await api_client.delete(
            f"/api/v1/spoke/ingestion/sources/{source_id}", headers=admin_headers
        )


@pytest.mark.asyncio
async def test_ingestion_sync_keeps_matched_rows_when_patterns_cannot_be_evaluated(
    api_client: httpx.AsyncClient,
    internal_headers: dict[str, str],
    admin_headers: dict[str, str],
) -> None:
    """Prune only on evidence: a not-evaluated pattern set keeps its stored matched rows.

    Walks one source from a matching state into each of the two step-2 no-match outcomes
    that differ on pruning, in the order that makes the preservation meaningful:

    1. **Matched** — a covering ``schema_pattern`` stores matched rows for the seeded
       Imazon catalog datasets (the rows whose survival is at stake).
    2. **Not evaluated** — the recipe is patched so the deciding key holds a bare string.
       The rows must survive, the source must be counted in ``sources_pattern_degraded``,
       and it must NOT be counted in ``sources_zero_coverage``.
    3. **Evaluated, derivable, matched nothing** — the recipe is patched to a well-formed
       pattern matching no dataset. The rows are now pruned. This is the backstop for
       phase 2: without it, "the rows survived" could equally mean the sweep never prunes.

    spec: BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes and prune invariant —
        'Not evaluated | … the deciding selection-pattern key is wrongly shaped … | left in
        place | warning naming the source and what could not be read;
        sources_pattern_degraded' versus 'Evaluated, derivable, matched nothing | the
        patterns ran and no dataset matched | pruned | … sources_zero_coverage'.
    spec: BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes — 'a source that
        declares no coverage and a source whose declared coverage could not be read are
        different facts, and only the first is an assertion about the estate.'
    spec: BACKEND.md §Sync + mapping sweep §Sweep summary — 'sources_zero_coverage and
        sources_pattern_degraded each report a condition'.
    """
    suffix = uuid.uuid4().hex[:8]

    create_resp = await api_client.post(
        "/api/v1/spoke/ingestion/sources",
        headers=admin_headers,
        json={
            "mode": "ACTIVE_CUSTOM_MANAGED",
            "name": f"spot-sync-degraded-{suffix}",
            "schedule": "0 0 * * *",
            "recipe": {
                "source": {
                    "type": "postgres",
                    "config": {
                        "host_port": _PG_HOST_PORT,
                        "database": "example_db",
                        "username": "postgres",
                        "password": "${dummy-data-pg__password}",
                        "env": "DEV",
                        "schema_pattern": {"allow": ["^catalog$"]},
                    },
                }
            },
        },
    )
    assert create_resp.status_code == 201, (
        f"Create source failed: {create_resp.status_code} {create_resp.text}"
    )
    source_id = create_resp.json()["id"]

    try:
        # ── Phase 1: evaluated + matched → rows stored ────────────────────────
        healthy_resp = await api_client.post(
            "/internal/activities/ingestion/sync",
            headers=internal_headers,
            json={},
        )
        assert healthy_resp.status_code == 200, (
            f"sync expected 200, got {healthy_resp.status_code}: {healthy_resp.text}"
        )
        healthy = healthy_resp.json()

        stored_resp = await api_client.get(
            f"/api/v1/spoke/ingestion/sources/{source_id}/datasets",
            headers=admin_headers,
            params={"limit": 100},
        )
        assert stored_resp.status_code == 200, stored_resp.text
        stored_urns = {d["dataset_urn"] for d in stored_resp.json()["datasets"]}
        assert stored_urns, (
            "The covering source must map at least one dataset before its recipe is "
            "degraded — otherwise there is nothing whose preservation can be proven. "
            "spec: BACKEND.md §Sync + mapping sweep step 2."
        )

        # ── Phase 2: not evaluated → rows left in place ───────────────────────
        # A bare string where an allow/deny mapping belongs. The shape is reachable over
        # REST: per API.md §Ingestion the write routes reject only a bad recipe *shape* and
        # malformed ${name__key} references, so writer-supplied pattern values reach the
        # sweep unchecked — which is why the sweep has to carry this outcome at all.
        degrade_resp = await api_client.patch(
            f"/api/v1/spoke/ingestion/sources/{source_id}",
            headers=admin_headers,
            json={
                "recipe": {
                    "source": {
                        "type": "postgres",
                        "config": {
                            "host_port": _PG_HOST_PORT,
                            "database": "example_db",
                            "username": "postgres",
                            "password": "${dummy-data-pg__password}",
                            "env": "DEV",
                            "schema_pattern": "oops",
                        },
                    }
                }
            },
        )
        assert degrade_resp.status_code == 200, (
            f"PATCH to a wrongly-shaped schema_pattern expected 200, got "
            f"{degrade_resp.status_code}: {degrade_resp.text}"
        )

        degraded_resp = await api_client.post(
            "/internal/activities/ingestion/sync",
            headers=internal_headers,
            json={},
        )
        assert degraded_resp.status_code == 200, (
            f"sync over a degraded recipe must still succeed for the rest of the estate; "
            f"got {degraded_resp.status_code}: {degraded_resp.text}"
        )
        degraded = degraded_resp.json()

        surviving_resp = await api_client.get(
            f"/api/v1/spoke/ingestion/sources/{source_id}/datasets",
            headers=admin_headers,
            params={"limit": 100},
        )
        assert surviving_resp.status_code == 200, surviving_resp.text
        surviving_urns = {d["dataset_urn"] for d in surviving_resp.json()["datasets"]}
        assert surviving_urns >= stored_urns, (
            f"Stored matched rows must be left in place when the pattern set could not be "
            f"evaluated: had {sorted(stored_urns)}, now {sorted(surviving_urns)}. "
            "spec: BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes — 'Not "
            "evaluated | … | left in place'."
        )

        assert degraded["sources_pattern_degraded"] >= (
            healthy["sources_pattern_degraded"] + 1
        ), (
            f"The source whose deciding selection-pattern key is wrongly shaped must be "
            f"counted: before={healthy['sources_pattern_degraded']}, "
            f"after={degraded['sources_pattern_degraded']}. "
            "spec: BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes — 'Not "
            "evaluated | … the deciding selection-pattern key is wrongly shaped … | "
            "sources_pattern_degraded'."
        )
        # Assumption this equality rests on: between the `healthy` and `degraded` sweeps
        # the only ingestion-source write is this test's PATCH, so no unrelated source on
        # the shared cluster entered or left a coverage outcome in that window.
        assert degraded["sources_zero_coverage"] == healthy["sources_zero_coverage"], (
            f"A not-evaluated source must not also be reported as zero coverage: "
            f"before={healthy['sources_zero_coverage']}, "
            f"after={degraded['sources_zero_coverage']}. "
            "spec: BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes — "
            "sources_zero_coverage belongs to the 'Evaluated, derivable, matched nothing' "
            "row, which requires the patterns to have run."
        )

        # ── Phase 3: evaluated, derivable, matched nothing → rows pruned ──────
        # Backstop for phase 2: the prune path is live, so the survival above is the
        # documented outcome difference rather than a sweep that never prunes.
        empty_schema = f"spot_no_such_schema_{suffix}"
        restore_resp = await api_client.patch(
            f"/api/v1/spoke/ingestion/sources/{source_id}",
            headers=admin_headers,
            json={
                "recipe": {
                    "source": {
                        "type": "postgres",
                        "config": {
                            "host_port": _PG_HOST_PORT,
                            "database": "example_db",
                            "username": "postgres",
                            "password": "${dummy-data-pg__password}",
                            "env": "DEV",
                            "schema_pattern": {"allow": [f"^{empty_schema}$"]},
                        },
                    }
                }
            },
        )
        assert restore_resp.status_code == 200, (
            f"PATCH to a well-formed non-matching pattern expected 200, got "
            f"{restore_resp.status_code}: {restore_resp.text}"
        )

        pruned_sync_resp = await api_client.post(
            "/internal/activities/ingestion/sync",
            headers=internal_headers,
            json={},
        )
        assert pruned_sync_resp.status_code == 200, pruned_sync_resp.text
        pruned_sync = pruned_sync_resp.json()

        pruned_resp = await api_client.get(
            f"/api/v1/spoke/ingestion/sources/{source_id}/datasets",
            headers=admin_headers,
            params={"limit": 100},
        )
        assert pruned_resp.status_code == 200, pruned_resp.text
        remaining_urns = {d["dataset_urn"] for d in pruned_resp.json()["datasets"]}
        assert remaining_urns & stored_urns == set(), (
            f"A pattern set that ran and matched nothing is evidence, so its stored "
            f"matched rows must be pruned; {sorted(remaining_urns & stored_urns)} "
            "survived. spec: BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes — "
            "'Evaluated, derivable, matched nothing | … | pruned'."
        )
        assert pruned_sync["sources_zero_coverage"] >= (
            degraded["sources_zero_coverage"] + 1
        ), (
            f"Once the pattern set is readable again and matches nothing, the source moves "
            f"into the zero-coverage row: before={degraded['sources_zero_coverage']}, "
            f"after={pruned_sync['sources_zero_coverage']}. "
            "spec: BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes."
        )
    finally:
        await api_client.delete(
            f"/api/v1/spoke/ingestion/sources/{source_id}", headers=admin_headers
        )


# ── IngestionService.sync() — the summary rows REST cannot reach ──────────────
#
# The tests below drive the same ``sync()`` call the activity endpoint above wraps, but
# against a handcrafted DataHub estate: a stubbed client whose ``list_ingestion_sources``
# / ``enumerate_datasets`` / ``get_pipeline_names`` are exactly what the test declares.
# Step 2 derives each dataset's name and platform by parsing the URN string, so a
# handcrafted URN list is full control over what the matcher is offered — which is what
# lets these assert **equalities** on estate-wide counters instead of ``>=`` deltas.
#
# Three step-2 rows and the state-change counters are only provable here:
#   - the platform-absent gate (a source.type naming no platform in the estate),
#   - the CLI-wrapper no-double-count rule (needs a `cli-` wrapper, which the dev-env
#     DataHub has no executor to create),
#   - 'Evaluated, no derivable patterns' → rows are **pruned** (the positive prune case),
#   - `pipeline_links` / `sources_synced`, which need a `systemMetadata.pipelineName`
#     stamp that is not accompanied by an `emitted` row — unreachable over REST, where the
#     only stamping path is an ACTIVE_CUSTOM_MANAGED run that writes both.
#
# Both tests reset ingestion sources before and after so the estate is exactly what they
# declare. Their sweeps also run the registry reconcile over the handcrafted URN set,
# which soft-flags the other seeded URNs `datahub_registered=false` until the next real
# sweep — the same side effect the neighbouring stub-driven sync tests
# (test_ingestion_cli_pipeline_inheritance.py) already carry.
#
# spec: TESTING.md §Spot integration tests §Boundary — direct Python or REST, whichever
#     proves the concern most directly.
# spec: BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes and prune invariant
# spec: BACKEND.md §Sync + mapping sweep §Sweep summary

# Two real Imazon catalog dataset URNs (postgres) and one snowflake URN that exists in no
# estate — the platform whose absence the gate turns on.
_SYNC_DS_A = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_SYNC_DS_B = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.editions,DEV)"
_SYNC_DS_SNOWFLAKE = (
    "urn:li:dataset:(urn:li:dataPlatform:snowflake,warehouse.public.shipments,PROD)"
)

# A schema no seeded dataset carries, so a pattern anchored on it is derivable, readable
# and matches nothing.
_SYNC_EMPTY_SCHEMA = "spot_sync_no_such_schema"


class _StubDataHubForSync:
    """Minimal DataHub stub exposing only what ``IngestionService.sync()`` touches.

    Every attribute is mutable so a test can walk one estate through several consecutive
    sweeps — adding a source, adding a dataset, rewriting a recipe, publishing an
    execution request — and read the summary each time. ``execution_requests`` maps a
    source URN to the ``listExecutionRequests`` payload DataHub would return for it;
    a source absent from the mapping has no run history.
    """

    def __init__(
        self,
        sources: list[dict[str, Any]],
        datasets: list[str],
        pipeline_names: dict[str, str] | None = None,
        execution_requests: dict[str, list[dict[str, Any]]] | None = None,
    ) -> None:
        self.sources = sources
        self.datasets = datasets
        self.pipeline_names = pipeline_names or {}
        self.execution_requests = execution_requests or {}

    async def list_ingestion_sources(self) -> list[dict[str, Any]]:
        return self.sources

    async def enumerate_datasets(self) -> list[str]:
        return self.datasets

    async def get_pipeline_names(self, urns: list[str]) -> dict[str, str | None]:
        # Mirror the real client contract (src/shared/datahub/client.py): an entry for
        # EVERY input URN, None where no pipelineName is stamped.
        return {u: self.pipeline_names.get(u) for u in urns}

    async def list_execution_requests(self, source_urn: str) -> list[dict[str, Any]]:
        return self.execution_requests.get(source_urn, [])


async def _matched_urns_for(async_session: AsyncSession, source_urn: str) -> set[str]:
    """Return the ``derivation='matched'`` dataset URNs stored for one DataHub source URN."""
    result = await async_session.execute(
        text(
            "SELECT d.dataset_urn FROM dataspoke.ingestion_source_dataset d "
            "JOIN dataspoke.ingestion_source s ON s.id = d.source_id "
            "WHERE s.datahub_source_urn = :urn AND d.derivation = 'matched'"
        ),
        {"urn": source_urn},
    )
    return {row[0] for row in result.all()}


@pytest.mark.asyncio
async def test_sync_zero_coverage_gates_and_pattern_less_prune(
    async_session: AsyncSession,
) -> None:
    """The two ``sources_zero_coverage`` exclusion gates, and the positive prune case.

    One handcrafted estate walked through five consecutive sweeps. Because
    ``reset_ingestion_sources`` empties the table first, every source in the count is one
    this test declared, so each reading is an exact equality:

    1. A covering source (``^catalog$``) and a non-covering one (a schema no dataset
       carries) → ``sources_zero_coverage == 1``. The positive leg: the counter does fire,
       exactly once, for the one source in the matched-nothing row.
    2. Add a **CLI wrapper** of the non-covering source, mirroring its recipe → still
       ``1``. A wrapper mirrors its parent's recipe, so counting it would report one
       misconfiguration twice.
    3. Add a **snowflake** source with a well-formed ``dataset_pattern`` while the estate
       holds no snowflake dataset → still ``1``. It was offered no candidate name.
    4. Add a snowflake dataset to the estate (nothing else changes) → ``2``. The backstop
       for step 3: the source *is* eligible for the counter, and it was the platform's
       absence — not its pattern — that kept it out.
    5. Rewrite the covering source's recipe to a well-formed one carrying none of the four
       selection-pattern keys → its stored ``matched`` rows are **pruned**, and it is not
       counted (Signal 'none'). The rows asserted present in step 1 are what makes this a
       prune rather than a source that never mapped anything.

    spec: BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes and prune invariant —
        'Evaluated, no derivable patterns | the recipe is well-formed and carries none of
        the four selection-pattern keys | **pruned** | none'.
    spec: BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes and prune invariant —
        'Evaluated, derivable, matched nothing | … | pruned | … sources_zero_coverage,
        **when DataHub holds datasets for that platform**'.
    spec: BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes — 'It is counted once
        per registered source — a CLI wrapper mirrors its parent's recipe, so counting it
        too would report one misconfiguration twice.'
    (The platform gate's rationale is spelled out in the build_matcher docstring §Caller
        contract — 'a source.type naming no platform present in the estate is offered no
        candidate name at all, and that case is reported nowhere'; the binding rule is the
        '**when DataHub holds datasets for that platform**' cell cited above.)
    """
    await dataspoke_db.reset_ingestion_sources()

    covering_urn = "urn:li:dataHubIngestionSource:" + str(uuid.uuid4())
    zero_coverage_urn = "urn:li:dataHubIngestionSource:" + str(uuid.uuid4())
    wrapper_urn = "urn:li:dataHubIngestionSource:cli-" + uuid.uuid4().hex
    snowflake_urn = "urn:li:dataHubIngestionSource:" + str(uuid.uuid4())

    covering_source = {
        "urn": covering_urn,
        "name": "spot-sync-covering",
        "recipe": json.dumps(
            {
                "source": {
                    "type": "postgres",
                    "config": {"schema_pattern": {"allow": ["^catalog$"]}},
                }
            }
        ),
        "schedule": None,
        "executor_id": "default",
    }
    zero_coverage_source = {
        "urn": zero_coverage_urn,
        "name": "spot-sync-zero-coverage",
        "recipe": json.dumps(
            {
                "source": {
                    "type": "postgres",
                    "config": {"schema_pattern": {"allow": [f"^{_SYNC_EMPTY_SCHEMA}$"]}},
                }
            }
        ),
        "schedule": None,
        "executor_id": "default",
    }
    # DataHub's CLI wrapper: cli- URN, CLI executor, recipe mirroring the parent's and
    # carrying top-level pipeline_name = the parent's registered source URN.
    wrapper_source = {
        "urn": wrapper_urn,
        "name": "[CLI] postgres",
        "recipe": json.dumps(
            {
                "source": {
                    "type": "postgres",
                    "config": {"schema_pattern": {"allow": [f"^{_SYNC_EMPTY_SCHEMA}$"]}},
                },
                "pipeline_name": zero_coverage_urn,
            }
        ),
        "schedule": None,
        "executor_id": "__datahub_cli_ingestion",
    }
    snowflake_source = {
        "urn": snowflake_urn,
        "name": "spot-sync-absent-platform",
        "recipe": json.dumps(
            {
                "source": {
                    "type": "snowflake",
                    # Well-formed and derivable; matches neither the postgres names nor
                    # the snowflake name added in phase 4.
                    "config": {"dataset_pattern": {"allow": [r"^analytics\..*$"]}},
                }
            }
        ),
        "schedule": None,
        "executor_id": "default",
    }

    stub = _StubDataHubForSync(
        sources=[covering_source, zero_coverage_source],
        datasets=[_SYNC_DS_A, _SYNC_DS_B],
    )
    service = IngestionService(datahub=stub, db=async_session)  # type: ignore[arg-type]

    try:
        # ── 1: the counter fires exactly once, for the matched-nothing source ──
        first = await service.sync()
        assert first["sources_zero_coverage"] == 1, (
            f"Exactly the one derivable-but-matched-nothing source must be counted; got "
            f"{first['sources_zero_coverage']}. "
            "spec: BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes."
        )
        covering_rows = await _matched_urns_for(async_session, covering_urn)
        assert covering_rows == {_SYNC_DS_A, _SYNC_DS_B}, (
            f"The covering source must map both catalog datasets as 'matched'; got "
            f"{sorted(covering_rows)}. spec: BACKEND.md §Sync + mapping sweep step 2."
        )
        assert await _matched_urns_for(async_session, zero_coverage_urn) == set(), (
            "The non-covering source must map nothing — otherwise it is not in the "
            "matched-nothing row at all."
        )

        # ── 2: a CLI wrapper mirroring its parent's recipe is not counted again ──
        stub.sources = [covering_source, zero_coverage_source, wrapper_source]
        with_wrapper = await service.sync()
        wrapper_lookup = await async_session.execute(
            text(
                "SELECT s.parent_source_id, p.datahub_source_urn "
                "FROM dataspoke.ingestion_source s "
                "LEFT JOIN dataspoke.ingestion_source p ON p.id = s.parent_source_id "
                "WHERE s.datahub_source_urn = :urn"
            ),
            {"urn": wrapper_urn},
        )
        wrapper_row = wrapper_lookup.one_or_none()
        assert wrapper_row is not None, (
            "Backstop: the wrapper must actually be stored, or 'not counted twice' is "
            "only 'not stored'. spec: BACKEND.md §Sync + mapping sweep step 1 Pass B."
        )
        assert wrapper_row[1] == zero_coverage_urn, (
            f"Backstop: the wrapper must be linked to the zero-coverage source it mirrors "
            f"(parent urn {zero_coverage_urn!r}); got {wrapper_row[1]!r}. Unlinked, it "
            "would be excluded by the wrapper rule for the wrong reason. "
            "spec: BACKEND.md §Sync + mapping sweep step 1 Pass B."
        )
        assert with_wrapper["sources_zero_coverage"] == 1, (
            f"A CLI wrapper mirrors its parent's recipe and must not be counted again; got "
            f"{with_wrapper['sources_zero_coverage']}. "
            "spec: BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes — 'counted "
            "once per registered source'."
        )

        # ── 3: a source whose platform the estate does not hold is not counted ──
        stub.sources = [
            covering_source,
            zero_coverage_source,
            wrapper_source,
            snowflake_source,
        ]
        with_absent_platform = await service.sync()
        assert with_absent_platform["sources_zero_coverage"] == 1, (
            f"A snowflake source is offered no candidate name while the estate holds no "
            f"snowflake dataset, so its empty match set is not a defect signal; got "
            f"{with_absent_platform['sources_zero_coverage']}. "
            "spec: BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes — "
            "'sources_zero_coverage, **when DataHub holds datasets for that platform**'."
        )

        # ── 4: backstop — give that platform a dataset and the same source counts ──
        stub.datasets = [_SYNC_DS_A, _SYNC_DS_B, _SYNC_DS_SNOWFLAKE]
        with_snowflake_dataset = await service.sync()
        assert with_snowflake_dataset["sources_zero_coverage"] == 2, (
            f"Once DataHub holds a snowflake dataset the same source enters the "
            f"matched-nothing row; got {with_snowflake_dataset['sources_zero_coverage']}. "
            "Without this the phase-3 exclusion could equally mean the source was never "
            "eligible. spec: BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes."
        )
        assert await _matched_urns_for(async_session, snowflake_urn) == set(), (
            "The snowflake source's dataset_pattern must still match nothing — the only "
            "thing phase 4 changed is that its platform now has a candidate."
        )

        # ── 5: 'Evaluated, no derivable patterns' → stored rows are pruned ──────
        assert await _matched_urns_for(async_session, covering_urn) == {
            _SYNC_DS_A,
            _SYNC_DS_B,
        }, "Backstop: the rows about to be pruned must still be stored going into phase 5."
        covering_source["recipe"] = json.dumps(
            {"source": {"type": "postgres", "config": {"host_port": "pg:5432"}}}
        )
        pattern_less = await service.sync()
        assert await _matched_urns_for(async_session, covering_urn) == set(), (
            "A well-formed recipe carrying none of the four selection-pattern keys "
            "declares no coverage, and coverage that cannot be inferred is never assumed — "
            "its stored matched rows must be pruned. "
            "spec: BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes — 'Evaluated, "
            "no derivable patterns | … | pruned | none'."
        )
        assert pattern_less["sources_zero_coverage"] == 2, (
            f"A pattern-less source carries Signal 'none' and must not be added to the "
            f"zero-coverage count; got {pattern_less['sources_zero_coverage']} (was 2 "
            "before its recipe changed). "
            "spec: BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes."
        )
        assert pattern_less["sources_pattern_degraded"] == 0, (
            f"A well-formed pattern-less recipe is not a degradation; got "
            f"{pattern_less['sources_pattern_degraded']}. "
            "spec: BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes — the "
            "Not-evaluated row needs a recipe or pattern that could not be read."
        )
    finally:
        await dataspoke_db.reset_ingestion_sources()


@pytest.mark.asyncio
async def test_sync_summary_counts_state_changes_not_rows_examined(
    async_session: AsyncSession,
) -> None:
    """All five state-change counters fall to zero on an unchanged next sweep, while
    ``sources_synced`` repeats its non-zero reading.

    A stubbed estate is what makes this provable: one registered DATAHUB_MANAGED source,
    two catalog datasets, and a ``systemMetadata.pipelineName`` stamp on each naming that
    source. Over REST the only way to stamp a pipelineName is an ACTIVE_CUSTOM_MANAGED run,
    and that same run writes ``derivation='emitted'`` rows for exactly the URNs it stamped,
    which the step-3 upsert's ``derivation != 'emitted'`` guard then filters — so
    ``pipeline_links`` can never leave zero from outside and the no-op assertion would be a
    green no-op there. The remaining three counters are equally unreachable from outside:
    ``events_mirrored`` needs DataHub to hold a terminal execution request,
    ``registry_inserted`` a dataset DataSpoke has never seen, and ``sources_removed`` a
    registered source disappearing from DataHub.

    One estate walked through eight sweeps, one variable per phase. Each counter is
    asserted non-zero on the phase whose single change should move it (the backstop) and
    zero on the next, unchanged phase:

    1. Baseline: 2 datasets mapped, 2 pipeline links created; ``sources_synced`` == 1.
    2. Unchanged → ``datasets_mapped`` and ``pipeline_links`` fall to 0, while
       ``sources_synced`` repeats 1 (the steady-state counterexample).
    3. Add a dataset DataSpoke's registry has never held → ``registry_inserted`` == 1.
    4. Unchanged → ``registry_inserted`` == 0.
    5. Publish one terminal ``SUCCESS`` execution request → ``events_mirrored`` == 1.
    6. Unchanged (the same execution request still listed) → ``events_mirrored`` == 0,
       because dedup keys on the execution-request URN, not on the sweep.
    7. Add a second registered source → ``sources_synced`` == 2, ``sources_removed`` == 0.
    8. Drop that source from DataHub → ``sources_removed`` == 1; the sweep after it → 0.

    spec: BACKEND.md §Sync + mapping sweep §Sweep summary — 'datasets_mapped,
        pipeline_links, events_mirrored, sources_removed and the registry_* counters
        increment only on an insert, a removal or a genuine transition (for pipeline_links,
        a new link or a matched → pipeline_name upgrade …). A second consecutive sweep over
        an unchanged estate returns zero for all of those.'
    spec: BACKEND.md §Sync + mapping sweep §Sweep summary — 'sources_synced reports how
        many DATAHUB_MANAGED rows were mirrored, counting inserts and updates alike, so an
        unchanged estate reports the same non-zero value on every sweep'.
    spec: BACKEND.md §Sync + mapping sweep step 3 — a dataset's pipelineName awards
        derivation='pipeline_name' to the source whose datahub_source_urn equals it.
    spec: BACKEND.md §Sync + mapping sweep step 4 — 'Identity / dedup = execution-request
        URN. One DataSpoke event per execution request, **upserted** by its URN … so
        repeated syncs and status transitions are idempotent (no per-sync event growth).'
    spec: BACKEND.md §Sync + mapping sweep step 4 — status table: 'SUCCESS, SUCCEEDED
        (cross-version) | INGESTION.COMPLETE'.
    spec: BACKEND.md §Sync + mapping sweep step 1 — DATAHUB_MANAGED rows whose source URN
        is no longer in DataHub are removed.
    """
    await dataspoke_db.reset_ingestion_sources()

    source_urn = "urn:li:dataHubIngestionSource:" + str(uuid.uuid4())
    registered_source = {
        "urn": source_urn,
        "name": "spot-sync-state-changes",
        "recipe": json.dumps(
            {
                "source": {
                    "type": "postgres",
                    "config": {"schema_pattern": {"allow": ["^catalog$"]}},
                }
            }
        ),
        "schedule": None,
        "executor_id": "default",
    }
    # A second registered source, added and then removed, so the removal does not take
    # the rest of the estate with it. Its recipe is well-formed and carries none of the
    # four selection-pattern keys, so it declares no coverage and maps nothing.
    removable_urn = "urn:li:dataHubIngestionSource:" + str(uuid.uuid4())
    removable_source = {
        "urn": removable_urn,
        "name": "spot-sync-removable",
        "recipe": json.dumps(
            {"source": {"type": "postgres", "config": {"host_port": "pg:5432"}}}
        ),
        "schedule": None,
        "executor_id": "default",
    }
    # A dataset DataSpoke's registry has never held. Its schema segment is unique, so it
    # matches no source's patterns and changes nothing but the registry.
    novel_dataset = (
        "urn:li:dataset:(urn:li:dataPlatform:postgres,"
        f"example_db.spot_sync_registry_{uuid.uuid4().hex}.probe,DEV)"
    )
    # One terminal SUCCESS execution request, carrying the stable URN the dedup keys on.
    execution_request_urn = "urn:li:dataHubExecutionRequest:" + uuid.uuid4().hex
    execution_request = {
        "urn": execution_request_urn,
        "status": "SUCCESS",
        "startTimeMs": 1_700_000_000_000,
        "requestedAt": 1_700_000_000_000,
        "durationMs": 4200,
    }

    stub = _StubDataHubForSync(
        sources=[registered_source],
        datasets=[_SYNC_DS_A, _SYNC_DS_B],
        # DataHub stamps the registered source's URN on the aspects its run emits.
        pipeline_names={_SYNC_DS_A: source_urn, _SYNC_DS_B: source_urn},
    )
    service = IngestionService(datahub=stub, db=async_session)  # type: ignore[arg-type]

    try:
        # ── 1 & 2: mapping and pipeline links move, then fall to zero ──────────
        first = await service.sync()
        second = await service.sync()

        for counter, expected_first in (("datasets_mapped", 2), ("pipeline_links", 2)):
            # Per-counter backstop: the counter genuinely moved on the first sweep, so the
            # zero below is a no-op reading and not a counter that never fires.
            assert first[counter] == expected_first, (
                f"The first sweep over two freshly-mapped, freshly-stamped datasets must "
                f"report {counter}={expected_first}; got {first[counter]}. "
                "spec: BACKEND.md §Sync + mapping sweep steps 2-3."
            )
            assert second[counter] == 0, (
                f"{counter} reports state changes, not rows examined, so a second "
                f"consecutive sweep over an unchanged estate must report 0; got "
                f"{second[counter]}. spec: BACKEND.md §Sync + mapping sweep §Sweep summary."
            )

        # The counterexample: a steady-state reading repeats instead of falling to zero.
        assert first["sources_synced"] == 1, (
            f"The one mirrored DATAHUB_MANAGED source must be counted; got "
            f"{first['sources_synced']}."
        )
        assert second["sources_synced"] == first["sources_synced"], (
            f"sources_synced counts inserts and updates alike, so an unchanged estate "
            f"reports the same non-zero value on every sweep: first="
            f"{first['sources_synced']}, second={second['sources_synced']}. "
            "spec: BACKEND.md §Sync + mapping sweep §Sweep summary."
        )

        # ── 3 & 4: registry_inserted fires on a genuinely new dataset, then zero ─
        stub.datasets = [_SYNC_DS_A, _SYNC_DS_B, novel_dataset]
        with_new_dataset = await service.sync()
        assert with_new_dataset["registry_inserted"] == 1, (
            f"Exactly the one dataset the registry has never held must be inserted; got "
            f"{with_new_dataset['registry_inserted']}. "
            "spec: BACKEND.md §Sync + mapping sweep §Sweep summary."
        )
        unchanged_dataset = await service.sync()
        assert unchanged_dataset["registry_inserted"] == 0, (
            f"registry_inserted counts inserts, so re-enumerating the same estate must "
            f"report 0; got {unchanged_dataset['registry_inserted']}. "
            "spec: BACKEND.md §Sync + mapping sweep §Sweep summary."
        )

        # ── 5 & 6: events_mirrored fires once per execution-request URN ────────
        stub.execution_requests = {source_urn: [execution_request]}
        with_run = await service.sync()
        assert with_run["events_mirrored"] == 1, (
            f"One terminal SUCCESS execution request must mirror one INGESTION.COMPLETE; "
            f"got {with_run['events_mirrored']}. "
            "spec: BACKEND.md §Sync + mapping sweep step 4."
        )
        unchanged_run = await service.sync()
        assert unchanged_run["events_mirrored"] == 0, (
            f"Dedup keys on the execution-request URN, not on the sweep, so listing the "
            f"same request again must mirror nothing; got "
            f"{unchanged_run['events_mirrored']}. "
            "spec: BACKEND.md §Sync + mapping sweep step 4 — 'repeated syncs and status "
            "transitions are idempotent (no per-sync event growth)'."
        )
        # Read the side effect back, not just the counter: exactly one event carries this
        # execution-request URN after two sweeps that both listed it.
        mirrored_rows = await async_session.execute(
            text(
                "SELECT count(*) FROM dataspoke.events "
                "WHERE entity_type = 'ingestion_source' "
                "AND event_type = 'INGESTION.COMPLETE' "
                "AND detail->>'execution_request_urn' = :urn"
            ),
            {"urn": execution_request_urn},
        )
        assert mirrored_rows.scalar_one() == 1, (
            "Exactly one event row may exist per execution-request URN across repeated "
            "sweeps. spec: BACKEND.md §Sync + mapping sweep step 4."
        )

        # ── 7 & 8: sources_removed fires on the removal, then falls to zero ────
        stub.sources = [registered_source, removable_source]
        with_second_source = await service.sync()
        assert with_second_source["sources_synced"] == 2, (
            f"Backstop: the second registered source must actually be mirrored before its "
            f"removal can be counted; got sources_synced="
            f"{with_second_source['sources_synced']}."
        )
        assert with_second_source["sources_removed"] == 0, (
            f"Nothing left DataHub in this phase, so sources_removed must be 0; got "
            f"{with_second_source['sources_removed']}."
        )

        stub.sources = [registered_source]
        with_removal = await service.sync()
        assert with_removal["sources_removed"] == 1, (
            f"A DATAHUB_MANAGED row whose source URN is no longer in DataHub must be "
            f"removed and counted once; got {with_removal['sources_removed']}. "
            "spec: BACKEND.md §Sync + mapping sweep step 1."
        )
        after_removal = await service.sync()
        assert after_removal["sources_removed"] == 0, (
            f"sources_removed counts removals, so the next sweep over the now-stable "
            f"estate must report 0; got {after_removal['sources_removed']}. "
            "spec: BACKEND.md §Sync + mapping sweep §Sweep summary."
        )
    finally:
        with suppress(Exception):
            await async_session.rollback()
            await async_session.execute(
                text(
                    "DELETE FROM dataspoke.events "
                    "WHERE detail->>'execution_request_urn' = :urn"
                ),
                {"urn": execution_request_urn},
            )
            await async_session.execute(
                text("DELETE FROM dataspoke.dataset_registry WHERE dataset_urn = :urn"),
                {"urn": novel_dataset},
            )
            await async_session.commit()
        await dataspoke_db.reset_ingestion_sources()


@pytest.mark.asyncio
async def test_sync_degradation_log_is_bounded_and_escaped_and_the_counter_persists(
    async_session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The sweep's ``ingestion_sync_pattern_not_derivable`` record is one bounded line,
    and ``sources_pattern_degraded`` persists while the source stays degraded.

    Two concerns, both at the sweep — the site the spec sentence describes. ``sync()``
    runs in-process here against a real DB session with a stubbed DataHub, so ``caplog``
    reaches the sweep's own logger; the same assertions made against
    ``build_matcher``'s convenience log would leave this call site unpinned (reverting
    its ``%r`` to ``%s`` would still pass).

    The estate holds three registered sources, and the healthy one is what keeps the
    counter from being a headcount:

      - ``^catalog$`` — well-formed and covering; must not be counted.
      - ``schema_pattern: {"allow": "not-a-list"}`` — ``AllowDenyPattern`` construction
        raises a multi-line pydantic error, so the reason genuinely contains ``\\n``
        (asserted before the record is inspected). A line-based collector would
        otherwise read the tail of writer-supplied recipe text as a forged record.
      - a ``table_pattern`` carrying an invalid group name built from 200 000
        characters, which ``re`` quotes back verbatim — a reason two orders of
        magnitude past the bound (also asserted first).

    ``sources_pattern_degraded`` is then read on a **second** sweep over the unchanged
    estate: it reports a *condition*, not a transition, so it must still read 2 rather
    than falling to zero the way the state-change counters do.

    spec: BACKEND.md §Sync + mapping sweep step 2 §Trust boundary on writer-supplied
        patterns — 'The reason that log line reports is derived from recipe content and
        is therefore itself untrusted, so it is bounded in length and escaped before it
        reaches a log record: a writer cannot forge log structure or grow a record
        without limit.'
    spec: BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes and prune invariant
        — the Not-evaluated row is reached when 'the deciding selection-pattern key is
        wrongly shaped' or 'a declared pattern does not compile', signalled by a
        'warning naming the source and what could not be read; sources_pattern_degraded'.
    spec: BACKEND.md §Sync + mapping sweep §Sweep summary — 'sources_zero_coverage and
        sources_pattern_degraded each report a **condition** … so each stays non-zero
        for as long as the affected sources do.'
    """
    await dataspoke_db.reset_ingestion_sources()

    healthy_urn = "urn:li:dataHubIngestionSource:" + str(uuid.uuid4())
    newline_urn = "urn:li:dataHubIngestionSource:" + str(uuid.uuid4())
    oversized_urn = "urn:li:dataHubIngestionSource:" + str(uuid.uuid4())

    newline_config = {"schema_pattern": {"allow": "not-a-list"}}
    oversized_config = {"table_pattern": {"allow": ["(?P<a-" + "a" * 200_000 + ">x)"]}}

    # Backstops on the fixtures: assert the reasons really carry a newline and really
    # exceed the bound, so the record assertions below are not trivially true.
    _, newline_reason = build_matcher_checked(
        {"source": {"type": "postgres", "config": newline_config}}
    )
    assert newline_reason is not None and "\n" in newline_reason, (
        "Backstop: this fixture must put a raw newline in the degradation reason, or the "
        f"one-line assertion below proves nothing. Got {newline_reason!r}."
    )
    _, oversized_reason = build_matcher_checked(
        {"source": {"type": "postgres", "config": oversized_config}}
    )
    assert oversized_reason is not None and len(oversized_reason) > MAX_REASON_CHARS * 10, (
        "Backstop: this fixture must produce a reason far past the bound, or the "
        f"boundedness assertion below is vacuous. Got len={len(oversized_reason or '')}."
    )

    sources = [
        {
            "urn": healthy_urn,
            "name": "spot-sync-degrade-healthy",
            "recipe": json.dumps(
                {
                    "source": {
                        "type": "postgres",
                        "config": {"schema_pattern": {"allow": ["^catalog$"]}},
                    }
                }
            ),
            "schedule": None,
            "executor_id": "default",
        },
        {
            "urn": newline_urn,
            "name": "spot-sync-degrade-newline",
            "recipe": json.dumps({"source": {"type": "postgres", "config": newline_config}}),
            "schedule": None,
            "executor_id": "default",
        },
        {
            "urn": oversized_urn,
            "name": "spot-sync-degrade-oversized",
            "recipe": json.dumps({"source": {"type": "postgres", "config": oversized_config}}),
            "schedule": None,
            "executor_id": "default",
        },
    ]

    service = IngestionService(
        datahub=_StubDataHubForSync(  # type: ignore[arg-type]
            sources=sources,
            datasets=[_SYNC_DS_A, _SYNC_DS_B],
        ),
        db=async_session,
    )

    try:
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger=_SYNC_LOGGER):
            first = await service.sync()

        assert first["sources_pattern_degraded"] == 2, (
            f"Exactly the two sources whose patterns could not be read must be counted — "
            f"the well-formed covering source must not; got "
            f"{first['sources_pattern_degraded']}. "
            "spec: BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes."
        )

        records = [
            r
            for r in caplog.records
            if r.name == _SYNC_LOGGER
            and "ingestion_sync_pattern_not_derivable" in r.getMessage()
        ]
        assert len(records) == 2, (
            f"The sweep must warn once per not-evaluated source, naming it; got "
            f"{len(records)} records. spec: BACKEND.md §Sync + mapping sweep step 2 "
            "§Coverage outcomes — 'warning naming the source and what could not be read'."
        )
        # Slack covers the record's fixed prefix, the source id and name, the truncation
        # marker and repr quoting. It is a constant: it does not scale with the recipe.
        slack = 512
        for record in records:
            message = record.getMessage()
            assert "\n" not in message, (
                "A writer-supplied newline must be escaped, not carried into the record — "
                f"a line-based collector would read the tail as a forged line. Got "
                f"{message!r}. spec: BACKEND.md §Sync + mapping sweep step 2 §Trust "
                "boundary on writer-supplied patterns."
            )
            assert len(message) <= MAX_REASON_CHARS + slack, (
                f"A 200 000-character recipe pattern must not grow the record; got "
                f"{len(message)} chars. spec: BACKEND.md §Sync + mapping sweep step 2 "
                "§Trust boundary — 'a writer cannot … grow a record without limit'."
            )
        # Both degraded sources are named, so neither record is the other one twice.
        named = {name for name in ("spot-sync-degrade-newline", "spot-sync-degrade-oversized")
                 if any(name in r.getMessage() for r in records)}
        assert named == {"spot-sync-degrade-newline", "spot-sync-degrade-oversized"}, (
            f"Each not-evaluated source must be named in its own record; got {named}. "
            "spec: BACKEND.md §Sync + mapping sweep step 2 §Coverage outcomes."
        )

        # The counter is a condition, not a transition: it persists while the sources do.
        second = await service.sync()
        assert second["sources_pattern_degraded"] == 2, (
            f"sources_pattern_degraded reports a condition, so a further sweep over the "
            f"same degraded sources must still report 2, not fall to 0; got "
            f"{second['sources_pattern_degraded']}. "
            "spec: BACKEND.md §Sync + mapping sweep §Sweep summary."
        )
    finally:
        await dataspoke_db.reset_ingestion_sources()
