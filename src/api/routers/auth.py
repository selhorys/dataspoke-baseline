"""Auth routes — /auth/...

Public routes (no auth required): register, token, token/refresh, token/revoke,
password/reset/*, google/*.

Authenticated routes: /auth/me, /auth/api-tokens/*.

Spec: spec/API.md §Auth, spec/feature/AUTH.md.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import bcrypt as _bcrypt
from fastapi import APIRouter, Cookie, Depends, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth.dependencies import AuthContext, require_authenticated
from src.api.dependencies import get_db, get_notification, get_redis
from src.api.middleware.rate_limit import limiter
from src.api.schemas.auth import (
    ApiTokenItem,
    ApiTokenListResponse,
    ApiTokenMintRequest,
    ApiTokenMintResponse,
    MePatchRequest,
    MeResponse,
    PasswordResetConfirmRequest,
    PasswordResetRequest,
    RefreshRequest,
    RegisterRequest,
    RevokeRequest,
    TokenRequest,
    TokenResponse,
)
from src.backend.auth import api_tokens, oauth_google, reset, users
from src.backend.auth import tokens as _tokens
from src.backend.auth.users import _prehash
from src.shared.cache.client import RedisClient
from src.shared.exceptions import (
    AuthenticationError,
    BadRequestError,
    OAuthNotConfiguredError,
)
from src.shared.notifications.service import NotificationService

# Pre-computed dummy hash to ensure constant-time password verification even
# when the email is unknown (prevents timing-based email enumeration attacks).
_DUMMY_HASH: str = users._hash_password("__dummy_password_for_timing__")  # noqa: SLF001

router = APIRouter(prefix="/auth", tags=["auth"])

_REFRESH_COOKIE = "refresh_token"
# Cookie path must match the full URL prefix the browser will send the cookie to.
# The auth router is mounted at /api/v1/auth, so the token sub-paths are under
# /api/v1/auth/token.  Using the shorter /auth/token would never match because
# browsers prefix-match and /auth/token is not a prefix of /api/v1/auth/token.
_REFRESH_COOKIE_PATH = "/api/v1/auth/token"


def _refresh_max_age() -> int:
    from src.shared.settings import settings

    return int(timedelta(days=settings.jwt_refresh_token_expire_days).total_seconds())


def _set_refresh_cookie(response: Response, refresh_token: str) -> None:
    from src.shared.settings import settings

    response.set_cookie(
        key=_REFRESH_COOKIE,
        value=refresh_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=_refresh_max_age(),
        path=_REFRESH_COOKIE_PATH,
    )


def _user_to_me(user: object) -> MeResponse:
    from src.shared.db.models import User

    assert isinstance(user, User)
    return MeResponse(
        id=user.id,
        email=user.email,
        name=user.name,
        has_google=user.google_sub is not None,
        role=user.role,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


# ── Register ──────────────────────────────────────────────────────────────────


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def post_register(
    request: Request,
    body: RegisterRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Create a new user account (open self-service).

    Password must be at least 10 characters. Registration is local-only — the
    DataHub corpuser is provisioned by DataHub's OIDC JIT on first DataHub
    login, and the nightly reconciliation pass projects role and marker-group
    membership onto it from there. Returns tokens so the user is immediately
    logged in.
    """
    user = await users.create_user(db, body.email, body.name, password=body.password, role="Reader")
    await db.commit()

    access_token, expires_in = _tokens.issue_access_token(user.id, user.email)
    refresh_token = _tokens.issue_refresh_token(user.id)
    _set_refresh_cookie(response, refresh_token)
    return TokenResponse(access_token=access_token, expires_in=expires_in)


# ── Token (login) ─────────────────────────────────────────────────────────────


