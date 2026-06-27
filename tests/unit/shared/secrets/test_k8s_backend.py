"""Unit tests for src/shared/secrets/k8s.py — KubernetesSecretBackend + require_k8s_client.

Kubernetes is mocked via direct injection into k8s._client_state; no real cluster needed.
Fixtures reset _client_state before and after every test.

Spec: spec/feature/SECRET_RESOLUTION.md §Design, §Name prefix policy,
      §Error taxonomy, §Reference discovery
"""

from __future__ import annotations

import base64
from unittest.mock import MagicMock, patch

import pytest

import src.shared.secrets.k8s as _k8s
from src.shared.secrets.interface import (
    SecretRefNotFound,
    SecretResolverUnavailable,
)
from src.shared.secrets.k8s import (
    SOURCE_CRED_NAME_PREFIX,
    KubernetesSecretBackend,
    require_k8s_client,
)

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_secret(data: dict[str, str] | None) -> MagicMock:
    """Build a fake kubernetes V1Secret with base64-encoded data values."""
    secret = MagicMock()
    if data is None:
        secret.data = None
    else:
        secret.data = {k: base64.b64encode(v.encode()).decode() for k, v in data.items()}
    return secret


def _make_api_exception(status: int) -> Exception:
    from kubernetes.client.exceptions import ApiException  # type: ignore[import-untyped]

    exc = ApiException(status=status)
    exc.status = status
    return exc


def _make_secret_item(name: str, keys: list[str]) -> MagicMock:
    item = MagicMock()
    item.metadata = MagicMock()
    item.metadata.name = name
    item.data = {k: base64.b64encode(b"val").decode() for k in keys}
    return item


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_client_state() -> None:
    """Reset _client_state before and after every test in this module."""
    _k8s._client_state["client"] = None
    _k8s._client_state["available"] = None
    _k8s._client_state["own_namespace"] = None
    yield
    _k8s._client_state["client"] = None
    _k8s._client_state["available"] = None
    _k8s._client_state["own_namespace"] = None


def _inject_client(namespace: str = "dataspoke-01") -> MagicMock:
    """Directly inject a mock CoreV1Api, bypassing _init()."""
    fake_core = MagicMock()
    _k8s._client_state["available"] = True
    _k8s._client_state["client"] = fake_core
    _k8s._client_state["own_namespace"] = namespace
    return fake_core


# ── require_k8s_client ─────────────────────────────────────────────────────────


class TestRequireK8sClient:
    """Spec: SECRET_RESOLUTION.md §Design — in-cluster init + no-silent-fallback."""

    def test_returns_injected_client_and_namespace(self) -> None:
        """require_k8s_client returns (CoreV1Api, namespace) when already initialised."""
        core = _inject_client("dataspoke-test")
        client, ns = require_k8s_client()
        assert client is core
        assert ns == "dataspoke-test"

    def test_incluster_config_failure_raises_unavailable(self) -> None:
        """In-cluster config failure → SecretResolverUnavailable immediately.

        Spec: SECRET_RESOLUTION.md §Design — 'If in-cluster k8s init fails, every
        subsequent call raises SecretResolverUnavailable — no silent fallback.'
        """
        with (
            patch(
                "kubernetes.config.load_incluster_config",
                side_effect=Exception("no cluster"),
            ),
            patch("kubernetes.client.CoreV1Api"),
        ):
            with pytest.raises(SecretResolverUnavailable):
                require_k8s_client()

    def test_unavailable_state_persists_on_second_call(self) -> None:
        """Once available=False, every subsequent call raises without re-patching.

        Spec: SECRET_RESOLUTION.md §Design — once failed, _init is not retried.
        """
        with (
            patch(
                "kubernetes.config.load_incluster_config",
                side_effect=Exception("no cluster"),
            ),
            patch("kubernetes.client.CoreV1Api"),
        ):
            with pytest.raises(SecretResolverUnavailable):
                require_k8s_client()

        assert _k8s._client_state["available"] is False

        # Second call — no patching needed; must still raise.
        with pytest.raises(SecretResolverUnavailable):
            require_k8s_client()


# ── KubernetesSecretBackend.read_value ─────────────────────────────────────────


