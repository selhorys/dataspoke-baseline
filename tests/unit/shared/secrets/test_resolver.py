"""Unit tests for src/shared/secrets/resolver.py — backend-neutral layer.

All tests run against a FakeBackend (plain dict + call counters). Zero
kubernetes imports or patches in this file.

Spec: spec/feature/SECRET_RESOLUTION.md §Cache, §Run-time resolve flow,
      §Reference verify flow, §Reference discovery, §Backend extensibility
"""

from __future__ import annotations

import time

import pytest

import src.shared.secrets.resolver as _resolver
from src.shared.secrets.interface import (
    SecretRefInfo,
    SecretRefMalformed,
    SecretRefNotFound,
    SecretResolverUnavailable,
)
from src.shared.secrets.resolver import (
    get_backend,
    list_source_cred_refs,
    resolve_recipe_secrets,
    resolve_secret_ref,
    set_backend,
    verify_secret_ref,
)

# ── FakeBackend ────────────────────────────────────────────────────────────────


class FakeBackend:
    """In-memory backend: dict of (name, key) → value.

    Tracks per-method call counts. Raises can be configured per (name, key).
    """

    def __init__(
        self,
        data: dict[tuple[str, str], str] | None = None,
        raise_on: dict[tuple[str, str], Exception] | None = None,
    ) -> None:
        self._data: dict[tuple[str, str], str] = data or {}
        self._raise_on: dict[tuple[str, str], Exception] = raise_on or {}
        self.read_calls: list[tuple[str, str]] = []
        self.verify_calls: list[tuple[str, str]] = []
        self.list_calls: int = 0
        self._list_exc: Exception | None = None

    def read_value(self, name: str, key: str) -> str:
        self.read_calls.append((name, key))
        if (name, key) in self._raise_on:
            raise self._raise_on[(name, key)]
        if (name, key) not in self._data:
            raise SecretRefNotFound(f"Key '{key}' not in fake backend for name '{name}'")
        return self._data[(name, key)]

    def verify(self, name: str, key: str) -> None:
        self.verify_calls.append((name, key))
        if (name, key) in self._raise_on:
            raise self._raise_on[(name, key)]
        if (name, key) not in self._data:
            raise SecretRefNotFound(f"Key '{key}' not in fake backend for name '{name}'")

    def list_refs(self) -> list[SecretRefInfo]:
        self.list_calls += 1
        if self._list_exc is not None:
            raise self._list_exc
        refs = []
        for (name, key) in sorted(self._data):
            refs.append(
                SecretRefInfo(
                    ref=f"{name}__{key}",
                    secret_name=f"dataspoke-source-cred-{name}",
                    key=key,
                )
            )
        return refs


# ── Fixtures ───────────────────────────────────────────────────────────────────


def _reset_resolver_state(fake: FakeBackend | None = None) -> None:
    """Inject fake backend and clear cache."""
    set_backend(fake)


@pytest.fixture()
def fake() -> FakeBackend:
    """Provide a FakeBackend pre-loaded with one credential."""
    fb = FakeBackend(data={("team-pg", "password"): "hunter2"})
    set_backend(fb)
    yield fb
    set_backend(None)
    # Restore None so get_backend() will re-instantiate the default lazily.


@pytest.fixture()
def empty_fake() -> FakeBackend:
    """Provide a FakeBackend with no credentials."""
    fb = FakeBackend()
    set_backend(fb)
    yield fb
    set_backend(None)


# ── resolve_secret_ref ─────────────────────────────────────────────────────────


