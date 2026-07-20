"""DataHub credential accessors backed by the Kubernetes Secret ``dataspoke-datahub-secret``.

The Secret carries two independent credentials under two data keys: ``token``
(the GMS personal access token) and ``kafka_sasl_password`` (the SASL credential
the event consumer uses to reach a secured Kafka).

Public surface:
    get_datahub_token() -> str
    set_datahub_token(value: str) -> None
    datahub_token_is_set() -> bool
    invalidate_datahub_token_cache() -> None

    get_datahub_kafka_sasl_password() -> str
    set_datahub_kafka_sasl_password(value: str) -> None
    datahub_kafka_sasl_password_is_set() -> bool
    invalidate_datahub_kafka_sasl_password_cache() -> None

    Exceptions re-raised from the k8s layer:
        SecretResolverUnavailable  (from src.shared.secrets) — k8s client init failure

Design notes:
- Reuses ``src.shared.secrets.k8s.require_k8s_client()`` to get the
  (CoreV1Api, namespace) pair without duplicating the in-cluster init logic and
  without weakening the source-cred prefix guard on the resolver's public functions.
- Cache: monotonic-TTL cache keyed by Secret data key.
- The plaintext credential values are NEVER logged or included in exception messages.
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
# KubernetesSecretBackend (src/shared/secrets/k8s.py). The *only* things
# scoping these calls to the DataHub credentials are the hardcoded name below and
# the fixed _ALLOWED_KEYS allowlist. Do NOT parameterize _SECRET_NAME or the key
# argument from any request input — doing so would let an admin read/overwrite
# dataspoke-secrets (JWT signing key, LLM key, etc.), or read an unrelated key out
# of this Secret. Callers reach these helpers only through the per-credential
# wrappers at the bottom of this module, each of which passes a module constant.
_SECRET_NAME = "dataspoke-datahub-secret"
_KEY_TOKEN = "token"
_KEY_KAFKA_SASL_PASSWORD = "kafka_sasl_password"
_ALLOWED_KEYS: frozenset[str] = frozenset({_KEY_TOKEN, _KEY_KAFKA_SASL_PASSWORD})
_TTL_SECONDS = 60.0

# key -> (value, expiry_monotonic)
_cache: dict[str, tuple[str, float]] = {}


def _assert_allowed(key: str) -> None:
    if key not in _ALLOWED_KEYS:
        raise ValueError(f"Secret key not in the DataHub allowlist: {key!r}")


def _invalidate(key: str) -> None:
    _assert_allowed(key)
    _cache.pop(key, None)


def _get_secret_value(key: str) -> str:
    """Return the value of *key*, resolving from the Secret with TTL caching.

    Resolution order:
    1. Cache hit (within TTL) → return cached value.
    2. Read ``dataspoke-datahub-secret`` ``data.<key>`` via k8s API.
       - Secret/key absent → treat as unset; cache ``""`` for TTL.
       - RBAC 403 → log a warning, return ``""`` (fail-safe; do not cache).
       - Other k8s errors (wrapped as SecretResolverUnavailable) → propagate.

    The plaintext value is NEVER logged.
    """
    from kubernetes.client.exceptions import ApiException

    _assert_allowed(key)

    # 1. Cache hit
    cached = _cache.get(key)
    if cached is not None:
        value, expiry = cached
        if time.monotonic() < expiry:
            return value
        _cache.pop(key, None)

    # 2. Read Secret via k8s API
    core: Any
    namespace: str
    core, namespace = require_k8s_client()

    try:
        secret = core.read_namespaced_secret(name=_SECRET_NAME, namespace=namespace)
    except ApiException as exc:
        if exc.status == 404:
            # Secret not yet created — treat as unset
            _cache[key] = ("", time.monotonic() + _TTL_SECONDS)
            return ""
        if exc.status == 403:
            # RBAC misconfiguration — fail safe, don't cache
            logger.warning(
                "datahub_secret_rbac_denied",
                extra={"secret_name": _SECRET_NAME, "secret_key": key, "namespace": namespace},
            )
            return ""
        # Other k8s error — propagate as unavailable (matches the resolver convention)
        raise SecretResolverUnavailable(
            "Kubernetes API unavailable when reading DataHub secret"
        ) from exc

    data: dict[str, str] | None = secret.data
    if data is None or key not in data:
        # Secret exists but key absent — treat as unset
        _cache[key] = ("", time.monotonic() + _TTL_SECONDS)
        return ""

    # base64-decode the raw k8s Secret data value
    decoded = base64.b64decode(data[key]).decode("utf-8")
    _cache[key] = (decoded, time.monotonic() + _TTL_SECONDS)
    return decoded


def _set_secret_value(key: str, value: str) -> None:
    """Write *value* to ``dataspoke-datahub-secret`` ``data.<key>``.

    Uses create-or-patch semantics; the patch is a strategic merge on ``data``,
    so sibling keys in the Secret are preserved.  Invalidates the cached entry
    for *key* on success so subsequent reads are fresh.

    Raises:
        SecretResolverUnavailable: if out-of-cluster (PATCH cannot persist
            without the cluster; surfaces as 503 at the API layer).
    """
    from kubernetes import client as k8s_client
    from kubernetes.client.exceptions import ApiException

    _assert_allowed(key)

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
        patch_body = {"data": {key: encoded}}
        try:
            core.patch_namespaced_secret(name=_SECRET_NAME, namespace=namespace, body=patch_body)
        except ApiException as exc:
            raise SecretResolverUnavailable(
                "Kubernetes API write failed for DataHub secret"
            ) from exc
    else:
        new_secret = k8s_client.V1Secret(
            metadata=k8s_client.V1ObjectMeta(name=_SECRET_NAME, namespace=namespace),
            data={key: encoded},
        )
        try:
            core.create_namespaced_secret(namespace=namespace, body=new_secret)
        except ApiException as exc:
            raise SecretResolverUnavailable(
                "Kubernetes API create failed for DataHub secret"
            ) from exc

    _invalidate(key)


# ── Per-credential wrappers ───────────────────────────────────────────────────


def invalidate_datahub_token_cache() -> None:
    """Evict the cached DataHub token, forcing a fresh read on the next call."""
    _invalidate(_KEY_TOKEN)


def get_datahub_token() -> str:
    """Return the DataHub GMS token, or ``""`` when unset."""
    return _get_secret_value(_KEY_TOKEN)


def set_datahub_token(value: str) -> None:
    """Write the DataHub GMS token to ``dataspoke-datahub-secret`` ``data.token``."""
    _set_secret_value(_KEY_TOKEN, value)


def datahub_token_is_set() -> bool:
    """Return True if the DataHub token is set (non-empty), without exposing the value."""
    return bool(get_datahub_token())


def invalidate_datahub_kafka_sasl_password_cache() -> None:
    """Evict the cached Kafka SASL password, forcing a fresh read on the next call."""
    _invalidate(_KEY_KAFKA_SASL_PASSWORD)


def get_datahub_kafka_sasl_password() -> str:
    """Return the Kafka SASL password, or ``""`` when unset."""
    return _get_secret_value(_KEY_KAFKA_SASL_PASSWORD)


def set_datahub_kafka_sasl_password(value: str) -> None:
    """Write the Kafka SASL password to ``dataspoke-datahub-secret``.

    Stored under the ``kafka_sasl_password`` data key, alongside ``token``.
    """
    _set_secret_value(_KEY_KAFKA_SASL_PASSWORD, value)


def datahub_kafka_sasl_password_is_set() -> bool:
    """Return True if the Kafka SASL password is set, without exposing the value."""
    return bool(get_datahub_kafka_sasl_password())
