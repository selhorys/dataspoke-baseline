"""Kubernetes Secret resolver, writer, and verifier for ingestion auth credentials.

Public surface:
    resolve_secret_ref(ref: str) -> str
    write_secret_value(name: str, key: str, value: str, force_overwrite: bool) -> None
    verify_secret_ref(name: str, key: str) -> None

    SecretRefMalformed
    SecretRefNameForbidden
    SecretRefNotFound
    SecretCollision
    SecretResolverUnavailable
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_NAMESPACE_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
_TTL_SECONDS = 60.0
_CACHE_MAX_SIZE = 512

# All caller-managed Secret names must start with this prefix.
# Prevents callers from targeting DataSpoke's own infra Secrets
# (dataspoke-secrets, dataspoke-internal-auth, etc.) via the vault or
# reference path.
_NAME_PREFIX = "dataspoke-source-cred-"

_init_lock = threading.Lock()

_resolver_state: dict[str, Any] = {
    "client": None,
    "available": None,
    "own_namespace": None,
}

# Bounded in-memory cache: (name, key) -> (value, expiry_monotonic).
# Manual LRU+TTL to avoid adding cachetools dependency — evict oldest
# entries when at capacity.
_cache: dict[tuple[str, str], tuple[str, float]] = {}
_cache_order: list[tuple[str, str]] = []  # insertion order for LRU eviction


class SecretRefMalformed(ValueError):
    """Raised when a secret_ref string does not match the expected format."""


class SecretRefNameForbidden(ValueError):
    """Raised when secret_ref.name does not start with the required prefix."""


class SecretRefNotFound(LookupError):
    """Raised when the target Secret or key does not exist (includes RBAC 403)."""


class SecretCollision(ValueError):
    """Raised when vault-write would overwrite an existing (name, key) and force_overwrite=False."""


class SecretResolverUnavailable(RuntimeError):
    """Raised when the in-cluster Kubernetes config is not loadable."""


def _enforce_name_prefix(name: str) -> None:
    """Raise SecretRefNameForbidden if *name* does not start with ``_NAME_PREFIX``."""
    if not name.startswith(_NAME_PREFIX):
        raise SecretRefNameForbidden(
            f"secret_ref.name must start with '{_NAME_PREFIX}'; "
            f"got '{name}'. Rename your Secret to '{_NAME_PREFIX}{name}' "
            "or use an ESO sync target under that prefix."
        )


def _cache_put(key: tuple[str, str], value: str, expiry: float) -> None:
    """Insert or update a cache entry, evicting the oldest entry if at capacity."""
    if key in _cache:
        _cache[key] = (value, expiry)
        return
    if len(_cache) >= _CACHE_MAX_SIZE:
        # Evict oldest entry (first in insertion order that is still present).
        while _cache_order and _cache_order[0] not in _cache:
            _cache_order.pop(0)
        if _cache_order:
            evict = _cache_order.pop(0)
            _cache.pop(evict, None)
    _cache[key] = (value, expiry)
    _cache_order.append(key)


def _init() -> None:
    from kubernetes import client, config

    with _init_lock:
        # Double-checked locking: another thread may have completed init while we waited.
        if _resolver_state["available"] is not None:
            return

        try:
            config.load_incluster_config()
        except Exception as exc:
            _resolver_state["available"] = False
            _resolver_state["client"] = None
            raise SecretResolverUnavailable(
                f"In-cluster Kubernetes config not available: {exc}"
            ) from exc

        try:
            with open(_NAMESPACE_FILE) as f:
                _resolver_state["own_namespace"] = f.read().strip()
        except OSError as exc:
            _resolver_state["available"] = False
            _resolver_state["client"] = None
            raise SecretResolverUnavailable(
                f"Cannot read pod namespace from {_NAMESPACE_FILE}: {exc}"
            ) from exc

        # Set client BEFORE marking available=True so any reader that races
        # on the flag always sees a fully-initialised client.
        _resolver_state["client"] = client.CoreV1Api()
        _resolver_state["available"] = True


def _require_client() -> tuple[Any, str]:
    """Return (CoreV1Api, own_namespace), initialising if needed.

    Raises SecretResolverUnavailable if in-cluster config is not loadable.
    """
    if _resolver_state["available"] is False:
        raise SecretResolverUnavailable(
            "Kubernetes in-cluster config failed to load; "
            "run the API in-cluster to use secret resolution."
        )
    if _resolver_state["available"] is None:
        _init()
    return _resolver_state["client"], _resolver_state["own_namespace"]


def _parse_ref(ref: str) -> tuple[str, str]:
    """Parse ``k8s-secret/<name>/<key>`` into ``(name, key)``.

    The 3-segment own-ns form is the only valid form.  4-segment cross-namespace
    refs are rejected as malformed (single-namespace policy).
    """
    if not ref.startswith("k8s-secret/"):
        raise SecretRefMalformed(
            f"secret_ref must start with 'k8s-secret/': {ref!r}"
        )
    tail = ref[len("k8s-secret/"):]
    parts = tail.split("/")

    if len(parts) == 2:
        name, key = parts
        if not name or not key:
            raise SecretRefMalformed(
                f"secret_ref segments must be non-empty: {ref!r}"
            )
        return name, key

    if len(parts) == 3:
        raise SecretRefMalformed(
            f"Cross-namespace secret_ref is not supported (single-namespace policy): {ref!r}"
        )

    raise SecretRefMalformed(
        f"secret_ref must have exactly 2 segments after 'k8s-secret/' (<name>/<key>): {ref!r}"
    )


def _b64enc(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


def resolve_secret_ref(ref: str) -> str:
    """Resolve ``k8s-secret/<name>/<key>`` to the plaintext secret value.

    Raises:
        SecretRefMalformed: ref does not match the 2-segment own-ns form.
        SecretRefNameForbidden: name does not start with the required prefix.
        SecretRefNotFound: the Secret or key does not exist (includes RBAC 403).
        SecretResolverUnavailable: in-cluster config is not available.
    """
    from kubernetes.client.exceptions import ApiException

    core, namespace = _require_client()

    name, key = _parse_ref(ref)
    _enforce_name_prefix(name)

    cache_key = (name, key)
    cached = _cache.get(cache_key)
    if cached is not None:
        value, expiry = cached
        if time.monotonic() < expiry:
            return value
        del _cache[cache_key]

    try:
        secret = core.read_namespaced_secret(name=name, namespace=namespace)
    except ApiException as exc:
        if exc.status in (403, 404):
            logger.warning(
                "Secret access denied or not found during resolve",
                extra={"secret_name": name, "secret_namespace": namespace, "status": exc.status},
            )
            raise SecretRefNotFound(
                "Secret access denied or not found"
            ) from exc
        logger.warning(
            "Kubernetes API error during resolve",
            extra={"secret_name": name, "secret_namespace": namespace, "status": exc.status},
        )
        raise SecretResolverUnavailable("Kubernetes API unavailable") from exc

    data: dict[str, str] | None = secret.data
    if data is None or key not in data:
        raise SecretRefNotFound(
            f"Key {key!r} not found in Secret {namespace}/{name}"
        )

    decoded = base64.b64decode(data[key]).decode("utf-8")
    _cache_put(cache_key, decoded, time.monotonic() + _TTL_SECONDS)
    return decoded


def write_secret_value(name: str, key: str, value: str, force_overwrite: bool) -> None:
    """Vault path: write ``value`` under ``data[key]`` of secret ``name`` in own-ns.

    - Secret does not exist: create with ``data={key: base64(value)}``.
    - Secret exists and ``data[key]`` is missing: merge-patch to add the key.
    - Secret exists and ``data[key]`` is present and ``force_overwrite=False``:
      raises ``SecretCollision``.
    - Secret exists and ``data[key]`` is present and ``force_overwrite=True``:
      merge-patch to update only ``data[key]``; other keys are preserved.

    Invalidates the cache entry for ``(name, key)`` on success.

    Raises:
        SecretRefNameForbidden: name does not start with the required prefix.
        SecretCollision: key already exists and force_overwrite is False.
        SecretResolverUnavailable: in-cluster config not loadable or k8s API error.
    """
    from kubernetes import client as k8s_client
    from kubernetes.client.exceptions import ApiException

    _enforce_name_prefix(name)
    core, namespace = _require_client()

    encoded = _b64enc(value)
    existing_data: dict[str, str] | None = None

    try:
        secret = core.read_namespaced_secret(name=name, namespace=namespace)
        existing_data = secret.data or {}
    except ApiException as exc:
        if exc.status == 404:
            existing_data = None
        else:
            logger.warning(
                "Kubernetes API error during collision check",
                extra={"secret_name": name, "secret_namespace": namespace, "status": exc.status},
            )
            raise SecretResolverUnavailable(
                "Kubernetes API unavailable"
            ) from exc

    if existing_data is None:
        # Secret does not exist — create it.
        new_secret = k8s_client.V1Secret(
            metadata=k8s_client.V1ObjectMeta(name=name, namespace=namespace),
            data={key: encoded},
        )
        try:
            core.create_namespaced_secret(namespace=namespace, body=new_secret)
        except ApiException as exc:
            logger.warning(
                "Kubernetes API error during secret create",
                extra={"secret_name": name, "secret_namespace": namespace, "status": exc.status},
            )
            raise SecretResolverUnavailable(
                "Kubernetes API write failed"
            ) from exc
    else:
        if key in existing_data:
            if not force_overwrite:
                raise SecretCollision(
                    f"Secret '{name}' already contains key '{key}'; "
                    "set force_overwrite=true to update it."
                )
        patch_body = {"data": {key: encoded}}
        try:
            core.patch_namespaced_secret(name=name, namespace=namespace, body=patch_body)
        except ApiException as exc:
            logger.warning(
                "Kubernetes API error during secret patch",
                extra={"secret_name": name, "secret_namespace": namespace, "status": exc.status},
            )
            raise SecretResolverUnavailable(
                "Kubernetes API write failed"
            ) from exc

    # Invalidate the cache for this (name, key) pair.
    _cache.pop((name, key), None)


def verify_secret_ref(name: str, key: str) -> None:
    """Reference path: confirm that secret ``name`` exists and contains ``key``.

    Raises:
        SecretRefNameForbidden: name does not start with the required prefix.
        SecretRefNotFound: secret missing or key absent.
        SecretResolverUnavailable: in-cluster config not loadable or k8s API error.
    """
    from kubernetes.client.exceptions import ApiException

    _enforce_name_prefix(name)
    core, namespace = _require_client()

    try:
        secret = core.read_namespaced_secret(name=name, namespace=namespace)
    except ApiException as exc:
        if exc.status in (403, 404):
            logger.warning(
                "Secret access denied or not found during verify",
                extra={"secret_name": name, "secret_namespace": namespace, "status": exc.status},
            )
            raise SecretRefNotFound(
                "Secret access denied or not found"
            ) from exc
        logger.warning(
            "Kubernetes API error during secret verify",
            extra={"secret_name": name, "secret_namespace": namespace, "status": exc.status},
        )
        raise SecretResolverUnavailable("Kubernetes API unavailable") from exc

    data: dict[str, str] | None = secret.data
    if data is None or key not in data:
        raise SecretRefNotFound(
            f"Key '{key}' not present in secret '{name}'"
        )
