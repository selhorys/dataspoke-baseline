"""Unit tests for src.backend.ingestion.secret_resolver.

Covers: parser, cache, host-mode unavailability, resolver error mapping,
writer, and verifier — all with a mocked kubernetes client.

spec: spec/feature/SECRET_RESOLUTION.md
"""

from __future__ import annotations

import base64
import time
from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest

import src.backend.ingestion.secret_resolver as _sr_module
from src.backend.ingestion.secret_resolver import (
    SecretCollision,
    SecretRefMalformed,
    SecretRefNameForbidden,
    SecretRefNotFound,
    SecretResolverUnavailable,
    resolve_secret_ref,
    verify_secret_ref,
    write_secret_value,
)

# ── helpers ───────────────────────────────────────────────────────────────────


def _make_secret(data: dict[str, str] | None) -> MagicMock:
    """Build a fake kubernetes V1Secret with base64-encoded data."""
    secret = MagicMock()
    if data is None:
        secret.data = None
    else:
        secret.data = {k: base64.b64encode(v.encode()).decode() for k, v in data.items()}
    return secret


def _make_api_exception(status: int) -> Exception:
    """Build a fake kubernetes ApiException with the given HTTP status."""
    from kubernetes.client.exceptions import ApiException

    exc = ApiException(status=status)
    exc.status = status
    return exc  # type: ignore[no-any-return]


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=False)
def mock_k8s_client() -> Generator[MagicMock]:
    """Patch kubernetes init so tests never touch real cluster state.

    Patches at the module's import boundary:
    - kubernetes.config.load_incluster_config — no-op (success)
    - kubernetes.client.CoreV1Api — returns a MagicMock

    Yields the MagicMock CoreV1Api instance.
    Resolver state is reset before and after via public-boundary patches only.
    """
    fake_core = MagicMock()
    namespace_file_content = "dataspoke-01"

    with (
        patch("kubernetes.config.load_incluster_config"),
        patch("kubernetes.client.CoreV1Api", return_value=fake_core),
        patch("builtins.open", mock_open_namespace(namespace_file_content)),
    ):
        # Reset resolver state so _init() runs fresh via the patched imports.
        with _sr_module._init_lock:
            _sr_module._resolver_state["client"] = None
            _sr_module._resolver_state["available"] = None
            _sr_module._resolver_state["own_namespace"] = None
        _sr_module._cache.clear()
        _sr_module._cache_order.clear()

        yield fake_core

    # Teardown: leave state clean for subsequent tests.
    with _sr_module._init_lock:
        _sr_module._resolver_state["client"] = None
        _sr_module._resolver_state["available"] = None
        _sr_module._resolver_state["own_namespace"] = None
    _sr_module._cache.clear()
    _sr_module._cache_order.clear()


def mock_open_namespace(content: str) -> MagicMock:
    """Return a context-manager mock for builtins.open that yields content."""
    from unittest.mock import mock_open

    return mock_open(read_data=content)  # type: ignore[no-any-return]


def _reset_resolver_state() -> None:
    """Reset global resolver state for tests that manage init themselves."""
    with _sr_module._init_lock:
        _sr_module._resolver_state["client"] = None
        _sr_module._resolver_state["available"] = None
        _sr_module._resolver_state["own_namespace"] = None
    _sr_module._cache.clear()
    _sr_module._cache_order.clear()


def _inject_mock_client(namespace: str = "ns1") -> MagicMock:
    """Inject a MagicMock CoreV1Api via patches at the resolver module boundary.

    Patches kubernetes.config.load_incluster_config and kubernetes.client.CoreV1Api
    so that _init() — called via _require_client() — sees the mock client.
    Returns the mock client.
    """
    fake_core = MagicMock()
    with _sr_module._init_lock:
        _sr_module._resolver_state["available"] = True
        _sr_module._resolver_state["client"] = fake_core
        _sr_module._resolver_state["own_namespace"] = namespace
    return fake_core


# ── Parser tests ──────────────────────────────────────────────────────────────
# spec: SECRET_RESOLUTION.md §Interfaces — resolve_secret_ref accepts only
# the k8s-secret/<name>/<key> form (3 segments, own-ns implicit).


