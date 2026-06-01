"""Kubernetes Secret resolver for ingestion source credentials.

Reference-only model: DataSpoke never writes credential values. An operator
pre-creates Kubernetes Secrets out-of-band; DataSpoke only lists, verifies,
and resolves them at run time.

Reference syntax: ``${name__key}`` embedded directly in ``recipe.source.config``.
Resolves to Kubernetes Secret ``dataspoke-source-cred-<name>``, data key ``<key>``.

The resolver is synchronous — k8s Python client calls are blocking but fast;
async wrappers add no value over the extractor latency that dominates ingestion.

Spec: spec/feature/SECRET_RESOLUTION.md
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_NAMESPACE_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
_TTL_SECONDS = 60.0
_CACHE_MAX_SIZE = 512

# All source-credential Secret names must start with this prefix.
# This is a security boundary: it prevents recipe authors from reading
# DataSpoke's own infra Secrets (dataspoke-secrets, dataspoke-llm-secret, …)
# via a recipe reference.
_NAME_PREFIX = "dataspoke-source-cred-"

_init_lock = threading.Lock()

_resolver_state: dict[str, Any] = {
    "client": None,
    "available": None,  # None=not tried, True=ok, False=failed
    "own_namespace": None,
}

# Bounded in-memory cache: (secret_name, key) -> (plaintext_value, expiry_monotonic).
# Manual LRU+TTL — insertion-order eviction when at capacity.
_cache: dict[tuple[str, str], tuple[str, float]] = {}
_cache_order: list[tuple[str, str]] = []


# ── Exceptions ────────────────────────────────────────────────────────────────


class SecretRefMalformed(ValueError):
    """Raised when a ``name__key`` token has no ``__``, or an empty name/key segment."""


class SecretRefNotFound(LookupError):
    """Raised when the target Secret or data key does not exist, including RBAC 403."""


class SecretResolverUnavailable(RuntimeError):
    """Raised when in-cluster Kubernetes config is not loadable or the API is unreachable."""


# ── Data model ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SecretRefInfo:
    """Metadata for one (secret, key) pair — values are never included."""

    ref: str          # ``name__key`` — paste into a recipe as ``${ref}``
    secret_name: str  # full k8s Secret name: ``dataspoke-source-cred-<name>``
    key: str          # key within the Secret's ``data`` map


# ── Cache helpers ─────────────────────────────────────────────────────────────


def _cache_put(cache_key: tuple[str, str], value: str, expiry: float) -> None:
    """Insert or update a cache entry, evicting the oldest when at capacity."""
    if cache_key in _cache:
        _cache[cache_key] = (value, expiry)
        return
    if len(_cache) >= _CACHE_MAX_SIZE:
        # Evict oldest entry present in the cache.
        while _cache_order and _cache_order[0] not in _cache:
            _cache_order.pop(0)
        if _cache_order:
            evict = _cache_order.pop(0)
            _cache.pop(evict, None)
    _cache[cache_key] = (value, expiry)
    _cache_order.append(cache_key)


# ── Initialisation ────────────────────────────────────────────────────────────


def _init() -> None:
    from kubernetes import client, config  # type: ignore[import-untyped]

    with _init_lock:
        if _resolver_state["available"] is not None:
            return  # double-checked locking

        try:
            config.load_incluster_config()
        except Exception as exc:
            _resolver_state["available"] = False
            _resolver_state["client"] = None
            raise SecretResolverUnavailable(
                f"In-cluster Kubernetes config not available: {exc}"
            ) from exc

        try:
            with open(_NAMESPACE_FILE) as fh:
                _resolver_state["own_namespace"] = fh.read().strip()
        except OSError as exc:
            _resolver_state["available"] = False
            _resolver_state["client"] = None
            raise SecretResolverUnavailable(
                f"Cannot read pod namespace from {_NAMESPACE_FILE}: {exc}"
            ) from exc

        # Set client before marking available so racing threads always see a
        # fully-initialised client.
        _resolver_state["client"] = client.CoreV1Api()
        _resolver_state["available"] = True


def _require_client() -> tuple[Any, str]:
    """Return ``(CoreV1Api, own_namespace)``, initialising on first call.

    Raises:
        SecretResolverUnavailable: if in-cluster config failed to load.
    """
    if _resolver_state["available"] is False:
        raise SecretResolverUnavailable(
            "Kubernetes in-cluster config failed to load; "
            "run the API in-cluster to use secret resolution."
        )
    if _resolver_state["available"] is None:
        _init()
    return _resolver_state["client"], _resolver_state["own_namespace"]


# ── Reference parsing ─────────────────────────────────────────────────────────


def _parse_name_key(ref: str) -> tuple[str, str]:
    """Split ``name__key`` on the last ``__`` into ``(name, key)``.

    Raises:
        SecretRefMalformed: if ``__`` is absent, or either segment is empty.
    """
    if "__" not in ref:
        raise SecretRefMalformed(
            f"Secret ref {ref!r} has no '__' separator. "
            "Expected format: 'name__key' (e.g. 'team-pg__password')."
        )
    # Split on the LAST ``__`` so ``name`` may contain ``__`` (though DNS-label
    # names cannot, this is a defensive choice consistent with the spec).
    last_sep = ref.rfind("__")
    name = ref[:last_sep]
    key = ref[last_sep + 2:]
    if not name:
        raise SecretRefMalformed(f"Secret ref {ref!r}: name segment is empty.")
    if not key:
        raise SecretRefMalformed(f"Secret ref {ref!r}: key segment is empty.")
    return name, key


def _secret_name(name: str) -> str:
    """Return the full k8s Secret name for a parsed ``name`` segment."""
    return f"{_NAME_PREFIX}{name}"


# ── Public surface ────────────────────────────────────────────────────────────


def resolve_secret_ref(ref: str) -> str:
    """Resolve a ``name__key`` token to its plaintext value.

    Reads k8s Secret ``dataspoke-source-cred-<name>``, data key ``<key>``,
    base64-decodes the value, and caches it for ``_TTL_SECONDS``.

    Args:
        ref: The inner ``name__key`` string (the content inside ``${...}``).

    Returns:
        Plaintext credential value.

    Raises:
        SecretRefMalformed: ``ref`` has no ``__``, or an empty name/key segment.
        SecretRefNotFound: Secret or key absent, or RBAC 403.
        SecretResolverUnavailable: In-cluster config not loadable, or k8s API error.
    """
    from kubernetes.client.exceptions import ApiException  # type: ignore[import-untyped]

    name, key = _parse_name_key(ref)
    full_name = _secret_name(name)

    core, namespace = _require_client()
    cache_key = (full_name, key)

    cached = _cache.get(cache_key)
    if cached is not None:
        value, expiry = cached
        if time.monotonic() < expiry:
            return value
        del _cache[cache_key]

    try:
        secret = core.read_namespaced_secret(name=full_name, namespace=namespace)
    except ApiException as exc:
        if exc.status in (403, 404):
            logger.warning(
                "Secret not found or access denied during resolve",
                extra={"secret_name": full_name, "namespace": namespace, "status": exc.status},
            )
            raise SecretRefNotFound(
                f"Secret '{full_name}' not found or access denied (k8s status {exc.status})"
            ) from exc
        logger.warning(
            "Kubernetes API error during secret resolve",
            extra={"secret_name": full_name, "namespace": namespace, "status": exc.status},
        )
        raise SecretResolverUnavailable(
            f"Kubernetes API returned status {exc.status} for secret '{full_name}'"
        ) from exc

    data: dict[str, str] | None = secret.data
    if data is None or key not in data:
        raise SecretRefNotFound(
            f"Key '{key}' not found in Secret '{namespace}/{full_name}'"
        )

    try:
        decoded = base64.b64decode(data[key]).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        raise SecretRefNotFound(
            f"Secret '{full_name}' key '{key}' value is not valid UTF-8"
        )
    _cache_put(cache_key, decoded, time.monotonic() + _TTL_SECONDS)
    return decoded


# Matches a ${name__key} secret reference: a DNS-label-safe name segment
# (lowercase alphanumerics and hyphens), then the literal __ separator, then a
# Secret data-key segment. This is the same shape the source-level extractor
# (shared.models.ingestion._SECRET_REF_RE) recognises, so a reference verified at
# save time is exactly the set substituted at run time. A ${...} token that does
# not match (e.g. an uppercase name, or a DataHub env placeholder without __) is
# left untouched.
_REF_RE = re.compile(r"\$\{([a-z0-9-]+__[A-Za-z0-9_.-]+)\}")


def _substitute_refs(obj: Any) -> Any:
    """Recursively substitute every ``${name__key}`` placeholder in *obj*.

    Works on strings, dicts, and lists. Returns a new object (deep-copy semantics
    provided by the caller via ``resolve_recipe_secrets``).
    """
    if isinstance(obj, str):
        def _replace(m: re.Match[str]) -> str:
            inner = m.group(1)
            if "__" not in inner:
                # Not a secret ref (no double-underscore) — leave unchanged.
                return m.group(0)
            return resolve_secret_ref(inner)
        return _REF_RE.sub(_replace, obj)
    elif isinstance(obj, dict):
        return {k: _substitute_refs(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_substitute_refs(item) for item in obj]
    return obj


def resolve_recipe_secrets(recipe: dict[str, Any]) -> dict[str, Any]:
    """Deep-copy *recipe* and substitute every ``${name__key}`` with its plaintext.

    The returned dict is a new object; the original recipe is never mutated.
    Plaintext values exist only in the returned in-memory dict.

    Raises:
        SecretRefMalformed / SecretRefNotFound / SecretResolverUnavailable:
            propagated from ``resolve_secret_ref`` for any failing reference.
    """
    import copy as _copy
    resolved = _copy.deepcopy(recipe)
    return _substitute_refs(resolved)  # type: ignore[return-value]


def verify_secret_ref(ref: str) -> None:
    """Confirm that the Secret and key referenced by ``name__key`` exist.

    Does NOT return the value. Used at source create/update time so the user
    gets immediate feedback before the source is persisted.

    Args:
        ref: The inner ``name__key`` string (the content inside ``${...}``).

    Raises:
        SecretRefMalformed: ``ref`` is malformed.
        SecretRefNotFound: Secret or key absent, or RBAC 403.
        SecretResolverUnavailable: In-cluster config not loadable, or k8s API error.
    """
    from kubernetes.client.exceptions import ApiException  # type: ignore[import-untyped]

    name, key = _parse_name_key(ref)
    full_name = _secret_name(name)

    core, namespace = _require_client()

    try:
        secret = core.read_namespaced_secret(name=full_name, namespace=namespace)
    except ApiException as exc:
        if exc.status in (403, 404):
            logger.warning(
                "Secret not found or access denied during verify",
                extra={"secret_name": full_name, "namespace": namespace, "status": exc.status},
            )
            raise SecretRefNotFound(
                f"Secret '{full_name}' not found or access denied (k8s status {exc.status})"
            ) from exc
        logger.warning(
            "Kubernetes API error during secret verify",
            extra={"secret_name": full_name, "namespace": namespace, "status": exc.status},
        )
        raise SecretResolverUnavailable(
            f"Kubernetes API returned status {exc.status} for secret '{full_name}'"
        ) from exc

    data: dict[str, str] | None = secret.data
    if data is None or key not in data:
        raise SecretRefNotFound(
            f"Key '{key}' not found in Secret '{namespace}/{full_name}'"
        )


def list_source_cred_refs() -> list[SecretRefInfo]:
    """List all available source-credential references in the own namespace.

    Enumerates Kubernetes Secrets whose name starts with ``dataspoke-source-cred-``,
    expands each Secret's data keys, and returns one ``SecretRefInfo`` per
    ``(secret, key)`` pair. Values are never read on this path.

    Requires the ``list`` verb on ``secrets`` in the API namespace.

    Returns:
        List of ``SecretRefInfo`` records (may be empty if no secrets exist).

    Raises:
        SecretResolverUnavailable: In-cluster config not loadable, or k8s API error.
    """
    from kubernetes.client.exceptions import ApiException  # type: ignore[import-untyped]

    core, namespace = _require_client()

    try:
        secret_list = core.list_namespaced_secret(namespace=namespace)
    except ApiException as exc:
        logger.warning(
            "Kubernetes API error during secret list",
            extra={"namespace": namespace, "status": exc.status},
        )
        raise SecretResolverUnavailable(
            f"Kubernetes API returned status {exc.status} when listing secrets"
        ) from exc

    refs: list[SecretRefInfo] = []
    for secret in secret_list.items:
        secret_name: str = secret.metadata.name
        if not secret_name.startswith(_NAME_PREFIX):
            continue
        # Derive the ``name`` segment by stripping the prefix.
        name_segment = secret_name[len(_NAME_PREFIX):]
        data: dict[str, str] | None = secret.data
        if not data:
            continue
        for key in sorted(data.keys()):
            refs.append(
                SecretRefInfo(
                    ref=f"{name_segment}__{key}",
                    secret_name=secret_name,
                    key=key,
                )
            )

    return refs