class TestReadValue:
    """Spec: SECRET_RESOLUTION.md §Run-time resolve flow."""

    def setup_method(self) -> None:
        self.core = _inject_client()
        self.backend = KubernetesSecretBackend()

    def test_returns_decoded_plaintext_value(self) -> None:
        """Reads the secret, base64-decodes, returns the plaintext.

        Spec: SECRET_RESOLUTION.md §Run-time resolve flow — 'base64-decoded'.
        """
        self.core.read_namespaced_secret.return_value = _make_secret({"password": "hunter2"})
        result = self.backend.read_value("team-pg", "password")
        assert result == "hunter2"

    def test_calls_correct_secret_name_and_namespace(self) -> None:
        """Constructs Secret name as 'dataspoke-source-cred-<name>' in own namespace.

        Spec: SECRET_RESOLUTION.md §Name prefix policy — the prefix is the
        security boundary.
        """
        self.core.read_namespaced_secret.return_value = _make_secret({"pw": "val"})
        self.backend.read_value("db", "pw")
        self.core.read_namespaced_secret.assert_called_once_with(
            name="dataspoke-source-cred-db", namespace="dataspoke-01"
        )

    def test_404_raises_secret_ref_not_found(self) -> None:
        """k8s 404 → SecretRefNotFound.

        Spec: SECRET_RESOLUTION.md §Error taxonomy — 'SecretRefNotFound: Secret
        or key absent'.
        """
        self.core.read_namespaced_secret.side_effect = _make_api_exception(404)
        with pytest.raises(SecretRefNotFound):
            self.backend.read_value("db", "pw")

    def test_403_raises_secret_ref_not_found(self) -> None:
        """RBAC k8s 403 → SecretRefNotFound (same as 404 for the caller).

        Spec: SECRET_RESOLUTION.md §Error taxonomy — 'RBAC Forbidden (403) →
        wrapped as SecretRefNotFound for read'.
        """
        self.core.read_namespaced_secret.side_effect = _make_api_exception(403)
        with pytest.raises(SecretRefNotFound):
            self.backend.read_value("db", "pw")

    def test_500_raises_resolver_unavailable(self) -> None:
        """k8s 5xx → SecretResolverUnavailable.

        Spec: SECRET_RESOLUTION.md §Error taxonomy — 'K8s API transient errors
        (5xx) → SecretResolverUnavailable'.
        """
        self.core.read_namespaced_secret.side_effect = _make_api_exception(500)
        with pytest.raises(SecretResolverUnavailable):
            self.backend.read_value("db", "pw")

    def test_missing_key_raises_not_found(self) -> None:
        """Secret exists but key absent → SecretRefNotFound.

        Spec: SECRET_RESOLUTION.md §Reference verify flow step 4 — 'data[key]
        missing → 422 SECRET_REF_NOT_FOUND'.
        """
        self.core.read_namespaced_secret.return_value = _make_secret({"other_key": "val"})
        with pytest.raises(SecretRefNotFound):
            self.backend.read_value("db", "pw")

    def test_none_data_raises_not_found(self) -> None:
        """Secret with data=None → SecretRefNotFound.

        Spec: SECRET_RESOLUTION.md §Run-time resolve flow.
        """
        self.core.read_namespaced_secret.return_value = _make_secret(None)
        with pytest.raises(SecretRefNotFound):
            self.backend.read_value("db", "pw")

    def test_non_utf8_value_raises_not_found_not_unicode_error(self) -> None:
        """Binary (non-UTF-8) secret value raises SecretRefNotFound, not UnicodeDecodeError.

        Spec: SECRET_RESOLUTION.md §Error taxonomy — surfaced as SecretRefNotFound
        with a value-free message (no plaintext leak).
        """
        raw_bytes = b"\xff\xfe"
        secret = MagicMock()
        secret.data = {"pw": base64.b64encode(raw_bytes).decode()}
        self.core.read_namespaced_secret.return_value = secret

        with pytest.raises(SecretRefNotFound):
            self.backend.read_value("db", "pw")

        # Confirm UnicodeDecodeError does not escape.
        try:
            self.backend.read_value("db", "pw")
        except SecretRefNotFound:
            pass
        except UnicodeDecodeError:
            pytest.fail(
                "UnicodeDecodeError must not propagate — must be wrapped as SecretRefNotFound"
            )

    def test_malformed_base64_value_raises_not_found_not_binascii_error(self) -> None:
        """Malformed base64 in secret data raises SecretRefNotFound, not binascii.Error.

        Spec: SECRET_RESOLUTION.md §Error taxonomy — surfaced as SecretRefNotFound
        with a value-free message (no plaintext leak).
        """
        import binascii

        secret = MagicMock()
        secret.data = {"pw": "not!valid!base64!"}
        self.core.read_namespaced_secret.return_value = secret

        with pytest.raises(SecretRefNotFound):
            self.backend.read_value("db", "pw")

        # Confirm binascii.Error does not escape.
        try:
            self.backend.read_value("db", "pw")
        except SecretRefNotFound:
            pass
        except binascii.Error:
            pytest.fail(
                "binascii.Error must not propagate — must be wrapped as SecretRefNotFound"
            )


