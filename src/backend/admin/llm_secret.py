"""LLM API key accessor backed by the Kubernetes Secret ``dataspoke-llm-secret``.

Public surface:
    get_llm_api_key() -> str
    set_llm_api_key(value: str) -> None
    llm_api_key_is_set() -> bool
    invalidate_llm_api_key_cache() -> None

    Exceptions re-raised from the k8s layer:
        SecretResolverUnavailable  (from secret_resolver) — out-of-cluster

Design notes:
- Reuses ``secret_resolver._require_client()`` to get the (CoreV1Api, namespace)
  pair without duplicating the in-cluster init logic and without weakening the
  source-cred prefix guard on secret_resolver's public functions.
- Cache: simple monotonic-TTL single-entry cache; no LRU needed (single key).
- The plaintext key is NEVER logged or included in exception messages.
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

from src.backend.ingestion.secret_resolver import SecretResolverUnavailable, _require_client
from src.shared.settings import settings

logger = logging.getLogger(__name__)

# SECURITY BOUNDARY: the API ServiceAccount's Role grants get/create/patch on ALL
# Secrets in the namespace (resourceNames unset), and this accessor deliberately
# bypasses secret_resolver's dataspoke-source-cred- prefix guard. The *only* thing
# scoping these calls to the LLM key is this hardcoded name. Do NOT parameterize
# _SECRET_NAME from any request input — doing so would let an admin read/overwrite
# dataspoke-secrets (JWT signing key, DataHub token, etc.).
_SECRET_NAME = "dataspoke-llm-secret"
_SECRET_KEY = "api_key"
_TTL_SECONDS = 60.0

# Single-entry cache: (value, expiry_monotonic) | None
_cache: tuple[str, float] | None = None


def invalidate_llm_api_key_cache() -> None:
    """Evict the cached LLM API key, forcing a fresh read on the next call."""
    global _cache
    _cache = None


def get_llm_api_key() -> str:
    """Return the LLM API key, resolving from the Kubernetes Secret with TTL caching.

    Resolution order:
    1. Cache hit (within TTL) → return cached value.
    2. In-cluster: read ``dataspoke-llm-secret`` ``data.api_key`` via k8s API.
       - Secret/key absent → treat as unset; cache ``""`` for TTL.
       - RBAC 403 → log a warning, return ``""`` (fail-safe; LLM call will
         fail clearly on an empty key; do not cache).
       - Other k8s errors (wrapped as SecretResolverUnavailable) → propagate.
    3. Out-of-cluster (SecretResolverUnavailable from _require_client) →
       fall back to ``settings.llm_api_key`` WITHOUT caching (host-mode only).

    The plaintext key value is NEVER logged.
    """
    global _cache
    from kubernetes.client.exceptions import ApiException

    # 1. Cache hit
    if _cache is not None:
        value, expiry = _cache
        if time.monotonic() < expiry:
            return value
        _cache = None

    # 2. Attempt in-cluster read
    core: Any
    namespace: str
    try:
        core, namespace = _require_client()
    except SecretResolverUnavailable:
        # 3. Out-of-cluster fallback — do NOT cache (host-mode transient)
        return settings.llm_api_key

    try:
        secret = core.read_namespaced_secret(name=_SECRET_NAME, namespace=namespace)
    except ApiException as exc:
        if exc.status == 404:
            # Secret not yet created — treat as unset
            _cache = ("", time.monotonic() + _TTL_SECONDS)
            return ""
        if exc.status == 403:
            # RBAC misconfiguration — fail safe, don't cache
            logger.warning(
                "llm_secret_rbac_denied",
                extra={"secret_name": _SECRET_NAME, "namespace": namespace},
            )
            return ""
        # Other k8s error — propagate as unavailable (matches secret_resolver convention)
        raise SecretResolverUnavailable(
            "Kubernetes API unavailable when reading LLM secret"
        ) from exc

    data: dict[str, str] | None = secret.data
    if data is None or _SECRET_KEY not in data:
        # Secret exists but key absent — treat as unset
        _cache = ("", time.monotonic() + _TTL_SECONDS)
        return ""

    # base64-decode the raw k8s Secret data value
    decoded = base64.b64decode(data[_SECRET_KEY]).decode("utf-8")
    _cache = (decoded, time.monotonic() + _TTL_SECONDS)
    return decoded


def set_llm_api_key(value: str) -> None:
    """Write the LLM API key to ``dataspoke-llm-secret`` ``data.api_key``.

    Uses create-or-patch semantics (mirrors write_secret_value's branching
    without the source-cred prefix guard — the fixed target name plus admin
    auth are the controls here).

    Invalidates the process cache on success so subsequent reads are fresh.

    Raises:
        SecretResolverUnavailable: if out-of-cluster (PATCH cannot persist
            without the cluster; surfaces as 503 at the API layer).
    """
    from kubernetes import client as k8s_client
    from kubernetes.client.exceptions import ApiException

    core, namespace = _require_client()  # raises SecretResolverUnavailable if out-of-cluster

    encoded = base64.b64encode(value.encode()).decode()
    existing: bool

    try:
        core.read_namespaced_secret(name=_SECRET_NAME, namespace=namespace)
        existing = True
    except ApiException as exc:
        if exc.status == 404:
            existing = False
        else:
            raise SecretResolverUnavailable(
                "Kubernetes API unavailable when checking LLM secret"
            ) from exc

    if existing:
        patch_body = {"data": {_SECRET_KEY: encoded}}
        try:
            core.patch_namespaced_secret(name=_SECRET_NAME, namespace=namespace, body=patch_body)
        except ApiException as exc:
            raise SecretResolverUnavailable(
                "Kubernetes API write failed for LLM secret"
            ) from exc
    else:
        new_secret = k8s_client.V1Secret(
            metadata=k8s_client.V1ObjectMeta(name=_SECRET_NAME, namespace=namespace),
            data={_SECRET_KEY: encoded},
        )
        try:
            core.create_namespaced_secret(namespace=namespace, body=new_secret)
        except ApiException as exc:
            raise SecretResolverUnavailable(
                "Kubernetes API create failed for LLM secret"
            ) from exc

    invalidate_llm_api_key_cache()


def llm_api_key_is_set() -> bool:
    """Return True if the LLM API key is set (non-empty), without exposing the value."""
    return bool(get_llm_api_key())
