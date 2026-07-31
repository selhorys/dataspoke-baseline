"""Auth routes — /auth/...

Public routes (no auth required): register, token, token/refresh, token/revoke,
password/reset/*, google/*.

Authenticated routes: /auth/me, /auth/api-tokens/*.

Spec: spec/API.md §Auth, spec/feature/AUTH.md.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import timedelta
from typing import TYPE_CHECKING
from urllib.parse import quote, urljoin

import bcrypt as _bcrypt
import structlog
from fastapi import APIRouter, Cookie, Depends, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.auth.dependencies import (
    AuthContext,
    require_authenticated,
    revalidate_under_user_lock,
)
from src.api.dependencies import get_db, get_notification, get_redis
from src.api.middleware.rate_limit import auth_route_limit
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
    DataSpokeError,
    OAuthNotConfiguredError,
)
from src.shared.notifications.service import NotificationService

if TYPE_CHECKING:
    from src.shared.settings import Settings

# Pre-computed dummy hash to ensure constant-time password verification even
# when the email is unknown (prevents timing-based email enumeration attacks).
_DUMMY_HASH: str = users._hash_password("__dummy_password_for_timing__")  # noqa: SLF001

logger = structlog.get_logger(__name__)

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
        has_password=user.password_hash is not None,
        has_google=user.google_sub is not None,
        role=user.role,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


# ── Register ──────────────────────────────────────────────────────────────────


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
@auth_route_limit("5/minute")
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

    access_token, expires_in = _tokens.issue_access_token(user.id, user.email, user.session_epoch)
    refresh_token = _tokens.issue_refresh_token(user.id, user.session_epoch)
    _set_refresh_cookie(response, refresh_token)
    return TokenResponse(access_token=access_token, expires_in=expires_in)


# ── Token (login) ─────────────────────────────────────────────────────────────


@router.post("/token", response_model=TokenResponse, status_code=status.HTTP_200_OK)
@auth_route_limit("10/minute")
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

    access_token, expires_in = _tokens.issue_access_token(user.id, user.email, user.session_epoch)
    refresh_token = _tokens.issue_refresh_token(user.id, user.session_epoch)
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

    # Session-epoch gate — a refresh token issued before a credential reset
    # names a session that no longer exists.
    if not _tokens.session_epoch_matches(payload.get("ses"), user.session_epoch):
        raise AuthenticationError("Session is no longer valid.")

    access_token, expires_in = _tokens.issue_access_token(user_id, user.email, user.session_epoch)
    new_refresh = _tokens.issue_refresh_token(user_id, user.session_epoch)
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

    Setting a password is a credential-creating write, so it runs under the
    ``users`` row lock and re-validates the credential that authorised the
    request once the lock is held (spec/feature/AUTH.md §Serialization of
    credential-creating writes).
    """
    user = ctx.user
    if body.password is not None:
        user = await revalidate_under_user_lock(db, ctx)
    if body.name is not None:
        user = await users.update_name(db, user.id, body.name)
    if body.password is not None:
        user = await users.update_password(db, user.id, body.password)
    await db.commit()
    return _user_to_me(user)


# ── Password reset ─────────────────────────────────────────────────────────────


