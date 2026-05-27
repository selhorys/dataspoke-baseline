from starlette.requests import Request

from slowapi import Limiter
from slowapi.util import get_remote_address

from src.shared.settings import settings


def _get_user_key(request: Request) -> str:
    """Extract per-user key from JWT sub claim, falling back to IP."""
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        try:
            from src.backend.auth.tokens import decode_access_token

            payload = decode_access_token(auth.removeprefix("Bearer "))
            return payload.get("sub", get_remote_address(request))
        except Exception:
            pass
    return get_remote_address(request)


_auth = f":{settings.redis_password}@" if settings.redis_password else ""
storage_uri = f"redis://{_auth}{settings.redis_host}:{settings.redis_port}"
limiter = Limiter(
    key_func=_get_user_key,
    storage_uri=storage_uri,
    default_limits=[f"{settings.rate_limit_per_minute}/minute"],
    # Fall back to in-process memory storage when Redis is unreachable so that
    # a transient Redis outage (or the unit-test environment where Redis is
    # absent) does not turn every request into an unhandled exception.
    in_memory_fallback_enabled=True,
)
