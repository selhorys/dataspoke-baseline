from slowapi import Limiter
from slowapi.util import get_remote_address

from src.api.config import settings

# In-memory rate limiter (single instance).
# TODO: switch to Redis backend for multi-instance correctness:
#   storage_uri = f"redis://{settings.redis_host}:{settings.redis_port}"
#   limiter = Limiter(key_func=get_remote_address, storage_uri=storage_uri)
limiter = Limiter(key_func=get_remote_address)

DEFAULT_LIMIT = f"{settings.rate_limit_per_minute}/minute"
