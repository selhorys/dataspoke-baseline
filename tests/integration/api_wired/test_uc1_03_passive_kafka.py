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
  5. Passive observation — emit ONE fresh DataHub Operation on imazon.orders.events at
     millisecond T (via util), poll sync (≤120s), then assert GET /sources/{id}/event
     carries a fresh passive_observation event for that topic at occurred_at==T (proves the
     observe-and-mirror path is live) with the spec'd detail key set.
  5b. Observation idempotence — two consecutive syncs over the unchanged estate: the
     second reports events_mirrored == 0 AND last_ingested_observed == 0, and the
     source's event feed is identical before and after (compared by event identity, not
     by count). Any last_ingested_observation rows carry exactly {source, dataset_urn}.
  5c. A PASSIVE source reports no run outcome — GET /data/{orders}/attr/ingestion returns
     latest_run: null even though the feed carries INGESTION.COMPLETE observations.
  6. After-sync negative check — poll GET /spoke/ingestion/unmanaged (≤120s) until
     both imazon.* topics are ABSENT (they are now mapped to the PASSIVE source)
  7. GET /sources/{id}/event → 200

Before/after delta (steps 0 and 6) proves the /unmanaged contract:
  - Step 0: registry is populated + no source maps the topics → they appear in /unmanaged.
  - Step 5: after sync with the PASSIVE source present → they are absent from /unmanaged.

Design note: dataset_registry is populated by the sync sweep
(POST /internal/activities/ingestion/sync). The registry starts empty after reset-all
(run before the api-wired group). Step 0 runs the first sync so the positive check has
data to work with. The system sources (datahub-gc, datahub-documents) declare no
topic_patterns → they map zero Kafka datasets → the imazon topics remain unmanaged.

