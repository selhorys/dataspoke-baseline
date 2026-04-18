import hashlib
import logging
import time
from datetime import timedelta

import jwt as pyjwt
import redis.exceptions
from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from passlib.context import CryptContext

from src.api.auth.jwt import create_access_token, create_refresh_token, decode_token
from src.api.config import settings
from src.api.dependencies import get_redis
from src.api.schemas.auth import RefreshRequest, RevokeRequest, TokenRequest, TokenResponse
from src.shared.cache.client import RedisClient

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

_REFRESH_COOKIE = "refresh_token"
_REFRESH_MAX_AGE = int(timedelta(days=settings.jwt_refresh_token_expire_days).total_seconds())


def _revocation_key(token: str) -> str:
    """Return a Redis key for the given raw refresh token (hashed for compactness)."""
    return f"revoked_refresh:{hashlib.sha256(token.encode()).hexdigest()[:16]}"


def _verify_credentials(email: str, password: str) -> bool:
    """Stub credential check against the configured admin user.

    TBD(user-accounts): Replace with DB/LDAP user lookup + bcrypt verify
    """
    if not settings.enable_stub_auth:
        return False
    return email == settings.admin_email and password == settings.admin_password


def _get_user_groups(email: str) -> list[str]:
    """Return groups for a user.

    TBD(user-accounts): Look up groups from identity store
    """
    if not settings.enable_stub_auth:
        return []
    if email == settings.admin_email:
        return list(settings.admin_groups)
    return []


@router.post("/token", response_model=TokenResponse, status_code=status.HTTP_200_OK)
async def issue_token(body: TokenRequest, response: Response) -> TokenResponse:
    """Exchange email + password for access token + refresh token cookie."""
    if not _verify_credentials(body.email, body.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "UNAUTHORIZED", "message": "Invalid credentials."},
        )

    groups = _get_user_groups(body.email)
    # TBD(user-accounts): Read email from user record instead of using subject directly
    access_token, expires_in = create_access_token(
        subject=body.email, groups=groups, email=body.email
    )
    refresh_token = create_refresh_token(subject=body.email)

    response.set_cookie(
        key=_REFRESH_COOKIE,
        value=refresh_token,
        httponly=True,
        secure=False,  # TBD(user-accounts): Set True via settings in production
        samesite="lax",
        max_age=_REFRESH_MAX_AGE,
        path="/auth/token",
    )

    return TokenResponse(access_token=access_token, expires_in=expires_in)


@router.post("/token/refresh", response_model=TokenResponse, status_code=status.HTTP_200_OK)
async def refresh_token(
    response: Response,
    _body: RefreshRequest = RefreshRequest(),
    refresh_token_cookie: str | None = Cookie(default=None, alias=_REFRESH_COOKIE),
    cache: RedisClient = Depends(get_redis),
) -> TokenResponse:
    """Issue a new access token using the HttpOnly refresh token cookie."""
    if refresh_token_cookie is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "UNAUTHORIZED", "message": "Refresh token cookie missing."},
        )

    # Fail-closed by design: if the revocation store is unavailable we deny the refresh.
    try:
        is_revoked = await cache.get(_revocation_key(refresh_token_cookie))
    except redis.exceptions.RedisError as exc:
        logger.warning("revocation_check_failed", exc_info=exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": "SERVICE_UNAVAILABLE",
                "message": "Token revocation store unavailable; refresh denied.",
            },
        )

    if is_revoked:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "UNAUTHORIZED", "message": "Refresh token has been revoked."},
        )

    try:
        payload = decode_token(refresh_token_cookie)
    except pyjwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "UNAUTHORIZED", "message": "Refresh token has expired."},
        )
    except pyjwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "UNAUTHORIZED", "message": "Invalid refresh token."},
        )

    if payload.get("type") != "refresh":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "UNAUTHORIZED", "message": "Not a refresh token."},
        )

    subject: str = payload["sub"]
    groups = _get_user_groups(subject)

    # Compute TTL from the old token's expiry before minting the new one.
    ttl = max(0, int(payload["exp"]) - int(time.time()))

    # Revoke old cookie in Redis BEFORE minting the new token and setting the cookie.
    # If this write fails, we return 503; the old token remains valid and the client
    # can retry safely — no new token has been issued yet.
    # Fail-closed by design: deny refresh if revocation write fails.
    if ttl > 0:
        try:
            await cache.set_nx(_revocation_key(refresh_token_cookie), "1", ttl)
        except redis.exceptions.RedisError as exc:
            logger.warning("revocation_write_failed", exc_info=exc)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={
                    "error_code": "SERVICE_UNAVAILABLE",
                    "message": "Token revocation store unavailable; refresh denied.",
                },
            )

    # TBD(user-accounts): Read email from user record instead of fabricating
    access_token, expires_in = create_access_token(
        subject=subject, groups=groups, email=subject
    )
    new_refresh = create_refresh_token(subject=subject)

    response.set_cookie(
        key=_REFRESH_COOKIE,
        value=new_refresh,
        httponly=True,
        secure=False,
        samesite="lax",
        max_age=_REFRESH_MAX_AGE,
        path="/auth/token",
    )

    return TokenResponse(access_token=access_token, expires_in=expires_in)


@router.post("/token/revoke", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_token(
    response: Response,
    _body: RevokeRequest = RevokeRequest(),
    refresh_token_cookie: str | None = Cookie(default=None, alias=_REFRESH_COOKIE),
    cache: RedisClient = Depends(get_redis),
) -> None:
    """Revoke the refresh token (logout). Clears the HttpOnly cookie."""
    if refresh_token_cookie is not None:
        try:
            payload = decode_token(refresh_token_cookie)
            ttl = max(0, int(payload.get("exp", 0)) - int(time.time()))
            if ttl > 0:
                # Best-effort: revocation write is idempotent; Redis failure is logged
                # but the cookie is still cleared so the client is logged out locally.
                try:
                    await cache.set_nx(_revocation_key(refresh_token_cookie), "1", ttl)
                except redis.exceptions.RedisError as exc:
                    logger.warning("revocation_write_failed_on_revoke", exc_info=exc)
        except pyjwt.PyJWTError:
            pass  # invalid token — nothing to revoke

    response.delete_cookie(key=_REFRESH_COOKIE, path="/auth/token")
