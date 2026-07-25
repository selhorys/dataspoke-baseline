"""FastAPI dependency family for role-based access control.

Authorization is method × role: Reader callers cannot use write methods
(POST/PUT/PATCH/DELETE); Admin-only routes require require_admin.
All dependencies read role from the DB on every request — instant demotion
takes effect immediately without waiting for token rotation.
"""

from __future__ import annotations

import uuid as _uuid
from dataclasses import dataclass

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.dependencies import get_db, get_redis
from src.backend.auth import api_tokens as _api_tokens
from src.backend.auth import tokens as _tokens
from src.backend.auth import users as _users
from src.shared.cache.client import RedisClient
from src.shared.db.models import User
from src.shared.exceptions import AuthenticationError, ForbiddenError


@dataclass(frozen=True)
class AuthContext:
    """Authenticated request context — passed downstream by dependencies.

    ``session_epoch`` and ``api_token_id`` name the credential that authorised
    the request — the JWT's ``ses`` claim for a bearer-JWT caller, the
    ``api_tokens`` row id for a PAT caller. Exactly one is populated;
    :func:`revalidate_under_user_lock` re-checks whichever it is before a
    credential-creating write commits.
    """

    user: User
    effective_role: str
    session_epoch: int | None = None
    api_token_id: _uuid.UUID | None = None


_bearer = HTTPBearer(auto_error=False)


async def require_authenticated(
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> AuthContext:
    """Validate the Bearer token and return an AuthContext.

    Fast-path: tokens starting with ``dsk_`` go straight to the opaque PAT
    lookup.  All other tokens go through JWT decode; jwt.PyJWTError causes
    AuthenticationError.

    Populates ``request.state.user`` and ``request.state.effective_role``
    for middleware / logging use.
    """
    if credentials is None:
        raise AuthenticationError(
            error_code="UNAUTHORIZED",
            message="Missing authorization token.",
        )

    token_str = credentials.credentials

    user: User | None = None
    session_epoch: int | None = None
    api_token_id: _uuid.UUID | None = None

    if token_str.startswith("dsk_"):
        # Opaque PAT fast-path
        user, effective_role, api_token_id = await _api_tokens.lookup_and_validate(db, token_str)
    else:
        # JWT path
        try:
            payload = _tokens.decode_access_token(token_str)
        except jwt.ExpiredSignatureError:
            raise AuthenticationError(
                error_code="UNAUTHORIZED",
                message="Token has expired.",
            )
        except jwt.PyJWTError:
            raise AuthenticationError(
                error_code="UNAUTHORIZED",
                message="Invalid token.",
            )

        sub = payload.get("sub", "")

        try:
            user_id = _uuid.UUID(sub)
        except (ValueError, AttributeError):
            raise AuthenticationError(
                error_code="UNAUTHORIZED",
                message="Invalid token subject.",
            )

        user = await _users.get_by_id(db, user_id)
        if user is None:
            raise AuthenticationError(
                error_code="UNAUTHORIZED",
                message="User no longer exists.",
            )
        # Session-epoch gate. The row is already in hand for the role read, so
        # the comparison costs no extra round trip. A token issued under a
        # superseded epoch — every token that predates a credential reset — is
        # rejected here, before any role gate runs.
        if not _tokens.session_epoch_matches(payload.get("ses"), user.session_epoch):
            raise AuthenticationError(
                error_code="UNAUTHORIZED",
                message="Session is no longer valid.",
            )
        session_epoch = user.session_epoch
        effective_role = user.role

    request.state.user = user
    request.state.effective_role = effective_role
    return AuthContext(
        user=user,
        effective_role=effective_role,
        session_epoch=session_epoch,
        api_token_id=api_token_id,
    )


async def revalidate_under_user_lock(db: AsyncSession, ctx: AuthContext) -> User:
    """Take the ``users`` row lock, re-check the caller's credential, return the row.

    Credential-creating self-service writes call this before minting anything
    (spec/feature/AUTH.md §Serialization of credential-creating writes). Taking
    the lock the Google-bind credential reset holds forces the ordering — the
    write cannot commit alongside the reset; re-reading once the lock is held
    catches an authorisation the reset superseded.

    A JWT-authorised caller is re-checked on its ``ses`` claim against the
    freshly read ``session_epoch``; a PAT-authorised caller on its own
    ``api_tokens`` row, which the reset revokes.

    Raises:
        AuthenticationError('UNAUTHORIZED')   — the row is gone, or the JWT's
            session epoch no longer matches.
        AuthenticationError('TOKEN_REVOKED')  — the authorising API token has
            been revoked.
    """
    user = await _users.lock_user(db, ctx.user.id)
    if user is None:
        raise AuthenticationError(
            error_code="UNAUTHORIZED",
            message="User no longer exists.",
        )

    if ctx.api_token_id is not None:
        if await _api_tokens.is_active(db, ctx.api_token_id):
            return user
        raise AuthenticationError(
            error_code="TOKEN_REVOKED",
            message="API token has been revoked.",
        )

    if not _tokens.session_epoch_matches(ctx.session_epoch, user.session_epoch):
        raise AuthenticationError(
            error_code="UNAUTHORIZED",
            message="Session is no longer valid.",
        )
    return user


async def require_writer(
    request: Request,
    ctx: AuthContext = Depends(require_authenticated),
) -> AuthContext:
    """Enforce that Reader-role callers cannot perform write methods.

    Raises:
        ForbiddenError('READ_ONLY_ROLE')  — Reader on POST/PUT/PATCH/DELETE.
    """
    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and ctx.effective_role == "Reader":
        raise ForbiddenError(
            error_code="READ_ONLY_ROLE",
            message="Reader role cannot perform write methods.",
        )
    return ctx


async def require_editor(
    ctx: AuthContext = Depends(require_authenticated),
) -> AuthContext:
    """Enforce that the caller is Editor or Admin (Reader blocked on any method).

    Use this for read endpoints that expose sensitive information and must be
    restricted to Editor-or-above regardless of HTTP method.

    Raises:
        ForbiddenError('READ_ONLY_ROLE')  — Reader on any method.
    """
    if ctx.effective_role == "Reader":
        raise ForbiddenError(
            error_code="READ_ONLY_ROLE",
            message="Reader role cannot access this resource.",
        )
    return ctx


async def require_admin(
    ctx: AuthContext = Depends(require_authenticated),
) -> AuthContext:
    """Enforce Admin role.

    Raises:
        ForbiddenError('FORBIDDEN')  — caller is not Admin.
    """
    if ctx.effective_role != "Admin":
        raise ForbiddenError(
            error_code="FORBIDDEN",
            message="Admin role required.",
        )
    return ctx