@router.post("/password/reset/request", status_code=status.HTTP_204_NO_CONTENT)
@auth_route_limit("5/minute")
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
@auth_route_limit("10/minute")
async def post_password_reset_confirm(
    request: Request,
    body: PasswordResetConfirmRequest,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Validate a reset token and set the new password."""
    await reset.confirm_reset(db, body.token, body.new_password)
    await db.commit()


# ── Google OAuth ──────────────────────────────────────────────────────────────

# Absolute path of the public UI page that renders an OAuth failure. Joined
# against the configured post-login redirect target to reach the UI origin.
_OAUTH_ERROR_PATH = "/oauth-error"

#: The error codes that reach ``/oauth-error`` as an ``?error=`` value
#: (spec/API.md §OAuth browser-redirect contract). Every other failure — a
#: backend error raised outside this set, or a non-DataSpoke exception —
#: redirects to the page with no ``error`` parameter, which renders the generic
#: copy spec/feature/FRONTEND_BASIC.md §OAuth error page specifies for an absent
#: code, and is logged at ERROR instead of being passed off as ordinary user
#: error.
_OAUTH_ERROR_CODES: frozenset[str] = frozenset(
    {
        "OAUTH_NOT_CONFIGURED",
        "OAUTH_STATE_MISMATCH",
        "OAUTH_EMAIL_NOT_VERIFIED",
        "GOOGLE_ACCOUNT_LINKED_ELSEWHERE",
        "EMAIL_BOUND_TO_ANOTHER_GOOGLE_ACCOUNT",
    }
)


def _oauth_error_url(settings: Settings, error_code: str | None) -> str:
    """Build the browser Location for a failed ``/auth/google/*`` navigation.

    The target is the ORIGIN of ``settings.oauth_post_login_redirect`` plus the
    absolute path ``/oauth-error`` — ``urljoin`` against an absolute path drops
    any path component the configured value carries, and the ``/`` default
    (same-host deployment) degrades to the relative location ``/oauth-error``.
    Note the asymmetry with the success path, which uses the configured value
    verbatim, path included.

    The host half is server configuration only and ``error_code`` is one of the
    five codes in ``_OAUTH_ERROR_CODES`` (or ``None``), never request input, so
    the location is not attacker-influenced.

    Spec: spec/API.md §OAuth browser-redirect contract.
    """
    post_login = settings.oauth_post_login_redirect or "/"
    base = urljoin(post_login, _OAUTH_ERROR_PATH)
    if error_code is None:
        return base
    return f"{base}?error={quote(error_code, safe='')}"


def _request_actor(request: Request) -> dict[str, str]:
    """Actor fields for a log line raised from inside a request handler.

    ``trace_id`` is *read* from ``request.state``, where
    ``RequestLoggingMiddleware`` publishes the id it minted, rather than derived
    again here. Re-deriving would copy a recipe whose fallback is a fresh
    ``uuid4`` — and these two routes are full-page browser navigations, which
    carry no ``X-Trace-Id``, so that fallback is the only branch either side ever
    takes. The line would then carry an id joining neither the
    ``request_started`` / ``request_finished`` pair nor the echoed response
    header. With no middleware in the stack the field is empty rather than
    fabricated, the same choice ``_error_json`` in ``src/api/main.py`` makes.

    ``client_ip`` is the address uvicorn observed, derived as the middleware
    derives it. Under the chart default ``config.trustedProxyIps: "127.0.0.1"``
    uvicorn does not rewrite it from ``X-Forwarded-For``, so for every caller
    outside the cluster it is the ingress pod and distinguishes nothing; it
    separates callers only where an operator widened that set
    (spec/feature/AUTH.md §Client-IP attribution for rate limiting). Reading the
    forwarded header here instead would widen the trust radius past what uvicorn
    is configured to accept.
    """
    return {
        "trace_id": getattr(request.state, "trace_id", ""),
        "client_ip": request.client.host if request.client else "unknown",
    }


def _oauth_error_redirect(request: Request, settings: Settings, exc: Exception) -> RedirectResponse:
    """Map a failed ``/auth/google/*`` navigation onto the 302 the browser gets.

    Both routes are browser-navigation endpoints, so every outcome their bodies
    produce is a redirect rather than the JSON error envelope (spec/API.md
    §OAuth browser-redirect contract). No cookie is set on this path.

    Only the five spec'd codes are forwarded to the page. Anything else — a
    ``DataSpokeError`` raised with another code, or an unexpected exception such
    as a transport or database failure — redirects without an ``error``
    parameter and is logged at ERROR, so the failure stays visible to
    monitoring instead of degrading into ordinary user-facing copy.

    Every refusal is logged, because the response no longer distinguishes it: a
    302 to the error page is indistinguishable from a successful sign-in in the
    request log, which makes this line the only detection surface for a refused
    pre-hijack bind or for ``OAUTH_STATE_MISMATCH`` probing. It therefore carries
    the request's ``trace_id`` and the observed ``client_ip`` alongside the route
    path and either the error code or the exception's class name, and nothing
    else. The ``trace_id`` ties the line back to the middleware's
    ``request_started`` / ``request_finished`` pair; the ``client_ip`` narrows to
    an actor only where the trusted-proxy set has been widened past the chart
    default — see ``_request_actor`` for both qualifications.

    The email, the Google ``sub``, and the exception message are all withheld:
    each can carry the authenticating identity, and a ``DBAPIError`` renders the
    failing statement's bind parameters into its own message.
    """
    actor = _request_actor(request)
    path = request.url.path
    code: str | None = None
    if isinstance(exc, DataSpokeError) and exc.error_code in _OAUTH_ERROR_CODES:
        code = exc.error_code
        logger.warning("oauth_route_refused", path=path, error_code=code, **actor)
    elif isinstance(exc, DataSpokeError):
        # The message may name the authenticating user, so log the code only.
        logger.error("oauth_route_failed_unmapped", path=path, error_code=exc.error_code, **actor)
    else:
        # No `exc_info`, and nothing else logs it either — this line is the whole
        # production record of an unexpected OAuth failure. The traceback is
        # withheld because a driver error renders the failing statement's bind
        # parameters, which on this path are the authenticating user's email and
        # Google `sub`, and the engine is built without `hide_parameters`
        # (`src/shared/db/session.py`). Setting that would make the frame safe to
        # emit here; until then the class name is all monitoring gets.
        logger.error(
            "oauth_route_error",
            path=path,
            error_type=type(exc).__name__,
            **actor,
        )
    return RedirectResponse(url=_oauth_error_url(settings, code), status_code=302)


@router.get("/google/login")
# Sits on the fail-closed plane with its callback, but carries a far larger
# budget: this route accepts no credential — it only starts the consent
# redirect — so it needs no brute-force bound, and under the chart default
# `config.trustedProxyIps: "127.0.0.1"` every external caller presents the
# ingress pod address, making any budget here a single deployment-wide counter.
# At the callback's 10/minute that would cap org-wide sign-in *initiation* at
# ten a minute.
@auth_route_limit("60/minute")
async def get_google_login(request: Request) -> Response:
    """Redirect the browser to Google's OAuth consent screen.

    Enabled path: delegates fully to authlib, which auto-generates state and
    nonce, stores them in ``request.session`` (via SessionMiddleware), and
    embeds them in the redirect URL. No custom state cookie is set.

    Disabled path: 302 to ``<ui>/oauth-error?error=OAUTH_NOT_CONFIGURED`` when
    credentials or the session secret are absent.

    Every failure the handler body produces — authlib's discovery-document fetch
    against Google included — also 302s to the error page rather than an error
    body (spec/API.md §OAuth browser-redirect contract). The rate-limit guard
    runs before the body and is the one outcome that is not a redirect: it
    answers ``429 RATE_LIMIT_EXCEEDED`` or, on a limiter-storage outage, ``503
    STORAGE_UNAVAILABLE``.

    Rate-limited on the fail-closed, IP-keyed auth plane at the same 10/minute
    sign-in budget as ``POST /auth/token``: this is a sign-in entry point whose
    callers are unauthenticated by definition, so the default plane's
    caller-selectable bucket key would bound nothing (spec/feature/AUTH.md
    §Client-IP attribution for rate limiting).

    What that budget actually buckets, for whoever tunes it next: the key is the
    address uvicorn observed, and under the chart default
    ``config.trustedProxyIps: "127.0.0.1"`` that is the ingress pod for every
    caller outside the cluster — so the 10/minute is one deployment-wide counter,
    not per client, until the trusted-proxy set is widened. This route accepts no
    credential; it only redirects to Google.
    """
    from src.shared.settings import settings

    try:
        if not oauth_google.is_configured(settings):
            raise OAuthNotConfiguredError("Google OAuth not configured.")

        client = oauth_google.build_oauth_client(settings)
        redirect_uri = str(request.url_for("get_google_callback"))
        return await client.google.authorize_redirect(request, redirect_uri)  # type: ignore[no-any-return]  # authlib authorize_redirect is untyped.
    except Exception as exc:
        return _oauth_error_redirect(request, settings, exc)


@router.get("/google/callback")
@auth_route_limit("10/minute")
async def get_google_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Complete the Google OAuth flow and set the refresh-token cookie.

    authlib's ``authorize_access_token`` validates the ``state`` query param
    against the session-stored value and verifies the ID-token ``nonce``
    against the session-stored nonce. Any mismatch is reported as
    ``OAUTH_STATE_MISMATCH``.

    On success, resolves (or creates) the DataSpoke user, sets the refresh
    cookie, and issues a 302 redirect to the post-login URL so the SPA can
    call ``POST /auth/token/refresh`` to obtain an access token.

    On any failure the handler body produces — a refused bind, an invalid token
    exchange, or an unexpected transport/database error alike — the browser is
    redirected to the ``/oauth-error`` page with no cookie set and the
    transaction rolled back (spec/API.md §OAuth browser-redirect contract). The
    rate-limit guard runs before the body and is the one outcome that is not a
    redirect: it answers ``429 RATE_LIMIT_EXCEEDED`` or, on a limiter-storage
    outage, ``503 STORAGE_UNAVAILABLE``.

    Rate-limited on the fail-closed, IP-keyed auth plane at the same 10/minute
    sign-in budget as ``POST /auth/token``: this route accepts a Google
    authorization code and mints a session, so it is credential-accepting, and
    the default plane keys on an identity the caller supplies — a fresh bearer
    value per request would buy a fresh budget each time (spec/feature/AUTH.md
    §Client-IP attribution for rate limiting).

    What that budget actually buckets, for whoever tunes it next: the key is the
    address uvicorn observed, and under the chart default
    ``config.trustedProxyIps: "127.0.0.1"`` that is the ingress pod for every
    caller outside the cluster — so the 10/minute is one deployment-wide counter,
    not per client, until the trusted-proxy set is widened.
    """
    from src.shared.settings import settings

    try:
        return await _google_callback(request, db, settings)
    except Exception as exc:
        # A failed rollback (dead connection) must not replace the redirect
        # with the 500 this route exists to avoid; the session is discarded by
        # the request-scoped dependency either way.
        with contextlib.suppress(Exception):
            await db.rollback()
        return _oauth_error_redirect(request, settings, exc)


async def _google_callback(request: Request, db: AsyncSession, settings: Settings) -> Response:
    """Success path of the Google callback; raises on every failure."""
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
    # Issued after the commit, so a bind that reset this row's credentials has
    # already landed and the token carries the new session epoch.
    refresh_token = _tokens.issue_refresh_token(user.id, user.session_epoch)

    # Redirect to the configured post-login target verbatim; the frontend calls
    # POST /auth/token/refresh to obtain an access token using the HttpOnly
    # refresh cookie.
    redirect_url = settings.oauth_post_login_redirect or "/"
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
    """Mint a new API token. The raw token is returned only in this response.

    Minting is a credential-creating write, so it runs under the ``users`` row
    lock and re-validates the credential that authorised the request once the
    lock is held (spec/feature/AUTH.md §Serialization of credential-creating
    writes). The re-check matters most here: the API-token authentication path
    runs no epoch check, so a token minted on a superseded authorisation would
    otherwise stay live past the credential reset that superseded it.
    """
    await revalidate_under_user_lock(db, ctx)
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
