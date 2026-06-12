"""DataHub token accessor backed by the Kubernetes Secret ``dataspoke-datahub-secret``.

Public surface:
    get_datahub_token() -> str
    set_datahub_token(value: str) -> None
    datahub_token_is_set() -> bool
    invalidate_datahub_token_cache() -> None

    Exceptions re-raised from the k8s layer:
        SecretResolverUnavailable  (from src.shared.secrets) — k8s client init failure

Design notes:
- Reuses ``src.shared.secrets.k8s.require_k8s_client()`` to get the
  (CoreV1Api, namespace) pair without duplicating the in-cluster init logic and
  without weakening the source-cred prefix guard on the resolver's public functions.
- Cache: simple monotonic-TTL single-entry cache; no LRU needed (single key).
- The plaintext token value is NEVER logged or included in exception messages.
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any

from src.shared.secrets import SecretResolverUnavailable
from src.shared.secrets.k8s import require_k8s_client

logger = logging.getLogger(__name__)

# SECURITY BOUNDARY: the API ServiceAccount's Role grants get/create/patch on ALL
# Secrets in the namespace (resourceNames unset), and this accessor deliberately
# bypasses the dataspoke-source-cred- prefix guard enforced by
# KubernetesSecretBackend (src/shared/secrets/k8s.py). The *only* thing
# scoping these calls to the DataHub token is this hardcoded name. Do NOT parameterize
# _SECRET_NAME from any request input — doing so would let an admin read/overwrite
# dataspoke-secrets (JWT signing key, LLM key, etc.).
_SECRET_NAME = "dataspoke-datahub-secret"
_SECRET_KEY = "token"
_TTL_SECONDS = 60.0

# Single-entry cache: (value, expiry_monotonic) | None
_cache: tuple[str, float] | None = None


def invalidate_datahub_token_cache() -> None:
    """Evict the cached DataHub token, forcing a fresh read on the next call."""
    global _cache
    _cache = None


def get_datahub_token() -> str:
    """Return the DataHub token, resolving from the Kubernetes Secret with TTL caching.

    Resolution order:
    1. Cache hit (within TTL) → return cached value.
    2. Read ``dataspoke-datahub-secret`` ``data.token`` via k8s API.
       - Secret/key absent → treat as unset; cache ``""`` for TTL.
       - RBAC 403 → log a warning, return ``""`` (fail-safe; do not cache).
       - Other k8s errors (wrapped as SecretResolverUnavailable) → propagate.

    The plaintext token value is NEVER logged.
    """
    global _cache
    from kubernetes.client.exceptions import ApiException

    # 1. Cache hit
    if _cache is not None:
        value, expiry = _cache
        if time.monotonic() < expiry:
            return value
        _cache = None

    # 2. Read Secret via k8s API
    core: Any
    namespace: str
    core, namespace = require_k8s_client()

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
                "datahub_secret_rbac_denied",
                extra={"secret_name": _SECRET_NAME, "namespace": namespace},
            )
            return ""
        # Other k8s error — propagate as unavailable (matches the resolver convention)
        raise SecretResolverUnavailable(
            "Kubernetes API unavailable when reading DataHub secret"
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


def set_datahub_token(value: str) -> None:
    """Write the DataHub token to ``dataspoke-datahub-secret`` ``data.token``.

    Uses create-or-patch semantics.  Invalidates the process cache on success
    so subsequent reads are fresh.

    Raises:
        SecretResolverUnavailable: if out-of-cluster (PATCH cannot persist
            without the cluster; surfaces as 503 at the API layer).
    """
    from kubernetes import client as k8s_client
    from kubernetes.client.exceptions import ApiException

    core, namespace = require_k8s_client()  # raises SecretResolverUnavailable if out-of-cluster

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
                "Kubernetes API unavailable when checking DataHub secret"
            ) from exc

    if existing:
        patch_body = {"data": {_SECRET_KEY: encoded}}
        try:
            core.patch_namespaced_secret(name=_SECRET_NAME, namespace=namespace, body=patch_body)
        except ApiException as exc:
            raise SecretResolverUnavailable(
                "Kubernetes API write failed for DataHub secret"
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
                "Kubernetes API create failed for DataHub secret"
            ) from exc

    invalidate_datahub_token_cache()


def datahub_token_is_set() -> bool:
    """Return True if the DataHub token is set (non-empty), without exposing the value."""
    return bool(get_datahub_token())
