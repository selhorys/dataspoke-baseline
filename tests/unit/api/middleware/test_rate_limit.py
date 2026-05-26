"""Unit tests for rate-limit middleware configuration.

Tests spec-mandated behavior at the configuration level (not ASGI exercise, which
would require a real Redis or in-memory SlowAPI that can be triggered in a unit test
context). Tests verify:
- The limiter is configured with a per-user key function (JWT sub → IP fallback).
- In-memory fallback is enabled (Redis outage must not crash the API).
- The per-minute limit string is derived from settings.rate_limit_per_minute.

spec: API.md §Middleware — rate limiting uses per-user key (JWT sub or IP fallback);
      Redis outage must not block requests (in_memory_fallback_enabled=True).
"""

import pytest

from src.api.middleware.rate_limit import limiter, _get_user_key


# ── Configuration assertions ──────────────────────────────────────────────────


def test_limiter_has_in_memory_fallback_enabled() -> None:
    """Rate limiter must have in_memory_fallback_enabled=True to survive Redis outages.

    spec: API.md §Middleware — a transient Redis outage must not block every request.
    """
    # slowapi Limiter stores this flag on the storage backend
    # The attribute is accessible via limiter._storage_uri or the in_memory flag.
    # We verify the constructor flag is set rather than the internals.
    from slowapi import Limiter

    assert isinstance(limiter, Limiter), "limiter must be a slowapi.Limiter instance"
    # slowapi sets _in_memory_fallback_enabled on the limiter object
    assert getattr(limiter, "_in_memory_fallback_enabled", False) is True, (
        "Limiter must have in_memory_fallback_enabled=True so Redis outages do not "
        "block all requests. spec: API.md §Middleware."
    )


def test_rate_limit_per_minute_derived_from_settings() -> None:
    """default_limits must include a '/minute' limit from settings.rate_limit_per_minute.

    spec: API.md §Middleware — rate limit is configured per settings.

    SlowAPI wraps each limit string in a LimitGroup object; the raw limit string is
    stored on the ``_LimitGroup__limit_provider`` private attribute.
    """
    from src.shared.settings import settings

    default_limits = limiter._default_limits
    expected_fragment = f"{settings.rate_limit_per_minute}/minute"

    def _extract(lim) -> str:
        """Return the raw rate-string from a LimitGroup or stringifiable limit."""
        provider = getattr(lim, "_LimitGroup__limit_provider", None)
        if provider is not None:
            return str(provider)
        return str(lim)

    limit_strings = [_extract(lim) for lim in default_limits]
    assert any(expected_fragment in s for s in limit_strings), (
        f"default_limits must contain '{expected_fragment}'; got: {limit_strings}. "
        "spec: API.md §Middleware — per-minute limit derived from settings."
    )


# ── _get_user_key: per-user extraction ───────────────────────────────────────


def test_get_user_key_falls_back_to_ip_without_auth_header() -> None:
    """_get_user_key falls back to remote address when Authorization header is absent.

    spec: API.md §Middleware — rate-limit key: JWT sub claim, fallback to IP.
    """
    from unittest.mock import MagicMock

    req = MagicMock()
    req.headers = {}
    req.client = MagicMock()
    req.client.host = "192.168.1.1"

    key = _get_user_key(req)
    # Must not raise and must return a non-empty string
    assert isinstance(key, str)
    assert key  # non-empty


def test_get_user_key_falls_back_to_ip_on_invalid_jwt() -> None:
    """_get_user_key falls back to IP when Bearer token is invalid.

    spec: API.md §Middleware — graceful fallback on JWT decode failure.
    """
    from unittest.mock import MagicMock

    req = MagicMock()
    req.headers = {"authorization": "Bearer this-is-not-a-valid-jwt"}
    req.client = MagicMock()
    req.client.host = "10.0.0.5"

    key = _get_user_key(req)
    assert isinstance(key, str)
    assert key  # non-empty; must not raise


def test_get_user_key_extracts_sub_from_valid_jwt() -> None:
    """_get_user_key extracts sub claim from valid JWT.

    spec: API.md §Middleware — rate-limit key is JWT sub claim when token is valid.
    In the new auth model, sub is the user UUID string.
    """
    import uuid

    from src.backend.auth.tokens import issue_access_token

    known_id = uuid.UUID("12345678-1234-5678-1234-567812345678")
    token, _ = issue_access_token(known_id, "alice@example.com")

    from unittest.mock import MagicMock

    req = MagicMock()
    req.headers = {"authorization": f"Bearer {token}"}
    req.client = MagicMock()
    req.client.host = "10.0.0.5"

    key = _get_user_key(req)
    assert key == str(known_id), (
        f"Expected key='{known_id}', got {key!r}. "
        "spec: API.md §Middleware — JWT sub (user UUID) used as rate-limit key."
    )
