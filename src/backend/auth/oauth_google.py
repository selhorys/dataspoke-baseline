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

import logging
from typing import TYPE_CHECKING, Any

from src.shared.exceptions import DataHubSyncError

if TYPE_CHECKING:
    from authlib.integrations.starlette_client import OAuth
    from sqlalchemy.ext.asyncio import AsyncSession

    from src.backend.admin.config_service import RuntimeConfigDTO
    from src.shared.datahub.client import DataHubClient
    from src.shared.db.models import User

logger = logging.getLogger(__name__)

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
    datahub: DataHubClient,
    *,
    google_sub: str,
    email: str,
    name: str,
    runtime_config: RuntimeConfigDTO,
) -> User:
    """Resolve an existing user or create a new one from Google ID-token claims.

    Resolution order per spec/feature/AUTH.md §Google OAuth registration & login:

    1. ``users.get_by_google_sub`` → if found, refresh display name when changed,
       and propagate the updated name to DataHub (best-effort).
    2. ``users.get_by_email`` → if found, link ``google_sub`` onto existing row.
    3. Otherwise create a fresh user with ``password_hash=None`` and run the
       DataHub mirror create sequence. On mirror failure, the session is rolled
       back and ``DataHubSyncError`` is raised — callers must not have other
       pending writes in the session.

    The caller is responsible for committing the session after this function
    returns successfully.
    """
    from src.backend.auth import users
    from src.backend.datahub import users as dh_users
    from src.shared.exceptions import DataHubUnavailableError

    # 1. Known google_sub — log in; refresh display name if changed.
    existing = await users.get_by_google_sub(db, google_sub)
    if existing is not None:
        if existing.name != name:
            existing = await users.update_name(db, existing.id, name)
            # Propagate updated name to DataHub (best-effort).
            try:
                await dh_users.ensure_corpuser_exists(datahub, existing.email, name)
            except DataHubUnavailableError:
                logger.warning(
                    "oauth_google_datahub_name_propagation_failed",
                    extra={"email": existing.email},
                    exc_info=True,
                )
        return existing

    # 2. Known email — link google_sub.
    by_email = await users.get_by_email(db, email)
    if by_email is not None:
        linked = await users.link_google_sub(db, by_email.id, google_sub)
        return linked

    # 3. New user — create local row then run DataHub mirror sequence.
    new_user = await users.create_user(
        db,
        email,
        name,
        google_sub=google_sub,
        password=None,
        role="Reader",
    )
    try:
        await dh_users.ensure_corpuser_exists(datahub, new_user.email, new_user.name)
        await dh_users.ensure_marker_group_exists(
            datahub, runtime_config.auth_datahub_corp_group
        )
        await dh_users.add_user_to_marker_group(
            datahub,
            dh_users.corpgroup_urn(runtime_config.auth_datahub_corp_group),
            dh_users.corpuser_urn(new_user.email),
        )
        await dh_users.propagate_role(datahub, dh_users.corpuser_urn(new_user.email), "Reader")
    except Exception as exc:
        # Compensating cleanup. create_user only flushed the new row (the caller
        # commits on success), so it is still uncommitted here — rollback removes
        # it cleanly and no explicit hard_delete is needed. This is the same intent
        # as the bootstrap path's hard_delete()+commit(), which diverges only
        # because that flow owns its own commit boundary.
        await db.rollback()
        logger.warning(
            "oauth_google_mirror_failed_compensating_delete",
            extra={"email": email},
            exc_info=True,
        )
        raise DataHubSyncError(
            "DataHub mirror failed during OAuth user creation; registration rolled back."
        ) from exc

    return new_user
