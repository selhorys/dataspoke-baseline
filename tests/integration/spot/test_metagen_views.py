"""Spot tests for Metadata Generation — covered-datasets view, per-dataset rollup,
and candidate run_id/created_at exposure.

These cases assert backend query/aggregation invariants that the UC4 narrative arc
does not naturally reach. They seed metagen state via raw SQL (boundary states,
already-emitted candidates with pinned created_at) because the concern under test
is the query/aggregation behaviour, not the LLM run pipeline that would produce the
data — the run pipeline cannot deterministically yield a fixed mix of
approved/rejected/multi-conf candidates or a pinned created_at ordering. They run
in stub mode (no LLM dependency).

Concerns covered (3 test functions):
  test_metagen_covered_datasets_view        — GET /conf/{id}/dataset boundary filter
  test_metagen_run_id_and_created_at_exposed — item-list created_at, candidate run_id
  test_metagen_dataset_rollup_view          — GET /metagen/dataset aggregation query

spec: USE_CASE_en.md §UC4: Metadata Generation
spec: API.md §Metadata Generation — covered-datasets view, per-dataset rollup
spec: feature/BACKEND.md §Metadata Generation Service — Covered-datasets view,
  Per-dataset rollup view
spec: TESTING.md §Spot vs Api-Wired Integration Tests
"""

import urllib.parse
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.util.metagen import (
    EU_PROFILES_URN,
    ORDERS_EVENTS_URN,
    delete_metagen_conf,
    delete_metagen_state_for_urn,
    seed_metagen_boundary,
    seed_metagen_candidate,
    seed_metagen_conf,
    seed_metagen_item,
)

# Declare fixture dependencies so module_dummy_data ingests customers.eu_profiles
# (PG) and the imazon.orders.events topic (Kafka) into DataHub before any tests run.
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"customers"})
DUMMY_DATA_DATAHUB_TOPICS: frozenset[str] = frozenset({"imazon.orders.events"})

_EU_ENCODED = urllib.parse.quote(EU_PROFILES_URN, safe="")
_OE_ENCODED = urllib.parse.quote(ORDERS_EVENTS_URN, safe="")


