"""UC4 — Metadata Generation: end-to-end through public REST API.

Maps `spec/USE_CASE_en.md §UC4` paragraphs to executable steps. REST-only
per `spec/TESTING.md §Api-Wired Integration Tests`.

Tests in this module:
  - test_uc4_conf_run_and_latest: PUT metagen conf, dry-run (returns MetagenRunResponse),
    GET latest result envelope, cleanup.
  - test_uc4_review_partial_and_reject: Seed a metagen_results row via raw SQL;
    first PATCH approves a field subset, second PATCH rejects cross_data action;
    verify field_status transitions.
  - test_uc4_review_approves_cross_data_create_action: Seed a metagen_results row with
    a cross_data.md create action; PATCH approves it; verify field_status flip to approved.
  - test_uc4_whole_proposal_reject: Seed a multi-field result; PATCH verdict=reject with
    no fields array; assert every field_status entry flips to 'rejected'.
  - test_uc4_cross_data_modify_and_delete_actions: Seed a result with modify + delete
    cross_data.md actions; PATCH approves both; assert both flip to 'approved'.
  - test_uc4_reads_uc3_approved_nodes: Seed an approved ontogen_nodes row and a
    dataset_node_map row; POST metagen run; assert 200 with proposals dict
    (UC3 → UC4 coupling test).

Note: Concurrent-run 409 GENERATION_RUNNING coverage lives in
`tests/integration/spot/test_metagen.py`; UC4 narrative scope does not require it.

proposals shape per spec/feature/BACKEND_SCHEMA.md §metagen_results:
  - dataset.description → str
  - column.description → dict[fieldPath, str]  (stored flat; field_status uses
    column.description.{fieldPath} keys)
  - cross_data.md → list[{action_id, action: create|modify|delete, ...}]
    Individual actions are referenced in field_status / PATCH fields as
    cross_data.md.<action_id>.
    spec: BACKEND_SCHEMA.md L134; BACKEND.md §Cross-data MD action types
"""
# spec: USE_CASE_en.md §UC4

import json
import urllib.parse
import uuid
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

# Declare fixture dependencies so module_dummy_data seeds catalog schema + DataHub.
# spec: TESTING.md §Per-Module Dummy-Data Reset
DUMMY_DATA_DATAHUB_SCHEMAS: frozenset[str] = frozenset({"catalog"})

# UC4 dataset: catalog.title_master — Imazon primary catalog table
# spec: TESTING.md §Imazon Dummy-Data Reference — catalog.title_master UC4
_TEST_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_ENCODED_URN = urllib.parse.quote(_TEST_URN, safe="")


# ── Raw-SQL helpers for DB seeding (setup/teardown only — not in test bodies) ──
# spec/TESTING.md §Api-Wired Integration Tests — "Setup/teardown fixtures may use
# tests.integration.util … the test itself stays REST-only."


async def _seed_pending_metagen_result(
    session: AsyncSession,
    *,
    dataset_urn: str,
    proposals: dict[str, Any],
    field_status: dict[str, str],
) -> str:
    """Insert a pending metagen_results row via raw SQL; return the result_id."""
    from sqlalchemy import text

    result_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())

    await session.execute(
        text(
            "INSERT INTO dataspoke.metagen_results"
            " (id, dataset_urn, proposals, field_status, run_id, generated_at)"
            " VALUES (:id, :dataset_urn, CAST(:proposals AS jsonb),"
            "  CAST(:field_status AS jsonb), :run_id, now())"
        ),
        {
            "id": result_id,
            "dataset_urn": dataset_urn,
            "proposals": json.dumps(proposals),
            "field_status": json.dumps(field_status),
            "run_id": run_id,
        },
    )
    await session.commit()
    return result_id


async def _delete_metagen_result(session: AsyncSession, result_id: str, dataset_urn: str) -> None:
    from sqlalchemy import text

    await session.execute(
        text("DELETE FROM dataspoke.metagen_results WHERE id = :id AND dataset_urn = :urn"),
        {"id": result_id, "urn": dataset_urn},
    )
    await session.commit()


