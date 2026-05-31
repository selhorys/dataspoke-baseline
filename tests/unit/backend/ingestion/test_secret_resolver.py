"""Unit tests for src/backend/ingestion/secret_resolver.py.

Covers:
- _parse_name_key: split on last __, malformed inputs
- prefix confinement: ref targets only dataspoke-source-cred-<name>
- resolve_secret_ref: cache hit, cache miss, 403/404 → SecretRefNotFound,
  5xx → SecretResolverUnavailable, non-UTF-8 → SecretRefNotFound (NOT UnicodeDecodeError)
- resolve_recipe_secrets: deep-copies input (source unmutated), recursively substitutes
- verify_secret_ref: no value returned; same error taxonomy as resolve
- list_source_cred_refs: prefix filter; no values; name -> ref derivation

Spec: spec/feature/SECRET_RESOLUTION.md
"""

from __future__ import annotations

import base64
import time
from collections.abc import Generator
from unittest.mock import MagicMock, patch

import pytest

import src.backend.ingestion.secret_resolver as _sr
from src.backend.ingestion.secret_resolver import (
    SecretRefInfo,
    SecretRefMalformed,
    SecretRefNotFound,
    SecretResolverUnavailable,
    list_source_cred_refs,
    resolve_recipe_secrets,
    resolve_secret_ref,
    verify_secret_ref,
)


# ── Test helpers ──────────────────────────────────────────────────────────────


def _make_secret(data: dict[str, str] | None) -> MagicMock:
    """Build a fake kubernetes V1Secret with base64-encoded data values."""
    secret = MagicMock()
    if data is None:
        secret.data = None
    else:
        secret.data = {k: base64.b64encode(v.encode()).decode() for k, v in data.items()}
    return secret


def _make_api_exception(status: int) -> Exception:
    from kubernetes.client.exceptions import ApiException

    exc = ApiException(status=status)
    exc.status = status
    return exc


def _reset_resolver() -> None:
    """Reset global module state between tests."""
    with _sr._init_lock:
        _sr._resolver_state["client"] = None
        _sr._resolver_state["available"] = None
        _sr._resolver_state["own_namespace"] = None
    _sr._cache.clear()
    _sr._cache_order.clear()


def _inject_client(namespace: str = "dataspoke-01") -> MagicMock:
    """Directly inject a mock client without going through _init()."""
    fake_core = MagicMock()
    with _sr._init_lock:
        _sr._resolver_state["available"] = True
        _sr._resolver_state["client"] = fake_core
        _sr._resolver_state["own_namespace"] = namespace
    return fake_core


# ── _parse_name_key ───────────────────────────────────────────────────────────


class TestParseNameKey:
    """Spec: SECRET_RESOLUTION.md §Run-time resolve flow — 'split on last __'."""

    def test_valid_ref_splits_correctly(self) -> None:
        """'team_pg__password' splits into name='team_pg', key='password'.

        Spec: SECRET_RESOLUTION.md §Reference syntax — '${name__key}' resolves to
        Secret 'dataspoke-source-cred-<name>', data key '<key>'.
        """
        name, key = _sr._parse_name_key("team_pg__password")
        assert name == "team_pg"
        assert key == "password"

    def test_last_double_underscore_used_as_split_point(self) -> None:
        """When name itself contains __, split on the LAST __ only.

        Spec: SECRET_RESOLUTION.md §Name prefix policy — 'split unambiguously on
        the last __ (since <name> cannot contain __)'. Defensive: we still handle it.
        """
        name, key = _sr._parse_name_key("team__pg__password")
        assert name == "team__pg"
        assert key == "password"

    def test_no_double_underscore_raises_malformed(self) -> None:
        """Ref without __ raises SecretRefMalformed.

        Spec: SECRET_RESOLUTION.md §Error taxonomy — 'SecretRefMalformed: no __
        or empty segment → 422 SECRET_REF_MALFORMED'.
        """
        with pytest.raises(SecretRefMalformed):
            _sr._parse_name_key("team-pg-password")

    def test_empty_name_segment_raises_malformed(self) -> None:
        """'__key' with empty name segment raises SecretRefMalformed.

        Spec: SECRET_RESOLUTION.md §Error taxonomy — empty segment.
        """
        with pytest.raises(SecretRefMalformed):
            _sr._parse_name_key("__password")

    def test_empty_key_segment_raises_malformed(self) -> None:
        """'name__' with empty key segment raises SecretRefMalformed.

        Spec: SECRET_RESOLUTION.md §Error taxonomy — empty segment.
        """
        with pytest.raises(SecretRefMalformed):
            _sr._parse_name_key("team_pg__")


