"""UC1 Case 3 — PASSIVE kafka source: end-to-end through public REST API.

The Imazon Kafka topics are ingested by an external pipeline (not registered
as a DataHub source). DataSpoke registers a passive source with a declared
allow/deny scope and syncs results from DataHub.

Steps mirror USE_CASE_en.md §UC1 Case 3:
  0. Pre-source positive check — trigger sync to populate dataset_registry, then poll
     GET /spoke/ingestion/unmanaged until both imazon.* topics appear (they are in
     DataHub but covered by no source yet — system sources map nothing).
  1. POST /spoke/ingestion/sources with mode=PASSIVE + kafka topic_patterns recipe
  2. Assert 201 + response body shape (no schedule on the wire)
  3. POST /sources/{id}/method/run → 409 INGESTION_RUN_NOT_APPLICABLE (dry and non-dry)
  4. Trigger sync (PASSIVE source now exists) → GET /sources/{id}/datasets maps
     imazon.* topics with derivation=matched
  5. After-sync negative check — poll GET /spoke/ingestion/unmanaged (≤120s) until
     both imazon.* topics are ABSENT (they are now mapped to the PASSIVE source)
  6. GET /sources/{id}/event → 200

Before/after delta (steps 0 and 5) proves the /unmanaged contract:
  - Step 0: registry is populated + no source maps the topics → they appear in /unmanaged.
  - Step 5: after sync with the PASSIVE source present → they are absent from /unmanaged.

Design note: dataset_registry is populated by the sync sweep
(POST /internal/activities/ingestion/sync). The registry starts empty after reset-all
(run before the api-wired group). Step 0 runs the first sync so the positive check has
data to work with. The system sources (datahub-gc, datahub-documents) declare no
topic_patterns → they map zero Kafka datasets → the imazon topics remain unmanaged.

spec: USE_CASE_en.md §UC1 Case 3
spec: API.md §Ingestion — PASSIVE mode, INGESTION_RUN_NOT_APPLICABLE
spec: feature/BACKEND.md §Ingestion Service §Sync sweep step 2 (mapping via AllowDenyPattern)
spec: TESTING.md §Api-Wired Integration Tests
"""

