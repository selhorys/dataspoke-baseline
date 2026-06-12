"""Backend-neutral secret resolution with a Kubernetes Secrets default backend.

Resolves DataHub-compatible ``${name__key}`` references to credential values.
The neutral layer (grammar, ``${...}`` substitution, cache, error taxonomy,
public functions) is independent of any concrete store; an extension implements
the ``SecretBackend`` protocol and binds it via ``set_backend()``.

Spec: spec/feature/SECRET_RESOLUTION.md
"""

from __future__ import annotations

from src.shared.secrets.interface import (
    SecretBackend,
    SecretRefInfo,
    SecretRefMalformed,
    SecretRefNotFound,
    SecretResolverUnavailable,
)
from src.shared.secrets.k8s import KubernetesSecretBackend
from src.shared.secrets.resolver import (
    get_backend,
    list_source_cred_refs,
    resolve_recipe_secrets,
    resolve_secret_ref,
    set_backend,
    verify_secret_ref,
)

__all__ = [
    "KubernetesSecretBackend",
    "SecretBackend",
    "SecretRefInfo",
    "SecretRefMalformed",
    "SecretRefNotFound",
    "SecretResolverUnavailable",
    "get_backend",
    "list_source_cred_refs",
    "resolve_recipe_secrets",
    "resolve_secret_ref",
    "set_backend",
    "verify_secret_ref",
]