async def _seed_approved_ontogen_node(session: AsyncSession, node_id: str, name: str) -> None:
    """Insert an approved ontogen_nodes row via raw SQL (UC3 → UC4 coupling setup).

    spec: BACKEND.md L425-L426 — UC4 reads UC3-approved nodes via
    dataset_node_map.status='approved'.
    """
    from sqlalchemy import text

    await session.execute(
        text(
            "INSERT INTO dataspoke.ontogen_nodes"
            " (id, name, description, confidence_score, status, evidence)"
            " VALUES (:id, :name, :desc, :conf, 'approved', CAST(:ev AS jsonb))"
        ),
        {
            "id": node_id,
            "name": name,
            "desc": "UC4 coupling test approved node",
            "conf": 0.90,
            "ev": json.dumps({"source": "uc4-coupling-test"}),
        },
    )
    await session.commit()


async def _seed_dataset_node_map(
    session: AsyncSession,
    *,
    dataset_urn: str,
    node_id: str,
    status: str = "approved",
) -> None:
    """Insert a dataset_node_map row via raw SQL.

    Composite PK: (dataset_urn, node_id).
    spec: src/shared/db/models.py:431-451 — DatasetNodeMap schema.
    spec: BACKEND.md L425-L426 — UC4 reads rows with status='approved'.
    """
    from sqlalchemy import text

    await session.execute(
        text(
            "INSERT INTO dataspoke.dataset_node_map"
            " (dataset_urn, node_id, confidence_score, status, is_primary)"
            " VALUES (:dataset_urn, :node_id, :conf, :status, false)"
            " ON CONFLICT (dataset_urn, node_id) DO UPDATE SET status = EXCLUDED.status"
        ),
        {
            "dataset_urn": dataset_urn,
            "node_id": node_id,
            "conf": 0.90,
            "status": status,
        },
    )
    await session.commit()


async def _delete_ontogen_node(session: AsyncSession, node_id: str) -> None:
    """Delete an ontogen_nodes row (cascades to dataset_node_map via FK)."""
    from sqlalchemy import text

    # Delete dataset_node_map rows first (FK constraint)
    await session.execute(
        text("DELETE FROM dataspoke.dataset_node_map WHERE node_id = :node_id"),
        {"node_id": node_id},
    )
    await session.execute(
        text("DELETE FROM dataspoke.ontogen_nodes WHERE id = :id"),
        {"id": node_id},
    )
    await session.commit()