class TestParseRef:
    """Parser: _parse_ref is exercised indirectly via resolve_secret_ref."""

    def setup_method(self) -> None:
        _reset_resolver_state()
        _inject_mock_client(namespace="dataspoke-01")

    def test_valid_ref_calls_k8s_with_correct_name_and_key(self) -> None:
        # spec: SECRET_RESOLUTION.md §Run-time read flow
        # k8s-secret/<name>/<key> — 2 segments after prefix — must call client
        # with name=dataspoke-conf-foo, key=password.
        _sr_module._resolver_state["client"].read_namespaced_secret.return_value = (
            _make_secret({"password": "hunter2"})
        )
        result = resolve_secret_ref("k8s-secret/dataspoke-conf-foo/password")
        _sr_module._resolver_state["client"].read_namespaced_secret.assert_called_once_with(
            name="dataspoke-conf-foo", namespace="dataspoke-01"
        )
        assert result == "hunter2"

    def test_four_segment_cross_namespace_raises_malformed(self) -> None:
        # spec: SECRET_RESOLUTION.md §Interfaces — "The 4-segment cross-namespace form
        # is rejected as SecretRefMalformed."
        with pytest.raises(SecretRefMalformed):
            resolve_secret_ref("k8s-secret/some-ns/dataspoke-conf-foo/password")

    def test_missing_key_segment_raises_malformed(self) -> None:
        # spec: SECRET_RESOLUTION.md §Interfaces — must have exactly 2 segments after prefix.
        with pytest.raises(SecretRefMalformed):
            resolve_secret_ref("k8s-secret/dataspoke-conf-foo")

    def test_wrong_prefix_raises_malformed(self) -> None:
        # spec: SECRET_RESOLUTION.md §Interfaces — ref must start with 'k8s-secret/'.
        with pytest.raises(SecretRefMalformed):
            resolve_secret_ref("vault-secret/dataspoke-conf-foo/password")

    def test_empty_name_segment_raises_malformed(self) -> None:
        # spec: SECRET_RESOLUTION.md §Error taxonomy — SecretRefMalformed on empty segments.
        with pytest.raises(SecretRefMalformed):
            resolve_secret_ref("k8s-secret//password")

    def test_name_without_prefix_raises_name_forbidden(self) -> None:
        # spec: SECRET_RESOLUTION.md §Name prefix policy — names not matching prefix
        # are rejected with SecretRefNameForbidden.
        with pytest.raises(SecretRefNameForbidden):
            resolve_secret_ref("k8s-secret/team-pg-prod/password")


# ── Cache tests ───────────────────────────────────────────────────────────────
# spec: SECRET_RESOLUTION.md §Cache — in-memory cache, 60s TTL, keyed on (name, key).


class TestCache:
    def setup_method(self) -> None:
        _reset_resolver_state()
        fake_core = _inject_mock_client()
        fake_core.read_namespaced_secret.return_value = _make_secret({"pw": "secret123"})

    def test_two_calls_within_ttl_hit_k8s_only_once(self) -> None:
        # spec: SECRET_RESOLUTION.md §Cache — "Bounds the k8s API call rate when a burst
        # of dry-runs hits the same Secret."
        resolve_secret_ref("k8s-secret/dataspoke-conf-db/pw")
        resolve_secret_ref("k8s-secret/dataspoke-conf-db/pw")
        core = _sr_module._resolver_state["client"]
        assert core.read_namespaced_secret.call_count == 1

    def test_cache_miss_after_ttl_expiry_hits_k8s_again(self) -> None:
        # spec: SECRET_RESOLUTION.md §Cache — TTL is short enough that secret rotations
        # propagate within a minute. Simulate expiry by directly manipulating cache entries.
        resolve_secret_ref("k8s-secret/dataspoke-conf-db/pw")
        core = _sr_module._resolver_state["client"]
        assert core.read_namespaced_secret.call_count == 1

        # Forcibly expire the cache entry (set expiry in the past).
        key = ("dataspoke-conf-db", "pw")
        value, _ = _sr_module._cache[key]
        _sr_module._cache[key] = (value, time.monotonic() - 1.0)

        resolve_secret_ref("k8s-secret/dataspoke-conf-db/pw")
        assert core.read_namespaced_secret.call_count == 2

    def test_write_invalidates_cache_and_next_resolve_returns_new_value(self) -> None:
        # spec: SECRET_RESOLUTION.md §Interfaces — write_secret_value "invalidates the
        # cache entry for (name, key) on success." The operator-observable invariant is:
        # after a write, a subsequent resolve returns the new value within the TTL window.
        core = _sr_module._resolver_state["client"]
        core.read_namespaced_secret.return_value = _make_secret({"pw": "original"})
        resolve_secret_ref("k8s-secret/dataspoke-conf-db/pw")
        first_call_count = core.read_namespaced_secret.call_count

        # write_secret_value collision check sees 404 (no existing secret), then creates.
        not_found = _make_api_exception(404)
        core.read_namespaced_secret.side_effect = [
            not_found,  # write_secret_value collision check -> 404 -> create
            _make_secret({"pw": "rotated"}),  # subsequent resolve
        ]
        core.create_namespaced_secret.return_value = MagicMock()

        write_secret_value("dataspoke-conf-db", "pw", "rotated", force_overwrite=False)

        # Next resolve must return the new value, not the stale cached "original".
        core.read_namespaced_secret.side_effect = None
        core.read_namespaced_secret.return_value = _make_secret({"pw": "rotated"})
        result = resolve_secret_ref("k8s-secret/dataspoke-conf-db/pw")
        assert result == "rotated"
        assert core.read_namespaced_secret.call_count > first_call_count

    def test_cache_is_bounded_after_many_distinct_keys(self) -> None:
        # spec: SECRET_RESOLUTION.md §Cache — "in-memory cache … bounded." The cache
        # must not grow without limit when many distinct (name, key) pairs are resolved.
        # Drive N=1000 distinct pairs through resolve_secret_ref (well above any
        # reasonable cap). Assert the cache size stays strictly below N.
        n = 1000
        core = _sr_module._resolver_state["client"]

        def make_secret_for(name: str, key: str) -> MagicMock:
            return _make_secret({key: f"val-{name}-{key}"})

        for i in range(n):
            name = f"dataspoke-conf-src-{i}"
            key = "pw"
            core.read_namespaced_secret.return_value = make_secret_for(name, key)
            resolve_secret_ref(f"k8s-secret/{name}/{key}")

        assert len(_sr_module._cache) < n, (
            f"Cache grew to {len(_sr_module._cache)} entries for {n} distinct refs; "
            "the cache must be bounded."
        )


