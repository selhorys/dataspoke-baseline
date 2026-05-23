"""Unit tests for src/backend/admin/langfuse_secret.py.

Covers the Langfuse secret key accessor — get, set, cache, fallback, and security invariants.
All Kubernetes API calls are mocked; no cluster is needed.

Concerns covered:

1.  get — in-cluster read: secret present with data.secret_key → base64-decoded value returned.
2.  get — cache hit within TTL: second call does not re-read the secret.
3.  get — cache expiry: advancing monotonic clock past TTL causes a re-read.
4.  get — 404: secret absent → returns "" and caches "" (second call skips re-read).
5.  get — 403 fail-safe: RBAC denied → returns "", does NOT cache, re-reads on next call.
6.  get — other k8s error (500): raises SecretResolverUnavailable.
7.  get — secret exists but key absent (empty data dict or data=None) → returns "".
8.  get — out-of-cluster fallback: _require_client raises → returns settings.langfuse_secret_key
    without caching (host-mode only).
9.  set — create path: secret missing (404 on read) → create_namespaced_secret called
    with base64-encoded value; cache invalidated.
10. set — patch path: secret exists → patch_namespaced_secret called with correct body.
11. set — out-of-cluster raises SecretResolverUnavailable.
12. set — invalidates cache: prime cache via get, then set, then next get re-reads.
13. langfuse_secret_key_is_set: True when non-empty, False when "".
14. Secret name and key constants: _SECRET_NAME="dataspoke-langfuse-secret", _SECRET_KEY="secret_key".

Spec traceability:
- plan/scalable-beaming-hamster.md §Backend — langfuse_secret mirrors llm_secret pattern.
- src/backend/admin/langfuse_secret.py — public surface.
"""

from __future__ import annotations

import base64
import logging
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.exceptions import ApiException

import src.backend.admin.langfuse_secret as _mod
from src.backend.admin.langfuse_secret import (
    get_langfuse_secret_key,
    invalidate_langfuse_secret_key_cache,
    langfuse_secret_key_is_set,
    set_langfuse_secret_key,
)
from src.backend.ingestion.secret_resolver import SecretResolverUnavailable

# ── Constants ─────────────────────────────────────────────────────────────────

_SECRET_NAME = "dataspoke-langfuse-secret"
_SECRET_KEY = "secret_key"
_NAMESPACE = "dataspoke"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _b64(value: str) -> str:
    """Base64-encode a string the same way Kubernetes stores Secret data."""
    return base64.b64encode(value.encode()).decode()


def _fake_secret(key_value: str | None) -> MagicMock:
    """Build a mock secret object whose data dict reflects key_value.

    key_value=None simulates data=None (secret exists but no data).
    key_value="" simulates data={} (key absent).
    Any other string is base64-encoded and stored under _SECRET_KEY.
    """
    secret = MagicMock()
    if key_value is None:
        secret.data = None
    elif key_value == "":
        secret.data = {}
    else:
        secret.data = {_SECRET_KEY: _b64(key_value)}
    return secret


def _api_exception(status: int) -> ApiException:
    """Build an ApiException with the given HTTP status code."""
    exc = ApiException(status=status)
    exc.status = status
    return exc


def _make_core(read_return=None, read_side_effect=None) -> MagicMock:
    """Build a fake CoreV1Api with configurable read_namespaced_secret."""
    core = MagicMock()
    if read_side_effect is not None:
        core.read_namespaced_secret.side_effect = read_side_effect
    else:
        core.read_namespaced_secret.return_value = read_return
    return core


# ── Fixture: flush cache before and after each test ──────────────────────────


@pytest.fixture(autouse=True)
def flush_cache():
    """Evict the module-level cache before and after every test."""
    invalidate_langfuse_secret_key_cache()
    yield
    invalidate_langfuse_secret_key_cache()


# ── 14. Secret name/key constants ────────────────────────────────────────────


def test_secret_name_constant() -> None:
    """_SECRET_NAME must be 'dataspoke-langfuse-secret'.

    spec: plan/scalable-beaming-hamster.md §Backend — hardcoded name guards security boundary.
    """
    assert _mod._SECRET_NAME == "dataspoke-langfuse-secret", (
        "_SECRET_NAME must be 'dataspoke-langfuse-secret' — cannot be parameterized."
    )


def test_secret_key_constant() -> None:
    """_SECRET_KEY must be 'secret_key'.

    spec: plan/scalable-beaming-hamster.md §Backend — Langfuse secret stored under 'secret_key'.
    """
    assert _mod._SECRET_KEY == "secret_key", (
        "_SECRET_KEY must be 'secret_key' for Langfuse secret."
    )


# ── 1. get — in-cluster read ──────────────────────────────────────────────────


