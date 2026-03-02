from datetime import timedelta

from fastapi import APIRouter, Cookie, HTTPException, Response, status
from passlib.context import CryptContext

from src.api.auth.jwt import create_access_token, create_refresh_token, decode_token
from src.api.config import settings
from src.api.schemas.auth import RefreshRequest, RevokeRequest, TokenRequest, TokenResponse

router = APIRouter(prefix="/auth", tags=["auth"])

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# In-memory refresh token revocation set.
# TODO: replace with Redis-backed set for multi-instance correctness.
_revoked_refresh_tokens: set[str] = set()

_REFRESH_COOKIE = "refresh_token"
_REFRESH_MAX_AGE = int(timedelta(days=settings.jwt_refresh_token_expire_days).total_seconds())


def _verify_credentials(username: str, password: str) -> bool:
    """Stub credential check against the configured admin user.

    TODO: replace with a real identity store (DB user table or LDAP).
    """
    return username == settings.admin_username and password == settings.admin_password


def _get_user_groups(username: str) -> list[str]:
    """Return groups for a user.

    TODO: look up groups from the identity store.
    """
    if username == settings.admin_username:
        return list(settings.admin_groups)
    return []


@router.post("/token", response_model=TokenResponse, status_code=status.HTTP_200_OK)
async def issue_token(body: TokenRequest, response: Response) -> TokenResponse:
    """Exchange username + password for access token + refresh token cookie."""
    if not _verify_credentials(body.username, body.password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "UNAUTHORIZED", "message": "Invalid credentials."},
        )

    groups = _get_user_groups(body.username)
    access_token, expires_in = create_access_token(
        subject=body.username, groups=groups, email=f"{body.username}@example.com"
    )
    refresh_token = create_refresh_token(subject=body.username)

    response.set_cookie(
        key=_REFRESH_COOKIE,
        value=refresh_token,
        httponly=True,
        secure=False,  # TODO: set True in production
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
) -> TokenResponse:
    """Issue a new access token using the HttpOnly refresh token cookie."""
    if refresh_token_cookie is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "UNAUTHORIZED", "message": "Refresh token cookie missing."},
        )

    if refresh_token_cookie in _revoked_refresh_tokens:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "UNAUTHORIZED", "message": "Refresh token has been revoked."},
        )

    import jwt as pyjwt

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
    access_token, expires_in = create_access_token(
        subject=subject, groups=groups, email=f"{subject}@example.com"
    )

    # Rotate refresh token
    new_refresh = create_refresh_token(subject=subject)
    _revoked_refresh_tokens.add(refresh_token_cookie)
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
) -> None:
    """Revoke the refresh token (logout). Clears the HttpOnly cookie."""
    if refresh_token_cookie is not None:
        _revoked_refresh_tokens.add(refresh_token_cookie)

    response.delete_cookie(key=_REFRESH_COOKIE, path="/auth/token")
