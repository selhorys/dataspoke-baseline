from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from src.api.config import settings


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


def create_access_token(subject: str, groups: list[str], email: str = "") -> tuple[str, int]:
    """Return (encoded_token, expires_in_seconds)."""
    expire_seconds = settings.jwt_access_token_expire_minutes * 60
    exp = _utc_now() + timedelta(seconds=expire_seconds)
    payload: dict[str, Any] = {
        "sub": subject,
        "email": email,
        "groups": groups,
        "exp": exp,
        "iat": _utc_now(),
    }
    token = jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, expire_seconds


def create_refresh_token(subject: str) -> str:
    exp = _utc_now() + timedelta(days=settings.jwt_refresh_token_expire_days)
    payload: dict[str, Any] = {
        "sub": subject,
        "exp": exp,
        "iat": _utc_now(),
        "type": "refresh",
    }
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    """Decode and validate a JWT. Raises jwt.PyJWTError on failure."""
    return jwt.decode(
        token,
        settings.jwt_secret_key,
        algorithms=[settings.jwt_algorithm],
    )


def extract_groups(token: str) -> list[str]:
    payload = decode_token(token)
    groups = payload.get("groups", [])
    if isinstance(groups, list):
        return [str(g) for g in groups]
    return []
