"""Backend-neutral secret-resolution contract: exceptions, ref metadata, protocol.

Defines the error taxonomy, the ``SecretRefInfo`` record, and the
``SecretBackend`` protocol that every concrete secret store implements. This
module is stdlib-only — it carries no backend wiring and never imports
``kubernetes``.

Spec: spec/feature/SECRET_RESOLUTION.md
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

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


# ── Backend protocol ──────────────────────────────────────────────────────────


class SecretBackend(Protocol):
    """Structural contract a secret store implements to back resolution.

    A backend maps a logical ``(name, key)`` pair to a credential value. The
    backend-neutral resolver layer (grammar, ``${...}`` substitution, cache,
    error taxonomy, public functions) is independent of any backend; an
    extension implements these three methods and binds via
    ``src.shared.secrets.set_backend()``.

    Per-backend obligations:

    - ``read_value`` / ``verify`` / ``list_refs`` must map ``name`` and ``key``
      to the store's own namespace so that a recipe reference can never reach a
      credential outside the source-credential boundary (the Kubernetes backend
      enforces this with the ``dataspoke-source-cred-`` object-name prefix).
    - ``read_value`` returns the decoded plaintext or raises ``SecretRefNotFound``
      (missing secret/key, RBAC denial) / ``SecretResolverUnavailable`` (store
      unreachable). It never returns a sentinel for absence.
    - ``verify`` confirms existence without returning or decoding the value and
      without consulting any cache, so a secret deleted after a successful read
      still fails verification. It raises the same exceptions as ``read_value``
      for absence/unavailability.
    - ``list_refs`` enumerates the available references as ``SecretRefInfo``
      records without reading values, raising ``SecretResolverUnavailable`` when
      the store is unreachable.
    """

    def read_value(self, name: str, key: str) -> str:
        """Return the decoded plaintext for logical ``(name, key)``."""
        ...

    def verify(self, name: str, key: str) -> None:
        """Confirm ``(name, key)`` exists without returning or caching the value."""
        ...

    def list_refs(self) -> list[SecretRefInfo]:
        """Enumerate available references as ``SecretRefInfo`` records (no values)."""
        ...
