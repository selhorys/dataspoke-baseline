"""Kubernetes helpers for integration test setup.

Out-of-band K8s Secret provisioning for tests that need source-credential Secrets
pre-created before the test body exercises the API. This is the test-setup analogue
of the operator authoring guide in spec/feature/SECRET_RESOLUTION.md §Admin authoring
guide — the test provisions the secret the same way an admin would, via the K8s API,
not via any DataSpoke endpoint (reference-only model preserved).

spec: spec/feature/SECRET_RESOLUTION.md §Reference-only model
spec: spec/TESTING.md §Integration Testing — setup in fixtures, not test body
"""

from __future__ import annotations

import base64
import logging
import os

logger = logging.getLogger(__name__)

# Default namespace — resolved at call time from env/context, defaulting to this.
_DEFAULT_NAMESPACE = "dataspoke-01"

_SOURCE_CRED_NAME_PREFIX = "dataspoke-source-cred-"


def _resolve_namespace() -> str:
    """Return DataSpoke's own namespace.

    Resolution order (the test-context convention; the runtime resolver in
    src/shared/secrets/k8s.py reads only the in-cluster namespace file):
    1. DATASPOKE_KUBE_DATASPOKE_NAMESPACE env var (set by helm-charts/.env; also used by
       tests/integration/spot/test_admin_peripherals.py)
    2. In-cluster namespace file at /var/run/secrets/kubernetes.io/serviceaccount/namespace
    3. Falls back to "dataspoke-01"

    spec: spec/feature/SECRET_RESOLUTION.md §Design — in-cluster init reads namespace file
    """
    ns = os.environ.get("DATASPOKE_KUBE_DATASPOKE_NAMESPACE", "")
    if ns:
        return ns
    ns_file = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
    try:
        with open(ns_file) as fh:
            ns = fh.read().strip()
        if ns:
            return ns
    except OSError:
        pass
    return _DEFAULT_NAMESPACE


def _build_k8s_client():
    """Bootstrap a kubernetes CoreV1Api client.

    Tries in-cluster config first; falls back to local kubeconfig for
    developer workstations. Mirrors the test-setup-facing init pattern used
    in tests/integration/conftest.py (datahub_actions_pod_required).

    spec: spec/TESTING.md §Integration Testing — tests provision secrets out-of-band
    """
    from kubernetes import client, config  # type: ignore[import-untyped]

    try:
        config.load_incluster_config()
        logger.debug("k8s: using in-cluster config")
    except Exception:
        config.load_kube_config()
        logger.debug("k8s: using local kubeconfig")

    return client.CoreV1Api()


def ensure_source_cred_secret(name: str, key: str, value: str) -> None:
    """Create K8s Secret dataspoke-source-cred-<name> with data {<key>: <value>} if absent.

    Idempotent: if the Secret already exists (HTTP 409 Conflict), the call
    succeeds silently without overwriting the existing content. Never updates
    an existing Secret — out-of-band ownership is intentional.

    This is the test-setup equivalent of the kubectl recipe in
    spec/feature/SECRET_RESOLUTION.md §Admin authoring guide:

        kubectl create secret generic dataspoke-source-cred-<name>
          --from-literal=<key>=<value>
          -n dataspoke-01

    Only creates; never reads the value back (reference-only model).

    Args:
        name: The DNS-label-safe name segment (e.g. "dummy-data-pg").
              The Secret is named dataspoke-source-cred-<name>.
        key:  The data key within the Secret (e.g. "password").
        value: The plaintext value to store (base64-encoded by this function
               before writing — k8s Secret data is always base64).

    Raises:
        RuntimeError: If the k8s client cannot be initialised (neither in-cluster
                      config nor a local kubeconfig is available).
        kubernetes.client.exceptions.ApiException: For non-409 k8s API errors.

    spec: spec/feature/SECRET_RESOLUTION.md §Reference-only model — no DataSpoke write API
    spec: spec/TESTING.md §Integration Testing — secret-mutating setup in fixtures
    """
    from kubernetes.client import V1ObjectMeta, V1Secret  # type: ignore[import-untyped]
    from kubernetes.client.exceptions import ApiException  # type: ignore[import-untyped]

    namespace = _resolve_namespace()
    secret_name = f"{_SOURCE_CRED_NAME_PREFIX}{name}"
    encoded_value = base64.b64encode(value.encode("utf-8")).decode("ascii")

    core = _build_k8s_client()

    secret = V1Secret(
        metadata=V1ObjectMeta(
            name=secret_name,
            namespace=namespace,
            labels={"app.kubernetes.io/managed-by": "dataspoke-test"},
        ),
        data={key: encoded_value},
    )

    try:
        core.create_namespaced_secret(namespace=namespace, body=secret)
        logger.info(
            "Created K8s Secret %s/%s (key=%r)",
            namespace,
            secret_name,
            key,
        )
    except ApiException as exc:
        if exc.status == 409:
            # Already exists — idempotent success; never overwrite.
            logger.info(
                "K8s Secret %s/%s already exists; skipping create (idempotent).",
                namespace,
                secret_name,
            )
        else:
            raise