# ── KubernetesSecretBackend.verify ─────────────────────────────────────────────


class TestVerify:
    """Spec: SECRET_RESOLUTION.md §Reference verify flow."""

    def setup_method(self) -> None:
        self.core = _inject_client()
        self.backend = KubernetesSecretBackend()

    def test_returns_none_when_secret_and_key_exist(self) -> None:
        """verify returns None (no exception) when Secret and key exist.

        Spec: SECRET_RESOLUTION.md §Reference verify flow — 'All references
        resolve → persist the source.'
        """
        self.core.read_namespaced_secret.return_value = _make_secret({"pw": "val"})
        result = self.backend.verify("team-pg", "pw")
        assert result is None

    def test_non_utf8_value_verifies_ok(self) -> None:
        """Non-UTF-8 value passes verify — verify never decodes the value.

        Verify only checks that data[key] exists (SECRET_RESOLUTION.md
        §Reference verify flow step 4); it never base64-decodes, so a non-UTF-8
        value passes save and only surfaces at run-time resolve. Impl-backed in
        src/shared/secrets/k8s_backend.py.
        """
        raw_bytes = b"\xff\xfe"
        secret = MagicMock()
        secret.data = {"pw": base64.b64encode(raw_bytes).decode()}
        self.core.read_namespaced_secret.return_value = secret
        # Must not raise — verify only checks existence, not UTF-8 validity.
        self.backend.verify("db", "pw")

    def test_404_raises_not_found(self) -> None:
        """k8s 404 → SecretRefNotFound.

        Spec: SECRET_RESOLUTION.md §Reference verify flow step 3.
        """
        self.core.read_namespaced_secret.side_effect = _make_api_exception(404)
        with pytest.raises(SecretRefNotFound):
            self.backend.verify("team-pg", "pw")

    def test_403_raises_not_found(self) -> None:
        """RBAC k8s 403 → SecretRefNotFound.

        Spec: SECRET_RESOLUTION.md §Error taxonomy.
        """
        self.core.read_namespaced_secret.side_effect = _make_api_exception(403)
        with pytest.raises(SecretRefNotFound):
            self.backend.verify("team-pg", "pw")

    def test_500_raises_unavailable(self) -> None:
        """k8s 5xx → SecretResolverUnavailable.

        Spec: SECRET_RESOLUTION.md §Error taxonomy.
        """
        self.core.read_namespaced_secret.side_effect = _make_api_exception(500)
        with pytest.raises(SecretResolverUnavailable):
            self.backend.verify("team-pg", "pw")

    def test_missing_key_raises_not_found(self) -> None:
        """Secret exists but key absent → SecretRefNotFound.

        Spec: SECRET_RESOLUTION.md §Reference verify flow step 4.
        """
        self.core.read_namespaced_secret.return_value = _make_secret({"other": "val"})
        with pytest.raises(SecretRefNotFound):
            self.backend.verify("team-pg", "pw")

    def test_none_data_raises_not_found(self) -> None:
        """Secret with data=None → SecretRefNotFound."""
        self.core.read_namespaced_secret.return_value = _make_secret(None)
        with pytest.raises(SecretRefNotFound):
            self.backend.verify("db", "pw")


# ── KubernetesSecretBackend.list_refs ──────────────────────────────────────────