# ── _secret_name prefix guard ─────────────────────────────────────────────────


class TestSecretNamePrefix:
    """Spec: SECRET_RESOLUTION.md §Name prefix policy — security boundary."""

    def test_name_prefix_prepended(self) -> None:
        """_secret_name prepends 'dataspoke-source-cred-' to the name segment.

        Spec: SECRET_RESOLUTION.md §Name prefix policy — 'the prefix is implicit
        in the syntax and <name> is the part after it'.
        """
        full = _sr._secret_name("team-pg")
        assert full == "dataspoke-source-cred-team-pg"


# ── resolve_secret_ref ────────────────────────────────────────────────────────


class TestResolveSecretRef:
    """Spec: SECRET_RESOLUTION.md §Run-time resolve flow."""

    def setup_method(self) -> None:
        _reset_resolver()
        self.core = _inject_client()

    def teardown_method(self) -> None:
        _reset_resolver()

    def test_returns_decoded_plaintext_value(self) -> None:
        """Reads the secret, base64-decodes, and returns the plaintext string.

        Spec: SECRET_RESOLUTION.md §Run-time resolve flow — 'base64-decoded'.
        """
        self.core.read_namespaced_secret.return_value = _make_secret({"password": "hunter2"})
        result = resolve_secret_ref("team_pg__password")
        assert result == "hunter2"

    def test_calls_correct_secret_name_and_namespace(self) -> None:
        """Constructs Secret name as 'dataspoke-source-cred-<name>' in own namespace.

        Spec: SECRET_RESOLUTION.md §Name prefix policy.
        """
        self.core.read_namespaced_secret.return_value = _make_secret({"pw": "val"})
        resolve_secret_ref("db__pw")
        self.core.read_namespaced_secret.assert_called_once_with(
            name="dataspoke-source-cred-db", namespace="dataspoke-01"
        )

    def test_cache_hit_calls_k8s_only_once(self) -> None:
        """Second call within TTL uses cache — no second k8s call.

        Spec: SECRET_RESOLUTION.md §Cache — 'Bounds the k8s API call rate when a
        burst of runs/dry-runs hits the same Secret.'
        """
        self.core.read_namespaced_secret.return_value = _make_secret({"pw": "s3cr3t"})
        resolve_secret_ref("db__pw")
        resolve_secret_ref("db__pw")
        assert self.core.read_namespaced_secret.call_count == 1

    def test_expired_cache_entry_hits_k8s_again(self) -> None:
        """Expired cache entry causes a new k8s call.

        Spec: SECRET_RESOLUTION.md §Cache — 'TTL is short enough that rotations
        propagate within a minute.'
        """
        self.core.read_namespaced_secret.return_value = _make_secret({"pw": "s3cr3t"})
        resolve_secret_ref("db__pw")
        # Force-expire the entry
        cache_key = ("dataspoke-source-cred-db", "pw")
        value, _ = _sr._cache[cache_key]
        _sr._cache[cache_key] = (value, time.monotonic() - 1.0)

        resolve_secret_ref("db__pw")
        assert self.core.read_namespaced_secret.call_count == 2

    def test_404_raises_secret_ref_not_found(self) -> None:
        """k8s 404 → SecretRefNotFound (not the raw ApiException).

        Spec: SECRET_RESOLUTION.md §Error taxonomy — 'SecretRefNotFound: Secret or
        key absent, or RBAC 403'.
        """
        self.core.read_namespaced_secret.side_effect = _make_api_exception(404)
        with pytest.raises(SecretRefNotFound):
            resolve_secret_ref("db__pw")

    def test_403_raises_secret_ref_not_found(self) -> None:
        """RBAC k8s 403 → SecretRefNotFound (same taxonomy as 404).

        Spec: SECRET_RESOLUTION.md §Error taxonomy — 'RBAC Forbidden (403) →
        wrapped as SecretRefNotFound for read'.
        """
        self.core.read_namespaced_secret.side_effect = _make_api_exception(403)
        with pytest.raises(SecretRefNotFound):
            resolve_secret_ref("db__pw")

    def test_500_raises_secret_resolver_unavailable(self) -> None:
        """k8s 5xx → SecretResolverUnavailable.

        Spec: SECRET_RESOLUTION.md §Error taxonomy — 'K8s API transient errors (5xx)
        → SecretResolverUnavailable'.
        """
        self.core.read_namespaced_secret.side_effect = _make_api_exception(500)
        with pytest.raises(SecretResolverUnavailable):
            resolve_secret_ref("db__pw")

    def test_missing_key_in_secret_data_raises_not_found(self) -> None:
        """Secret exists but key absent → SecretRefNotFound.

        Spec: SECRET_RESOLUTION.md §Error taxonomy — 'data[key] missing →
        SecretRefNotFound'.
        """
        self.core.read_namespaced_secret.return_value = _make_secret({"other_key": "val"})
        with pytest.raises(SecretRefNotFound):
            resolve_secret_ref("db__pw")

    def test_none_data_raises_not_found(self) -> None:
        """Secret with data=None → SecretRefNotFound (key cannot exist).

        Spec: SECRET_RESOLUTION.md §Run-time resolve flow.
        """
        self.core.read_namespaced_secret.return_value = _make_secret(None)
        with pytest.raises(SecretRefNotFound):
            resolve_secret_ref("db__pw")

    def test_non_utf8_value_raises_secret_ref_not_found_not_unicode_error(self) -> None:
        """Binary-only (non-UTF-8) secret value raises SecretRefNotFound, NOT UnicodeDecodeError.

        Spec: SECRET_RESOLUTION.md §Error taxonomy — surfaced as SecretRefNotFound
        with a value-free message (no plaintext leak).
        """
        secret = MagicMock()
        # raw bytes that are not valid UTF-8: the b64 value is already a string in k8s API
        # but when decoded will fail UTF-8 decode
        raw_bytes = b"\xff\xfe"
        secret.data = {"pw": base64.b64encode(raw_bytes).decode()}
        self.core.read_namespaced_secret.return_value = secret
        with pytest.raises(SecretRefNotFound):
            resolve_secret_ref("db__pw")
        # Must NOT propagate UnicodeDecodeError
        try:
            resolve_secret_ref("db__pw")
        except SecretRefNotFound:
            pass
        except UnicodeDecodeError:
            pytest.fail("UnicodeDecodeError must not propagate — must be wrapped as SecretRefNotFound")

    def test_cache_is_bounded(self) -> None:
        """Cache size does not grow without limit after many distinct refs.

        Spec: SECRET_RESOLUTION.md §Cache — 'Bounded with a hard cap (LRU eviction
        by insertion order) so a long-running pod … cannot grow the cache without limit.'
        """
        n = 600  # more than _CACHE_MAX_SIZE = 512
        for i in range(n):
            secret_name = f"src{i}"
            self.core.read_namespaced_secret.return_value = _make_secret({"pw": f"v{i}"})
            resolve_secret_ref(f"{secret_name}__pw")
        assert len(_sr._cache) < n, (
            f"Cache grew to {len(_sr._cache)} entries for {n} distinct refs; must be bounded."
        )


