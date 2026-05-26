"""Spot integration test: API token effective-role intersection (Admin-snapshot / Reader-current).

Scenario:
1. Seed an Admin user; mint a token via POST /auth/api-tokens (role_snapshot = Admin).
2. Demote the user to Reader via direct DB update.
3. With the original dsk_ token, attempt a write — must return 403 READ_ONLY_ROLE
   because min(Admin, Reader) = Reader rejects the write.
4. With the same token, attempt a GET — must return 200 (Reader can still read).

Note: requires dev environment running. Skipped automatically when integration
infrastructure is not available (conftest.py preflight handles this).

spec: spec/feature/AUTH.md §API Tokens §Effective privilege — intersection:
      effective_role = min(token.role_snapshot, owner.users.role).
spec: spec/feature/AUTH.md §Failure Modes — API token whose owner was demoted:
      write attempts return 403 READ_ONLY_ROLE.
"""

import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Simple spoke endpoint used as the write target — auth check fires before body validation
_WRITE_URL = "/api/v1/spoke/common/metagen/method/run"
_READ_URL = "/api/v1/spoke/common/ingestion"


def _unique_email(prefix: str = "intersection") -> str:
    return f"{prefix}-{str(uuid.uuid4())[:8]}@test.dataspoke.example.com"


@pytest.mark.asyncio
async def test_api_token_intersection_demote_blocks_write(
    api_client: httpx.AsyncClient,
    async_session: AsyncSession,
) -> None:
    """Admin-snapshot token + Reader-current role → 403 READ_ONLY_ROLE on writes; 200 on reads.

    Seed path (no /auth/register, to avoid rate limits):
    - Insert user with role=Admin directly in DB.
    - Mint a dsk_ token via POST /auth/api-tokens (role_snapshot captured = Admin).
    - Directly demote user to Reader via SQL UPDATE.
    - Attempt write with the token → 403 READ_ONLY_ROLE.
    - Attempt read with the same token → 200 (authentication valid, just Reader-limited).

    spec: spec/feature/AUTH.md §API Tokens §Effective privilege —
          effective_role = min(token.role_snapshot, owner.users.role).
    spec: spec/feature/AUTH.md §Failure Modes — API token whose owner was demoted:
          effective privilege drops to min(snapshot, current role).
    """
    from src.backend.auth import users as user_service

    email = _unique_email()

    # Seed Admin user via create_user (avoids /auth/register rate limit; avoids DataHub mirror).
    # create_user handles the password hash — test only holds the plaintext.
    user = await user_service.create_user(
        async_session, email, "Intersection Test User", password="password1234", role="Admin"
    )
    await async_session.commit()
    user_id = user.id

    try:
        # Login to get a JWT for the Admin user
        login_resp = await api_client.post(
            "/api/v1/auth/token",
            json={"email": email, "password": "password1234"},
        )
        assert login_resp.status_code == 200, (
            f"Admin user login failed: {login_resp.text}"
        )
        jwt_access_token = login_resp.json()["access_token"]

        # Mint an API token while the user is Admin (role_snapshot = Admin)
        mint_resp = await api_client.post(
            "/api/v1/auth/api-tokens",
            json={"name": "intersection-test-token"},
            headers={"Authorization": f"Bearer {jwt_access_token}"},
        )
        assert mint_resp.status_code == 201, (
            f"Minting token for Admin user must succeed, got {mint_resp.status_code}: {mint_resp.text}"
        )
        raw_token = mint_resp.json()["token"]
        role_snapshot = mint_resp.json()["role_snapshot"]

        assert raw_token.startswith("dsk_"), "Minted token must start with dsk_"
        assert role_snapshot == "Admin", (
            f"role_snapshot must be Admin at mint time, got: {role_snapshot}"
        )

        # Verify the token works for writes before demotion
        write_before = await api_client.post(
            _WRITE_URL,
            json={},
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert write_before.status_code != 403, (
            f"Admin token must NOT return 403 before demotion, got {write_before.status_code}"
        )

        # Directly demote the user to Reader via SQL UPDATE
        await async_session.execute(
            text("UPDATE dataspoke.users SET role = 'Reader' WHERE id = :id"),
            {"id": str(user_id)},
        )
        await async_session.commit()

        # After demotion: effective_role = min(Admin, Reader) = Reader
        # Write must be rejected with 403 READ_ONLY_ROLE
        write_after = await api_client.post(
            _WRITE_URL,
            json={},
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert write_after.status_code == 403, (
            f"min(Admin, Reader)=Reader must reject writes with 403 READ_ONLY_ROLE "
            f"per spec/feature/AUTH.md §API Tokens §Effective privilege, "
            f"got {write_after.status_code}: {write_after.text}"
        )
        assert write_after.json().get("error_code") == "READ_ONLY_ROLE", (
            "Error code must be READ_ONLY_ROLE per spec/feature/AUTH.md §Failure Modes"
        )

        # Read must still succeed (Reader can GET /spoke/*)
        read_after = await api_client.get(
            _READ_URL,
            headers={"Authorization": f"Bearer {raw_token}"},
        )
        assert read_after.status_code == 200, (
            f"min(Admin, Reader)=Reader must still allow GETs per spec/feature/AUTH.md §Privilege Model, "
            f"got {read_after.status_code}: {read_after.text}"
        )

    finally:
        # CASCADE removes api_tokens; then remove user
        await async_session.execute(
            text("DELETE FROM dataspoke.users WHERE id = :id"),
            {"id": str(user_id)},
        )
        await async_session.commit()
