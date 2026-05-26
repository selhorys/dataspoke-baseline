"""Spot integration test: POST /auth/register.

Concerns covered:
- Successful registration with valid body creates a users row and returns tokens
- Duplicate email registration returns 409 EMAIL_ALREADY_REGISTERED
- Short password (< 10 chars) returns 422 validation error
- Registered user's profile (GET /auth/me) returns correct shape without password_hash

spec: spec/feature/AUTH.md §Lifecycle §Email + password registration
spec: spec/API.md §Auth POST /auth/register
"""

import uuid

import httpx
import pytest

# No dummy-data schemas needed — auth tests are self-contained
_TEST_EMAIL_BASE = "spot-register-{uid}@test.dataspoke.example.com"


def _unique_email() -> str:
    return _TEST_EMAIL_BASE.format(uid=str(uuid.uuid4())[:8])


@pytest.mark.asyncio
async def test_register_valid_user_returns_tokens(
    api_client: httpx.AsyncClient,
) -> None:
    """POST /auth/register with valid body returns access_token and sets refresh cookie.

    spec: spec/feature/AUTH.md §Lifecycle §Email + password registration — on success,
    returns an access + refresh token pair so the user is immediately logged in.
    spec: spec/API.md §Auth POST /auth/register — returns TokenResponse.
    """
    email = _unique_email()
    resp = await api_client.post(
        "/api/v1/auth/register",
        json={"email": email, "name": "Spot Test User", "password": "password1234"},
    )

    assert resp.status_code == 201, (
        f"POST /auth/register with valid body must return 201 "
        f"per spec/API.md §Auth, got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert "access_token" in body, "register response must include access_token"
    assert "expires_in" in body, "register response must include expires_in"
    assert body["expires_in"] == 900, "expires_in must be 900s (15 min) per spec/API.md §Token Strategy"

    # Refresh token must be in the HttpOnly cookie
    assert "refresh_token" in resp.cookies, (
        "register must set HttpOnly refresh_token cookie per spec/API.md §Token Strategy"
    )


@pytest.mark.asyncio
async def test_register_creates_user_with_reader_role(
    api_client: httpx.AsyncClient,
) -> None:
    """POST /auth/register creates a user with Reader role and correct profile fields.

    spec: spec/feature/AUTH.md §Lifecycle §Email + password registration —
    role defaults to Reader at registration.
    spec: spec/API.md §Auth GET /auth/me — returns {id, email, name, has_google, role, ...}.
    """
    email = _unique_email()
    reg_resp = await api_client.post(
        "/api/v1/auth/register",
        json={"email": email, "name": "Reader User", "password": "password1234"},
    )
    assert reg_resp.status_code == 201

    access_token = reg_resp.json()["access_token"]
    me_resp = await api_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert me_resp.status_code == 200

    me = me_resp.json()
    assert me["email"] == email
    assert me["name"] == "Reader User"
    assert me["role"] == "Reader", (
        "Default role at registration must be Reader "
        "per spec/feature/AUTH.md §Lifecycle §Email + password registration"
    )
    assert "has_google" in me
    assert me["has_google"] is False  # no Google sub
    assert "password_hash" not in me, (
        "password_hash must NEVER be returned by GET /auth/me "
        "per spec/API.md §Auth GET /auth/me"
    )
    assert "id" in me
    assert "created_at" in me
    assert "updated_at" in me


@pytest.mark.asyncio
async def test_register_duplicate_email_returns_conflict(
    api_client: httpx.AsyncClient,
) -> None:
    """Second registration with same email returns 409 EMAIL_ALREADY_REGISTERED.

    spec: spec/feature/AUTH.md §Data Model — email is a UNIQUE citext column.
    spec: spec/API.md §Error Catalogue — 409 EMAIL_ALREADY_REGISTERED.
    """
    email = _unique_email()
    # First registration
    first = await api_client.post(
        "/api/v1/auth/register",
        json={"email": email, "name": "First User", "password": "password1234"},
    )
    assert first.status_code == 201

    # Second registration with the same email
    second = await api_client.post(
        "/api/v1/auth/register",
        json={"email": email, "name": "Duplicate User", "password": "password5678"},
    )
    assert second.status_code == 409, (
        f"Duplicate email must return 409 per spec/feature/AUTH.md §Data Model, "
        f"got {second.status_code}: {second.text}"
    )
    assert second.json()["error_code"] == "EMAIL_ALREADY_REGISTERED"


@pytest.mark.asyncio
async def test_register_short_password_returns_422(
    api_client: httpx.AsyncClient,
) -> None:
    """POST /auth/register with password < 10 chars returns 422 validation error.

    spec: spec/feature/AUTH.md §Lifecycle §Email + password registration —
    password must be at least 10 characters.
    """
    resp = await api_client.post(
        "/api/v1/auth/register",
        json={"email": _unique_email(), "name": "Short Pass", "password": "short"},
    )
    assert resp.status_code == 422, (
        f"Password < 10 chars must return 422 per spec/feature/AUTH.md §Lifecycle §Registration, "
        f"got {resp.status_code}"
    )


@pytest.mark.asyncio
async def test_register_case_insensitive_duplicate_email_returns_409(
    api_client: httpx.AsyncClient,
) -> None:
    """Second registration with a case-variant of an existing email returns 409 EMAIL_ALREADY_REGISTERED.

    The users.email column is CITEXT — case-insensitive unique. Registering
    'FOO@example.com' when 'foo@example.com' is already registered must be rejected.

    spec: spec/feature/AUTH.md §Data Model — email is a UNIQUE citext column;
          CITEXT enforces case-insensitive uniqueness.
    spec: spec/API.md §Error Catalogue — 409 EMAIL_ALREADY_REGISTERED.
    """
    base_email = _unique_email()
    # First registration with lowercase email
    first = await api_client.post(
        "/api/v1/auth/register",
        json={"email": base_email, "name": "Original User", "password": "password1234"},
    )
    assert first.status_code == 201, (
        f"First registration must succeed, got {first.status_code}: {first.text}"
    )

    # Second registration with uppercase variant of the same email
    upper_email = base_email.upper()
    second = await api_client.post(
        "/api/v1/auth/register",
        json={"email": upper_email, "name": "Case Duplicate", "password": "password5678"},
    )
    assert second.status_code == 409, (
        f"Duplicate email (case-insensitive via CITEXT) must return 409 EMAIL_ALREADY_REGISTERED "
        f"per spec/feature/AUTH.md §Data Model, got {second.status_code}: {second.text}"
    )
    assert second.json()["error_code"] == "EMAIL_ALREADY_REGISTERED", (
        "error_code must be EMAIL_ALREADY_REGISTERED per spec/API.md §Error Catalogue"
    )
