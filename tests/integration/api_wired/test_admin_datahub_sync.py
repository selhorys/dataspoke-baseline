"""API-wired integration tests for POST /internal/admin/datahub/sync.

Tests verify the DataHub sync endpoint end-to-end against the in-cluster
DataSpoke API. Each test seeds its own dataset_registry rows directly via
the test DB session, calls the endpoint, and checks both the response body
and the resulting DB state.

Prerequisites:
- In-cluster DataSpoke server running with DATASPOKE_TEST_MODE=true
  (start via: ./dev_env/dataspoke-test-mode.sh)
- DATASPOKE_INTERNAL_TOKEN set in the test environment
- DataHub GMS accessible and Imazon dummy data ingested
  (run: uv run python -m tests.integration.util --reset-all)

Seeding strategy:
- Row A: a real Imazon URN that exists in DataHub after seed, seeded with
  datahub_registered=False so the endpoint should flip it to True.
- Row B: a fake URN that does NOT exist in DataHub, seeded with
  datahub_registered=True so the endpoint should flip it to False.

All payloads are inlined per feedback_test_readability convention.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.api_wired.spot.conftest import (
    delete_dataset_registry_db,
    seed_dataset_registry,
)

# Imazon URN: catalog.title_master is always ingested into DataHub by seed
# (platform=postgres, instance=example_db, env=DEV — see tests/integration/util/datahub.py)
_IMAZON_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"

# Fake URN: never exists in DataHub
_GHOST_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.public.ghost_table_datahub_sync,DEV)"


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _get_registry_flag(session: AsyncSession, urn: str) -> bool | None:
    """Query datahub_registered for a registry row; returns None if absent."""
    result = await session.execute(
        text(
            "SELECT datahub_registered FROM dataspoke.dataset_registry"
            " WHERE dataset_urn = :urn"
        ),
        {"urn": urn},
    )
    row = result.one_or_none()
    return bool(row[0]) if row is not None else None


# ── Tests ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_datahub_sync_full_sweep(
    internal_http_client,
    async_session: AsyncSession,
):
    """Full sweep reconciles all registry rows against DataHub.

    Row A (real Imazon URN, registered=False) → flipped to True.
    Row B (ghost URN, registered=True) → flipped to False.
    Response counts reflect at least these two flips.
    """
    await seed_dataset_registry(async_session, _IMAZON_URN, datahub_registered=False)
    await seed_dataset_registry(async_session, _GHOST_URN, datahub_registered=True)

    try:
        resp = await internal_http_client.post("/internal/admin/datahub/sync", json={})

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()

        assert isinstance(body["checked"], int) and body["checked"] >= 2
        assert isinstance(body["flipped_true"], int) and body["flipped_true"] >= 1
        assert isinstance(body["flipped_false"], int) and body["flipped_false"] >= 1

        # Requery to confirm DB state was updated
        await async_session.expire_all()
        flag_a = await _get_registry_flag(async_session, _IMAZON_URN)
        flag_b = await _get_registry_flag(async_session, _GHOST_URN)

        assert flag_a is True, f"Row A (real Imazon URN) should be True after sync, got {flag_a}"
        assert flag_b is False, f"Row B (ghost URN) should be False after sync, got {flag_b}"

    finally:
        await delete_dataset_registry_db(async_session, _IMAZON_URN)
        await delete_dataset_registry_db(async_session, _GHOST_URN)
        await async_session.commit()


@pytest.mark.asyncio
async def test_datahub_sync_scoped(
    internal_http_client,
    async_session: AsyncSession,
):
    """Scoped sync: only Row A is listed in dataset_urns.

    Row A flips to True. Row B is untouched (still True because ghost URN
    is not included in the scoped list).
    """
    await seed_dataset_registry(async_session, _IMAZON_URN, datahub_registered=False)
    await seed_dataset_registry(async_session, _GHOST_URN, datahub_registered=True)

    try:
        resp = await internal_http_client.post(
            "/internal/admin/datahub/sync",
            json={"dataset_urns": [_IMAZON_URN]},
        )

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()

        assert body["checked"] == 1
        assert body["flipped_true"] == 1
        assert body["flipped_false"] == 0

        # Requery to confirm only Row A changed
        await async_session.expire_all()
        flag_a = await _get_registry_flag(async_session, _IMAZON_URN)
        flag_b = await _get_registry_flag(async_session, _GHOST_URN)

        assert flag_a is True, f"Row A should be True after scoped sync, got {flag_a}"
        assert flag_b is True, f"Row B should be unchanged (True), got {flag_b}"

    finally:
        await delete_dataset_registry_db(async_session, _IMAZON_URN)
        await delete_dataset_registry_db(async_session, _GHOST_URN)
        await async_session.commit()


@pytest.mark.asyncio
async def test_datahub_sync_invalid_urn_rejected(internal_http_client):
    """dataset_urns containing a string that does not match the URN pattern → 422."""
    resp = await internal_http_client.post(
        "/internal/admin/datahub/sync",
        json={"dataset_urns": ["not-a-urn"]},
    )
    assert resp.status_code == 422, (
        f"Expected 422 for invalid URN pattern, got {resp.status_code}: {resp.text}"
    )


@pytest.mark.asyncio
async def test_datahub_sync_list_too_large_rejected(internal_http_client):
    """dataset_urns with more than 10_000 entries → 422 (max_length validator)."""
    large_list = [
        f"urn:li:dataset:(urn:li:dataPlatform:postgres,db.t{i},PROD)"
        for i in range(10_001)
    ]
    resp = await internal_http_client.post(
        "/internal/admin/datahub/sync",
        json={"dataset_urns": large_list},
    )
    assert resp.status_code == 422, (
        f"Expected 422 for oversized list, got {resp.status_code}: {resp.text}"
    )