# ── Host-mode unavailability tests ────────────────────────────────────────────
# spec: SECRET_RESOLUTION.md §Host-mode behavior — in-cluster k8s config not available
# → every subsequent call raises SecretResolverUnavailable.

_INCLUSTER_TARGET = "kubernetes.config.load_incluster_config"
_NO_KUBECONFIG = Exception("no kubeconfig")


class TestHostMode:
    def setup_method(self) -> None:
        _reset_resolver_state()

    def teardown_method(self) -> None:
        _reset_resolver_state()

    def test_incluster_config_failure_sets_unavailable(self) -> None:
        # spec: SECRET_RESOLUTION.md §Design — "If that fails (host-mode dev), every
        # subsequent call raises SecretResolverUnavailable — no silent fallback."
        with patch(_INCLUSTER_TARGET, side_effect=_NO_KUBECONFIG):
            with pytest.raises(SecretResolverUnavailable):
                resolve_secret_ref("k8s-secret/dataspoke-conf-foo/pw")

    def test_subsequent_calls_raise_unavailable_after_failed_init(self) -> None:
        # spec: SECRET_RESOLUTION.md §Design — once available=False, no retry of _init.
        with patch(_INCLUSTER_TARGET, side_effect=_NO_KUBECONFIG):
            with pytest.raises(SecretResolverUnavailable):
                resolve_secret_ref("k8s-secret/dataspoke-conf-foo/pw")

        assert _sr_module._resolver_state["available"] is False
        with pytest.raises(SecretResolverUnavailable):
            resolve_secret_ref("k8s-secret/dataspoke-conf-foo/pw")


# ── Resolver error-mapping tests ──────────────────────────────────────────────
# spec: SECRET_RESOLUTION.md §Error taxonomy