def test_get_returns_base64_decoded_value() -> None:
    """get_langfuse_secret_key() decodes the Kubernetes Secret's base64-encoded secret_key.

    spec: plan/scalable-beaming-hamster.md §Backend — key stored as base64 in dataspoke-langfuse-secret.
    """
    secret = _fake_secret("sk-langfuse-key")
    core = _make_core(read_return=secret)

    with patch("src.backend.admin.langfuse_secret._require_client", return_value=(core, _NAMESPACE)):
        result = get_langfuse_secret_key()

    assert result == "sk-langfuse-key"
    core.read_namespaced_secret.assert_called_once_with(
        name=_SECRET_NAME, namespace=_NAMESPACE
    )


# ── 2. get — cache hit within TTL ─────────────────────────────────────────────


def test_get_cache_hit_does_not_re_read() -> None:
    """A second call within TTL returns the cached value without re-reading the Secret.

    spec: plan/scalable-beaming-hamster.md §Backend — short-TTL process cache.
    """
    secret = _fake_secret("cached-key")
    core = _make_core(read_return=secret)

    with patch("src.backend.admin.langfuse_secret._require_client", return_value=(core, _NAMESPACE)):
        first = get_langfuse_secret_key()
        second = get_langfuse_secret_key()

    assert first == "cached-key"
    assert second == "cached-key"
    assert core.read_namespaced_secret.call_count == 1, (
        "read_namespaced_secret must be called exactly once within TTL"
    )


# ── 3. get — cache expiry forces re-read ─────────────────────────────────────


def test_get_re_reads_after_ttl_expires(monkeypatch) -> None:
    """Once the TTL has elapsed the next call re-reads the Secret.

    spec: plan/scalable-beaming-hamster.md §Backend — TTL-based cache; stale entry causes re-read.
    """
    import time as _time

    secret = _fake_secret("fresh-key")
    core = _make_core(read_return=secret)
    real_now = _time.monotonic()

    with patch("src.backend.admin.langfuse_secret._require_client", return_value=(core, _NAMESPACE)):
        get_langfuse_secret_key()
        first_count = core.read_namespaced_secret.call_count

        monkeypatch.setattr(_mod.time, "monotonic", lambda: real_now + _mod._TTL_SECONDS + 1.0)
        get_langfuse_secret_key()

    assert core.read_namespaced_secret.call_count > first_count, (
        "Cache entry must expire after TTL; expired entry must trigger a fresh Secret read"
    )


# ── 4. get — 404 returns "" and caches ───────────────────────────────────────


def test_get_404_returns_empty_string_and_caches() -> None:
    """read_namespaced_secret raises 404 → get returns "" and caches it.

    spec: plan/scalable-beaming-hamster.md §Backend — Secret absent → treat as unset; cache empty.
    """
    core = _make_core(read_side_effect=_api_exception(404))

    with patch("src.backend.admin.langfuse_secret._require_client", return_value=(core, _NAMESPACE)):
        result = get_langfuse_secret_key()
        assert result == "", "404 must yield empty string (secret unset)"

        result2 = get_langfuse_secret_key()
        assert result2 == ""

    assert core.read_namespaced_secret.call_count == 1, (
        "After a 404 the empty-string result must be cached"
    )


# ── 5. get — 403 fail-safe: returns "" but does NOT cache ────────────────────


def test_get_403_returns_empty_string_not_cached() -> None:
    """read_namespaced_secret raises 403 → returns "" WITHOUT caching.

    spec: plan/scalable-beaming-hamster.md §Backend — RBAC 403 → fail safe, do not cache.
    """
    core = _make_core(read_side_effect=_api_exception(403))

    with patch("src.backend.admin.langfuse_secret._require_client", return_value=(core, _NAMESPACE)):
        result = get_langfuse_secret_key()
        assert result == ""

        result2 = get_langfuse_secret_key()
        assert result2 == ""

    assert core.read_namespaced_secret.call_count == 2, (
        "403 result must NOT be cached — each subsequent call must re-attempt the read"
    )


def test_get_403_logs_warning(caplog) -> None:
    """The 403 path emits a warning log and does NOT log any previously-set secret_key.

    Procedure:
    1. Set secret_key to a known sentinel via a successful get (primes the cache).
    2. Invalidate the cache, then call get_langfuse_secret_key against a 403-returning k8s mock.
    3. Assert a warning was emitted AND the sentinel does NOT appear in any log record.

    spec: plan/scalable-beaming-hamster.md §Backend — plaintext secret is NEVER logged.
    """
    _SENTINEL = "plaintext-sentinel-lf-67890"

    # Step 1: prime the cache with the sentinel so it's known to the module.
    core_ok = _make_core(read_return=_fake_secret(_SENTINEL))
    with patch("src.backend.admin.langfuse_secret._require_client", return_value=(core_ok, _NAMESPACE)):
        get_langfuse_secret_key()

    # Step 2: invalidate cache, then induce a 403.
    invalidate_langfuse_secret_key_cache()

    core_403 = _make_core(read_side_effect=_api_exception(403))
    with patch("src.backend.admin.langfuse_secret._require_client", return_value=(core_403, _NAMESPACE)):
        with caplog.at_level(logging.WARNING, logger="src.backend.admin.langfuse_secret"):
            get_langfuse_secret_key()

    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warning_records, "A warning must be emitted on 403 RBAC denial"

    # Step 3: sentinel must never appear in any warning message.
    for record in warning_records:
        assert _SENTINEL not in record.getMessage(), (
            f"Plaintext secret_key '{_SENTINEL}' must never appear in any log record. "
            f"Offending message: {record.getMessage()!r}. "
            "spec: plan/scalable-beaming-hamster.md §Backend — plaintext secret never logged."
        )