class TestResolveSecretRef:
    """Spec: SECRET_RESOLUTION.md §Run-time resolve flow."""

    def test_returns_value_from_backend(self, fake: FakeBackend) -> None:
        """resolve_secret_ref delegates to backend.read_value and returns the value.

        Spec: SECRET_RESOLUTION.md §Run-time resolve flow.
        """
        result = resolve_secret_ref("team-pg__password")
        assert result == "hunter2"

    def test_second_call_within_ttl_is_cache_hit(self, fake: FakeBackend) -> None:
        """Second call within TTL reads from cache — backend called only once.

        Spec: SECRET_RESOLUTION.md §Cache — 'Bounds the k8s API call rate when a
        burst of runs/dry-runs hits the same Secret.'
        """
        resolve_secret_ref("team-pg__password")
        resolve_secret_ref("team-pg__password")
        assert len(fake.read_calls) == 1, (
            f"Backend read_value called {len(fake.read_calls)} times; expected 1 (cache hit)."
        )

    def test_expired_cache_entry_re_reads_backend(self, fake: FakeBackend) -> None:
        """Expired cache entry causes a fresh backend read.

        Spec: SECRET_RESOLUTION.md §Cache — TTL-based expiry.
        """
        resolve_secret_ref("team-pg__password")
        # Manually expire the cache entry.
        cache_key = ("team-pg", "password")
        value, _ = _resolver._cache[cache_key]
        _resolver._cache[cache_key] = (value, time.monotonic() - 1.0)

        resolve_secret_ref("team-pg__password")
        assert len(fake.read_calls) == 2, (
            f"Backend read_value called {len(fake.read_calls)} times after expiry; expected 2."
        )

    def test_cache_bounded_at_512(self, empty_fake: FakeBackend) -> None:
        """Cache does not exceed _CACHE_MAX_SIZE after many distinct refs.

        Spec: SECRET_RESOLUTION.md §Cache — 'Bounded with a hard cap (LRU eviction
        by insertion order) so a long-running pod … cannot grow the cache without limit.'
        """
        n = 600  # more than _CACHE_MAX_SIZE = 512
        for i in range(n):
            empty_fake._data[(f"src{i}", "pw")] = f"v{i}"
            resolve_secret_ref(f"src{i}__pw")
        assert len(_resolver._cache) < n, (
            f"Cache grew to {len(_resolver._cache)} entries for {n} distinct refs; must be < {n}."
        )
        assert len(_resolver._cache) <= _resolver._CACHE_MAX_SIZE

    def test_not_found_propagates(self, empty_fake: FakeBackend) -> None:
        """SecretRefNotFound from backend propagates to the caller.

        Spec: SECRET_RESOLUTION.md §Error taxonomy.
        """
        with pytest.raises(SecretRefNotFound):
            resolve_secret_ref("missing__key")

    def test_unavailable_propagates(self, empty_fake: FakeBackend) -> None:
        """SecretResolverUnavailable from backend propagates to the caller.

        Spec: SECRET_RESOLUTION.md §Error taxonomy.
        """
        empty_fake._raise_on[("broken", "pw")] = SecretResolverUnavailable("store down")
        with pytest.raises(SecretResolverUnavailable):
            resolve_secret_ref("broken__pw")

    def test_malformed_ref_raises_before_touching_backend(self, fake: FakeBackend) -> None:
        """Malformed ref (no __) raises SecretRefMalformed without calling the backend.

        Spec: SECRET_RESOLUTION.md §Error taxonomy — 'SecretRefMalformed raised at
        parse time, before any backend call.'
        """
        with pytest.raises(SecretRefMalformed):
            resolve_secret_ref("nodoubleunderscore")
        assert fake.read_calls == [], "Backend must not be called for a malformed ref."


# ── resolve_recipe_secrets ─────────────────────────────────────────────────────


