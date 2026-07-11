"""Unit tests for auth-related schema invariants.

The old per-dataset AuthSpec/CredentialAuth/SecretRefRecord schemas are removed in the
per-source model. This file retains auth-related schema tests that are still relevant
but do not depend on the removed types.

For the current ingestion schema tests (CreateIngestionSourceRequest, SecretRefInfo, etc.)
see tests/unit/api/schemas/test_ingestion_schemas.py.

Spec: spec/feature/AUTH.md
Spec: spec/API.md §Authentication & Authorization
"""

import pytest
from pydantic import ValidationError

from src.api.schemas.auth import (
    RegisterRequest,
    TokenRequest,
)


class TestTokenRequest:
    """Spec: API.md §Auth — POST /auth/token body {email, password}."""

    def test_valid_login(self) -> None:
        req = TokenRequest(email="user@example.com", password="s3cr3t")
        assert req.email == "user@example.com"

    def test_missing_email_raises(self) -> None:
        with pytest.raises(ValidationError):
            TokenRequest(password="s3cr3t")

    def test_missing_password_raises(self) -> None:
        with pytest.raises(ValidationError):
            TokenRequest(email="user@example.com")


class TestRegisterRequest:
    """Spec: API.md §Auth — POST /auth/register body {email, name, password}."""

    def test_valid_register(self) -> None:
        req = RegisterRequest(
            email="new@example.com",
            name="New User",
            password="longpassword1",
        )
        assert req.name == "New User"

    def test_short_password_raises(self) -> None:
        """Password must be >= 10 chars.

        Spec: API.md §Auth — 'password >= 10 chars'.
        """
        with pytest.raises(ValidationError):
            RegisterRequest(
                email="new@example.com",
                name="New User",
                password="short",
            )

    def test_missing_name_raises(self) -> None:
        with pytest.raises(ValidationError):
            RegisterRequest(
                email="new@example.com",
                password="longpassword1",
            )