# ── 6. get — other k8s error raises SecretResolverUnavailable ────────────────


def test_get_500_raises_secret_resolver_unavailable() -> None:
    """read_namespaced_secret raises ApiException(500) → SecretResolverUnavailable propagated.

    spec: plan/scalable-beaming-hamster.md §Backend — other k8s errors propagate as unavailable.
    """
    core = _make_core(read_side_effect=_api_exception(500))

    with patch("src.backend.admin.langfuse_secret._require_client", return_value=(core, _NAMESPACE)):
        with pytest.raises(SecretResolverUnavailable):
            get_langfuse_secret_key()


# ── 7. get — secret exists but key absent ────────────────────────────────────


def test_get_returns_empty_string_when_data_is_none() -> None:
    """Secret exists with data=None → treated as unset; returns ""."""
    secret = _fake_secret(None)
    core = _make_core(read_return=secret)

    with patch("src.backend.admin.langfuse_secret._require_client", return_value=(core, _NAMESPACE)):
        result = get_langfuse_secret_key()

    assert result == ""


def test_get_returns_empty_string_when_key_absent_from_data() -> None:
    """Secret exists with data={} (secret_key missing) → treated as unset; returns ""."""
    secret = _fake_secret("")  # data={}
    core = _make_core(read_return=secret)

    with patch("src.backend.admin.langfuse_secret._require_client", return_value=(core, _NAMESPACE)):
        result = get_langfuse_secret_key()

    assert result == ""


# ── 8. get — out-of-cluster fallback ─────────────────────────────────────────


def test_get_falls_back_to_settings_when_out_of_cluster(monkeypatch) -> None:
    """When _require_client raises SecretResolverUnavailable, returns settings.langfuse_secret_key.

    spec: plan/scalable-beaming-hamster.md §Backend — out-of-cluster fallback to env var (host-mode).
    """
    monkeypatch.setattr(
        "src.backend.admin.langfuse_secret.settings.langfuse_secret_key", "env-lf-secret"
    )

    with patch(
        "src.backend.admin.langfuse_secret._require_client",
        side_effect=SecretResolverUnavailable("out-of-cluster"),
    ):
        result = get_langfuse_secret_key()

    assert result == "env-lf-secret"


def test_get_out_of_cluster_fallback_does_not_cache(monkeypatch) -> None:
    """Out-of-cluster fallback is NOT cached.

    spec: plan/scalable-beaming-hamster.md §Backend — fallback does not cache (host-mode transient).
    """
    monkeypatch.setattr(
        "src.backend.admin.langfuse_secret.settings.langfuse_secret_key", "env-lf-secret"
    )

    call_count = 0

    def _require_side_effect():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise SecretResolverUnavailable("out-of-cluster")
        secret = _fake_secret("incluster-lf-key")
        core = _make_core(read_return=secret)
        return core, _NAMESPACE

    with patch("src.backend.admin.langfuse_secret._require_client", side_effect=_require_side_effect):
        first = get_langfuse_secret_key()
        second = get_langfuse_secret_key()

    assert first == "env-lf-secret"
    assert second == "incluster-lf-key"


# ── 9. set — create path ─────────────────────────────────────────────────────


def test_set_create_path_calls_create_with_base64_value() -> None:
    """set_langfuse_secret_key creates the Secret when it does not exist.

    spec: plan/scalable-beaming-hamster.md §Backend — create-or-patch write semantics.
    """
    core = MagicMock()
    core.read_namespaced_secret.side_effect = _api_exception(404)

    with patch("src.backend.admin.langfuse_secret._require_client", return_value=(core, _NAMESPACE)):
        set_langfuse_secret_key("new-lf-key")

    core.create_namespaced_secret.assert_called_once()
    core.patch_namespaced_secret.assert_not_called()

    created_body = core.create_namespaced_secret.call_args[1]["body"]
    # Subset check: verify the key/value we care about without pinning the full body shape.
    assert created_body.data[_SECRET_KEY] == _b64("new-lf-key"), (
        f"create_namespaced_secret body.data[{_SECRET_KEY!r}] must be base64('new-lf-key'). "
        "spec: plan/scalable-beaming-hamster.md §Backend — secret stored as base64 in K8s Secret."
    )