class TestResolveRecipeSecrets:
    """Spec: SECRET_RESOLUTION.md §Run-time resolve flow — deep-copy + substitute."""

    def test_substitutes_ref_in_config_value(self, fake: FakeBackend) -> None:
        """${name__key} replaced by plaintext in returned dict.

        Spec: SECRET_RESOLUTION.md §Run-time resolve flow.
        """
        recipe = {
            "source": {
                "type": "postgres",
                "config": {"password": "${team-pg__password}", "host_port": "pg:5432"},
            }
        }
        resolved = resolve_recipe_secrets(recipe)
        assert resolved["source"]["config"]["password"] == "hunter2"
        assert resolved["source"]["config"]["host_port"] == "pg:5432"

    def test_original_recipe_not_mutated(self, fake: FakeBackend) -> None:
        """Input recipe dict is never modified — deep-copy semantics.

        Spec: SECRET_RESOLUTION.md §Run-time resolve flow — 'The returned dict is
        a new object; the original recipe is never mutated.'
        """
        original_val = "${team-pg__password}"
        recipe = {"source": {"type": "postgres", "config": {"password": original_val}}}
        _ = resolve_recipe_secrets(recipe)
        assert recipe["source"]["config"]["password"] == original_val

    def test_env_placeholder_without_double_underscore_left_untouched(
        self, fake: FakeBackend
    ) -> None:
        """${ENVIRONMENT} (no __) is not a secret ref — left as-is in the resolved copy.

        Spec: SECRET_RESOLUTION.md §Reference syntax — tokens without __ are not matched.
        """
        recipe = {"source": {"type": "postgres", "config": {"tag": "${ENVIRONMENT}"}}}
        resolved = resolve_recipe_secrets(recipe)
        assert resolved["source"]["config"]["tag"] == "${ENVIRONMENT}"

    def test_non_matching_token_left_untouched(self, fake: FakeBackend) -> None:
        """Tokens that do not match SECRET_REF_RE (e.g. uppercase name) are left as-is."""
        recipe = {"source": {"type": "postgres", "config": {"val": "${UPPER__key}"}}}
        resolved = resolve_recipe_secrets(recipe)
        assert resolved["source"]["config"]["val"] == "${UPPER__key}"

    def test_nested_dict_values_are_substituted(self, fake: FakeBackend) -> None:
        """Secret refs in deeply-nested config dicts are substituted.

        Spec: SECRET_RESOLUTION.md §Run-time resolve flow — recursive substitution.
        """
        fake._data[("team-pg", "cert")] = "cert-pem"
        recipe = {
            "source": {
                "type": "postgres",
                "config": {"ssl": {"cert": "${team-pg__cert}"}},
            }
        }
        resolved = resolve_recipe_secrets(recipe)
        assert resolved["source"]["config"]["ssl"]["cert"] == "cert-pem"

    def test_list_values_are_substituted(self, fake: FakeBackend) -> None:
        """Secret refs inside list values are substituted.

        Spec: SECRET_RESOLUTION.md §Run-time resolve flow — recursive substitution.
        """
        fake._data[("kafka", "api_key")] = "KEY123"
        fake._data[("kafka", "api_secret")] = "SECRET456"
        recipe = {
            "source": {
                "type": "kafka",
                "config": {
                    "creds": ["${kafka__api_key}", "${kafka__api_secret}"],
                },
            }
        }
        resolved = resolve_recipe_secrets(recipe)
        assert resolved["source"]["config"]["creds"] == ["KEY123", "SECRET456"]

    def test_multiple_refs_in_one_string_all_substituted(self, fake: FakeBackend) -> None:
        """Multiple ${...} tokens in a single string value are all substituted."""
        fake._data[("db", "host")] = "pg.internal"
        fake._data[("db", "port")] = "5432"
        recipe = {
            "source": {
                "type": "postgres",
                "config": {"host_port": "${db__host}:${db__port}"},
            }
        }
        resolved = resolve_recipe_secrets(recipe)
        assert resolved["source"]["config"]["host_port"] == "pg.internal:5432"


# ── verify_secret_ref ──────────────────────────────────────────────────────────


class TestVerifySecretRef:
    """Spec: SECRET_RESOLUTION.md §Reference verify flow."""

    def test_existing_ref_returns_none(self, fake: FakeBackend) -> None:
        """verify_secret_ref returns None when Secret and key exist.

        Spec: SECRET_RESOLUTION.md §Reference verify flow — 'All references resolve
        → persist the source.'
        """
        result = verify_secret_ref("team-pg__password")
        assert result is None

    def test_calls_backend_verify_not_read_value(self, fake: FakeBackend) -> None:
        """verify_secret_ref calls backend.verify(), not backend.read_value().

        Verify is the save-time existence check (SECRET_RESOLUTION.md §Reference
        verify flow); it routes through backend.verify() and never reads or
        decodes the value. Impl-backed in src/shared/secrets/_resolver.py.
        """
        verify_secret_ref("team-pg__password")
        assert fake.verify_calls == [("team-pg", "password")]
        assert fake.read_calls == [], "verify must call backend.verify, not read_value."

    def test_verify_does_not_populate_cache(self, fake: FakeBackend) -> None:
        """verify does not write to the cache.

        The 60s cache (SECRET_RESOLUTION.md §Cache) is populated only by the
        run-time resolve flow; verify bypasses it, so a just-deleted Secret
        still fails verify even with a recent cache entry. Impl-backed in
        src/shared/secrets/_resolver.py.
        """
        verify_secret_ref("team-pg__password")
        assert ("team-pg", "password") not in _resolver._cache

    def test_cached_resolve_then_verify_still_calls_backend_verify(
        self, fake: FakeBackend
    ) -> None:
        """A cached resolve does not satisfy a subsequent verify — backend is still called.

        Verify bypasses the resolve cache (SECRET_RESOLUTION.md §Cache covers
        the resolve-path cache only). Impl-backed in
        src/shared/secrets/_resolver.py.
        """
        resolve_secret_ref("team-pg__password")
        assert ("team-pg", "password") in _resolver._cache  # cached

        verify_secret_ref("team-pg__password")
        assert len(fake.verify_calls) == 1, "verify must call backend even if entry is cached."

    def test_not_found_propagates(self, empty_fake: FakeBackend) -> None:
        """SecretRefNotFound from backend propagates.

        Spec: SECRET_RESOLUTION.md §Error taxonomy.
        """
        with pytest.raises(SecretRefNotFound):
            verify_secret_ref("missing__key")

    def test_unavailable_propagates(self, empty_fake: FakeBackend) -> None:
        """SecretResolverUnavailable from backend propagates.

        Spec: SECRET_RESOLUTION.md §Error taxonomy.
        """
        empty_fake._raise_on[("broken", "pw")] = SecretResolverUnavailable("store down")
        with pytest.raises(SecretResolverUnavailable):
            verify_secret_ref("broken__pw")

    def test_malformed_ref_raises_before_backend(self, fake: FakeBackend) -> None:
        """Malformed ref raises SecretRefMalformed without calling backend.verify.

        Spec: SECRET_RESOLUTION.md §Error taxonomy.
        """
        with pytest.raises(SecretRefMalformed):
            verify_secret_ref("nodoubleunderscore")
        assert fake.verify_calls == []


