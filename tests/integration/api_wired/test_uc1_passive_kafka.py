"""UC1 Case 3 — PASSIVE kafka source: end-to-end through public REST API.

The Imazon Kafka topics are ingested by an external pipeline (not registered
as a DataHub source). DataSpoke registers a passive source with a declared
allow/deny scope and syncs results from DataHub.

Steps mirror USE_CASE_en.md §UC1 Case 3:
  1. POST /spoke/ingestion/sources with mode=PASSIVE + kafka topic_patterns recipe
  2. Assert 201 + response body shape (no schedule on the wire)
  3. POST /sources/{id}/method/run → 409 INGESTION_RUN_NOT_APPLICABLE
  4. Before-sync: assert imazon.* topics appear in GET /spoke/ingestion/unmanaged
  5. Trigger sync → GET /sources/{id}/datasets maps imazon.* topics by declared scope
  6. After-sync: assert imazon.* topics ABSENT from GET /spoke/ingestion/unmanaged
  7. Cleanup: DELETE /sources/{id}

Before/after delta (steps 4 and 6) proves the positive presence invariant:
  - Before sync the topics are covered by no source → they appear in /unmanaged.
  - After sync they are mapped to this PASSIVE source → they are absent from /unmanaged.
This is robust against cross-test flakiness because we track specific URNs.

spec: USE_CASE_en.md §UC1 Case 3
spec: API.md §Ingestion — PASSIVE mode, INGESTION_RUN_NOT_APPLICABLE
spec: feature/BACKEND.md §Ingestion Service §Sync sweep step 2 (mapping via AllowDenyPattern)
spec: TESTING.md §Api-Wired Integration Tests
"""

from collections.abc import AsyncGenerator

import httpx
import pytest
import pytest_asyncio

from tests.integration.util import dataspoke_db

# ── Dummy-data module constants ────────────────────────────────────────────────
# spec: TESTING.md §Per-Module Dummy-Data Reset — Kafka topics for UC1 Case 3
DUMMY_DATA_DATAHUB_TOPICS: frozenset[str] = frozenset([
    "imazon.orders.events",
    "imazon.shipping.updates",
])

# ── Kafka URNs ─────────────────────────────────────────────────────────────────
# spec: TESTING.md §Imazon Dummy-Data Reference — Kafka topics
_ORDERS_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.orders.events,DEV)"
)
_SHIPPING_URN = (
    "urn:li:dataset:(urn:li:dataPlatform:kafka,example_kafka.imazon.shipping.updates,DEV)"
)

_IMAZON_KAFKA_URNS = {_ORDERS_URN, _SHIPPING_URN}


@pytest_asyncio.fixture(autouse=True)
async def _clean_ingestion_sources() -> AsyncGenerator[None]:
    """Reset ingestion_source table before and after each test in this module.

    The test body stays REST-only; setup/teardown that touches the DB goes here.
    spec: TESTING.md §Api-Wired Integration Tests — 'the test itself stays REST-only;
          setup/teardown fixtures may use util'.
    spec: feedback_reset_before_api_wired — reset before api-wired tests for clean baseline.
    """
    await dataspoke_db.reset_ingestion_sources()
    yield
    await dataspoke_db.reset_ingestion_sources()


