"""Kubernetes Secrets backend and in-cluster client bootstrap.

Reference-only model: DataSpoke never writes source-credential values. An
operator pre-creates Kubernetes Secrets out-of-band; this backend only lists,
verifies, and resolves them at run time.

``${name__key}`` resolves to Kubernetes Secret ``dataspoke-source-cred-<name>``,
data key ``<key>``. The ``dataspoke-source-cred-`` prefix is the security
boundary: it keeps a recipe reference from reaching DataSpoke's own infra
Secrets (``dataspoke-secrets``, ``dataspoke-llm-secret``, …). Calls are
synchronous — k8s Python client calls are blocking but fast.

``require_k8s_client()`` exposes the in-cluster ``(CoreV1Api, namespace)`` pair
to infra-secret accessors (DataHub token, LLM key, …) that target fixed Secret
names under their own controls and bypass the prefix guard.

Spec: spec/feature/SECRET_RESOLUTION.md
"""

from __future__ import annotations

import base64
import binascii
import logging
import threading
from typing import Any

from src.shared.secrets.interface import (
    SecretRefInfo,
    SecretRefNotFound,
    SecretResolverUnavailable,
)

logger = logging.getLogger(__name__)

_NAMESPACE_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"

# All source-credential Secret names must start with this prefix.
# This is a security boundary: it prevents recipe authors from reading
# DataSpoke's own infra Secrets (dataspoke-secrets, dataspoke-llm-secret, …)
# via a recipe reference.
SOURCE_CRED_NAME_PREFIX = "dataspoke-source-cred-"

_init_lock = threading.Lock()

_client_state: dict[str, Any] = {
    "client": None,
    "available": None,  # None=not tried, True=ok, False=failed
    "own_namespace": None,
}


# ── Initialisation ────────────────────────────────────────────────────────────


def _init() -> None:
    from kubernetes import client, config  # type: ignore[import-untyped]

    with _init_lock:
        if _client_state["available"] is not None:
            return  # double-checked locking

        try:
            config.load_incluster_config()
        except Exception as exc:
            _client_state["available"] = False
            _client_state["client"] = None
            raise SecretResolverUnavailable(
                f"In-cluster Kubernetes config not available: {exc}"
            ) from exc

        try:
            with open(_NAMESPACE_FILE) as fh:
                _client_state["own_namespace"] = fh.read().strip()
        except OSError as exc:
            _client_state["available"] = False
            _client_state["client"] = None
            raise SecretResolverUnavailable(
                f"Cannot read pod namespace from {_NAMESPACE_FILE}: {exc}"
            ) from exc

        # Set client before marking available so racing threads always see a
        # fully-initialised client.
        _client_state["client"] = client.CoreV1Api()
        _client_state["available"] = True


def require_k8s_client() -> tuple[Any, str]:
    """Return ``(CoreV1Api, own_namespace)``, initialising on first call.

    Raises:
        SecretResolverUnavailable: if in-cluster config failed to load.
    """
    if _client_state["available"] is False:
        raise SecretResolverUnavailable(
            "Kubernetes in-cluster config failed to load; "
            "run the API in-cluster to use secret resolution."
        )
    if _client_state["available"] is None:
        _init()
    return _client_state["client"], _client_state["own_namespace"]


# ── Kubernetes Secrets backend ────────────────────────────────────────────────


class KubernetesSecretBackend:
    """Resolve logical ``(name, key)`` against ``dataspoke-source-cred-*`` Secrets.

    Maps ``name`` to the Kubernetes object ``dataspoke-source-cred-<name>`` and
    reads data key ``<key>``. The ``dataspoke-source-cred-`` prefix is the
    per-backend obligation from the ``SecretBackend`` protocol: a recipe
    reference can never reach an infra Secret.
    """

    def _secret_name(self, name: str) -> str:
        """Return the full k8s Secret name for a parsed ``name`` segment."""
        return f"{SOURCE_CRED_NAME_PREFIX}{name}"

    def read_value(self, name: str, key: str) -> str:
        """Read ``dataspoke-source-cred-<name>`` data key ``<key>``, base64-decoded.

        Raises:
            SecretRefNotFound: Secret or key absent, RBAC 403, or non-UTF-8 value.
            SecretResolverUnavailable: In-cluster config not loadable, or k8s API error.
        """
        from kubernetes.client.exceptions import ApiException  # type: ignore[import-untyped]

        full_name = self._secret_name(name)
        core, namespace = require_k8s_client()

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
            return base64.b64decode(data[key]).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            raise SecretRefNotFound(
                f"Secret '{full_name}' key '{key}' value is not valid UTF-8"
            )

    def verify(self, name: str, key: str) -> None:
        """Confirm Secret ``dataspoke-source-cred-<name>`` holds data key ``<key>``.

        Does NOT return or decode the value, and never consults a cache.

        Raises:
            SecretRefNotFound: Secret or key absent, or RBAC 403.
            SecretResolverUnavailable: In-cluster config not loadable, or k8s API error.
        """
        from kubernetes.client.exceptions import ApiException  # type: ignore[import-untyped]

        full_name = self._secret_name(name)
        core, namespace = require_k8s_client()

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

    def list_refs(self) -> list[SecretRefInfo]:
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

        core, namespace = require_k8s_client()

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
            if not secret_name.startswith(SOURCE_CRED_NAME_PREFIX):
                continue
            # Derive the ``name`` segment by stripping the prefix.
            name_segment = secret_name[len(SOURCE_CRED_NAME_PREFIX):]
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