class TestResolverErrorMapping:
    def setup_method(self) -> None:
        _reset_resolver_state()
        self._fake_core = _inject_mock_client()

    def test_secret_with_key_returns_decoded_value(self) -> None:
        # spec: SECRET_RESOLUTION.md §Run-time read flow — cache + return data[key] (base64-decoded)
        self._fake_core.read_namespaced_secret.return_value = _make_secret({"pw": "my-password"})
        result = resolve_secret_ref("k8s-secret/dataspoke-conf-x/pw")
        assert result == "my-password"

    def test_secret_without_key_raises_not_found(self) -> None:
        # spec: SECRET_RESOLUTION.md §Error taxonomy — SecretRefNotFound when key absent.
        self._fake_core.read_namespaced_secret.return_value = _make_secret({"other_key": "val"})
        with pytest.raises(SecretRefNotFound):
            resolve_secret_ref("k8s-secret/dataspoke-conf-x/pw")

    def test_secret_with_none_data_raises_not_found(self) -> None:
        # spec: SECRET_RESOLUTION.md §Run-time read flow — secret.data is None means
        # the key cannot be present; SecretRefNotFound must be raised.
        self._fake_core.read_namespaced_secret.return_value = _make_secret(None)
        with pytest.raises(SecretRefNotFound):
            resolve_secret_ref("k8s-secret/dataspoke-conf-x/pw")

    def test_404_api_exception_raises_not_found(self) -> None:
        # spec: SECRET_RESOLUTION.md §Error taxonomy — SecretRefNotFound on 404.
        self._fake_core.read_namespaced_secret.side_effect = _make_api_exception(404)
        with pytest.raises(SecretRefNotFound):
            resolve_secret_ref("k8s-secret/dataspoke-conf-x/pw")

    def test_403_api_exception_raises_not_found(self) -> None:
        # spec: SECRET_RESOLUTION.md §Error taxonomy — "RBAC 403 from k8s API → wrapped to
        # SecretRefNotFound for read."
        self._fake_core.read_namespaced_secret.side_effect = _make_api_exception(403)
        with pytest.raises(SecretRefNotFound):
            resolve_secret_ref("k8s-secret/dataspoke-conf-x/pw")

    def test_500_api_exception_raises_resolver_unavailable(self) -> None:
        # spec: SECRET_RESOLUTION.md §Error taxonomy — "K8s API transient errors (5xx) →
        # SecretResolverUnavailable."
        self._fake_core.read_namespaced_secret.side_effect = _make_api_exception(500)
        with pytest.raises(SecretResolverUnavailable):
            resolve_secret_ref("k8s-secret/dataspoke-conf-x/pw")


# ── Writer tests ──────────────────────────────────────────────────────────────
# spec: SECRET_RESOLUTION.md §Vault-write flow


class TestWriteSecretValue:
    def setup_method(self) -> None:
        _reset_resolver_state()
        self._fake_core = _inject_mock_client()

    def test_secret_missing_creates_new_secret_with_encoded_value(self) -> None:
        # spec: SECRET_RESOLUTION.md §Vault-write flow step 3 — "Secret does not exist
        # → create_namespaced_secret with data: {key: base64(password)}."
        self._fake_core.read_namespaced_secret.side_effect = _make_api_exception(404)
        write_secret_value("dataspoke-conf-db", "password", "s3cr3t", force_overwrite=False)

        self._fake_core.create_namespaced_secret.assert_called_once()
        call_kwargs = self._fake_core.create_namespaced_secret.call_args
        body = call_kwargs.kwargs.get("body") or call_kwargs.args[1]
        encoded = base64.b64encode(b"s3cr3t").decode()
        assert body.data == {"password": encoded}

    def test_secret_exists_key_absent_patches_regardless_of_force_overwrite(self) -> None:
        # spec: SECRET_RESOLUTION.md §Vault-write flow step 3 — "Secret exists, target
        # key absent from data → patch_namespaced_secret (merge-patch) to add data[key].
        # force_overwrite is irrelevant here."
        self._fake_core.read_namespaced_secret.return_value = _make_secret({"other": "val"})
        write_secret_value("dataspoke-conf-db", "password", "s3cr3t", force_overwrite=False)

        self._fake_core.patch_namespaced_secret.assert_called_once()
        self._fake_core.create_namespaced_secret.assert_not_called()

        # Patch body must contain only data[key] — other keys in the secret are preserved
        # by the merge-patch semantics (server-side).
        patch_body = self._fake_core.patch_namespaced_secret.call_args.kwargs.get(
            "body"
        ) or self._fake_core.patch_namespaced_secret.call_args.args[2]
        encoded_new = base64.b64encode(b"s3cr3t").decode()
        assert patch_body == {"data": {"password": encoded_new}}

    def test_secret_exists_key_present_no_overwrite_raises_collision(self) -> None:
        # spec: SECRET_RESOLUTION.md §Vault-write flow step 2 — "If the Secret exists and
        # contains data[key], and force_overwrite=false, return 422 SecretCollision."
        self._fake_core.read_namespaced_secret.return_value = _make_secret({"password": "old"})
        with pytest.raises(SecretCollision):
            write_secret_value("dataspoke-conf-db", "password", "new", force_overwrite=False)

    def test_secret_exists_key_present_force_overwrite_patches_only_target_key(self) -> None:
        # spec: SECRET_RESOLUTION.md §Vault-write flow step 3 — "Secret exists and
        # force_overwrite=true → merge-patch setting only data[key]. Other keys preserved."
        self._fake_core.read_namespaced_secret.return_value = _make_secret(
            {"password": "old", "other": "keep"}
        )
        write_secret_value("dataspoke-conf-db", "password", "new", force_overwrite=True)

        self._fake_core.patch_namespaced_secret.assert_called_once()
        call_kwargs = self._fake_core.patch_namespaced_secret.call_args
        body = call_kwargs.kwargs.get("body") or call_kwargs.args[2]
        encoded_new = base64.b64encode(b"new").decode()
        # Only the target key must appear in the patch body data dict.
        assert body == {"data": {"password": encoded_new}}

    def test_name_without_prefix_raises_name_forbidden_no_k8s_calls(self) -> None:
        # spec: SECRET_RESOLUTION.md §Vault-write flow step 0 — prefix check at writer.
        with pytest.raises(SecretRefNameForbidden):
            write_secret_value("team-pg-prod", "password", "val", force_overwrite=False)

        self._fake_core.read_namespaced_secret.assert_not_called()
        self._fake_core.create_namespaced_secret.assert_not_called()
        self._fake_core.patch_namespaced_secret.assert_not_called()

    def test_403_on_create_raises_resolver_unavailable(self) -> None:
        # spec: SECRET_RESOLUTION.md §Error taxonomy — "RBAC Forbidden (403) on write
        # → wrapped as SecretResolverUnavailable."
        self._fake_core.read_namespaced_secret.side_effect = _make_api_exception(404)
        self._fake_core.create_namespaced_secret.side_effect = _make_api_exception(403)
        with pytest.raises(SecretResolverUnavailable):
            write_secret_value("dataspoke-conf-db", "pw", "val", force_overwrite=False)

    def test_500_on_patch_raises_resolver_unavailable(self) -> None:
        # spec: SECRET_RESOLUTION.md §Error taxonomy — "K8s API transient errors (5xx) →
        # SecretResolverUnavailable."
        self._fake_core.read_namespaced_secret.return_value = _make_secret({"other": "val"})
        self._fake_core.patch_namespaced_secret.side_effect = _make_api_exception(500)
        with pytest.raises(SecretResolverUnavailable):
            write_secret_value("dataspoke-conf-db", "pw", "val", force_overwrite=False)