@router.post("/token", response_model=TokenResponse, status_code=status.HTTP_200_OK)
@limiter.limit("10/minute")
async def post_token(
    request: Request,
    body: TokenRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> TokenResponse:
    """Exchange email + password for access token + refresh token cookie."""
    user = await users.get_by_email(db, body.email)
    if user is None:
        # Run a dummy bcrypt check so the response time is identical to the
        # password-mismatch branch, preventing timing-based email enumeration.
        _bcrypt.checkpw(_prehash(body.password), _DUMMY_HASH.encode())
        raise AuthenticationError("Invalid credentials.")
    if not await users.verify_password(user, body.password):
        raise AuthenticationError("Invalid credentials.")

    access_token, expires_in = _tokens.issue_access_token(user.id, user.email)
    refresh_token = _tokens.issue_refresh_token(user.id)
    _set_refresh_cookie(response, refresh_token)
    return TokenResponse(access_token=access_token, expires_in=expires_in)


# ── Token refresh ──────────────────────────────────────────────────────────────


@router.post("/token/refresh", response_model=TokenResponse, status_code=status.HTTP_200_OK)
async def post_token_refresh(
    response: Response,
    _body: RefreshRequest = RefreshRequest(),
    refresh_token_cookie: str | None = Cookie(default=None, alias=_REFRESH_COOKIE),
    db: AsyncSession = Depends(get_db),
    redis: RedisClient = Depends(get_redis),
) -> TokenResponse:
    """Issue a new access token using the HttpOnly refresh token cookie."""
    if refresh_token_cookie is None:
        raise AuthenticationError("Refresh token cookie missing.")

    if await _tokens.is_refresh_revoked(redis, refresh_token_cookie):
        raise AuthenticationError("Refresh token has been revoked.")

    payload = _tokens.decode_refresh_token(refresh_token_cookie)
    sub = payload["sub"]
    try:
        user_id = uuid.UUID(sub)
    except (ValueError, AttributeError):
        raise AuthenticationError("Invalid refresh token subject.")

    # Revoke old token before minting new one (fail-closed).
    await _tokens.mark_refresh_revoked(redis, refresh_token_cookie)

    # Reject refresh for a deleted user — the session is no longer valid.
    user = await users.get_by_id(db, user_id)
    if user is None:
        raise AuthenticationError("User no longer exists.")

    access_token, expires_in = _tokens.issue_access_token(user_id, user.email)
    new_refresh = _tokens.issue_refresh_token(user_id)
    _set_refresh_cookie(response, new_refresh)
    return TokenResponse(access_token=access_token, expires_in=expires_in)


# ── Token revoke ───────────────────────────────────────────────────────────────


@router.post("/token/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def post_token_revoke(
    response: Response,
    _body: RevokeRequest = RevokeRequest(),
    refresh_token_cookie: str | None = Cookie(default=None, alias=_REFRESH_COOKIE),
    redis: RedisClient = Depends(get_redis),
) -> None:
    """Revoke the refresh token (logout). Clears the HttpOnly cookie.

    Fails closed: if the Redis revocation write is unavailable, the request
    fails with ``503 STORAGE_UNAVAILABLE`` and the cookie is retained, so the
    caller learns the token is still valid rather than believing it revoked.

    Logout is idempotent. A missing cookie, or one that names no live refresh
    token (undecodable, wrong signature, non-refresh, or expired), clears the
    cookie and returns 204 — there is nothing to revoke and nothing to report.

    Spec: spec/feature/AUTH.md §Refresh & revoke.
    """
    if refresh_token_cookie is not None:
        await _tokens.mark_refresh_revoked(redis, refresh_token_cookie)

    response.delete_cookie(key=_REFRESH_COOKIE, path=_REFRESH_COOKIE_PATH)


# ── Me ─────────────────────────────────────────────────────────────────────────


@router.get("/me", response_model=MeResponse)
async def get_me(
    ctx: AuthContext = Depends(require_authenticated),
) -> MeResponse:
    """Return the current user's profile."""
    return _user_to_me(ctx.user)


@router.patch("/me", response_model=MeResponse)
async def patch_me(
    body: MePatchRequest,
    ctx: AuthContext = Depends(require_authenticated),
    db: AsyncSession = Depends(get_db),
) -> MeResponse:
    """Update own display name and/or password.

    The display name is DataSpoke-local; the DataHub-side profile is owned by
    DataHub's OIDC JIT provisioning, which refreshes it from the Google claims
    on each DataHub login.
    """
    user = ctx.user
    if body.name is not None:
        user = await users.update_name(db, user.id, body.name)
    if body.password is not None:
        user = await users.update_password(db, user.id, body.password)
    await db.commit()
    return _user_to_me(user)


# ── Password reset ─────────────────────────────────────────────────────────────


@router.post("/password/reset/request", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("5/minute")
async def post_password_reset_request(
    request: Request,
    body: PasswordResetRequest,
    db: AsyncSession = Depends(get_db),
    notification_service: NotificationService = Depends(get_notification),
) -> None:
    """Send a password-reset email. Always returns 204 (no enumeration leak)."""
    await reset.issue_reset_token(db, notification_service, body.email)
    await db.commit()


@router.post("/password/reset/confirm", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("10/minute")
async def post_password_reset_confirm(
    request: Request,
    body: PasswordResetConfirmRequest,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Validate a reset token and set the new password."""
    await reset.confirm_reset(db, body.token, body.new_password)
    await db.commit()


# ── Google OAuth ──────────────────────────────────────────────────────────────


@router.get("/google/login")
async def get_google_login(request: Request) -> Response:
    """Redirect the browser to Google's OAuth consent screen.

    Disabled path: returns ``503 OAUTH_NOT_CONFIGURED`` when credentials or
    the session secret are absent.

    Enabled path: delegates fully to authlib, which auto-generates state and
    nonce, stores them in ``request.session`` (via SessionMiddleware), and
    embeds them in the redirect URL. No custom state cookie is set.
    """
    from src.shared.settings import settings

    if not oauth_google.is_configured(settings):
        raise OAuthNotConfiguredError("Google OAuth not configured.")

    client = oauth_google.build_oauth_client(settings)
    redirect_uri = str(request.url_for("get_google_callback"))
    return await client.google.authorize_redirect(request, redirect_uri)  # type: ignore[no-any-return]  # authlib authorize_redirect is untyped.


@router.get("/google/callback")
async def get_google_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Complete the Google OAuth flow and set the refresh-token cookie.

    authlib's ``authorize_access_token`` validates the ``state`` query param
    against the session-stored value and verifies the ID-token ``nonce``
    against the session-stored nonce. Any mismatch raises an exception which
    we map to ``400 OAUTH_STATE_MISMATCH``.

    On success, resolves (or creates) the DataSpoke user, sets the refresh
    cookie, and issues a 302 redirect to the post-login URL so the SPA can
    call ``POST /auth/token/refresh`` to obtain an access token.
    """
    from src.shared.settings import settings

    if not oauth_google.is_configured(settings):
        raise OAuthNotConfiguredError("Google OAuth not configured.")

    # ── Token exchange (authlib validates state + nonce via session) ──────
    client = oauth_google.build_oauth_client(settings)
    try:
        token_response = await client.google.authorize_access_token(request)
    except Exception as exc:
        raise BadRequestError(
            error_code="OAUTH_STATE_MISMATCH",
            message="OAuth state mismatch.",
        ) from exc

    # ── Claims extraction ─────────────────────────────────────────────────
    # authlib populates ``userinfo`` whenever the ``openid`` scope is requested.
    id_token = token_response.get("userinfo")
    if not id_token or "sub" not in id_token:
        raise BadRequestError(
            error_code="OAUTH_STATE_MISMATCH",
            message="OAuth ID token missing required claims.",
        )

    # ── Enforce email_verified ────────────────────────────────────────────
    if not id_token.get("email_verified", False):
        raise BadRequestError(
            error_code="OAUTH_EMAIL_NOT_VERIFIED",
            message="Google account email is not verified.",
        )

    google_sub = str(id_token.get("sub", ""))
    email = str(id_token.get("email", ""))
    name = str(id_token.get("name") or email)

    if not google_sub or not email:
        raise BadRequestError(
            error_code="OAUTH_STATE_MISMATCH",
            message="Google ID token missing required claims.",
        )

    # ── User resolution ───────────────────────────────────────────────────
    user = await oauth_google.resolve_or_create_user(
        db,
        google_sub=google_sub,
        email=email,
        name=name,
    )
    await db.commit()

    # ── Issue refresh token + redirect ────────────────────────────────────
    refresh_token = _tokens.issue_refresh_token(user.id)

    # Redirect to SPA root; the frontend calls POST /auth/token/refresh to
    # obtain an access token using the HttpOnly refresh cookie.
    redirect_url = getattr(settings, "oauth_post_login_redirect", "/")
    redirect_response = RedirectResponse(url=redirect_url, status_code=302)

    _set_refresh_cookie(redirect_response, refresh_token)
    return redirect_response


# ── API tokens ─────────────────────────────────────────────────────────────────


def _token_to_item(t: object) -> ApiTokenItem:
    from src.shared.db.models import ApiToken

    assert isinstance(t, ApiToken)
    return ApiTokenItem(
        id=t.id,
        name=t.name,
        role_snapshot=t.role_snapshot,
        created_at=t.created_at,
        last_used_at=t.last_used_at,
        expires_at=t.expires_at,
    )


@router.get("/api-tokens", response_model=ApiTokenListResponse)
async def get_api_tokens(
    ctx: AuthContext = Depends(require_authenticated),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=1000),
    sort: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> ApiTokenListResponse:
    """List the caller's active (non-revoked) API tokens.

    Paginated; sortable by created_at (default: created_at descending).
    """
    token_list = await api_tokens.list_active(db, ctx.user.id)
    token_list = api_tokens.sort_tokens(token_list, sort)
    total = len(token_list)
    page = token_list[offset : offset + limit]
    return ApiTokenListResponse(
        offset=offset,
        limit=limit,
        total_count=total,
        tokens=[_token_to_item(t) for t in page],
    )


@router.post(
    "/api-tokens", response_model=ApiTokenMintResponse, status_code=status.HTTP_201_CREATED
)
async def post_api_tokens(
    body: ApiTokenMintRequest,
    ctx: AuthContext = Depends(require_authenticated),
    db: AsyncSession = Depends(get_db),
) -> ApiTokenMintResponse:
    """Mint a new API token. The raw token is returned only in this response."""
    raw_token, token = await api_tokens.mint(db, ctx.user.id, body.name, body.expires_at)
    await db.commit()
    return ApiTokenMintResponse(
        id=token.id,
        name=token.name,
        role_snapshot=token.role_snapshot,
        token=raw_token,
        created_at=token.created_at,
        expires_at=token.expires_at,
    )


@router.delete("/api-tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_api_token(
    token_id: uuid.UUID,
    ctx: AuthContext = Depends(require_authenticated),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke an own API token."""
    await api_tokens.revoke(db, token_id=token_id, owner_user_id=ctx.user.id)
    await db.commit()