# ── 10. set — patch path ─────────────────────────────────────────────────────


def test_set_patch_path_calls_patch_with_correct_body() -> None:
    """set_langfuse_secret_key patches the Secret when it already exists.

    spec: plan/scalable-beaming-hamster.md §Backend — patch merges only the secret_key field.
    """
    existing_secret = _fake_secret("old-lf-key")
    core = MagicMock()
    core.read_namespaced_secret.return_value = existing_secret

    with patch("src.backend.admin.langfuse_secret._require_client", return_value=(core, _NAMESPACE)):
        set_langfuse_secret_key("updated-lf-key")

    core.patch_namespaced_secret.assert_called_once()
    core.create_namespaced_secret.assert_not_called()

    patch_body = core.patch_namespaced_secret.call_args[1]["body"]
    # Subset check: verify the key/value we care about without pinning the full body shape.
    assert patch_body["data"][_SECRET_KEY] == _b64("updated-lf-key"), (
        f"patch body['data'][{_SECRET_KEY!r}] must be base64('updated-lf-key'). "
        "spec: plan/scalable-beaming-hamster.md §Backend — patch merges only the secret_key field."
    )


# ── 11. set — out-of-cluster raises SecretResolverUnavailable ────────────────


def test_set_out_of_cluster_raises() -> None:
    """set_langfuse_secret_key propagates SecretResolverUnavailable when out-of-cluster.

    spec: plan/scalable-beaming-hamster.md §Backend — PATCH cannot persist without the cluster.
    """
    with patch(
        "src.backend.admin.langfuse_secret._require_client",
        side_effect=SecretResolverUnavailable("out-of-cluster"),
    ):
        with pytest.raises(SecretResolverUnavailable):
            set_langfuse_secret_key("any-key")


# ── 12. set — invalidates cache ───────────────────────────────────────────────


def test_set_invalidates_cache_so_next_get_re_reads() -> None:
    """After set, the next get re-reads the Secret (cache was invalidated).

    spec: plan/scalable-beaming-hamster.md §Backend — invalidates cache on success.
    """
    secret_v1 = _fake_secret("v1-key")
    core_v1 = _make_core(read_return=secret_v1)
    with patch("src.backend.admin.langfuse_secret._require_client", return_value=(core_v1, _NAMESPACE)):
        get_langfuse_secret_key()

    core_set = MagicMock()
    core_set.read_namespaced_secret.return_value = _fake_secret("v1-key")
    with patch("src.backend.admin.langfuse_secret._require_client", return_value=(core_set, _NAMESPACE)):
        set_langfuse_secret_key("v2-key")

    secret_v2 = _fake_secret("v2-key")
    core_v2 = _make_core(read_return=secret_v2)
    with patch("src.backend.admin.langfuse_secret._require_client", return_value=(core_v2, _NAMESPACE)):
        result = get_langfuse_secret_key()

    assert core_v2.read_namespaced_secret.call_count == 1
    assert result == "v2-key"


# ── 13. langfuse_secret_key_is_set ────────────────────────────────────────────


def test_langfuse_secret_key_is_set_true_when_present() -> None:
    """langfuse_secret_key_is_set returns True when the Secret contains a non-empty key.

    spec: plan/scalable-beaming-hamster.md §Backend — is_set used for is_configured predicate.
    """
    secret = _fake_secret("live-lf-key")
    core = _make_core(read_return=secret)

    with patch("src.backend.admin.langfuse_secret._require_client", return_value=(core, _NAMESPACE)):
        result = langfuse_secret_key_is_set()

    assert result is True


def test_langfuse_secret_key_is_set_false_when_absent() -> None:
    """langfuse_secret_key_is_set returns False when the Secret is absent (404).

    spec: plan/scalable-beaming-hamster.md §Backend — is_set false when unset.
    """
    core = _make_core(read_side_effect=_api_exception(404))

    with patch("src.backend.admin.langfuse_secret._require_client", return_value=(core, _NAMESPACE)):
        result = langfuse_secret_key_is_set()

    assert result is False


def test_langfuse_secret_key_is_set_false_when_key_absent() -> None:
    """langfuse_secret_key_is_set returns False when Secret exists but key is missing.

    spec: plan/scalable-beaming-hamster.md §Backend — is_set false when key absent.
    """
    secret = _fake_secret("")  # data={}
    core = _make_core(read_return=secret)

    with patch("src.backend.admin.langfuse_secret._require_client", return_value=(core, _NAMESPACE)):
        result = langfuse_secret_key_is_set()

    assert result is False