# ── Verifier tests ────────────────────────────────────────────────────────────
# spec: SECRET_RESOLUTION.md §Reference-path verify flow


class TestVerifySecretRef:
    def setup_method(self) -> None:
        _reset_resolver_state()
        self._fake_core = _inject_mock_client()

    def test_secret_exists_key_in_data_no_exception(self) -> None:
        # spec: SECRET_RESOLUTION.md §Reference-path verify flow — step 4: persist auth.
        self._fake_core.read_namespaced_secret.return_value = _make_secret({"pw": "val"})
        verify_secret_ref("dataspoke-conf-db", "pw")  # must not raise

    def test_secret_missing_raises_not_found(self) -> None:
        # spec: SECRET_RESOLUTION.md §Reference-path verify flow step 2 — "Secret missing
        # → 422 SecretRefNotFound."
        self._fake_core.read_namespaced_secret.side_effect = _make_api_exception(404)
        with pytest.raises(SecretRefNotFound):
            verify_secret_ref("dataspoke-conf-db", "pw")

    def test_key_missing_in_data_raises_not_found(self) -> None:
        # spec: SECRET_RESOLUTION.md §Reference-path verify flow step 3 — "data[key]
        # missing → 422 SecretRefNotFound."
        self._fake_core.read_namespaced_secret.return_value = _make_secret({"other": "val"})
        with pytest.raises(SecretRefNotFound):
            verify_secret_ref("dataspoke-conf-db", "pw")

    def test_403_raises_not_found(self) -> None:
        # spec: SECRET_RESOLUTION.md §Error taxonomy — "RBAC Forbidden (403) →
        # SecretRefNotFound for read."
        self._fake_core.read_namespaced_secret.side_effect = _make_api_exception(403)
        with pytest.raises(SecretRefNotFound):
            verify_secret_ref("dataspoke-conf-db", "pw")

    def test_500_raises_resolver_unavailable(self) -> None:
        # spec: SECRET_RESOLUTION.md §Error taxonomy — K8s 5xx → SecretResolverUnavailable.
        self._fake_core.read_namespaced_secret.side_effect = _make_api_exception(500)
        with pytest.raises(SecretResolverUnavailable):
            verify_secret_ref("dataspoke-conf-db", "pw")

    def test_name_without_prefix_raises_name_forbidden(self) -> None:
        # spec: SECRET_RESOLUTION.md §Reference-path verify flow step 0 — prefix check.
        with pytest.raises(SecretRefNameForbidden):
            verify_secret_ref("team-pg-prod", "pw")
