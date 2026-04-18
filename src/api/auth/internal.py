"""Shared-secret auth for internal (Airflow-called) endpoints."""

import logging
import secrets

from fastapi import Header, HTTPException, status

from src.shared.settings import settings

logger = logging.getLogger(__name__)


async def require_internal_token(
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
) -> None:
    """Require a matching X-Internal-Token header; constant-time compare.

    - 503 INTERNAL_AUTH_NOT_CONFIGURED if DATASPOKE_INTERNAL_TOKEN is blank.
    - 401 UNAUTHORIZED on missing or mismatched header.
    """
    if not settings.internal_token:
        logger.error(
            "internal_auth_not_configured",
            extra={"missing_env": "DATASPOKE_INTERNAL_TOKEN"},
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error_code": "INTERNAL_AUTH_NOT_CONFIGURED",
                "message": "Internal auth is not configured on this deployment.",
            },
        )
    token_ok = x_internal_token and secrets.compare_digest(
        x_internal_token, settings.internal_token
    )
    if not token_ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={
                "error_code": "UNAUTHORIZED",
                "message": "Missing or invalid internal auth token.",
            },
        )
