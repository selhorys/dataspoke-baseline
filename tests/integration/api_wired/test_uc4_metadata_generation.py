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

# UC4 dataset: catalog.title_master — Imazon primary catalog table
# spec: TESTING.md §Imazon Dummy-Data Reference — inventory.book_stock/catalog.title_master UC4
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
