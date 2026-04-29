import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.api.auth.jwt import decode_token

_bearer = HTTPBearer(auto_error=False)


def _get_credentials(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> dict:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "UNAUTHORIZED", "message": "Missing authorization token."},
        )
    try:
        payload = decode_token(credentials.credentials)
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "UNAUTHORIZED", "message": "Token has expired."},
        )
    except jwt.PyJWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"error_code": "UNAUTHORIZED", "message": "Invalid token."},
        )
    return payload  # type: ignore[return-value]


def require_auth(payload: dict = Depends(_get_credentials)) -> dict:
    """Dependency that validates the JWT and returns the claims dict."""
    return payload


def _coerce_groups(payload: dict) -> list[str]:
    """Defensively coerce the 'groups' claim to a list[str].

    A malformed JWT carrying ``groups: "admin"`` (string instead of array)
    would match ``"admin" in groups`` via substring, granting unintended access.
    This helper normalises the claim before any membership check.
    """
    groups_raw = payload.get("groups", [])
    if not isinstance(groups_raw, list):
        groups_raw = []
    return [g for g in groups_raw if isinstance(g, str)]


def _require_group(group: str):
    def _dep(payload: dict = Depends(require_auth)) -> dict:
        groups = _coerce_groups(payload)
        if "admin" in groups or group in groups:
            return payload
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "FORBIDDEN",
                "message": f"This route requires the '{group}' group.",
            },
        )

    return _dep


require_any_group = _require_group("__any__")


def _require_any_valid_group(payload: dict = Depends(require_auth)) -> dict:
    groups = _coerce_groups(payload)
    valid = {"de", "da", "dg", "admin"}
    if not set(groups) & valid:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error_code": "FORBIDDEN",
                "message": "Route requires at least one valid group (de, da, dg).",
            },
        )
    return payload


require_common = _require_any_valid_group
require_dg = _require_group("dg")
require_de = _require_group("de")
require_da = _require_group("da")
require_admin = _require_group("admin")
