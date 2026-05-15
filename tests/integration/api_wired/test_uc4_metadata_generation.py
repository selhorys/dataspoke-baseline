"""UC4 — Metadata Generation: end-to-end through public REST API.

Maps `spec/USE_CASE_en.md §UC4` paragraphs to executable steps. REST-only
per `spec/TESTING.md §Api-Wired Integration Tests`. This module covers the
UC4 narrative arc and the UC3 → UC4 cross-feature coupling; single-concern
coverage (field-level approve/reject, cross_data create/modify/delete actions,
whole-proposal reject, follow-up PATCH preservation, GENERATION_DISABLED gate)
lives in `tests/integration/spot/test_metagen.py`.

Tests in this module:
  - test_uc4_conf_run_and_latest: PUT metagen conf, run (returns MetagenRunResponse),
    GET latest result envelope, cleanup.
  - test_uc4_reads_uc3_approved_nodes: Seed an approved ontogen_nodes row and a
    dataset_node_map row; POST metagen run; assert 200 with proposals dict
    (UC3 → UC4 coupling test).
"""
# spec: USE_CASE_en.md §UC4

import json
import urllib.parse
import uuid

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