class TestListRefs:
    """Spec: SECRET_RESOLUTION.md §Reference discovery (list flow)."""

    def setup_method(self) -> None:
        self.core = _inject_client()
        self.backend = KubernetesSecretBackend()

    def test_filters_to_source_cred_prefix_only(self) -> None:
        """Only 'dataspoke-source-cred-*' Secrets appear in the listing.

        Spec: SECRET_RESOLUTION.md §Reference discovery — 'enumerates Kubernetes
        Secrets whose name starts with dataspoke-source-cred-'.
        """
        self.core.list_namespaced_secret.return_value = MagicMock(
            items=[
                _make_secret_item("dataspoke-source-cred-team-pg", ["password", "ssl_key"]),
                _make_secret_item("dataspoke-secrets", ["jwt_key"]),  # infra secret — excluded
                _make_secret_item("dataspoke-llm-secret", ["api_key"]),  # excluded
            ]
        )
        refs = self.backend.list_refs()
        secret_names = {r.secret_name for r in refs}
        assert "dataspoke-source-cred-team-pg" in secret_names
        assert "dataspoke-secrets" not in secret_names, (
            "dataspoke-secrets (infra secret) must be filtered by prefix guard."
        )
        assert "dataspoke-llm-secret" not in secret_names, (
            "dataspoke-llm-secret (infra secret) must be filtered by prefix guard."
        )

    def test_ref_field_is_name_segment_double_underscore_key(self) -> None:
        """'ref' field is '<name_segment>__<key>' — paste as ${ref} in a recipe.

        Spec: SECRET_RESOLUTION.md §Reference discovery — 'ref is the literal
        string an author pastes into a recipe as ${...}'.
        """
        self.core.list_namespaced_secret.return_value = MagicMock(
            items=[_make_secret_item("dataspoke-source-cred-team-pg", ["password"])]
        )
        refs = self.backend.list_refs()
        assert len(refs) == 1
        ref = refs[0]
        assert ref.ref == "team-pg__password"
        assert ref.secret_name == "dataspoke-source-cred-team-pg"
        assert ref.key == "password"

    def test_multiple_keys_expand_to_multiple_rows(self) -> None:
        """One Secret with N keys → N SecretRefInfo rows.

        Spec: SECRET_RESOLUTION.md §Reference discovery — 'expands each Secret's
        data keys, and returns one row per (secret, key) pair'.
        """
        self.core.list_namespaced_secret.return_value = MagicMock(
            items=[
                _make_secret_item(
                    "dataspoke-source-cred-team-pg",
                    ["password", "ssl_cert", "ssl_key"],
                )
            ]
        )
        refs = self.backend.list_refs()
        assert len(refs) == 3
        keys_returned = {r.key for r in refs}
        assert keys_returned == {"password", "ssl_cert", "ssl_key"}

    def test_secret_with_none_data_skipped(self) -> None:
        """Secret with data=None contributes no rows.

        Spec: SECRET_RESOLUTION.md §Reference discovery — secrets with empty/None
        data are skipped.
        """
        item = MagicMock()
        item.metadata = MagicMock()
        item.metadata.name = "dataspoke-source-cred-empty"
        item.data = None
        self.core.list_namespaced_secret.return_value = MagicMock(items=[item])
        refs = self.backend.list_refs()
        assert refs == []

    def test_secret_with_empty_data_skipped(self) -> None:
        """Secret with data={} contributes no rows."""
        item = MagicMock()
        item.metadata = MagicMock()
        item.metadata.name = "dataspoke-source-cred-empty"
        item.data = {}
        self.core.list_namespaced_secret.return_value = MagicMock(items=[item])
        refs = self.backend.list_refs()
        assert refs == []

    def test_empty_namespace_returns_empty_list(self) -> None:
        """No Secrets at all → empty list (not an error).

        Spec: SECRET_RESOLUTION.md §Reference discovery.
        """
        self.core.list_namespaced_secret.return_value = MagicMock(items=[])
        refs = self.backend.list_refs()
        assert refs == []

    def test_5xx_raises_resolver_unavailable(self) -> None:
        """k8s 5xx on list → SecretResolverUnavailable.

        Spec: SECRET_RESOLUTION.md §Error taxonomy.
        """
        self.core.list_namespaced_secret.side_effect = _make_api_exception(500)
        with pytest.raises(SecretResolverUnavailable):
            self.backend.list_refs()

    def test_values_are_never_returned(self) -> None:
        """SecretRefInfo has no 'value' field — values are never exposed.

        Spec: SECRET_RESOLUTION.md §Reference discovery — 'Values are never returned'.
        """
        self.core.list_namespaced_secret.return_value = MagicMock(
            items=[_make_secret_item("dataspoke-source-cred-x", ["pw"])]
        )
        refs = self.backend.list_refs()
        assert len(refs) == 1
        from src.shared.secrets.interface import SecretRefInfo

        assert isinstance(refs[0], SecretRefInfo)
        assert not hasattr(refs[0], "value")


# ── SOURCE_CRED_NAME_PREFIX ────────────────────────────────────────────────────


class TestSourceCredNamePrefix:
    """Spec: SECRET_RESOLUTION.md §Name prefix policy — security boundary."""

    def test_prefix_constant_is_correct(self) -> None:
        """SOURCE_CRED_NAME_PREFIX is 'dataspoke-source-cred-'.

        Spec: SECRET_RESOLUTION.md §Name prefix policy — 'resolves to Kubernetes
        Secret dataspoke-source-cred-<name>'.
        """
        assert SOURCE_CRED_NAME_PREFIX == "dataspoke-source-cred-"

    def test_backend_constructs_prefixed_secret_name(self) -> None:
        """KubernetesSecretBackend._secret_name prepends the prefix to the name segment."""
        backend = KubernetesSecretBackend()
        assert backend._secret_name("team-pg") == "dataspoke-source-cred-team-pg"
        assert backend._secret_name("kafka") == "dataspoke-source-cred-kafka"
