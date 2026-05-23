"""Langfuse secret key accessor backed by the Kubernetes Secret ``dataspoke-langfuse-secret``.

Public surface:
    get_langfuse_secret_key() -> str
    set_langfuse_secret_key(value: str) -> None
    langfuse_secret_key_is_set() -> bool
    invalidate_langfuse_secret_key_cache() -> None

    Exceptions re-raised from the k8s layer:
        SecretResolverUnavailable  (from secret_resolver) — out-of-cluster

Design notes:
- Reuses ``secret_resolver._require_client()`` to get the (CoreV1Api, namespace)
  pair without duplicating the in-cluster init logic and without weakening the
  source-cred prefix guard on secret_resolver's public functions.
- Cache: simple monotonic-TTL single-entry cache; no LRU needed (single key).
- The plaintext secret key value is NEVER logged or included in exception messages.
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
# scoping these calls to the Langfuse key is this hardcoded name. Do NOT parameterize
# _SECRET_NAME from any request input — doing so would let an admin read/overwrite
# dataspoke-secrets (JWT signing key, DataHub token, etc.).
_SECRET_NAME = "dataspoke-langfuse-secret"
_SECRET_KEY = "secret_key"
_TTL_SECONDS = 60.0

# Single-entry cache: (value, expiry_monotonic) | None
_cache: tuple[str, float] | None = None


def invalidate_langfuse_secret_key_cache() -> None:
    """Evict the cached Langfuse secret key, forcing a fresh read on the next call."""
    global _cache
    _cache = None


def get_langfuse_secret_key() -> str:
    """Return the Langfuse secret key, resolving from the Kubernetes Secret with TTL caching.

    Resolution order:
    1. Cache hit (within TTL) → return cached value.
    2. In-cluster: read ``dataspoke-langfuse-secret`` ``data.secret_key`` via k8s API.
       - Secret/key absent → treat as unset; cache ``""`` for TTL.
       - RBAC 403 → log a warning, return ``""`` (fail-safe; do not cache).
       - Other k8s errors (wrapped as SecretResolverUnavailable) → propagate.
    3. Out-of-cluster (SecretResolverUnavailable from _require_client) →
       fall back to ``settings.langfuse_secret_key`` WITHOUT caching (host-mode only).

    The plaintext secret key value is NEVER logged.
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
        return settings.langfuse_secret_key

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
                "langfuse_secret_rbac_denied",
                extra={"secret_name": _SECRET_NAME, "namespace": namespace},
            )
            return ""
        # Other k8s error — propagate as unavailable (matches secret_resolver convention)
        raise SecretResolverUnavailable(
            "Kubernetes API unavailable when reading Langfuse secret"
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


def set_langfuse_secret_key(value: str) -> None:
    """Write the Langfuse secret key to ``dataspoke-langfuse-secret`` ``data.secret_key``.

    Uses create-or-patch semantics.  Invalidates the process cache on success
    so subsequent reads are fresh.

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
                "Kubernetes API unavailable when checking Langfuse secret"
            ) from exc

    if existing:
        patch_body = {"data": {_SECRET_KEY: encoded}}
        try:
            core.patch_namespaced_secret(name=_SECRET_NAME, namespace=namespace, body=patch_body)
        except ApiException as exc:
            raise SecretResolverUnavailable(
                "Kubernetes API write failed for Langfuse secret"
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
                "Kubernetes API create failed for Langfuse secret"
            ) from exc

    invalidate_langfuse_secret_key_cache()


def langfuse_secret_key_is_set() -> bool:
    """Return True if the Langfuse secret key is set (non-empty), without exposing the value."""
    return bool(get_langfuse_secret_key())