import asyncio
import time
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
      - Step 0 (before source): registry populated by sync; imazon.* topics appear in
        /unmanaged because no source maps them yet.
      - Step 5 (after sync with PASSIVE source present): topics mapped to this source
        → absent from /unmanaged.

    spec: USE_CASE_en.md §UC1 Case 3
    spec: API.md §Ingestion — POST /spoke/ingestion/sources (PASSIVE)
    spec: feature/BACKEND.md §Ingestion Service §Sync sweep step 2 (AllowDenyPattern)
    """
    source_id: str | None = None

    try:
        # ── Step 0: Positive /unmanaged check — registry populated, topics unmapped ──
        # dataset_registry starts empty after reset-all. Run sync to populate it from
        # DataHub. The system sources (datahub-gc, datahub-documents) declare no
        # topic_patterns so they map zero Kafka datasets. The imazon.* topics land in
        # dataset_registry with datahub_registered=true and no source mapping → they
        # appear in GET /spoke/ingestion/unmanaged.
        #
        # Poll pattern: re-trigger sync on every iteration so newly-ES-indexed URNs
        # surface each time the matcher runs. Budget 180s covers the ES indexing lag
        # (~2-3 min) documented in project_es_indexing_lag_after_reset_seed.
        #
        # spec: USE_CASE_en.md §UC1 — "Datasets covered by no source appear in
        #   GET /spoke/ingestion/unmanaged"
        # spec: API.md §Ingestion — GET /spoke/ingestion/unmanaged: datasets in DataHub
        #   linked to no source
        # spec: project_es_indexing_lag_after_reset_seed — ES lags ~2-3 min after seed.
        # spec: feature/BACKEND.md §Sync sweep — populates dataset_registry from DataHub
        poll_deadline_step0 = time.time() + 180.0
        poll_interval = 5.0
        before_unmanaged_urns: set[str] = set()
        while time.time() < poll_deadline_step0:
            # Re-sync each iteration: newly-ES-indexed URNs surface on each call.
            sync_resp_0 = await api_client.post(
                "/internal/activities/ingestion/sync",
                headers=internal_headers,
            )
            # Tolerate transient blips (e.g. API briefly busy) — retry within budget.
            if sync_resp_0.status_code == 200:
                unmanaged_resp_0 = await api_client.get(
                    "/api/v1/spoke/ingestion/unmanaged?limit=500",
                    headers=admin_headers,
                )
                if unmanaged_resp_0.status_code == 200:
                    before_unmanaged_urns = set(
                        unmanaged_resp_0.json().get("dataset_urns", [])
                    )
                    if _IMAZON_KAFKA_URNS.issubset(before_unmanaged_urns):
                        break
            await asyncio.sleep(poll_interval)

        # Positive presence assertion: both imazon.* topics must appear in /unmanaged
        # before the PASSIVE source is created. Fails loudly if either is absent —
        # this would mean either (a) the ES index lag exceeded 180s, or (b) a source
        # is already mapping them (which would be a real coverage regression).
        # spec: USE_CASE_en.md §UC1 Case 3 — "Datasets covered by no source appear in
        #   GET /spoke/ingestion/unmanaged" (positive invariant before source creation).
        before_missing = _IMAZON_KAFKA_URNS - before_unmanaged_urns
        assert not before_missing, (
            f"Before PASSIVE source creation, imazon.* topics must appear in /unmanaged. "
            f"The registry is populated by sync; system sources map no Kafka datasets, "
            f"so topics have no source mapping and are therefore unmanaged. "
            f"Missing URNs: {sorted(before_missing)}. "
            f"All /unmanaged URNs seen: {sorted(before_unmanaged_urns)}. "
            "This check fails if: (a) ES indexing lag exceeded the 180s poll budget, or "
            "(b) some source unexpectedly maps these topics already. "
            "spec: USE_CASE_en.md §UC1 Case 3 — unmanaged bucket contains unmapped datasets; "
            "spec: project_es_indexing_lag_after_reset_seed — ES lag budget 2-3 min."
        )

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
            f"{source_run_url}?dry_run=true",
            headers=admin_headers,
        )
        assert dry_run_resp.status_code == 409, (
            f"PASSIVE source dry-run must also return 409; got {dry_run_resp.status_code}. "
            "spec: API.md §Ingestion — INGESTION_RUN_NOT_APPLICABLE for all PASSIVE run attempts"
        )

        # ── Step 4: Trigger sync (PASSIVE source now exists) → topics mapped ──
        # Now that the PASSIVE source is registered, re-run the sync sweep. The
        # matcher evaluates the source's AllowDenyPattern (^imazon\..*$) against
        # DataHub datasets and maps matching topics to this source with derivation=matched.
        #
        # Poll: re-trigger sync each iteration so newly-ES-indexed URNs surface.
        # Budget 180s covers remaining ES lag from the seed operation.
        #
        # spec: feature/BACKEND.md §Sync sweep step 2 — "evaluate each source's
        #   filter-matcher — derived from the declared AllowDenyPattern scope for PASSIVE;
        #   derivation = matched"
        # spec: TESTING.md §Imazon Dummy-Data Reference — both Kafka topics seeded in DataHub.
        poll_deadline_step4 = time.time() + 180.0
        datasets_body: dict = {}
        dataset_urns: set[str] = set()
        while time.time() < poll_deadline_step4:
            try:
                sync_resp_4 = await api_client.post(
                    "/internal/activities/ingestion/sync",
                    headers=internal_headers,
                )
                assert sync_resp_4.status_code == 200, (
                    f"POST /internal/activities/ingestion/sync expected 200, "
                    f"got {sync_resp_4.status_code}: {sync_resp_4.text}"
                )
            except Exception:
                pass  # transient; outer deadline handles retry

            datasets_resp = await api_client.get(source_datasets_url, headers=admin_headers)
            assert datasets_resp.status_code == 200, (
                f"GET /sources/{source_id}/datasets expected 200, "
                f"got {datasets_resp.status_code}: {datasets_resp.text}"
            )
            datasets_body = datasets_resp.json()
            dataset_urns = {d["dataset_urn"] for d in datasets_body.get("datasets", [])}
            if _IMAZON_KAFKA_URNS.issubset(dataset_urns):
                break
            await asyncio.sleep(poll_interval)

        assert "datasets" in datasets_body
        assert "total_count" in datasets_body

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

        # Mapped datasets must have derivation='matched' (PASSIVE scope-based mapping).
        # spec: feature/BACKEND.md §Sync sweep step 2 — derivation=matched for PASSIVE.
        # spec: BACKEND_SCHEMA.md §ingestion_source_dataset — matched→authority=medium.
        for d in datasets_body["datasets"]:
            if d["dataset_urn"] in (_ORDERS_URN, _SHIPPING_URN):
                assert d["derivation"] == "matched", (
                    f"PASSIVE source dataset {d['dataset_urn']!r} must have "
                    f"derivation='matched'; got {d['derivation']!r}. "
                    "spec: feature/BACKEND.md §Sync sweep step 2 — PASSIVE uses matched derivation"
                )
                assert d["authority"] == "medium", (
                    f"PASSIVE source dataset {d['dataset_urn']!r} must have authority='medium'; "
                    f"got {d['authority']!r}. "
                    "spec: BACKEND_SCHEMA.md §ingestion_source_dataset — matched→medium."
                )

        # ── Step 5: AFTER-SYNC — mapped URNs must be ABSENT from /unmanaged ────
        # Negative invariant (mapped ⇒ not unmanaged). Poll until both imazon topics
        # leave /unmanaged (mapping commit + ES propagation may take a moment).
        # Bounded: ≤120s. Fails loudly if topics remain unmanaged past the deadline.
        #
        # The before/after delta (steps 0+5) prevents vacuous passes: if /unmanaged
        # were always empty, step 0 would already have failed. Here we assert that
        # the topics — which were confirmed present in step 0 — are now absent after
        # the PASSIVE source maps them.
        #
        # spec: USE_CASE_en.md §UC1 — "Datasets covered by no source appear in
        #   GET /spoke/ingestion/unmanaged"
        # spec: API.md §Ingestion — GET /spoke/ingestion/unmanaged: datasets in DataHub
        #   linked to no source
        poll_deadline_step5 = time.time() + 120.0
        after_unmanaged_urns: set[str] = set()
        mapped_but_still_unmanaged: set[str] = _IMAZON_KAFKA_URNS.copy()
        while time.time() < poll_deadline_step5:
            unmanaged_resp = await api_client.get(
                "/api/v1/spoke/ingestion/unmanaged?limit=500",
                headers=admin_headers,
            )
            if unmanaged_resp.status_code == 200:
                after_unmanaged_urns = set(unmanaged_resp.json().get("dataset_urns", []))
                mapped_but_still_unmanaged = _IMAZON_KAFKA_URNS & after_unmanaged_urns
                if not mapped_but_still_unmanaged:
                    break
            await asyncio.sleep(poll_interval)

        # Structural invariant: mapped URNs must be absent from /unmanaged after sync.
        assert not mapped_but_still_unmanaged, (
            f"Datasets mapped to the PASSIVE source must NOT appear in /unmanaged after sync. "
            f"Still listed as unmanaged: {sorted(mapped_but_still_unmanaged)}. "
            "The before/after delta (step 0 confirmed presence; this step must confirm absence) "
            "proves the mapping took effect. "
            "spec: USE_CASE_en.md §UC1 — mapped datasets absent from unmanaged bucket"
        )

        # ── Step 6: GET /sources/{id}/event — events list accessible ─────────
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
