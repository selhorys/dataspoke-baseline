"""Google OAuth helpers for DataSpoke.

Responsibilities:
- Build and memoize an authlib OAuth client for Google.
- Resolve or create a DataSpoke user from the Google ID-token claims.

The module is intentionally side-effect-free at import time: the OAuth
client is built lazily on first call so that the module can be imported
in test environments without valid credentials.

OAuth state and nonce management is delegated to authlib's session-backed
mechanism (via Starlette SessionMiddleware). The session is HMAC-signed
with ``oauth_state_secret``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from authlib.integrations.starlette_client import OAuth
    from sqlalchemy.ext.asyncio import AsyncSession

    from src.shared.db.models import User

# ── Configuration check ───────────────────────────────────────────────────────


def is_configured(settings: Any) -> bool:
    """Return True when Google OAuth credentials and session secret are all present."""
    return bool(
        getattr(settings, "google_oauth_client_id", "")
        and getattr(settings, "google_oauth_client_secret", "")
        and getattr(settings, "oauth_state_secret", "")
    )


# ── OAuth client ──────────────────────────────────────────────────────────────

_oauth_instance: OAuth | None = None


def build_oauth_client(settings: Any) -> OAuth:
    """Return a memoized authlib OAuth client registered with the Google provider.

    Registers ``google`` with the OpenID Connect discovery URL so authlib
    fetches the JWKS endpoint automatically.

    The return value is module-level memoized — subsequent calls with
    different settings objects are not supported. This is intentional:
    credentials come from environment settings which are process-constant.
    """
    global _oauth_instance
    if _oauth_instance is not None:
        return _oauth_instance

    from authlib.integrations.starlette_client import OAuth

    oauth = OAuth()
    oauth.register(
        name="google",
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_id=settings.google_oauth_client_id,
        client_secret=settings.google_oauth_client_secret,
        client_kwargs={"scope": "openid email profile"},
    )
    _oauth_instance = oauth
    return _oauth_instance


def invalidate_oauth_client() -> None:
    """Reset the memoized OAuth client instance.

    Used when OAuth credentials are rotated at runtime (mirrors the
    invalidate_*_cache() pattern used by other peripheral clients).
    """
    global _oauth_instance
    _oauth_instance = None


# ── User resolver ─────────────────────────────────────────────────────────────


async def resolve_or_create_user(
    db: AsyncSession,
    *,
    google_sub: str,
    email: str,
    name: str,
) -> User:
    """Resolve an existing user or create a new one from Google ID-token claims.

    Resolution order per spec/feature/AUTH.md §Google OAuth registration & login:

    1. ``users.get_by_google_sub`` → if found, refresh display name when changed.
    2. ``users.get_by_email`` → if found, link ``google_sub`` onto existing row.
    3. Otherwise create a fresh user with ``password_hash=None``.

    Every branch is DataSpoke-local: no DataHub call is made, so the flow
    succeeds whether or not the DataHub peripheral is configured or reachable.
    The corpuser is provisioned by DataHub's OIDC JIT on first DataHub login,
    and the nightly reconciliation pass projects role and marker-group
    membership onto it from there.

    The caller is responsible for committing the session after this function
    returns.
    """
    from src.backend.auth import users

    # 1. Known google_sub — log in; refresh display name if changed.
    existing = await users.get_by_google_sub(db, google_sub)
    if existing is not None:
        if existing.name != name:
            existing = await users.update_name(db, existing.id, name)
        return existing

    # 2. Known email — link google_sub.
    by_email = await users.get_by_email(db, email)
    if by_email is not None:
        linked = await users.link_google_sub(db, by_email.id, google_sub)
        return linked

    # 3. New user.
    return await users.create_user(
        db,
        email,
        name,
        google_sub=google_sub,
        password=None,
        role="Reader",
    )