# ── _init unavailability guard ────────────────────────────────────────────────


class TestResolverUnavailability:
    """Spec: SECRET_RESOLUTION.md §Design — 'no silent fallback' on init failure."""

    def setup_method(self) -> None:
        _reset_resolver()

    def teardown_method(self) -> None:
        _reset_resolver()

    def test_incluster_config_failure_raises_unavailable(self) -> None:
        """In-cluster config failure → SecretResolverUnavailable immediately.

        Spec: SECRET_RESOLUTION.md §Design — 'If in-cluster k8s init fails, every
        subsequent call raises SecretResolverUnavailable — no silent fallback.'
        """
        with (
            patch("kubernetes.config.load_incluster_config", side_effect=Exception("no cluster")),
            patch("kubernetes.client.CoreV1Api"),
        ):
            with pytest.raises(SecretResolverUnavailable):
                resolve_secret_ref("db__pw")

    def test_unavailable_state_persists_on_subsequent_calls(self) -> None:
        """Once available=False, every subsequent call raises SecretResolverUnavailable.

        Spec: SECRET_RESOLUTION.md §Design — once failed, no retry of _init.
        """
        with (
            patch("kubernetes.config.load_incluster_config", side_effect=Exception("no cluster")),
            patch("kubernetes.client.CoreV1Api"),
        ):
            with pytest.raises(SecretResolverUnavailable):
                resolve_secret_ref("db__pw")

        assert _sr._resolver_state["available"] is False
        # Second call without re-patching must also raise
        with pytest.raises(SecretResolverUnavailable):
            resolve_secret_ref("db__pw")


# ── resolve_recipe_secrets ────────────────────────────────────────────────────