spec: USE_CASE_en.md §UC1 Case 3
spec: API.md §Ingestion — PASSIVE mode, INGESTION_RUN_NOT_APPLICABLE
spec: feature/BACKEND.md §Ingestion Service — Sync + mapping sweep step 2 (AllowDenyPattern)
spec: TESTING.md §Api-Wired Integration Tests
"""

import asyncio
import time
from collections.abc import AsyncGenerator
from datetime import UTC, datetime

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
      "The Imazon Kafka topics are ingested by an external pipeline (or a
       `datahub` CLI run not registered as a DataHub source). DataSpoke
       registers a passive source with a declared allow/deny scope and syncs
       results from DataHub."

    Before/after delta for /unmanaged:
      - Step 0 (before source): registry populated by sync; imazon.* topics appear in
        /unmanaged because no source maps them yet.
      - Step 6 (after sync with PASSIVE source present): topics mapped to this source
        → absent from /unmanaged.

    Step 5 adds the passive-observation assertion: one fresh DataHub Operation on the
    orders topic at millisecond T must mirror into a passive_observation event at
    occurred_at==T (a fresh observation, distinct from any seed-time event).

    spec: USE_CASE_en.md §UC1 Case 3
    spec: API.md §Ingestion — POST /spoke/ingestion/sources (PASSIVE)
    spec: feature/BACKEND.md §Ingestion Service — Sync + mapping sweep step 2 (AllowDenyPattern)
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
        # spec: feature/BACKEND.md §Ingestion Service — Sync + mapping sweep —
        #   populates dataset_registry from DataHub
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
        # spec: API.md §Ingestion — Source body shape — PASSIVE has no schedule
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
        # spec: API.md §Ingestion — Source body shape — no schedule_tier on the wire
        assert "schedule_tier" not in body, (
            "schedule_tier must NOT appear in the API response. "
            "spec: API.md §Ingestion — Source body shape"
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
        # spec: feature/BACKEND.md §Ingestion Service — Sync + mapping sweep
        #   step 2 — "evaluating each source's **filter-matcher** — derived from … the
        #   declared `AllowDenyPattern` scope for `PASSIVE`. `derivation = matched`"
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
            "spec: USE_CASE_en.md §UC1 Case 3 + feature/BACKEND.md "
            "§Ingestion Service — Sync + mapping sweep step 2"
        )
        assert _SHIPPING_URN in dataset_urns, (
            f"imazon.shipping.updates must be mapped to the PASSIVE source after sync; "
            f"found: {sorted(dataset_urns)}. "
            "spec: USE_CASE_en.md §UC1 Case 3"
        )

        # Mapped datasets must have derivation='matched' (PASSIVE scope-based mapping).
        # spec: feature/BACKEND.md §Ingestion Service — Sync + mapping sweep
        #   step 2 — derivation=matched for PASSIVE.
        # spec: BACKEND_SCHEMA.md §ingestion_source_dataset — matched→authority=medium.
        for d in datasets_body["datasets"]:
            if d["dataset_urn"] in (_ORDERS_URN, _SHIPPING_URN):
                assert d["derivation"] == "matched", (
                    f"PASSIVE source dataset {d['dataset_urn']!r} must have "
                    f"derivation='matched'; got {d['derivation']!r}. "
                    "spec: feature/BACKEND.md §Ingestion Service — Sync + mapping "
                    "sweep step 2 — PASSIVE uses matched derivation"
                )
                assert d["authority"] == "medium", (
                    f"PASSIVE source dataset {d['dataset_urn']!r} must have authority='medium'; "
                    f"got {d['authority']!r}. "
                    "spec: BACKEND_SCHEMA.md §ingestion_source_dataset — matched→medium."
                )

        # ── Step 5: PASSIVE observation — a fresh Operation mirrors to an event ───
        # The Imazon Kafka topics are appended to by their external ingestor. Each
        # append lands in DataHub as an Operation timeseries record. For a PASSIVE
        # source, the sync sweep observes that Operation on its mapped dataset and
        # mirrors it as a passive_observation event whose occurred_at is the Operation's
        # lastUpdatedTimestamp.
        #
        # This step emits ONE fresh INSERT Operation on the orders topic and asserts a
        # corresponding fresh passive_observation event surfaces — proving the
        # observe-and-mirror path is live. (Dedup behavior is covered exhaustively by the
        # unit tests; this is not a dedup/collision test.) The emit is a DataHub-side
        # action via util; the assertion reads stay REST-only (GET /sources/{id}/event).
        #
        # spec: USE_CASE_en.md §UC1 Case 3 — "DataSpoke ... syncs results from DataHub"
        # spec: feature/BACKEND.md §Ingestion Service — Sync step 4, sub-pass table —
        #   "`Operation` observation | `Operation` aspects (`operationType ∈ {INSERT,
        #   UPDATE, CREATE, ALTER}`) | `PASSIVE` | per dataset | `COMPLETE` only |
        #   `passive_observation`"
        from tests.integration.util import datahub

        # The test owns T: the fresh Operation's millisecond timestamp. The resulting
        # passive_observation event's occurred_at must equal T, distinguishing it from any
        # seed-time observation.
        T = int(time.time() * 1000)
        await datahub.emit_operation(_ORDERS_URN, T)

        # occurred_at for a passive_observation event is derived from the Operation's
        # lastUpdatedTimestamp (== T). Compare instants, not strings: parse both sides to
        # aware datetimes so trailing-zero / offset formatting differences don't matter.
        # spec: feature/BACKEND.md §Ingestion Service — Sync step 4 (occurred_at from
        #   Operation lastUpdatedTimestamp).
        expected_occurred_at = datetime.fromtimestamp(T / 1000, tz=UTC)

        # Poll: re-trigger sync each iteration so the Operation-observation sweep runs
        # after DataHub indexes the freshly-emitted timeseries record. Budget ≤120s.
        poll_interval_step5 = 5.0
        poll_deadline_step5 = time.time() + 120.0
        fresh_event: dict | None = None
        while time.time() < poll_deadline_step5:
            try:
                sync_resp_5 = await api_client.post(
                    "/internal/activities/ingestion/sync",
                    headers=internal_headers,
                )
                assert sync_resp_5.status_code == 200, (
                    f"POST /internal/activities/ingestion/sync expected 200, "
                    f"got {sync_resp_5.status_code}: {sync_resp_5.text}"
                )
            except Exception:
                pass  # transient; outer deadline handles retry

            event_resp_5 = await api_client.get(source_event_url, headers=admin_headers)
            if event_resp_5.status_code == 200:
                for ev in event_resp_5.json().get("events", []):
                    detail = ev.get("detail") or {}
                    if detail.get("source") != "passive_observation":
                        continue
                    if detail.get("dataset_urn") != _ORDERS_URN:
                        continue
                    raw = ev.get("occurred_at")
                    if not raw:
                        continue
                    occurred_at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                    # Source timestamp T is integer-ms, so a sub-millisecond tolerance is
                    # exact-enough while staying robust to timestamp serialization changes.
                    if abs((occurred_at - expected_occurred_at).total_seconds()) < 0.001:
                        fresh_event = ev
                        break
                if fresh_event is not None:
                    break
            await asyncio.sleep(poll_interval_step5)

        # A fresh Operation on the mapped dataset must surface as a passive_observation
        # event at occurred_at==T — proving the observe-and-mirror path is live (distinct
        # from any seed-time event).
        # spec: feature/BACKEND.md §Ingestion Service — Sync step 4, sub-pass table — the
        #   `Operation` observation sub-pass books a per-dataset `COMPLETE` carrying
        #   `detail.source = passive_observation` (paraphrase of the table row).
        assert fresh_event is not None, (
            f"A fresh passive_observation event for {_ORDERS_URN} at "
            f"occurred_at=={expected_occurred_at.isoformat()} (T={T}) must surface after a "
            f"fresh Operation is emitted and the sync sweep runs. None found within 120s. "
            "spec: feature/BACKEND.md §Ingestion Service — Sync step 4 — PASSIVE Operation "
            "observation."
        )
        fresh_detail = fresh_event["detail"]
        assert set(fresh_detail) == {"source", "dataset_urn", "operation_type"}, (
            f"passive_observation detail keys must be exactly "
            f"{{source, dataset_urn, operation_type}}; got {sorted(fresh_detail)}. "
            "spec: feature/BACKEND.md §Event Catalogue — INGESTION.COMPLETE / "
            "INGESTION.FAIL producers."
        )
        assert fresh_detail["source"] == "passive_observation"
        assert fresh_detail["dataset_urn"] == _ORDERS_URN
        assert fresh_detail["operation_type"] == "INSERT", (
            f"the observation must echo the Operation's own operationType; got "
            f"{fresh_detail['operation_type']!r}. "
            "spec: feature/BACKEND.md §Ingestion Service — Sync step 4."
        )
        assert fresh_event["event_type"] == "INGESTION.COMPLETE", (
            "observation is success-only — an Operation is written when data changes and "
            "cannot express a failure. "
            "spec: feature/BACKEND.md §Ingestion Service — Sync step 4."
        )
        assert fresh_event["status"] == "success"

        # ── Step 5b: two consecutive sweeps over an unchanged estate book nothing ──
        # The observation layer appends by observed instant, not per sweep: the estate is
        # unchanged between these two syncs (no new Operation is emitted, no aspect
        # written), so both step-4 counters must read zero on the second and the source's
        # feed must be identical before and after.
        #
        # The feed comparison is bound by event IDENTITY, not by a count delta — a shared
        # dev cluster invalidates count windows. It is also the assertion that catches the
        # defect this arc exists for: an observer that walked its read window backwards one
        # record per sweep would report non-zero counters, and grow the feed, on an estate
        # where nothing happened.
        #
        # spec: feature/BACKEND.md §Ingestion Service — Sync step 4 — "An unchanged
        #   observation books nothing on the next sweep. The guarantee is 'at least one
        #   event over the dataset's lifetime', not one per hour".
        # spec: feature/BACKEND.md §Ingestion Service — Sweep summary — "A second
        #   consecutive sweep over an unchanged estate returns zero for all of those."
        # spec: TESTING.md §Integration Lifecycle & Isolation — "Bind event assertions by
        #   identity, never by count."
        def _feed_identity(events: list[dict]) -> set[tuple]:
            """The (type, instant, dataset, producer) identity of every event in a feed."""
            return {
                (
                    ev["event_type"],
                    ev["occurred_at"],
                    (ev.get("detail") or {}).get("dataset_urn"),
                    (ev.get("detail") or {}).get("source"),
                )
                for ev in events
            }

        settle_resp = await api_client.post(
            "/internal/activities/ingestion/sync", headers=internal_headers
        )
        assert settle_resp.status_code == 200, (
            f"POST /internal/activities/ingestion/sync expected 200, "
            f"got {settle_resp.status_code}: {settle_resp.text}"
        )
        settle_summary = settle_resp.json()

        before_resp = await api_client.get(
            f"/api/v1/spoke/ingestion/sources/{source_id}/event?offset=0&limit=1000",
            headers=admin_headers,
        )
        assert before_resp.status_code == 200, (
            f"GET /sources/{source_id}/event expected 200, got "
            f"{before_resp.status_code}: {before_resp.text}"
        )
        before_feed = _feed_identity(before_resp.json()["events"])
        assert before_feed, (
            "Backstop: the source's feed must be non-empty before the idempotence check, "
            "or 'the feed did not grow' is a statement about nothing."
        )

        # The counter set is part of the contract: a missing key means the sub-pass is not
        # wired at all, which otherwise reads exactly like an estate with nothing
        # observable.
        # spec: feature/BACKEND.md §Ingestion Service — Sweep summary — the
        #   last_ingested_observed counter.
        for counter in ("events_mirrored", "last_ingested_observed"):
            assert counter in settle_summary, (
                f"the sweep summary must report {counter!r}; got "
                f"{sorted(settle_summary)}. "
                "spec: feature/BACKEND.md §Ingestion Service — Sweep summary."
            )

        second_resp = await api_client.post(
            "/internal/activities/ingestion/sync", headers=internal_headers
        )
        assert second_resp.status_code == 200, (
            f"POST /internal/activities/ingestion/sync expected 200, "
            f"got {second_resp.status_code}: {second_resp.text}"
        )
        second_summary = second_resp.json()
        # The two count-window assertions in this step. BOTH counters are sweep-wide over
        # the whole estate rather than scoped to this source, so both carry the same
        # shared-cluster exposure. Triage note for a failure on either — it is estate churn,
        # not a regression, when:
        #   - events_mirrored: a DATAHUB_MANAGED execution reaches a terminal status
        #     between the two sweeps;
        #   - last_ingested_observed: another dataset's Dataset.lastIngested becomes visible
        #     between them. uc1_02 runs earlier in the same session and emits with a
        #     non-default systemMetadata.runId on two catalog datasets, and DataHub's search
        #     index lags, so the settle sweep and the second sweep can land on opposite
        #     sides of the moment those readings appear.
        # The identity-bound feed comparison below is the scoped statement, and it is the
        # one that fails for a defect in this arc.
        assert second_summary["events_mirrored"] == 0, (
            f"a second consecutive sweep over an unchanged estate must mirror no events; "
            f"got events_mirrored={second_summary['events_mirrored']}. "
            "spec: feature/BACKEND.md §Ingestion Service — Sweep summary."
        )
        assert second_summary["last_ingested_observed"] == 0, (
            f"a second consecutive sweep over an unchanged estate must observe no new "
            f"lastIngested instants; got "
            f"last_ingested_observed={second_summary['last_ingested_observed']}. "
            "spec: feature/BACKEND.md §Ingestion Service — Sweep summary."
        )

        after_resp = await api_client.get(
            f"/api/v1/spoke/ingestion/sources/{source_id}/event?offset=0&limit=1000",
            headers=admin_headers,
        )
        assert after_resp.status_code == 200
        after_feed = _feed_identity(after_resp.json()["events"])
        assert after_feed == before_feed, (
            f"the source's event feed must be identical across two sweeps over an "
            f"unchanged estate; added {sorted(after_feed - before_feed)!r}, lost "
            f"{sorted(before_feed - after_feed)!r}. "
            "spec: feature/BACKEND.md §Ingestion Service — Sync step 4."
        )

        # NOTE on what this arc does NOT assert: the lastIngested observation's own
        # per-dataset rules (detail key set, dataset scoping, success-only) are not checked
        # here. The seeding emitter writes DataHub's "no-run-id-provided" sentinel for these
        # Kafka topics, so their lastIngested is null and this source books no
        # last_ingested_observation at all — any check here would inspect an empty set. The
        # positive end-to-end case lives in uc1_02 step 7b (the ACM run is the only harness
        # path that emits with non-default systemMetadata and therefore actually advances
        # lastIngested), and the key-set / booking rules are unit-covered in
        # tests/unit/backend/ingestion/test_service.py::TestObserveLastIngested. What this
        # arc does assert unconditionally is the counter's presence and its zero reading on
        # the unchanged sweep, both above.
        #
        # spec: feature/BACKEND.md §Ingestion Service — Sync step 4 — "A null
        #   lastIngested books nothing."

        # ── Step 5c: a PASSIVE source reports no run outcome ──────────────────
        # attr/ingestion.latest_run reads run-level producers only. This source's feed
        # provably carries INGESTION.COMPLETE rows (the observation asserted in step 5),
        # and every one of them is a per-dataset observation — neither run-level producer
        # covers PASSIVE — so latest_run must be null. A route that reported the head of
        # the feed would answer 'success' here.
        #
        # spec: feature/BACKEND.md §Ingestion Service — Sync step 4 — "**A PASSIVE source
        #   reports no latest_run, by construction.** … a passive source's only
        #   INGESTION.COMPLETEs are per-dataset observations, and attr/ingestion.latest_run
        #   is null for it."
        # spec: API.md §Ingestion — GET /spoke/common/data/{urn}/attr/ingestion.
        orders_urn_enc = (
            _ORDERS_URN.replace("(", "%28").replace(")", "%29").replace(",", "%2C")
        )
        reverse_resp = await api_client.get(
            f"/api/v1/spoke/common/data/{orders_urn_enc}/attr/ingestion",
            headers=admin_headers,
        )
        assert reverse_resp.status_code == 200, (
            f"GET /data/{_ORDERS_URN}/attr/ingestion expected 200, got "
            f"{reverse_resp.status_code}: {reverse_resp.text}"
        )
        reverse_body = reverse_resp.json()
        assert reverse_body["source_id"] == source_id, (
            f"Backstop: the PASSIVE source must be the owner of {_ORDERS_URN}, or the "
            f"null latest_run below is about an unmapped dataset; got "
            f"{reverse_body['source_id']!r}."
        )
        assert reverse_body["latest_run"] is None, (
            f"a PASSIVE source's per-dataset observations must not be reported as a run "
            f"outcome; got latest_run={reverse_body['latest_run']!r}. "
            "spec: feature/BACKEND.md §Ingestion Service — Sync step 4 — Source "
            "latest_run."
        )

        # ── Step 6: AFTER-SYNC — mapped URNs must be ABSENT from /unmanaged ────
        # Negative invariant (mapped ⇒ not unmanaged). Poll until both imazon topics
        # leave /unmanaged (mapping commit + ES propagation may take a moment).
        # Bounded: ≤120s. Fails loudly if topics remain unmanaged past the deadline.
        #
        # The before/after delta (steps 0+6) prevents vacuous passes: if /unmanaged
        # were always empty, step 0 would already have failed. Here we assert that
        # the topics — which were confirmed present in step 0 — are now absent after
        # the PASSIVE source maps them.
        #
        # spec: USE_CASE_en.md §UC1 — "Datasets covered by no source appear in
        #   GET /spoke/ingestion/unmanaged"
        # spec: API.md §Ingestion — GET /spoke/ingestion/unmanaged: datasets in DataHub
        #   linked to no source
        poll_deadline_step6 = time.time() + 120.0
        after_unmanaged_urns: set[str] = set()
        mapped_but_still_unmanaged: set[str] = _IMAZON_KAFKA_URNS.copy()
        while time.time() < poll_deadline_step6:
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

        # ── Step 7: GET /sources/{id}/event — events list accessible ─────────
        # spec: API.md §Ingestion — GET /sources/{id}/event returns event history.
        # After step 5 the list carries the passive_observation events; assert the
        # endpoint shape (200 + events list) holds.
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
