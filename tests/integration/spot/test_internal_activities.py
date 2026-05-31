"""Spot tests for internal activity endpoints.

Concerns covered — for each domain (ingestion, validation, metagen, metrics, ontogen):
- POST /internal/activities/ingestion/list-active — returns source IDs for given tier
- POST /internal/activities/ingestion/run — executes for a source_id (per-source model)
- POST /internal/activities/ingestion/sync — reconcile all sources against DataHub
- POST /internal/activities/{domain}/list-active — returns list of IDs for given tier
- POST /internal/activities/{domain}/run — executes for a metric_id

Auth: X-Internal-Token header (internal_headers fixture).
Internal routes are mounted WITHOUT the /api/v1 prefix (see src/api/main.py line 271).
"""
# spec: BACKEND.md §Tier-DAG selection
# spec: BACKEND.md §Ingestion Service §Active-custom run pipeline
# spec: BACKEND.md §Validation Service / §Metrics Service

import os
import urllib.parse

import httpx
import pytest

_FAIL_TAIL: frozenset[str] = frozenset({"fail", "failed", "failure", "error", "errored"})

# In-cluster hostname for the dummy-data postgres (resolvable inside the cluster).
# spec: TESTING.md — example_db on the dev-env host.
_PG_HOST_PORT = os.environ.get(
    "DATASPOKE_TEST_DUMMY_DATA_POSTGRES_HOST_PORT",
    "example-postgres.dataspoke-dummy-data-01.svc.cluster.local:5432",
)
# Secret reference: K8s Secret dataspoke-source-cred-spot-pg, key 'password'.
_SECRET_REF_HOURLY = "${spot_pg_hourly__password}"
_SECRET_REF_DAILY = "${spot_pg_daily__password}"

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
                        "password": "${spot_pg_daily__password}",
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
            f"Expected both 'run_id' and 'status' in ingestion run response, got: {list(body.keys())}. "
            "spec: BACKEND.md §Active-custom run pipeline"
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

    spec: BACKEND.md §UC4 Generation Pipeline — singleton run, response carries status.
    spec: USE_CASE_en.md §UC4 — dry_run permitted regardless of is_enabled.
    """
    resp = await api_client.post(
        "/internal/activities/metagen/run",
        headers=internal_headers,
        json={"dry_run": True},
    )

    assert resp.status_code == 200
    body = resp.json()
    # spec: BACKEND.md §UC4 — MetagenRunResponse carries status
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
    # spec: API.md §DG metric — POST /spoke/governance/metric is the explicit create
    # (metric_id in body); PUT /metric/{id}/attr/conf replaces an existing one.
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
    # spec: API.md §DG metric — POST /spoke/governance/metric is the explicit create
    # (metric_id in body); PUT /metric/{id}/attr/conf replaces an existing one.
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

    spec: BACKEND.md §Ontogen Inference Pipeline — response carries status.
    """
    # spec: BACKEND.md §Ontology Generation Service
    resp = await api_client.post(
        "/internal/activities/ontogen/run",
        headers=internal_headers,
        json={"dry_run": True},
    )

    assert resp.status_code == 200
    body = resp.json()
    # spec: BACKEND.md §Ontogen Inference Pipeline — response must carry status
    assert "status" in body, (
        f"Expected 'status' in ontogen run response, got: {list(body.keys())}"
    )