@pytest.mark.asyncio
async def test_uc1_passive_kafka(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    internal_headers: dict[str, str],
) -> None:
    """UC1 Case 3 — PASSIVE Kafka source: register scope, no run, sync maps topics.

    Narrative from USE_CASE_en.md §UC1 Case 3:
      "The Imazon Kafka topics are ingested by an external pipeline.
       DataSpoke registers a passive source with a declared allow/deny scope
       and syncs results from DataHub."

    Before/after delta for /unmanaged:
      - Before sync: imazon.* topics are covered by no source → appear in /unmanaged.
      - After sync: topics are mapped to this source → absent from /unmanaged.

    spec: USE_CASE_en.md §UC1 Case 3
    spec: API.md §Ingestion — POST /spoke/ingestion/sources (PASSIVE)
    spec: feature/BACKEND.md §Ingestion Service §Sync sweep step 2 (AllowDenyPattern)
    """
    source_id: str | None = None

    try:
        # ── Step 1: POST source — PASSIVE with kafka topic_patterns recipe ─────
        # spec: USE_CASE_en.md §UC1 Case 3 — exact recipe YAML → JSON body:
        #   mode: PASSIVE, no schedule (external ingestor owns it),
        #   recipe.source.type: kafka, topic_patterns.allow: ['^imazon\\..*$']
        create_resp = await api_client.post(
            "/api/v1/spoke/ingestion/sources",
            headers=admin_headers,
            json={
                "mode": "PASSIVE",
                "name": "dummy kafka topics",
                "schedule": None,
                "recipe": {
                    "source": {
                        "type": "kafka",
                        "config": {
                            "topic_patterns": {
                                "allow": ["^imazon\\..*$"]
                            }
                        },
                    }
                },
            },
        )
        assert create_resp.status_code == 201, (
            f"POST /spoke/ingestion/sources expected 201, "
            f"got {create_resp.status_code}: {create_resp.text}"
        )

        # ── Step 2: Assert 201 body shape ──────────────────────────────────────
        # spec: API.md §Ingestion §Source body shape — PASSIVE has no schedule
        body = create_resp.json()
        assert "id" in body
        source_id = body["id"]
        assert body["mode"] == "PASSIVE", (
            f"mode must be 'PASSIVE'; got {body['mode']!r}. "
            "spec: USE_CASE_en.md §UC1 Case 3"
        )
        assert body["name"] == "dummy kafka topics"
        # PASSIVE sources have no schedule (external ingestor owns connectivity/cadence).
        assert body.get("schedule") is None, (
            f"PASSIVE source must have schedule=null; got {body.get('schedule')!r}. "
            "spec: USE_CASE_en.md §UC1 Case 3 — no schedule on PASSIVE sources"
        )
        # spec: API.md §Ingestion §Source body shape — no schedule_tier on the wire
        assert "schedule_tier" not in body, (
            "schedule_tier must NOT appear in the API response. "
            "spec: API.md §Ingestion §Source body shape"
        )
        assert body.get("platform") == "kafka", (
            "platform must be 'kafka' (derived from recipe.source.type); "
            f"got {body.get('platform')!r}"
        )
        # datahub_source_urn must be null for PASSIVE (not a DataHub-managed source)
        assert body.get("datahub_source_urn") is None, (
            "PASSIVE source must have datahub_source_urn=null. "
            "spec: API.md §Ingestion — datahub_source_urn only set for DATAHUB_MANAGED"
        )
        assert "status" in body
        assert "created_at" in body
        assert "updated_at" in body

        source_run_url = f"/api/v1/spoke/ingestion/sources/{source_id}/method/run"
        source_datasets_url = f"/api/v1/spoke/ingestion/sources/{source_id}/datasets"
        source_event_url = f"/api/v1/spoke/ingestion/sources/{source_id}/event"

        # ── Step 3: POST method/run → 409 INGESTION_RUN_NOT_APPLICABLE ────────
        # spec: API.md §Ingestion — POST /sources/{id}/method/run: ACTIVE_CUSTOM_MANAGED only;
        #   else 409 INGESTION_RUN_NOT_APPLICABLE
        # spec: USE_CASE_en.md §UC1 Case 3 — "No connectivity, auth, or schedule — those
        #   belong to the external ingestor"
        run_resp = await api_client.post(
            source_run_url,
            headers=admin_headers,
            json={"dry_run": False},
        )
        assert run_resp.status_code == 409, (
            f"PASSIVE source method/run expected 409, got {run_resp.status_code}: {run_resp.text}. "
            "spec: API.md §Ingestion — INGESTION_RUN_NOT_APPLICABLE for PASSIVE"
        )
        run_err_code = run_resp.json().get("error_code")
        assert run_err_code == "INGESTION_RUN_NOT_APPLICABLE", (
            f"error_code must be 'INGESTION_RUN_NOT_APPLICABLE'; "
            f"got {run_err_code!r}. spec: USE_CASE_en.md §UC1 API Mapping"
        )
        # dry_run=True must also return 409 (PASSIVE cannot be run regardless)
        dry_run_resp = await api_client.post(
            source_run_url,
            headers=admin_headers,
            json={"dry_run": True},
        )
        assert dry_run_resp.status_code == 409, (
            f"PASSIVE source dry-run must also return 409; got {dry_run_resp.status_code}. "
            "spec: API.md §Ingestion — INGESTION_RUN_NOT_APPLICABLE for all PASSIVE run attempts"
        )

        # ── Step 4: BEFORE-SYNC — imazon.* topics must appear in /unmanaged ────
        # Before any sync, the topics are seeded in DataHub but covered by no source.
        # They must appear in GET /spoke/ingestion/unmanaged (positive presence).
        # spec: USE_CASE_en.md §UC1 — "Datasets covered by no source appear in
        #   GET /spoke/ingestion/unmanaged"
        # spec: API.md §Ingestion — GET /spoke/ingestion/unmanaged: datasets in DataHub
        #   linked to no source
        # Note: the source was just created but not yet synced; the sync sweep has not run.
        before_unmanaged_resp = await api_client.get(
            "/api/v1/spoke/ingestion/unmanaged?limit=200",
            headers=admin_headers,
        )
        assert before_unmanaged_resp.status_code == 200, (
            f"GET /spoke/ingestion/unmanaged (before sync) expected 200, "
            f"got {before_unmanaged_resp.status_code}: {before_unmanaged_resp.text}"
        )
        before_unmanaged_urns = set(before_unmanaged_resp.json().get("dataset_urns", []))
        # Both imazon.* topics must be present in /unmanaged before this source is synced.
        # This is the positive presence check; spec: USE_CASE_en.md §UC1 §Imazon Examples
        # Case 3 — "Datasets covered by no source appear in GET /spoke/ingestion/unmanaged".
        before_missing = _IMAZON_KAFKA_URNS - before_unmanaged_urns
        assert not before_missing, (
            f"Before sync, imazon.* topics must appear in /unmanaged (they are seeded in "
            f"DataHub but covered by no source yet). Missing: {sorted(before_missing)}. "
            "spec: USE_CASE_en.md §UC1 Case 3 — unmanaged bucket contains unmapped datasets."
        )

        # ── Step 5: Trigger sync → datasets mapped via declared scope ─────────
        # spec: feature/BACKEND.md §Sync sweep step 2 — "evaluate each source's
        #   filter-matcher — derived from the declared AllowDenyPattern scope for PASSIVE;
        #   origin = matcher"
        # The DUMMY_DATA_DATAHUB_TOPICS constant ensures both imazon.* topics are in DataHub.
        sync_resp = await api_client.post(
            "/internal/activities/ingestion/sync",
            headers=internal_headers,
        )
        assert sync_resp.status_code == 200, (
            f"POST /internal/activities/ingestion/sync expected 200, "
            f"got {sync_resp.status_code}: {sync_resp.text}"
        )

        # GET /sources/{id}/datasets — topics matching ^imazon\..*$ must appear
        datasets_resp = await api_client.get(source_datasets_url, headers=admin_headers)
        assert datasets_resp.status_code == 200, (
            f"GET /sources/{source_id}/datasets expected 200, "
            f"got {datasets_resp.status_code}: {datasets_resp.text}"
        )
        datasets_body = datasets_resp.json()
        assert "datasets" in datasets_body
        assert "total_count" in datasets_body
        dataset_urns = {d["dataset_urn"] for d in datasets_body["datasets"]}

        # Both imazon.* topics must be mapped by the AllowDenyPattern matcher.
        # spec: USE_CASE_en.md §UC1 Case 3 — "DataSpoke maps the datasets matching
        #   the declared scope"
        # spec: TESTING.md §Imazon Dummy-Data Reference — both Kafka topics seeded
        assert _ORDERS_URN in dataset_urns, (
            f"imazon.orders.events must be mapped to the PASSIVE source after sync; "
            f"found: {sorted(dataset_urns)}. "
            "spec: USE_CASE_en.md §UC1 Case 3 + feature/BACKEND.md §Sync sweep step 2"
        )
        assert _SHIPPING_URN in dataset_urns, (
            f"imazon.shipping.updates must be mapped to the PASSIVE source after sync; "
            f"found: {sorted(dataset_urns)}. "
            "spec: USE_CASE_en.md §UC1 Case 3"
        )

        # Mapped datasets must have origin='matcher' (PASSIVE scope-based mapping).
        # spec: feature/BACKEND.md §Sync sweep step 2 — origin=matcher for PASSIVE
        for d in datasets_body["datasets"]:
            if d["dataset_urn"] in (_ORDERS_URN, _SHIPPING_URN):
                assert d["origin"] == "matcher", (
                    f"PASSIVE source dataset {d['dataset_urn']!r} must have origin='matcher'; "
                    f"got {d['origin']!r}. "
                    "spec: feature/BACKEND.md §Sync sweep step 2 — PASSIVE uses matcher origin"
                )

        # ── Step 6: AFTER-SYNC — mapped URNs must be ABSENT from /unmanaged ────
        # Negative invariant (mapped ⇒ not unmanaged).
        # spec: USE_CASE_en.md §UC1 — "Datasets covered by no source appear in
        #   GET /spoke/ingestion/unmanaged"
        # spec: API.md §Ingestion — GET /spoke/ingestion/unmanaged: datasets in DataHub
        #   linked to no source
        #
        # F3 robustness: we verify each mapped URN individually against the
        # unmanaged set — this is the real contract.  The before/after delta (steps 4+6)
        # prevents the vacuous-pass risk: if /unmanaged returns [] (all mapped), the
        # before-check would have already failed.
        unmanaged_resp = await api_client.get(
            "/api/v1/spoke/ingestion/unmanaged?limit=200",
            headers=admin_headers,
        )
        assert unmanaged_resp.status_code == 200, (
            f"GET /spoke/ingestion/unmanaged (after sync) expected 200, "
            f"got {unmanaged_resp.status_code}: {unmanaged_resp.text}"
        )
        unmanaged_body = unmanaged_resp.json()
        assert "dataset_urns" in unmanaged_body
        after_unmanaged_urns = set(unmanaged_body["dataset_urns"])
        # Structural invariant: mapped URNs must be absent from /unmanaged after sync.
        mapped_but_still_unmanaged = _IMAZON_KAFKA_URNS & after_unmanaged_urns
        assert not mapped_but_still_unmanaged, (
            f"Datasets mapped to the PASSIVE source must NOT appear in /unmanaged after sync. "
            f"Still listed as unmanaged: {sorted(mapped_but_still_unmanaged)}. "
            "spec: USE_CASE_en.md §UC1 — mapped datasets absent from unmanaged bucket"
        )

        # ── Step 6b: GET /sources/{id}/event — events list accessible ─────────
        # spec: API.md §Ingestion — GET /sources/{id}/event returns event history
        # For a PASSIVE source with no actual run, the list may be empty but must be 200.
        event_resp = await api_client.get(source_event_url, headers=admin_headers)
        assert event_resp.status_code == 200, (
            f"GET /sources/{source_id}/event expected 200, "
            f"got {event_resp.status_code}: {event_resp.text}"
        )
        event_body = event_resp.json()
        assert "events" in event_body
        assert isinstance(event_body["events"], list)

    finally:
        # ── Cleanup: DELETE the source ────────────────────────────────────────
        if source_id is not None:
            await api_client.delete(
                f"/api/v1/spoke/ingestion/sources/{source_id}",
                headers=admin_headers,
            )