# ── list_source_cred_refs ──────────────────────────────────────────────────────


class TestListSourceCredRefs:
    """Spec: SECRET_RESOLUTION.md §Reference discovery (list flow)."""

    def test_passthrough_of_backend_list_refs(self, fake: FakeBackend) -> None:
        """list_source_cred_refs delegates to backend.list_refs() and returns the result.

        Spec: SECRET_RESOLUTION.md §Reference discovery.
        """
        refs = list_source_cred_refs()
        assert fake.list_calls == 1
        assert len(refs) == 1
        assert refs[0].ref == "team-pg__password"
        assert refs[0].key == "password"

    def test_unavailable_propagates_from_list(self, empty_fake: FakeBackend) -> None:
        """SecretResolverUnavailable from backend.list_refs propagates.

        Spec: SECRET_RESOLUTION.md §Error taxonomy.
        """
        empty_fake._list_exc = SecretResolverUnavailable("store down")
        with pytest.raises(SecretResolverUnavailable):
            list_source_cred_refs()

    def test_empty_list_returned_when_no_secrets(self, empty_fake: FakeBackend) -> None:
        """Empty list returned when backend reports no secrets.

        Spec: SECRET_RESOLUTION.md §Reference discovery.
        """
        refs = list_source_cred_refs()
        assert refs == []


# ── set_backend / get_backend ──────────────────────────────────────────────────


class TestSetBackend:
    """Spec: SECRET_RESOLUTION.md §Backend extensibility — set_backend() swap + cache clear."""

    @pytest.fixture(autouse=True)
    def _restore_backend(self) -> None:
        """Restore the global backend to None after every test, even on assertion failure."""
        yield
        set_backend(None)

    def test_swap_backend_clears_cache_and_uses_new_value(self) -> None:
        """set_backend(new) clears cache; subsequent resolve uses the new backend's value.

        Spec: SECRET_RESOLUTION.md §Backend extensibility — 'set_backend() swaps +
        clears cache'.
        """
        fb1 = FakeBackend(data={("db", "pw"): "value-one"})
        set_backend(fb1)
        resolve_secret_ref("db__pw")
        assert ("db", "pw") in _resolver._cache

        fb2 = FakeBackend(data={("db", "pw"): "value-two"})
        set_backend(fb2)

        # Cache must have been cleared.
        assert ("db", "pw") not in _resolver._cache, "Cache must be cleared on set_backend()."

        # New resolve must use fb2's value.
        result = resolve_secret_ref("db__pw")
        assert result == "value-two"
        assert fb2.read_calls == [("db", "pw")]
        assert fb1.read_calls == [("db", "pw")]  # only the first call

    def test_set_backend_none_restores_default_lazy_backend(self) -> None:
        """set_backend(None) restores the default; get_backend() returns a
        KubernetesSecretBackend instance (lazy construction, no cluster required).

        Spec: SECRET_RESOLUTION.md §Backend extensibility — 'set_backend(None)
        restores the default lazily-instantiated Kubernetes backend.'
        """
        from src.shared.secrets.k8s import KubernetesSecretBackend

        # Put a fake in first.
        fb = FakeBackend()
        set_backend(fb)
        assert get_backend() is fb

        # Restore default.
        set_backend(None)

        # Instantiation of KubernetesSecretBackend must not require a cluster.
        backend = get_backend()
        assert isinstance(backend, KubernetesSecretBackend), (
            f"Expected KubernetesSecretBackend after set_backend(None), got {type(backend)!r}."
        )