class TestResolveRecipeSecrets:
    """Spec: SECRET_RESOLUTION.md §Run-time resolve flow — deep-copy + substitute."""

    def setup_method(self) -> None:
        _reset_resolver()
        self.core = _inject_client()

    def teardown_method(self) -> None:
        _reset_resolver()

    def test_substitutes_secret_ref_with_plaintext(self) -> None:
        """${name__key} is replaced by the plaintext value in the returned dict.

        Spec: SECRET_RESOLUTION.md §Run-time resolve flow.
        """
        self.core.read_namespaced_secret.return_value = _make_secret({"password": "s3cr3t"})
        recipe = {
            "source": {
                "type": "postgres",
                "config": {"password": "${team_pg__password}", "host_port": "pg:5432"},
            }
        }
        resolved = resolve_recipe_secrets(recipe)
        assert resolved["source"]["config"]["password"] == "s3cr3t"
        assert resolved["source"]["config"]["host_port"] == "pg:5432"

    def test_original_recipe_not_mutated(self) -> None:
        """Input recipe dict must not be modified (deep-copy semantics).

        Spec: SECRET_RESOLUTION.md §Run-time resolve flow — 'The returned dict is
        a new object; the original recipe is never mutated. Plaintext values exist
        only in the returned in-memory dict.'
        """
        self.core.read_namespaced_secret.return_value = _make_secret({"password": "s3cr3t"})
        original_password = "${team_pg__password}"
        recipe = {
            "source": {
                "type": "postgres",
                "config": {"password": original_password},
            }
        }
        _ = resolve_recipe_secrets(recipe)
        # Original must be unchanged
        assert recipe["source"]["config"]["password"] == original_password

    def test_plain_var_without_double_underscore_left_unchanged(self) -> None:
        """${PLAIN_VAR} without __ is not a secret ref — left as-is in resolved copy.

        Spec: SECRET_RESOLUTION.md §Reference syntax — tokens without __ are ignored.
        """
        recipe = {
            "source": {
                "type": "postgres",
                "config": {"env_tag": "${ENVIRONMENT}"},
            }
        }
        resolved = resolve_recipe_secrets(recipe)
        assert resolved["source"]["config"]["env_tag"] == "${ENVIRONMENT}"

    def test_nested_config_is_fully_resolved(self) -> None:
        """Secret refs in deeply nested config dicts are substituted."""
        self.core.read_namespaced_secret.return_value = _make_secret({"cert": "cert-val"})
        recipe = {
            "source": {
                "type": "postgres",
                "config": {
                    "ssl": {"cert": "${team_pg__cert}"},
                },
            }
        }
        resolved = resolve_recipe_secrets(recipe)
        assert resolved["source"]["config"]["ssl"]["cert"] == "cert-val"


# ── verify_secret_ref ─────────────────────────────────────────────────────────


class TestVerifySecretRef:
    """Spec: SECRET_RESOLUTION.md §Reference verify flow (at source save time)."""

    def setup_method(self) -> None:
        _reset_resolver()
        self.core = _inject_client()

    def teardown_method(self) -> None:
        _reset_resolver()

    def test_existing_secret_and_key_does_not_raise(self) -> None:
        """verify_secret_ref returns None (no exception) when Secret and key exist.

        Spec: SECRET_RESOLUTION.md §Reference verify flow — 'All references resolve
        → persist the source.'
        """
        self.core.read_namespaced_secret.return_value = _make_secret({"pw": "val"})
        result = verify_secret_ref("team_pg__pw")
        assert result is None

    def test_missing_secret_raises_not_found(self) -> None:
        """404 from k8s → SecretRefNotFound.

        Spec: SECRET_RESOLUTION.md §Reference verify flow step 3 — 'Secret missing
        → 422 SECRET_REF_NOT_FOUND'.
        """
        self.core.read_namespaced_secret.side_effect = _make_api_exception(404)
        with pytest.raises(SecretRefNotFound):
            verify_secret_ref("team_pg__pw")

    def test_missing_key_raises_not_found(self) -> None:
        """Secret exists but key absent → SecretRefNotFound.

        Spec: SECRET_RESOLUTION.md §Reference verify flow step 4 — 'data[key]
        missing → 422 SECRET_REF_NOT_FOUND'.
        """
        self.core.read_namespaced_secret.return_value = _make_secret({"other": "val"})
        with pytest.raises(SecretRefNotFound):
            verify_secret_ref("team_pg__pw")

    def test_malformed_ref_raises_malformed(self) -> None:
        """Ref without __ raises SecretRefMalformed — no k8s call made.

        Spec: SECRET_RESOLUTION.md §Reference verify flow step 0.
        """
        with pytest.raises(SecretRefMalformed):
            verify_secret_ref("nodoublescore")
        self.core.read_namespaced_secret.assert_not_called()

    def test_403_raises_not_found(self) -> None:
        """RBAC 403 → SecretRefNotFound.

        Spec: SECRET_RESOLUTION.md §Error taxonomy.
        """
        self.core.read_namespaced_secret.side_effect = _make_api_exception(403)
        with pytest.raises(SecretRefNotFound):
            verify_secret_ref("team_pg__pw")

    def test_5xx_raises_resolver_unavailable(self) -> None:
        """k8s 5xx → SecretResolverUnavailable.

        Spec: SECRET_RESOLUTION.md §Error taxonomy.
        """
        self.core.read_namespaced_secret.side_effect = _make_api_exception(500)
        with pytest.raises(SecretResolverUnavailable):
            verify_secret_ref("team_pg__pw")