# ── Tests ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_uc4_conf_run_and_latest(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
) -> None:
    """UC4 narrative: 'DataSpoke proposes documentation for under-documented datasets,
    and lets me approve, edit, or reject proposals field-by-field, so that documentation
    coverage improves without me writing every description by hand.'

    Steps mirror USE_CASE_en.md §UC4:
      1. PUT metagen conf (targets, schedule_tier, is_enabled)
      2. POST dry-run — returns MetagenRunResponse (id, dataset_urn, proposals)
      3. GET attr/metagen/result?latest=true — paginated envelope
      4. Cleanup — DELETE conf
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"
    base_run = f"/api/v1/spoke/common/data/{_ENCODED_URN}/method/metagen/run"
    base_results = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/result"

    try:
        # ── Step 1: PUT metagen conf ──────────────────────────────────────────
        # UC4 narrative: "The catalog team enables doc generation on catalog.books."
        # spec: USE_CASE_en.md §UC4 L541-L549
        put_resp = await api_client.put(
            base_conf,
            headers=admin_headers,
            json={
                "targets": ["dataset.description", "column.description", "cross_data.md"],
                "schedule_tier": "weekly",
                "is_enabled": True,
                "owner": "uc4-api-wired@imazon.com",
            },
        )
        assert put_resp.status_code in (200, 201), (
            f"PUT metagen conf failed: {put_resp.status_code} {put_resp.text}"
        )
        conf_body = put_resp.json()
        assert conf_body["dataset_urn"] == _TEST_URN
        assert "dataset.description" in conf_body["targets"]
        # spec: USE_CASE_en.md §UC4 L541-L549 — conf round-trip preserves schedule_tier,
        # is_enabled, and full targets set
        assert conf_body["schedule_tier"] == "weekly", (
            f"schedule_tier not preserved: {conf_body.get('schedule_tier')!r}. "
            "spec: USE_CASE_en.md §UC4 L541-L549"
        )
        assert conf_body["is_enabled"] is True, (
            f"is_enabled not preserved: {conf_body.get('is_enabled')!r}. "
            "spec: USE_CASE_en.md §UC4 L541-L549"
        )
        assert set(conf_body["targets"]) == {
            "dataset.description",
            "column.description",
            "cross_data.md",
        }, (
            f"targets not preserved: {conf_body.get('targets')!r}. "
            "spec: USE_CASE_en.md §UC4 L541-L549"
        )

        # ── Step 2: POST run (non-dry) ────────────────────────────────────────
        # UC4 narrative: "POST .../method/metagen/run"
        # spec: USE_CASE_en.md §UC4 L551-L553
        # Note: dry_run=False so a result row is persisted; the ?latest=true query
        # in Step 3 anchors the invariant that at most one row is returned.
        # spec: API.md L257 — ?latest=true returns the most recent result row.
        run_resp = await api_client.post(
            base_run,
            headers=admin_headers,
            json={"dry_run": False},
        )
        assert run_resp.status_code == 200, (
            f"POST metagen run failed: {run_resp.status_code} {run_resp.text}"
        )
        run_body = run_resp.json()
        # spec: USE_CASE_en.md §UC4 — MetagenRunResponse: id, dataset_urn, proposals
        assert "id" in run_body, "MetagenRunResponse must carry 'id'"
        assert "dataset_urn" in run_body, "MetagenRunResponse must carry 'dataset_urn'"
        assert "proposals" in run_body, "MetagenRunResponse must carry 'proposals'"
        assert run_body["dataset_urn"] == _TEST_URN
        # spec: BACKEND_SCHEMA.md L133 — proposals is a JSONB dict
        assert isinstance(run_body["proposals"], dict), (
            f"proposals must be a dict; got {type(run_body['proposals'])!r}. "
            "spec: BACKEND_SCHEMA.md L133"
        )

        # ── Step 3: GET latest result ─────────────────────────────────────────
        # UC4 narrative: "Latest proposal: GET .../attr/metagen/result?latest=true"
        # spec: USE_CASE_en.md §UC4 L555-L557
        latest_resp = await api_client.get(
            f"{base_results}?latest=true",
            headers=admin_headers,
        )
        assert latest_resp.status_code == 200, (
            f"GET metagen result latest failed: {latest_resp.status_code}"
        )
        latest_body = latest_resp.json()
        # spec: API.md §Standard Envelope
        assert "results" in latest_body
        assert "offset" in latest_body
        assert "limit" in latest_body
        assert "total_count" in latest_body
        assert isinstance(latest_body["results"], list)
        # spec: API.md L257 — ?latest=true returns at most one result row for the dataset
        assert len(latest_body["results"]) <= 1, (
            f"?latest=true must return at most one result row; "
            f"got {len(latest_body['results'])}. spec: API.md L257"
        )
        if latest_body["results"]:
            assert latest_body["results"][0]["dataset_urn"] == _TEST_URN, (
                f"latest result dataset_urn expected {_TEST_URN!r}; "
                f"got {latest_body['results'][0].get('dataset_urn')!r}. spec: API.md L257"
            )

    finally:
        # ── Step 4: Cleanup ───────────────────────────────────────────────────
        await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_uc4_review_partial_and_reject(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """UC4 narrative: 'The reviewer approves the table description and 4 of 5 columns,
    then issues follow-up calls to edit author and reject the cross-data MD.'

    Steps mirror USE_CASE_en.md §UC4 L594-L615:
      a. Seed a metagen_results row with proposals for dataset.description,
         column.description.*, and cross_data.md (list of action dicts keyed by action_id).
         field_status uses flat cross_data.md.<action_id> keys per
         spec/feature/BACKEND_SCHEMA.md §metagen_results and
         spec/feature/BACKEND.md §Cross-data MD action types.
      b. First PATCH approves a subset of fields → those flip to 'approved',
         others stay 'pending'
      c. Second PATCH rejects cross_data.md.a1 → that field flips to 'rejected'
      d. Cleanup — DELETE seeded row and conf
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"

    result_id: str | None = None

    try:
        # Ensure metagen conf exists for the URN
        # spec: src/api/schemas/metagen.py MetagenConfPutRequest.owner — required field
        await api_client.put(
            base_conf,
            headers=admin_headers,
            json={
                "targets": ["dataset.description", "column.description", "cross_data.md"],
                "schedule_tier": "weekly",
                "is_enabled": False,
                "owner": "uc4-api-wired@imazon.com",
            },
        )

        # ── Step a: Seed a pending metagen_results row ────────────────────────
        # spec: TESTING.md §Api-Wired Integration Tests — "Setup/teardown fixtures may
        # use tests.integration.util and may execute raw SQL against async_session for
        # setup/teardown only; the test itself stays REST-only."
        #
        # proposals shape per spec/feature/BACKEND_SCHEMA.md §metagen_results L134:
        #   cross_data.md stores an ordered list of action dicts;
        #   column.description stores a dict keyed by fieldPath (not flat top-level keys).
        #   field_status is always flat-keyed: cross_data.md.<action_id> and
        #   column.description.<fieldPath> are the PATCH field-reference format.
        #   spec: BACKEND.md §Cross-data MD action types (create row)
        proposals: dict[str, Any] = {
            "dataset.description": "Master catalog of every title Imazon offers.",
            "column.description": {
                "book_id": "Stable, opaque identifier for a book.",
                "title": "Display title shown to customers.",
                "author": "Free-text author / creator name.",
            },
            "cross_data.md": [
                {
                    "action_id": "a1",
                    "action": "create",
                    "title": "How orders reference books",
                    "body": (
                        "`orders.order_items.book_id` joins to `catalog.title_master.book_id`."
                    ),
                    "related_assets": [
                        "urn:li:dataset:(urn:li:dataPlatform:postgres,orders.order_items,PROD)",
                        "urn:li:dataset:(urn:li:dataPlatform:postgres,catalog.title_master,PROD)",
                    ],
                    "confidence": 0.81,
                }
            ],
        }
        # field_status keys are the PATCH field-reference format (flat):
        #   cross_data.md.<action_id> and column.description.<fieldPath>
        # spec: BACKEND_SCHEMA.md §metagen_results L135
        field_status = {
            "dataset.description": "pending",
            "column.description.book_id": "pending",
            "column.description.title": "pending",
            "column.description.author": "pending",
            "cross_data.md.a1": "pending",
        }
        result_id = await _seed_pending_metagen_result(
            async_session,
            dataset_urn=_TEST_URN,
            proposals=proposals,
            field_status=field_status,
        )
        encoded_result_id = urllib.parse.quote(result_id, safe="")
        patch_url = (
            f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/result/{encoded_result_id}"
        )

        # ── Step b: First PATCH — approve a subset of fields ─────────────────
        # UC4 narrative: "The reviewer approves the table description and 4 of 5
        # columns … PATCH .../attr/metagen/result/7e8b… {verdict: approve, fields: [...]}"
        # spec: USE_CASE_en.md §UC4 L594-L607
        approve_resp = await api_client.patch(
            patch_url,
            headers=admin_headers,
            json={
                "verdict": "approve",
                "fields": [
                    "dataset.description",
                    "column.description.book_id",
                    "column.description.title",
                ],
                "reason": "Approved as generated.",
            },
        )
        assert approve_resp.status_code == 200, (
            f"First PATCH (approve subset) failed: {approve_resp.status_code} {approve_resp.text}"
        )
        approve_body = approve_resp.json()
        assert approve_body["id"] == result_id
        fs_after_approve = approve_body["field_status"]

        # Approved fields must flip to 'approved'
        # spec: USE_CASE_en.md §UC4 — verdict=approve on specific fields flips those to approved
        approved_fields = [
            "dataset.description",
            "column.description.book_id",
            "column.description.title",
        ]
        for field in approved_fields:
            assert fs_after_approve.get(field) == "approved", (
                f"Field {field!r} should be 'approved' after first PATCH; "
                f"got {fs_after_approve.get(field)!r}. spec: USE_CASE_en.md §UC4 L594-L607"
            )

        # Non-approved fields must remain 'pending'
        for field in ["column.description.author", "cross_data.md.a1"]:
            assert fs_after_approve.get(field) == "pending", (
                f"Field {field!r} should still be 'pending' after partial approve; "
                f"got {fs_after_approve.get(field)!r}. spec: USE_CASE_en.md §UC4 L594-L607"
            )

        # ── Step c: Second PATCH — reject the cross_data.md action ───────────
        # UC4 narrative: "a third PATCH rejects the proposed cross_data.md create action
        # with {verdict: reject, fields: ['cross_data.md.a1'], reason: '...'}"
        # spec: USE_CASE_en.md §UC4 L609-L615
        reject_resp = await api_client.patch(
            patch_url,
            headers=admin_headers,
            json={
                "verdict": "reject",
                "fields": ["cross_data.md.a1"],
                "reason": "Reject proposed cross-data doc — not needed at this stage.",
            },
        )
        assert reject_resp.status_code == 200, (
            f"Second PATCH (reject) failed: {reject_resp.status_code} {reject_resp.text}"
        )
        reject_body = reject_resp.json()
        fs_after_reject = reject_body["field_status"]
        assert fs_after_reject.get("cross_data.md.a1") == "rejected", (
            f"Field 'cross_data.md.a1' should be 'rejected' after second PATCH; "
            f"got {fs_after_reject.get('cross_data.md.a1')!r}. "
            "spec: USE_CASE_en.md §UC4 L609-L615"
        )
        # spec: BACKEND.md L296-L302 — field-level review preserves prior status for
        # fields not referenced in the current PATCH
        assert fs_after_reject.get("dataset.description") == "approved", (
            f"'dataset.description' should remain 'approved' after reject PATCH; "
            f"got {fs_after_reject.get('dataset.description')!r}. "
            "spec: BACKEND.md §Metadata Generation Service L296-L302"
        )
        assert fs_after_reject.get("column.description.book_id") == "approved", (
            f"'column.description.book_id' should remain 'approved' after reject PATCH; "
            f"got {fs_after_reject.get('column.description.book_id')!r}. "
            "spec: BACKEND.md §Metadata Generation Service L296-L302"
        )
        assert fs_after_reject.get("column.description.title") == "approved", (
            f"'column.description.title' should remain 'approved' after reject PATCH; "
            f"got {fs_after_reject.get('column.description.title')!r}. "
            "spec: BACKEND.md §Metadata Generation Service L296-L302"
        )
        assert fs_after_reject.get("column.description.author") == "pending", (
            f"'column.description.author' should remain 'pending' (never touched); "
            f"got {fs_after_reject.get('column.description.author')!r}. "
            "spec: BACKEND.md §Metadata Generation Service L296-L302"
        )

    finally:
        # ── Step d: Cleanup ───────────────────────────────────────────────────
        if result_id is not None:
            await _delete_metagen_result(async_session, result_id, _TEST_URN)
        await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_uc4_review_approves_cross_data_create_action(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """UC4 invariant: approving a cross_data.md action flips its field_status to 'approved'.

    Seeds a metagen_results row whose proposals['cross_data.md'] carries one 'create'
    action (spec shape: list of action dicts) and whose field_status has the flat
    cross_data.md.a1 key in 'pending' state.

    PATCH with verdict=approve + fields=['cross_data.md.a1'] must:
      - return HTTP 200
      - set field_status['cross_data.md.a1'] == 'approved'

    Note on DataHub emission: on approval the service calls apply_actions() which
    attempts to emit a documentInfo MCP.  In test-mode the DataHub client is real
    (not stubbed — see spec/TESTING.md §Test-Mode Stubs).  If DataHub is unreachable
    the apply_actions() call is logged as a warning and swallowed (best-effort per
    _apply_approved_fields implementation); field_status is committed first, so the
    assertion on field_status['cross_data.md.a1'] == 'approved' is authoritative
    regardless of whether the DataHub emit succeeded.

    spec: USE_CASE_en.md §UC4 L594-L615
    spec: BACKEND.md §Cross-data MD action types (create row)
    spec: BACKEND_SCHEMA.md §metagen_results L134-L135
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"

    result_id: str | None = None

    try:
        # Ensure metagen conf exists for the URN
        await api_client.put(
            base_conf,
            headers=admin_headers,
            json={
                "targets": ["cross_data.md"],
                "schedule_tier": "weekly",
                "is_enabled": False,
                "owner": "uc4-api-wired@imazon.com",
            },
        )

        # ── Seed a pending metagen_results row with a cross_data.md create action ──
        # proposals['cross_data.md'] is a list of action dicts per
        # spec/feature/BACKEND_SCHEMA.md §metagen_results L134.
        # spec: BACKEND.md §Cross-data MD action types — create requires title, body,
        # and related_assets (≥1 urn:li:dataset: URN).
        proposals: dict[str, Any] = {
            "cross_data.md": [
                {
                    "action_id": "a1",
                    "action": "create",
                    "title": "How orders reference books",
                    "body": (
                        "`orders.order_items.book_id` joins to `catalog.title_master.book_id`."
                    ),
                    "related_assets": [
                        "urn:li:dataset:(urn:li:dataPlatform:postgres,orders.order_items,PROD)",
                        "urn:li:dataset:(urn:li:dataPlatform:postgres,catalog.title_master,PROD)",
                    ],
                    "confidence": 0.81,
                }
            ]
        }
        # field_status key is the PATCH field-reference format:
        # cross_data.md.<action_id> per spec/feature/BACKEND_SCHEMA.md L135
        field_status = {"cross_data.md.a1": "pending"}

        result_id = await _seed_pending_metagen_result(
            async_session,
            dataset_urn=_TEST_URN,
            proposals=proposals,
            field_status=field_status,
        )
        encoded_result_id = urllib.parse.quote(result_id, safe="")
        patch_url = (
            f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/result/{encoded_result_id}"
        )

        # ── PATCH: approve the cross_data.md.a1 action ───────────────────────
        # spec: USE_CASE_en.md §UC4 L594-L607 — verdict=approve + fields=[action ref]
        # flips that action's field_status entry to 'approved'.
        # The PATCH field reference cross_data.md.<action_id> is the stable PATCH
        # address for an individual action per BACKEND_SCHEMA.md L134.
        approve_resp = await api_client.patch(
            patch_url,
            headers=admin_headers,
            json={
                "verdict": "approve",
                "fields": ["cross_data.md.a1"],
                "reason": "Cross-data join document approved.",
            },
        )
        assert approve_resp.status_code == 200, (
            f"PATCH approve cross_data.md.a1 failed: {approve_resp.status_code} {approve_resp.text}"
        )
        approve_body = approve_resp.json()
        assert approve_body["id"] == result_id

        # The approved action's field_status must flip to 'approved'
        # spec: USE_CASE_en.md §UC4 L594-L607
        # spec: BACKEND.md §Approval flow — verdict=approve + fields=[...] approves
        # only the listed field paths / cross-data action IDs
        assert approve_body["field_status"].get("cross_data.md.a1") == "approved", (
            f"field_status['cross_data.md.a1'] should be 'approved' after PATCH; "
            f"got {approve_body['field_status'].get('cross_data.md.a1')!r}. "
            "spec: BACKEND.md §Approval flow; BACKEND_SCHEMA.md §metagen_results L135"
        )

    finally:
        if result_id is not None:
            await _delete_metagen_result(async_session, result_id, _TEST_URN)
        await api_client.delete(base_conf, headers=admin_headers)


# ── New-boundary + coupling tests ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_uc4_whole_proposal_reject(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """UC4 invariant: PATCH verdict=reject with no fields array rejects all field_status entries.

    spec: BACKEND.md L446 — 'verdict: reject → reject the whole proposal (or the listed
    fields only).' When fields is absent, every entry in field_status must flip to 'rejected'.
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"

    result_id: str | None = None

    try:
        await api_client.put(
            base_conf,
            headers=admin_headers,
            json={
                "targets": ["dataset.description", "column.description"],
                "schedule_tier": "weekly",
                "is_enabled": False,
                "owner": "uc4-api-wired@imazon.com",
            },
        )

        # ── Seed a multi-field pending result ────────────────────────────────
        # spec: TESTING.md §Api-Wired Integration Tests — setup may use raw SQL
        proposals: dict[str, Any] = {
            "dataset.description": "Whole-reject test description.",
            "column.description": {
                "book_id": "Whole-reject book_id desc.",
                "title": "Whole-reject title desc.",
            },
        }
        field_status = {
            "dataset.description": "pending",
            "column.description.book_id": "pending",
            "column.description.title": "pending",
        }
        result_id = await _seed_pending_metagen_result(
            async_session,
            dataset_urn=_TEST_URN,
            proposals=proposals,
            field_status=field_status,
        )
        encoded_result_id = urllib.parse.quote(result_id, safe="")
        patch_url = (
            f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/result/{encoded_result_id}"
        )

        # ── PATCH: reject whole proposal (no fields array) ───────────────────
        # spec: BACKEND.md L446 — reject without fields rejects all entries
        reject_resp = await api_client.patch(
            patch_url,
            headers=admin_headers,
            json={
                "verdict": "reject",
                "reason": "uc4-api-wired: whole reject test",
            },
        )
        assert reject_resp.status_code == 200, (
            f"PATCH whole-reject failed: {reject_resp.status_code} {reject_resp.text}"
        )
        body = reject_resp.json()
        assert body["id"] == result_id
        # Every field_status entry must be 'rejected'
        # spec: BACKEND.md L446 — verdict=reject with no fields rejects the whole proposal
        for field, status_val in body["field_status"].items():
            assert status_val == "rejected", (
                f"Field {field!r} should be 'rejected' after whole-proposal reject; "
                f"got {status_val!r}. spec: BACKEND.md L446"
            )
    finally:
        if result_id is not None:
            await _delete_metagen_result(async_session, result_id, _TEST_URN)
        await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_uc4_cross_data_modify_and_delete_actions(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """UC4 invariant: approving modify and delete cross_data.md actions flips their field_status.

    Seeds a result with two cross_data.md actions:
      - a1: modify (requires document_urn and body)
      - a2: delete (requires document_urn only)
    PATCH approve both actions; assert both flip to 'approved'.

    spec: USE_CASE_en.md L594-L597 — modify/delete action semantics.
    spec: BACKEND.md §Cross-data MD action types — modify: document_urn + body; delete:
    document_urn only.
    spec: BACKEND_SCHEMA.md L134-L135 — field_status uses cross_data.md.<action_id> keys.
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"

    result_id: str | None = None

    try:
        await api_client.put(
            base_conf,
            headers=admin_headers,
            json={
                "targets": ["cross_data.md"],
                "schedule_tier": "weekly",
                "is_enabled": False,
                "owner": "uc4-api-wired@imazon.com",
            },
        )

        # ── Seed a result with modify + delete actions ─────────────────────
        # spec: BACKEND.md §Cross-data MD action types — modify requires document_urn
        # and body; delete requires document_urn only.
        # NOTE: document_urn values below are fictitious; on approval apply_actions() warns
        # and continues per impl best-effort emit (src/backend/metagen/service.py ~L1085).
        # This test asserts the DataSpoke field_status flip only; end-to-end DataHub apply
        # for modify/delete actions is out of scope for this test.
        proposals: dict[str, Any] = {
            "cross_data.md": [
                {
                    "action_id": "a1",
                    "action": "modify",
                    "document_urn": "urn:li:document:uc4-test-existing-doc-1",
                    "body": "Updated cross-data body for modify action test.",
                    "confidence": 0.78,
                },
                {
                    "action_id": "a2",
                    "action": "delete",
                    "document_urn": "urn:li:document:uc4-test-existing-doc-2",
                    "confidence": 0.65,
                },
            ]
        }
        field_status = {
            "cross_data.md.a1": "pending",
            "cross_data.md.a2": "pending",
        }
        result_id = await _seed_pending_metagen_result(
            async_session,
            dataset_urn=_TEST_URN,
            proposals=proposals,
            field_status=field_status,
        )
        encoded_result_id = urllib.parse.quote(result_id, safe="")
        patch_url = (
            f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/result/{encoded_result_id}"
        )

        # ── PATCH: approve both modify and delete actions ─────────────────────
        # spec: USE_CASE_en.md L594-L597 — each action independently approvable via PATCH
        approve_resp = await api_client.patch(
            patch_url,
            headers=admin_headers,
            json={
                "verdict": "approve",
                "fields": ["cross_data.md.a1", "cross_data.md.a2"],
                "reason": "uc4-api-wired: modify+delete action approve",
            },
        )
        assert approve_resp.status_code == 200, (
            f"PATCH approve modify+delete actions failed: "
            f"{approve_resp.status_code} {approve_resp.text}"
        )
        body = approve_resp.json()
        assert body["id"] == result_id
        # Both actions must flip to 'approved'
        # spec: BACKEND_SCHEMA.md L134-L135 — field_status key is cross_data.md.<action_id>
        assert body["field_status"].get("cross_data.md.a1") == "approved", (
            f"field_status['cross_data.md.a1'] (modify) should be 'approved'; "
            f"got {body['field_status'].get('cross_data.md.a1')!r}. "
            "spec: USE_CASE_en.md L594-L597; BACKEND_SCHEMA.md L134"
        )
        assert body["field_status"].get("cross_data.md.a2") == "approved", (
            f"field_status['cross_data.md.a2'] (delete) should be 'approved'; "
            f"got {body['field_status'].get('cross_data.md.a2')!r}. "
            "spec: USE_CASE_en.md L594-L597; BACKEND_SCHEMA.md L134"
        )
        # Shape-check: action_id and action must be present in seeded proposals
        actions_by_id = {a["action_id"]: a for a in body["proposals"]["cross_data.md"]}
        assert "a1" in actions_by_id and actions_by_id["a1"]["action"] == "modify", (
            f"Proposal a1 should have action='modify'; got {actions_by_id.get('a1')!r}. "
            "spec: BACKEND.md §Cross-data MD action types"
        )
        assert "a2" in actions_by_id and actions_by_id["a2"]["action"] == "delete", (
            f"Proposal a2 should have action='delete'; got {actions_by_id.get('a2')!r}. "
            "spec: BACKEND.md §Cross-data MD action types"
        )
    finally:
        if result_id is not None:
            await _delete_metagen_result(async_session, result_id, _TEST_URN)
        await api_client.delete(base_conf, headers=admin_headers)


@pytest.mark.asyncio
async def test_uc4_reads_uc3_approved_nodes(
    api_client: httpx.AsyncClient,
    admin_headers: dict[str, str],
    async_session: AsyncSession,
) -> None:
    """UC3 → UC4 coupling: metagen run succeeds when approved ontogen nodes exist.

    Setup (raw SQL):
      1. Insert an approved ontogen_nodes row.
      2. Insert a dataset_node_map row linking that node to _TEST_URN with status='approved'.
      3. PUT metagen conf with targets=['dataset.description'], is_enabled=True.
      4. POST method/metagen/run with dry_run=False.
    Assert:
      - HTTP 200 with proposals dict present.
    Cleanup:
      - DELETE metagen result row, dataset_node_map row, ontogen_nodes row, conf.

    This test proves the UC4 service correctly joins dataset_node_map on status='approved'
    without exploding, and that the UC3 → UC4 data handoff is plumbed end-to-end.

    spec: BACKEND.md L425-L426 — 'UC3-approved nodes and triples filtered by
    dataset_node_map.status=approved'.
    spec: src/shared/db/models.py:431-451 — DatasetNodeMap schema.
    """
    base_conf = f"/api/v1/spoke/common/data/{_ENCODED_URN}/attr/metagen/conf"
    base_run = f"/api/v1/spoke/common/data/{_ENCODED_URN}/method/metagen/run"

    suffix = uuid.uuid4().hex[:8]
    node_id = f"uc4-coupling-{suffix}"
    run_result_id: str | None = None

    try:
        # ── Step 1-2: Seed approved UC3 node + dataset_node_map row ──────────
        # spec: TESTING.md §Api-Wired Integration Tests — setup may use raw SQL
        await _seed_approved_ontogen_node(
            async_session, node_id, f"Uc4CouplingNode-{suffix}"
        )
        await _seed_dataset_node_map(
            async_session,
            dataset_urn=_TEST_URN,
            node_id=node_id,
            status="approved",
        )

        # ── Step 3: PUT metagen conf ──────────────────────────────────────────
        # spec: USE_CASE_en.md §UC4 L541-L549
        put_resp = await api_client.put(
            base_conf,
            headers=admin_headers,
            json={
                "targets": ["dataset.description"],
                "schedule_tier": "weekly",
                "is_enabled": True,
                "owner": "uc4-coupling-test@imazon.com",
            },
        )
        assert put_resp.status_code in (200, 201), (
            f"PUT metagen conf failed: {put_resp.status_code} {put_resp.text}"
        )

        # ── Step 4: POST metagen run ──────────────────────────────────────────
        # spec: USE_CASE_en.md §UC4 L551-L553
        run_resp = await api_client.post(
            base_run,
            headers=admin_headers,
            json={"dry_run": False},
        )
        assert run_resp.status_code == 200, (
            f"POST metagen run failed: {run_resp.status_code} {run_resp.text}. "
            "UC3 → UC4 coupling: dataset_node_map join must not blow up when approved "
            "rows exist. spec: BACKEND.md L425-L426"
        )
        run_body = run_resp.json()

        # ── Assert: 200 with proposals dict (no LLM-output assertions) ───────
        # spec: USE_CASE_en.md §UC4 — MetagenRunResponse: id, dataset_urn, proposals
        assert "proposals" in run_body and isinstance(run_body["proposals"], dict), (
            f"MetagenRunResponse must carry 'proposals' (dict); got {run_body!r}. "
            "spec: USE_CASE_en.md §UC4 L551-L553"
        )
        assert run_body.get("dataset_urn") == _TEST_URN, (
            f"MetagenRunResponse dataset_urn expected {_TEST_URN!r}; "
            f"got {run_body.get('dataset_urn')!r}"
        )
        run_result_id = run_body.get("id")

    finally:
        # ── Cleanup: DELETE result, dataset_node_map, ontogen_node, conf ─────
        if run_result_id is not None:
            await _delete_metagen_result(async_session, run_result_id, _TEST_URN)
        # _delete_ontogen_node cascades to dataset_node_map first
        await _delete_ontogen_node(async_session, node_id)
        await api_client.delete(base_conf, headers=admin_headers)
