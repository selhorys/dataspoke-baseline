"""FastAPI dependency family for role-based access control.

All three dependencies read role from the DB on every request — instant
demotion takes effect immediately without waiting for token rotation.
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
    """Authenticated request context — passed downstream by dependencies."""

    user: User
    effective_role: str


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

    if token_str.startswith("dsk_"):
        # Opaque PAT fast-path
        user, effective_role = await _api_tokens.lookup_and_validate(db, token_str)
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
            raise ForbiddenError(
                error_code="FORBIDDEN",
                message="User no longer exists.",
            )
        effective_role = user.role

    request.state.user = user
    request.state.effective_role = effective_role
    return AuthContext(user=user, effective_role=effective_role)


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


def require_tier(tier: str):
    """Workspace-tier gate (extensibility hook).

    In the baseline deployment the JWT ``groups`` claim is the constant
    ``["de","da","dg"]`` so every authenticated caller passes every tier.
    This dependency exists as an affordance for organizations that selectively
    populate the claim to gate workspace-specific paths.  API-token callers are
    treated as workspace-universal (tokens are not workspace-scoped).

    Usage::

        router = APIRouter(dependencies=[Depends(require_tier("dg"))])
    """

    async def _check(ctx: AuthContext = Depends(require_authenticated)) -> AuthContext:
        # No-op for the baseline — constant groups covers all tiers.
        # Override this dependency to implement real tier partitioning.
        return ctx

    return _check