# ── list_source_cred_refs ─────────────────────────────────────────────────────


class TestListSourceCredRefs:
    """Spec: SECRET_RESOLUTION.md §Reference discovery (list flow)."""

    def setup_method(self) -> None:
        _reset_resolver()
        self.core = _inject_client()

    def teardown_method(self) -> None:
        _reset_resolver()

    def _make_secret_item(self, name: str, keys: list[str]) -> MagicMock:
        item = MagicMock()
        item.metadata = MagicMock()
        item.metadata.name = name
        item.data = {k: base64.b64encode(b"val").decode() for k in keys}
        return item

    def test_returns_refs_for_prefixed_secrets(self) -> None:
        """Only 'dataspoke-source-cred-*' Secrets appear in the listing.

        Spec: SECRET_RESOLUTION.md §Reference discovery — 'enumerates Kubernetes
        Secrets whose name starts with dataspoke-source-cred-'.
        """
        self.core.list_namespaced_secret.return_value = MagicMock(
            items=[
                self._make_secret_item("dataspoke-source-cred-team-pg", ["password", "ssl_key"]),
                self._make_secret_item("dataspoke-secrets", ["jwt_key"]),  # filtered out
            ]
        )
        refs = list_source_cred_refs()
        ref_names = [r.secret_name for r in refs]
        assert "dataspoke-source-cred-team-pg" in ref_names
        assert "dataspoke-secrets" not in ref_names

    def test_ref_field_is_name_segment_plus_key(self) -> None:
        """'ref' field in the response is '<name_segment>__<key>'.

        Spec: SECRET_RESOLUTION.md §Reference discovery — 'ref is the literal
        string an author pastes into a recipe as ${...}'.
        """
        self.core.list_namespaced_secret.return_value = MagicMock(
            items=[
                self._make_secret_item("dataspoke-source-cred-team-pg", ["password"]),
            ]
        )
        refs = list_source_cred_refs()
        assert len(refs) == 1
        ref = refs[0]
        assert ref.ref == "team-pg__password"
        assert ref.secret_name == "dataspoke-source-cred-team-pg"
        assert ref.key == "password"

    def test_values_are_never_returned(self) -> None:
        """SecretRefInfo has no 'value' field — values are never exposed.

        Spec: SECRET_RESOLUTION.md §Reference discovery — 'Values are never returned'.
        """
        self.core.list_namespaced_secret.return_value = MagicMock(
            items=[
                self._make_secret_item("dataspoke-source-cred-x", ["pw"]),
            ]
        )
        refs = list_source_cred_refs()
        assert len(refs) == 1
        ref = refs[0]
        assert isinstance(ref, SecretRefInfo)
        assert not hasattr(ref, "value")

    def test_empty_namespace_returns_empty_list(self) -> None:
        """No matching Secrets → empty list returned (not an error).

        Spec: SECRET_RESOLUTION.md §Reference discovery.
        """
        self.core.list_namespaced_secret.return_value = MagicMock(items=[])
        refs = list_source_cred_refs()
        assert refs == []

    def test_5xx_raises_resolver_unavailable(self) -> None:
        """k8s 5xx on list → SecretResolverUnavailable.

        Spec: SECRET_RESOLUTION.md §Error taxonomy.
        """
        self.core.list_namespaced_secret.side_effect = _make_api_exception(500)
        with pytest.raises(SecretResolverUnavailable):
            list_source_cred_refs()

    def test_multiple_keys_expand_to_multiple_rows(self) -> None:
        """One Secret with N keys → N SecretRefInfo rows.

        Spec: SECRET_RESOLUTION.md §Reference discovery — 'expands each Secret's
        data keys, and returns one row per (secret, key) pair'.
        """
        self.core.list_namespaced_secret.return_value = MagicMock(
            items=[
                self._make_secret_item(
                    "dataspoke-source-cred-team-pg", ["password", "ssl_cert", "ssl_key"]
                ),
            ]
        )
        refs = list_source_cred_refs()
        assert len(refs) == 3
        keys_returned = {r.key for r in refs}
        assert keys_returned == {"password", "ssl_cert", "ssl_key"}