@pytest.mark.asyncio
async def test_metagen_covered_datasets_view(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """GET /spoke/metagen/conf/{conf_id}/dataset — covered datasets boundary view.

    A conf scoped (via an explicit `dataset_urn IN (…)` filter clause) to two datasets:
      - eu_profiles: writable boundary (is_enabled=true, non-empty allowed) → not blocked
      - orders.events: blocked boundary (is_enabled=false) → boundary-blocked

    Asserts the spec invariants for the covered view:
      1. Default (include_disallowed omitted): only the writable covered dataset is
         returned; the boundary-blocked one is hidden.
      2. ?include_disallowed=true: both appear; the blocked one carries blocked=true
         with a reason; the writable one carries blocked=false.
      3. Each row's is_enabled / allowed / owner boundary summary is correct.
      4. Unknown conf_id → 404 METAGEN_CONF_NOT_FOUND.

    Spec: API.md §Metadata Generation — GET /spoke/metagen/conf/{conf_id}/dataset
    Spec: feature/BACKEND.md §Metadata Generation Service — Covered-datasets view
    """
    conf_id: str | None = None
    try:
        # Seed a conf scoped to exactly the two fulfillment datasets via a
        # `dataset_urn IN (…)` filter clause.
        # spec: feature/BACKEND.md §Metadata Generation Service — Covered-datasets view — resolution
        #   reuses
        #   resolve_dataset_scope for the conf's dataset_filter.
        conf_id = await seed_metagen_conf(
            async_session,
            name=f"uc4-covered-{uuid.uuid4().hex[:8]}",
            is_enabled=True,
            schedule_tier="daily",
            dataset_filter=f"dataset_urn IN ('{EU_PROFILES_URN}', '{ORDERS_EVENTS_URN}')",
        )

        # eu_profiles: writable boundary (enabled + non-empty allowed) → blocked=false.
        await seed_metagen_boundary(
            async_session,
            dataset_urn=EU_PROFILES_URN,
            is_enabled=True,
            allowed=["dataset.description", "column.description"],
        )
        # orders.events: disabled boundary → boundary-blocked.
        await seed_metagen_boundary(
            async_session,
            dataset_urn=ORDERS_EVENTS_URN,
            is_enabled=False,
            allowed=["column.description"],
        )

        covered_url = f"/api/v1/spoke/metagen/conf/{conf_id}/dataset"

        # ── 1. Default response excludes the boundary-blocked covered dataset ──
        # spec: feature/BACKEND.md §Metadata Generation Service — Covered-datasets view — default
        #   returns only
        #   writable (non-blocked) covered datasets.
        default_resp = await api_client.get(covered_url, headers=admin_headers)
        assert default_resp.status_code == 200, (
            f"GET covered datasets (default) failed: "
            f"{default_resp.status_code} {default_resp.text}. "
            "spec: API.md §Metadata Generation — GET /conf/{conf_id}/dataset"
        )
        default_body = default_resp.json()
        # Standard envelope. spec: API.md §Standard Response Envelope
        for key in ("offset", "limit", "total_count"):
            assert key in default_body, (
                f"covered-datasets response missing '{key}'. spec: API.md §Standard Response "
                f"Envelope"
            )
        # The covered view mirrors /uncovered, whose rows live under 'datasets'.
        # spec: API.md §Metadata Generation — /conf/{conf_id}/dataset mirrors /uncovered
        assert "datasets" in default_body and isinstance(default_body["datasets"], list), (
            "covered-datasets response must carry a 'datasets' list of rows. "
            "spec: API.md §Metadata Generation — mirrors /uncovered"
        )
        default_urns = {r["dataset_urn"] for r in default_body["datasets"]}
        assert EU_PROFILES_URN in default_urns, (
            "Writable covered dataset eu_profiles must appear in the default covered view. "
            "spec: feature/BACKEND.md §Metadata Generation Service — Covered-datasets view — "
            "writable datasets returned"
        )
        assert ORDERS_EVENTS_URN not in default_urns, (
            "Boundary-blocked covered dataset orders.events must be hidden by default. "
            "spec: feature/BACKEND.md §Metadata Generation Service — Covered-datasets view — "
            "default hides blocked"
        )
        for r in default_body["datasets"]:
            assert r["blocked"] is False, (
                f"Default covered view must only contain non-blocked rows; got "
                f"blocked={r['blocked']!r} for {r['dataset_urn']!r}. "
                "spec: feature/BACKEND.md §Metadata Generation Service — Covered-datasets view"
            )

        # ── 2. ?include_disallowed=true reveals the blocked covered dataset ───
        # spec: feature/BACKEND.md §Metadata Generation Service — Covered-datasets view —
        #   include_disallowed adds
        #   boundary-blocked covered datasets flagged with a reason.
        all_resp = await api_client.get(
            f"{covered_url}?include_disallowed=true", headers=admin_headers
        )
        assert all_resp.status_code == 200, (
            f"GET covered datasets (include_disallowed) failed: "
            f"{all_resp.status_code} {all_resp.text}"
        )
        all_rows = all_resp.json()["datasets"]
        by_urn = {r["dataset_urn"]: r for r in all_rows}
        assert EU_PROFILES_URN in by_urn and ORDERS_EVENTS_URN in by_urn, (
            "include_disallowed=true must reveal both the writable and the blocked "
            f"covered dataset; got {sorted(by_urn)!r}. "
            "spec: feature/BACKEND.md §Metadata Generation Service — Covered-datasets view"
        )

        # ── 3. Per-row boundary summary correctness ──────────────────────────
        eu_row = by_urn[EU_PROFILES_URN]
        assert eu_row["blocked"] is False, (
            "eu_profiles has an enabled, non-empty-allowed boundary → blocked=false. "
            "spec: feature/BACKEND.md §Metadata Generation Service — Covered-datasets view"
        )
        assert eu_row["is_enabled"] is True, (
            f"eu_profiles boundary is_enabled must echo true; got {eu_row['is_enabled']!r}. "
            "spec: API.md §Metadata Generation — covered row carries is_enabled"
        )
        assert set(eu_row["allowed"]) == {"dataset.description", "column.description"}, (
            f"eu_profiles allowed not echoed: {eu_row['allowed']!r}. "
            "spec: API.md §Metadata Generation — covered row carries allowed"
        )

        oe_row = by_urn[ORDERS_EVENTS_URN]
        assert oe_row["blocked"] is True, (
            "orders.events has a disabled boundary → blocked=true under include_disallowed. "
            "spec: feature/BACKEND.md §Metadata Generation Service — Covered-datasets view — "
            "disabled boundary blocks"
        )
        assert oe_row["is_enabled"] is False, (
            f"orders.events boundary is_enabled must echo false; got {oe_row['is_enabled']!r}. "
            "spec: feature/BACKEND.md §Metadata Generation Service — Covered-datasets view"
        )
        assert oe_row.get("reason"), (
            "A boundary-blocked covered row must carry a non-empty 'reason'. "
            "spec: feature/BACKEND.md §Metadata Generation Service — Covered-datasets view — "
            "boundary_blocked reason"
        )

        # ── 4. Unknown conf → 404 METAGEN_CONF_NOT_FOUND ─────────────────────
        # spec: API.md §Metadata Generation — 404 METAGEN_CONF_NOT_FOUND when absent
        # spec: API.md §Application Error Codes — METAGEN_CONF_NOT_FOUND
        missing_resp = await api_client.get(
            f"/api/v1/spoke/metagen/conf/{uuid.uuid4()}/dataset", headers=admin_headers
        )
        assert missing_resp.status_code == 404, (
            f"Unknown conf_id must return 404; got {missing_resp.status_code} {missing_resp.text}. "
            "spec: API.md §Metadata Generation — 404 METAGEN_CONF_NOT_FOUND"
        )
        assert missing_resp.json().get("error_code") == "METAGEN_CONF_NOT_FOUND", (
            f"Unknown conf error code must be METAGEN_CONF_NOT_FOUND; got "
            f"{missing_resp.json().get('error_code')!r}. spec: API.md §Application Error Codes"
        )

    finally:
        with suppress(Exception):
            await delete_metagen_state_for_urn(async_session, EU_PROFILES_URN)
        with suppress(Exception):
            await delete_metagen_state_for_urn(async_session, ORDERS_EVENTS_URN)
        if conf_id is not None:
            with suppress(Exception):
                await delete_metagen_conf(async_session, conf_id)


@pytest.mark.asyncio
async def test_metagen_run_id_and_created_at_exposed(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """Candidate responses carry run_id; item-list rows carry created_at.

    Mirrors plan #6 (evidence link from candidate run_id; result table Created At
    column). Seeds one item + candidate and reads:
      - the per-dataset item LIST rows → each carries non-null created_at
      - the item DETAIL candidate → carries run_id and created_at

    Spec: API.md §Metadata Generation — item-list row carries created_at;
      item-detail candidate carries run_id, created_at.
    """
    boundary_url = f"/api/v1/spoke/common/data/{_EU_ENCODED}/attr/metagen/boundary"
    item_id = "dataset.description"
    try:
        await seed_metagen_boundary(
            async_session,
            dataset_urn=EU_PROFILES_URN,
            is_enabled=True,
            allowed=["dataset.description", "column.description"],
        )
        await seed_metagen_item(
            async_session,
            dataset_urn=EU_PROFILES_URN,
            item_id=item_id,
            kind="dataset.description",
        )
        cid = await seed_metagen_candidate(
            async_session,
            dataset_urn=EU_PROFILES_URN,
            item_id=item_id,
            value="run_id/created_at exposure probe",
            status="llm_approved",
            item_kind="dataset.description",
        )

        # ── Item LIST rows carry created_at ──────────────────────────────────
        # spec: API.md §Metadata Generation — item row carries created_at.
        list_resp = await api_client.get(
            f"/api/v1/spoke/common/data/{_EU_ENCODED}/attr/metagen/item",
            headers=admin_headers,
        )
        assert list_resp.status_code == 200, (
            f"GET item list failed: {list_resp.status_code} {list_resp.text}"
        )
        list_rows = list_resp.json().get("items", [])
        target_row = next((r for r in list_rows if r["item_id"] == item_id), None)
        assert target_row is not None, (
            "Seeded item must appear in the per-dataset item list."
        )
        assert "created_at" in target_row and target_row["created_at"], (
            f"Item-list row must carry a non-empty 'created_at'; got "
            f"{target_row.get('created_at')!r}. "
            "spec: API.md §Metadata Generation — item row carries created_at"
        )

        # ── Item DETAIL candidate carries run_id + created_at ────────────────
        # spec: API.md §Metadata Generation — item-detail candidate carries run_id, created_at.
        encoded_item = urllib.parse.quote(item_id, safe="")
        detail_resp = await api_client.get(
            f"/api/v1/spoke/common/data/{_EU_ENCODED}/attr/metagen/item/{encoded_item}",
            headers=admin_headers,
        )
        assert detail_resp.status_code == 200, (
            f"GET item detail failed: {detail_resp.status_code} {detail_resp.text}"
        )
        cands = detail_resp.json().get("candidates", [])
        cand = next((c for c in cands if c["candidate_id"] == cid), None)
        assert cand is not None, "Seeded candidate must appear in item detail."
        assert "run_id" in cand and cand["run_id"], (
            f"Candidate response must carry a non-empty 'run_id'; got {cand.get('run_id')!r}. "
            "spec: API.md §Metadata Generation — item-detail candidate carries run_id"
        )
        uuid.UUID(str(cand["run_id"]))  # raises ValueError if malformed
        assert "created_at" in cand and cand["created_at"], (
            f"Candidate response must carry a non-empty 'created_at'; got "
            f"{cand.get('created_at')!r}. "
            "spec: API.md §Metadata Generation — item-detail candidate carries created_at"
        )

    finally:
        with suppress(Exception):
            await delete_metagen_state_for_urn(async_session, EU_PROFILES_URN)
        with suppress(Exception):
            await api_client.delete(boundary_url, headers=admin_headers)


@pytest.mark.asyncio
async def test_metagen_dataset_rollup_view(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """GET /spoke/metagen/dataset — per-dataset rollup of generation results.

    Seeds a deterministic candidate set across the two fulfillment datasets via raw
    SQL (the concern is the aggregation query, not the LLM run pipeline that would
    produce the data; the run pipeline cannot deterministically yield a fixed mix of
    approved / rejected / multi-conf candidates). Two confs (EU, RIVAL) and the
    following items:

      eu_profiles  (boundary enabled, allowed=[dataset.description, column.description]):
        - dataset.description       : conf EU approved  + conf EU rejected      (2 cands)
        - column.email.description  : conf EU llm_approved + conf RIVAL llm_approved (2 cands)
      orders.events  (NO boundary):
        - column.foo.description    : conf RIVAL rejected                       (1 cand)

    Asserts the spec invariants of API.md §Metadata Generation — GET /spoke/metagen/dataset:
      1. Unfiltered: one row per dataset; item_count = distinct items; candidate-level
         approved_count / rejected_count / candidate_count (candidate_count counts ALL
         candidates incl. rejected); boundary is_enabled / allowed via LEFT JOIN
         (is_enabled=false, allowed=[] when no boundary); last_modified_at equals the
         max item created_at of the dataset.
      1b. Default sort is last_modified_at_desc; ?sort=last_modified_at_asc reverses it.
      2. dataset_urn is a substring filter.
      3. conf_id restricts rows to datasets holding a candidate from that conf AND
         scopes every count to that conf's candidates.
      4. Malformed conf_id → 404 metagen_conf.

    Spec: API.md §Metadata Generation — GET /spoke/metagen/dataset
    Spec: feature/BACKEND.md §Metadata Generation Service — Per-dataset rollup view
    """
    dataset_url = "/api/v1/spoke/metagen/dataset"
    conf_eu_id: str | None = None
    conf_rival_id: str | None = None
    # Pin item created_at so the rollup's last_modified_at (= max item created_at
    # per dataset) is deterministic and the two datasets order distinctly.
    # spec: feature/BACKEND.md §Metadata Generation Service — Per-dataset rollup view —
    #   last_modified_at = max item created_at;
    # spec: API.md §Metadata Generation — default sort last_modified_at_desc.
    base_ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    OE_ITEM_TS = base_ts  # orders.events: oldest → sorts last under _desc
    EU_OLD_ITEM_TS = base_ts + timedelta(hours=1)
    EU_NEW_ITEM_TS = base_ts + timedelta(hours=2)  # eu_profiles max → sorts first under _desc
    try:
        # ── Seed two confs ────────────────────────────────────────────────────
        # spec: feature/BACKEND.md §Metadata Generation Service — conf collection.
        suffix = uuid.uuid4().hex[:8]
        conf_eu_id = await seed_metagen_conf(
            async_session,
            name=f"uc4-rollup-eu-{suffix}",
            is_enabled=True,
            dataset_filter=f"dataset_urn = '{EU_PROFILES_URN}'",
        )
        conf_rival_id = await seed_metagen_conf(
            async_session,
            name=f"uc4-rollup-rival-{suffix}",
            is_enabled=True,
            dataset_filter=f"dataset_urn IN ('{EU_PROFILES_URN}', '{ORDERS_EVENTS_URN}')",
        )

        # ── eu_profiles: enabled boundary + two items ─────────────────────────
        # spec: feature/BACKEND.md §Metadata Generation Service — Per-dataset rollup view — LEFT
        #   JOIN boundary.
        await seed_metagen_boundary(
            async_session,
            dataset_urn=EU_PROFILES_URN,
            is_enabled=True,
            allowed=["dataset.description", "column.description"],
        )
        # dataset.description: conf EU approved + conf EU rejected.
        await seed_metagen_item(
            async_session,
            dataset_urn=EU_PROFILES_URN,
            item_id="dataset.description",
            kind="dataset.description",
            created_at=EU_OLD_ITEM_TS,
        )
        await seed_metagen_candidate(
            async_session,
            dataset_urn=EU_PROFILES_URN,
            item_id="dataset.description",
            value="approved dataset desc (rollup)",
            status="approved",
            conf_id=conf_eu_id,
            item_kind="dataset.description",
        )
        await seed_metagen_candidate(
            async_session,
            dataset_urn=EU_PROFILES_URN,
            item_id="dataset.description",
            value="rejected dataset desc (rollup)",
            status="rejected",
            conf_id=conf_eu_id,
            item_kind="dataset.description",
        )
        # column.email.description: conf EU llm_approved + conf RIVAL llm_approved.
        await seed_metagen_item(
            async_session,
            dataset_urn=EU_PROFILES_URN,
            item_id="column.email.description",
            kind="column.description",
            field_path="email",
            created_at=EU_NEW_ITEM_TS,
        )
        await seed_metagen_candidate(
            async_session,
            dataset_urn=EU_PROFILES_URN,
            item_id="column.email.description",
            value="EU email desc (rollup)",
            status="llm_approved",
            conf_id=conf_eu_id,
            item_kind="column.description",
        )
        await seed_metagen_candidate(
            async_session,
            dataset_urn=EU_PROFILES_URN,
            item_id="column.email.description",
            value="RIVAL email desc (rollup)",
            status="llm_approved",
            conf_id=conf_rival_id,
            item_kind="column.description",
        )

        # ── orders.events: NO boundary + one item (conf RIVAL rejected) ───────
        await seed_metagen_item(
            async_session,
            dataset_urn=ORDERS_EVENTS_URN,
            item_id="column.foo.description",
            kind="column.description",
            field_path="foo",
            created_at=OE_ITEM_TS,
        )
        await seed_metagen_candidate(
            async_session,
            dataset_urn=ORDERS_EVENTS_URN,
            item_id="column.foo.description",
            value="OE foo desc (rollup)",
            status="rejected",
            conf_id=conf_rival_id,
            item_kind="column.description",
        )

        # ── 1. Unfiltered rollup: per-dataset rows + candidate-level counts ───
        # spec: API.md §Metadata Generation — GET /spoke/metagen/dataset row shape.
        resp = await api_client.get(f"{dataset_url}?limit=100", headers=admin_headers)
        assert resp.status_code == 200, (
            f"GET /spoke/metagen/dataset failed: {resp.status_code} {resp.text}. "
            "spec: API.md §Metadata Generation — GET /spoke/metagen/dataset"
        )
        body = resp.json()
        # Standard pagination envelope. spec: API.md §Standard Response Envelope.
        for key in ("offset", "limit", "total_count"):
            assert key in body, (
                f"rollup response missing '{key}'. spec: API.md §Standard Response Envelope"
            )
        assert "datasets" in body and isinstance(body["datasets"], list), (
            "rollup response must carry a 'datasets' list. "
            "spec: API.md §Metadata Generation — GET /spoke/metagen/dataset"
        )
        by_urn = {r["dataset_urn"]: r for r in body["datasets"]}
        assert EU_PROFILES_URN in by_urn and ORDERS_EVENTS_URN in by_urn, (
            "Both seeded datasets must appear as rollup rows; got "
            f"{sorted(by_urn)!r}. spec: API.md §Metadata Generation — one row per dataset"
        )

        eu = by_urn[EU_PROFILES_URN]
        # item_count = distinct items (2: dataset.description, column.email.description).
        # spec: feature/BACKEND.md §Metadata Generation Service — Per-dataset rollup view —
        #   item_count distinct items.
        assert eu["item_count"] == 2, (
            f"eu_profiles item_count must be 2 (distinct items); got {eu['item_count']!r}. "
            "spec: feature/BACKEND.md §Metadata Generation Service — Per-dataset rollup view — "
            "item_count distinct items"
        )
        # candidate_count counts ALL candidates incl. rejected (4: 2 on each item).
        # spec: BACKEND.md §Metadata Generation Service — Per-dataset rollup view —
        #   candidate_count derived from joined metagen_candidates (all statuses)
        assert eu["candidate_count"] == 4, (
            f"eu_profiles candidate_count must be 4 (all candidates); got "
            f"{eu['candidate_count']!r}. spec: BACKEND.md §Metadata Generation "
            "Service — Per-dataset rollup view (candidate_count)"
        )
        assert eu["approved_count"] == 1, (
            f"eu_profiles approved_count must be 1; got {eu['approved_count']!r}. "
            "spec: API.md §Metadata Generation — candidate-level approved_count"
        )
        assert eu["rejected_count"] == 1, (
            f"eu_profiles rejected_count must be 1; got {eu['rejected_count']!r}. "
            "spec: API.md §Metadata Generation — candidate-level rejected_count"
        )
        # Boundary surfaced via LEFT JOIN.
        # spec: API.md §Metadata Generation — row carries is_enabled / allowed boundary.
        assert eu["is_enabled"] is True, (
            f"eu_profiles is_enabled must echo the enabled boundary; got {eu['is_enabled']!r}. "
            "spec: API.md §Metadata Generation — row carries boundary is_enabled"
        )
        assert set(eu["allowed"]) == {"dataset.description", "column.description"}, (
            f"eu_profiles allowed must echo the boundary; got {eu['allowed']!r}. "
            "spec: API.md §Metadata Generation — row carries boundary allowed"
        )
        # last_modified_at = the MAX created_at of the dataset's items.
        # eu_profiles has two items (EU_OLD_ITEM_TS, EU_NEW_ITEM_TS) → the newer wins.
        # spec: feature/BACKEND.md §Metadata Generation Service — Per-dataset rollup view (max item
        #   created_at).
        assert eu["last_modified_at"] is not None, (
            "eu_profiles last_modified_at must be present when items exist. "
            "spec: API.md §Metadata Generation — row carries last_modified_at"
        )
        eu_lm = datetime.fromisoformat(eu["last_modified_at"])
        assert eu_lm == EU_NEW_ITEM_TS, (
            "eu_profiles last_modified_at must equal the MAX of its items' created_at "
            f"({EU_NEW_ITEM_TS.isoformat()}), not the older item's; "
            f"got {eu['last_modified_at']!r}. spec: feature/BACKEND.md §Metadata "
            "Generation Service — Per-dataset rollup view — last_modified_at = max "
            "item created_at"
        )

        oe = by_urn[ORDERS_EVENTS_URN]
        # orders.events has NO boundary → is_enabled=false, allowed=[] (LEFT JOIN default).
        # spec: API.md §Metadata Generation — is_enabled=false/allowed=[] when no boundary.
        assert oe["is_enabled"] is False, (
            f"orders.events has no boundary → is_enabled must be false; got {oe['is_enabled']!r}. "
            "spec: API.md §Metadata Generation — is_enabled=false when no boundary"
        )
        assert oe["allowed"] == [], (
            f"orders.events has no boundary → allowed must be []; got {oe['allowed']!r}. "
            "spec: API.md §Metadata Generation — allowed=[] when no boundary"
        )
        assert oe["item_count"] == 1 and oe["candidate_count"] == 1, (
            f"orders.events must show item_count=1 candidate_count=1; got "
            f"item_count={oe['item_count']!r} candidate_count={oe['candidate_count']!r}. "
            "spec: feature/BACKEND.md §Metadata Generation Service — Per-dataset rollup view"
        )
        assert oe["rejected_count"] == 1 and oe["approved_count"] == 0, (
            f"orders.events must show rejected_count=1 approved_count=0; got "
            f"rejected_count={oe['rejected_count']!r} approved_count={oe['approved_count']!r}. "
            "spec: API.md §Metadata Generation — candidate-level counts"
        )
        # orders.events has a single item → last_modified_at is exactly its created_at.
        # spec: feature/BACKEND.md §Metadata Generation Service — Per-dataset rollup view (max item
        #   created_at).
        assert oe["last_modified_at"] is not None, (
            "orders.events last_modified_at must be present when an item exists. "
            "spec: API.md §Metadata Generation — row carries last_modified_at"
        )
        oe_lm = datetime.fromisoformat(oe["last_modified_at"])
        assert oe_lm == OE_ITEM_TS, (
            "orders.events last_modified_at must equal its single item's created_at "
            f"({OE_ITEM_TS.isoformat()}); got {oe['last_modified_at']!r}. "
            "spec: feature/BACKEND.md §Metadata Generation Service — Per-dataset rollup view (max "
            "item created_at)"
        )

        # ── 1b. Default sort is last_modified_at_desc; ?sort=..._asc reverses ──
        # eu_profiles (max item created_at = base+2h) is newer than orders.events
        # (base); under the default desc sort eu_profiles precedes orders.events.
        # spec: API.md §Metadata Generation — default sort last_modified_at_desc;
        #   sortable by last_modified_at (last_modified_at_asc reverses).
        # Restrict to the two seeded URNs so unrelated rows don't perturb ordering.
        seeded = {EU_PROFILES_URN, ORDERS_EVENTS_URN}
        default_order = [
            r["dataset_urn"]
            for r in body["datasets"]
            if r["dataset_urn"] in seeded
        ]
        assert default_order == [EU_PROFILES_URN, ORDERS_EVENTS_URN], (
            "Default rollup order must be last_modified_at-descending: eu_profiles "
            f"(newer) before orders.events (older); got {default_order!r}. "
            "spec: API.md §Metadata Generation — default sort last_modified_at_desc"
        )

        asc_resp = await api_client.get(
            f"{dataset_url}?sort=last_modified_at_asc&limit=100", headers=admin_headers
        )
        assert asc_resp.status_code == 200, (
            f"GET rollup ?sort=last_modified_at_asc failed: "
            f"{asc_resp.status_code} {asc_resp.text}. "
            "spec: API.md §Metadata Generation — sortable by last_modified_at"
        )
        asc_order = [
            r["dataset_urn"]
            for r in asc_resp.json()["datasets"]
            if r["dataset_urn"] in seeded
        ]
        assert asc_order == [ORDERS_EVENTS_URN, EU_PROFILES_URN], (
            "?sort=last_modified_at_asc must reverse the default: orders.events "
            f"(older) before eu_profiles (newer); got {asc_order!r}. "
            "spec: API.md §Metadata Generation — last_modified_at_asc"
        )

        # ── 2. dataset_urn is a substring filter ──────────────────────────────
        # spec: API.md §Metadata Generation — filterable by dataset_urn text.
        text_resp = await api_client.get(
            f"{dataset_url}?dataset_urn=eu_profiles&limit=100", headers=admin_headers
        )
        assert text_resp.status_code == 200, (
            f"GET rollup with dataset_urn filter failed: {text_resp.status_code} {text_resp.text}"
        )
        text_urns = {r["dataset_urn"] for r in text_resp.json()["datasets"]}
        assert EU_PROFILES_URN in text_urns, (
            "dataset_urn substring 'eu_profiles' must match the eu_profiles row. "
            "spec: API.md §Metadata Generation — dataset_urn text filter"
        )
        assert ORDERS_EVENTS_URN not in text_urns, (
            "dataset_urn substring 'eu_profiles' must NOT match orders.events. "
            "spec: API.md §Metadata Generation — dataset_urn text filter"
        )

        # ── 3. conf_id scopes membership AND counts ───────────────────────────
        # conf EU has candidates only on eu_profiles, so orders.events drops out;
        # counts scope to conf EU's candidates only.
        #   eu_profiles under conf EU: dataset.description (approved + rejected) +
        #   column.email.description (1 EU llm_approved) = 3 EU candidates over 2 items;
        #   the RIVAL email candidate is excluded from the count.
        # spec: API.md §Metadata Generation — conf_id restricts rows + scopes counts.
        eu_scoped_resp = await api_client.get(
            f"{dataset_url}?conf_id={conf_eu_id}&limit=100", headers=admin_headers
        )
        assert eu_scoped_resp.status_code == 200, (
            f"GET rollup conf_id={conf_eu_id} failed: "
            f"{eu_scoped_resp.status_code} {eu_scoped_resp.text}"
        )
        eu_scoped = {r["dataset_urn"]: r for r in eu_scoped_resp.json()["datasets"]}
        assert ORDERS_EVENTS_URN not in eu_scoped, (
            "conf EU holds no candidate on orders.events → it must be excluded under "
            f"conf_id=conf_eu. got {sorted(eu_scoped)!r}. "
            "spec: API.md §Metadata Generation — conf_id restricts rows to that conf's datasets"
        )
        assert EU_PROFILES_URN in eu_scoped, (
            "eu_profiles holds conf EU candidates → it must appear under conf_id=conf_eu. "
            "spec: API.md §Metadata Generation — conf_id row membership"
        )
        eu_s = eu_scoped[EU_PROFILES_URN]
        assert eu_s["candidate_count"] == 3, (
            f"Under conf_id=conf_eu, eu_profiles candidate_count must scope to conf EU's 3 "
            f"candidates (RIVAL's email candidate excluded); got {eu_s['candidate_count']!r}. "
            "spec: API.md §Metadata Generation — conf_id scopes counts"
        )
        assert eu_s["approved_count"] == 1 and eu_s["rejected_count"] == 1, (
            f"Under conf_id=conf_eu, eu_profiles approved_count/rejected_count must be 1/1; "
            f"got {eu_s['approved_count']!r}/{eu_s['rejected_count']!r}. "
            "spec: API.md §Metadata Generation — conf_id scopes candidate-level counts"
        )

        # conf RIVAL has candidates on BOTH datasets → both rows present; the
        # orders.events count scopes to RIVAL's single rejected candidate.
        rival_scoped_resp = await api_client.get(
            f"{dataset_url}?conf_id={conf_rival_id}&limit=100", headers=admin_headers
        )
        assert rival_scoped_resp.status_code == 200
        rival_scoped = {r["dataset_urn"]: r for r in rival_scoped_resp.json()["datasets"]}
        assert EU_PROFILES_URN in rival_scoped and ORDERS_EVENTS_URN in rival_scoped, (
            "conf RIVAL holds candidates on both datasets → both rows must appear under "
            f"conf_id=conf_rival. got {sorted(rival_scoped)!r}. "
            "spec: API.md §Metadata Generation — conf_id row membership"
        )
        # eu_profiles under conf RIVAL: only the single RIVAL email llm_approved candidate.
        assert rival_scoped[EU_PROFILES_URN]["candidate_count"] == 1, (
            "Under conf_id=conf_rival, eu_profiles candidate_count must scope to RIVAL's 1 "
            f"candidate; got {rival_scoped[EU_PROFILES_URN]['candidate_count']!r}. "
            "spec: API.md §Metadata Generation — conf_id scopes counts"
        )
        assert rival_scoped[ORDERS_EVENTS_URN]["rejected_count"] == 1, (
            "Under conf_id=conf_rival, orders.events rejected_count must be 1; got "
            f"{rival_scoped[ORDERS_EVENTS_URN]['rejected_count']!r}. "
            "spec: API.md §Metadata Generation — conf_id scopes counts"
        )

        # ── 4. Malformed conf_id → 404 metagen_conf ──────────────────────────
        # spec: feature/BACKEND.md §Metadata Generation Service — Per-dataset rollup view — conf_id
        #   validated UUID,
        #   404 metagen_conf when absent (mirrors list_items).
        bad_resp = await api_client.get(
            f"{dataset_url}?conf_id=not-a-uuid", headers=admin_headers
        )
        assert bad_resp.status_code == 404, (
            f"Malformed conf_id must return 404; got {bad_resp.status_code} {bad_resp.text}. "
            "spec: feature/BACKEND.md §Metadata Generation Service — Per-dataset rollup view — "
            "conf_id 404 on bad/absent"
        )

    finally:
        with suppress(Exception):
            await delete_metagen_state_for_urn(async_session, EU_PROFILES_URN)
        with suppress(Exception):
            await delete_metagen_state_for_urn(async_session, ORDERS_EVENTS_URN)
        for cid in (conf_eu_id, conf_rival_id):
            if cid is not None:
                with suppress(Exception):
                    await delete_metagen_conf(async_session, cid)
