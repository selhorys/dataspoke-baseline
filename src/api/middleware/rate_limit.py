from starlette.requests import Request

from slowapi import Limiter
from slowapi.util import get_remote_address

from src.shared.settings import settings


def _get_user_key(request: Request) -> str:
    """Extract per-user key from JWT sub claim, falling back to IP."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        try:
            from src.api.auth.jwt import decode_token

            payload = decode_token(auth.removeprefix("Bearer "))
            return payload.get("sub", get_remote_address(request))
        except Exception:
            pass
    return get_remote_address(request)


storage_uri = f"redis://{settings.redis_host}:{settings.redis_port}"
limiter = Limiter(key_func=_get_user_key, storage_uri=storage_uri)

DEFAULT_LIMIT = f"{settings.rate_limit_per_minute}/minute"
