"""Backend-neutral secret resolution: cache, substitution, public functions.

Holds the ``${name__key}`` substitution, the TTL+LRU cache keyed on logical
``(name, key)``, the backend binding, and the four public functions. This layer
is independent of any concrete store and never imports ``kubernetes``; the
default binding is the lazily-instantiated Kubernetes backend, swappable via
``set_backend()``.

Spec: spec/feature/SECRET_RESOLUTION.md
"""

from __future__ import annotations

import copy
import re
import threading
import time
from typing import Any

from src.shared.secrets.grammar import SECRET_REF_RE, parse_name_key
from src.shared.secrets.interface import (
    SecretBackend,
    SecretRefInfo,
)

_TTL_SECONDS = 60.0
_CACHE_MAX_SIZE = 512

# Bounded in-memory cache: (name, key) -> (plaintext_value, expiry_monotonic).
# Manual LRU+TTL — insertion-order eviction when at capacity.
_cache: dict[tuple[str, str], tuple[str, float]] = {}
_cache_order: list[tuple[str, str]] = []

# Backend binding: module-level lazy default + set_backend() override.
_backend_lock = threading.Lock()
_backend: SecretBackend | None = None


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


def _clear_cache() -> None:
    """Drop every cache entry (used when the backend binding changes)."""
    _cache.clear()
    _cache_order.clear()


# ── Backend binding ───────────────────────────────────────────────────────────


def get_backend() -> SecretBackend:
    """Return the active secret backend, instantiating the default on first use.

    The default is the Kubernetes backend; ``kubernetes`` is imported only when
    that backend is constructed (inside ``src.shared.secrets.k8s``).
    """
    global _backend
    if _backend is not None:
        return _backend
    with _backend_lock:
        if _backend is None:
            from src.shared.secrets.k8s import KubernetesSecretBackend

            _backend = KubernetesSecretBackend()
        return _backend


def set_backend(backend: SecretBackend | None) -> None:
    """Bind *backend* as the active secret backend and clear the cache.

    Passing ``None`` restores the lazily-instantiated Kubernetes default.
    """
    global _backend
    with _backend_lock:
        _backend = backend
        _clear_cache()


# ── Public surface ────────────────────────────────────────────────────────────


def resolve_secret_ref(ref: str) -> str:
    """Resolve a ``name__key`` token to its plaintext value.

    Parses ``ref`` into ``(name, key)``, returns a cached value within
    ``_TTL_SECONDS``, or reads it from the active backend and caches it.

    Args:
        ref: The inner ``name__key`` string (the content inside ``${...}``).

    Returns:
        Plaintext credential value.

    Raises:
        SecretRefMalformed: ``ref`` has no ``__``, or an empty name/key segment.
        SecretRefNotFound: Secret or key absent, or RBAC 403.
        SecretResolverUnavailable: In-cluster config not loadable, or k8s API error.
    """
    name, key = parse_name_key(ref)
    cache_key = (name, key)

    cached = _cache.get(cache_key)
    if cached is not None:
        value, expiry = cached
        if time.monotonic() < expiry:
            return value
        del _cache[cache_key]

    decoded = get_backend().read_value(name, key)
    _cache_put(cache_key, decoded, time.monotonic() + _TTL_SECONDS)
    return decoded


def _substitute_refs(obj: Any) -> Any:
    """Recursively substitute every ``${name__key}`` placeholder in *obj*.

    Works on strings, dicts, and lists. Returns a new object (deep-copy semantics
    provided by the caller via ``resolve_recipe_secrets``).
    """
    if isinstance(obj, str):
        def _replace(m: re.Match[str]) -> str:
            return resolve_secret_ref(f"{m.group(1)}__{m.group(2)}")
        return SECRET_REF_RE.sub(_replace, obj)
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
    resolved = copy.deepcopy(recipe)
    return _substitute_refs(resolved)  # type: ignore[return-value]


def verify_secret_ref(ref: str) -> None:
    """Confirm that the Secret and key referenced by ``name__key`` exist.

    Does NOT return the value and never consults the cache. Used at source
    create/update time so the user gets immediate feedback before the source is
    persisted.

    Args:
        ref: The inner ``name__key`` string (the content inside ``${...}``).

    Raises:
        SecretRefMalformed: ``ref`` is malformed.
        SecretRefNotFound: Secret or key absent, or RBAC 403.
        SecretResolverUnavailable: In-cluster config not loadable, or k8s API error.
    """
    name, key = parse_name_key(ref)
    get_backend().verify(name, key)


def list_source_cred_refs() -> list[SecretRefInfo]:
    """List all available source-credential references via the active backend.

    Returns one ``SecretRefInfo`` per available ``(secret, key)`` pair. Values
    are never read on this path.

    Returns:
        List of ``SecretRefInfo`` records (may be empty if none exist).

    Raises:
        SecretResolverUnavailable: In-cluster config not loadable, or k8s API error.
    """
    return get_backend().list_refs()
