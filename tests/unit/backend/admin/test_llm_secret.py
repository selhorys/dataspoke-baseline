"""Unit tests for src/backend/admin/llm_secret.py.

Covers the LLM API key accessor — get, set, cache, fallback, and security invariants.
All Kubernetes API calls are mocked; no cluster is needed.

Concerns covered:

1.  get — in-cluster read: secret present with data.api_key → base64-decoded value returned.
2.  get — cache hit within TTL: second call does not re-read the secret.
3.  get — cache expiry: advancing monotonic clock past TTL causes a re-read.
4.  get — 404: secret absent → returns "" and caches "" (second call skips re-read).
5.  get — 403 fail-safe: RBAC denied → returns "", does NOT cache, re-reads on next call.
6.  get — other k8s error (500): raises SecretResolverUnavailable.
7.  get — secret exists but key absent (empty data dict or data=None) → returns "".
8.  get — out-of-cluster fallback: _require_client raises → returns settings.llm_api_key
    without caching (subsequent in-cluster call still reads the secret).
9.  set — create path: secret missing (404 on read) → create_namespaced_secret called
    with base64-encoded value; cache invalidated.
10. set — patch path: secret exists → patch_namespaced_secret called with correct body;
    other keys untouched; cache invalidated.
11. set — clears with "": set_llm_api_key("") writes base64(""); subsequent get returns "".
12. set — out-of-cluster raises SecretResolverUnavailable.
13. set — invalidates cache: prime cache via get, then set, then next get re-reads.
14. llm_api_key_is_set: True when non-empty, False when "".
15. Plaintext never logged: 403 warning log record contains no key value.

Spec traceability:
- spec/feature/BACKEND_LLM.md §LLM API key — base64 decode, TTL cache, 403 fail-safe,
  404 as unset, out-of-cluster fallback, create-or-patch write, plaintext never logged.
"""

from __future__ import annotations

import base64
import logging
from unittest.mock import MagicMock, call, patch

import pytest
from kubernetes.client.exceptions import ApiException

import src.backend.admin.llm_secret as _mod
from src.backend.admin.llm_secret import (
    get_llm_api_key,
    invalidate_llm_api_key_cache,
    llm_api_key_is_set,
    set_llm_api_key,
)
from src.backend.ingestion.secret_resolver import SecretResolverUnavailable

# ── Constants ─────────────────────────────────────────────────────────────────

_SECRET_NAME = "dataspoke-llm-secret"
_SECRET_KEY = "api_key"
_NAMESPACE = "dataspoke"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _b64(value: str) -> str:
    """Base64-encode a string the same way Kubernetes stores Secret data."""
    return base64.b64encode(value.encode()).decode()


def _fake_secret(api_key_value: str | None) -> MagicMock:
    """Build a mock secret object whose data dict reflects api_key_value.

    api_key_value=None simulates data=None (secret exists but no data).
    api_key_value="" simulates data={} (key absent).
    Any other string is base64-encoded and stored under _SECRET_KEY.
    """
    secret = MagicMock()
    if api_key_value is None:
        secret.data = None
    elif api_key_value == "":
        secret.data = {}
    else:
        secret.data = {_SECRET_KEY: _b64(api_key_value)}
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


# ── Fixture: always flush the cache before and after each test ────────────────


@pytest.fixture(autouse=True)
def flush_cache():
    """Evict the module-level cache before and after every test.

    Prevents cache state from leaking across tests in the same process.
    spec: task brief — autouse fixture calls invalidate_llm_api_key_cache().
    """
    invalidate_llm_api_key_cache()
    yield
    invalidate_llm_api_key_cache()


# ── 1. get — in-cluster read ──────────────────────────────────────────────────


def test_get_returns_base64_decoded_value() -> None:
    """get_llm_api_key() decodes the Kubernetes Secret's base64-encoded api_key.

    spec: BACKEND_LLM.md §LLM API key — key stored as base64 in dataspoke-llm-secret.
    """
    secret = _fake_secret("sk-xyz")
    core = _make_core(read_return=secret)

    with patch("src.backend.admin.llm_secret._require_client", return_value=(core, _NAMESPACE)):
        result = get_llm_api_key()

    assert result == "sk-xyz", (
        "get_llm_api_key must return the base64-decoded api_key value from the Secret"
    )
    core.read_namespaced_secret.assert_called_once_with(
        name=_SECRET_NAME, namespace=_NAMESPACE
    )


# ── 2. get — cache hit within TTL ─────────────────────────────────────────────


def test_get_cache_hit_does_not_re_read() -> None:
    """A second call within TTL returns the cached value without re-reading the Secret.

    spec: BACKEND_LLM.md §LLM API key — short-TTL process cache.
    """
    secret = _fake_secret("sk-cached")
    core = _make_core(read_return=secret)

    with patch("src.backend.admin.llm_secret._require_client", return_value=(core, _NAMESPACE)):
        first = get_llm_api_key()
        second = get_llm_api_key()

    assert first == "sk-cached"
    assert second == "sk-cached"
    assert core.read_namespaced_secret.call_count == 1, (
        "read_namespaced_secret must be called exactly once within TTL — "
        "second call must come from cache"
    )


# ── 3. get — cache expiry forces re-read ─────────────────────────────────────


def test_get_re_reads_after_ttl_expires(monkeypatch) -> None:
    """Once the TTL has elapsed the next call re-reads the Secret.

    spec: BACKEND_LLM.md §LLM API key — TTL-based cache; stale entry causes re-read.
    Technique: populate cache, then advance time.monotonic past _TTL_SECONDS.
    """
    import time as _time

    secret = _fake_secret("sk-fresh")
    core = _make_core(read_return=secret)
    real_now = _time.monotonic()

    with patch("src.backend.admin.llm_secret._require_client", return_value=(core, _NAMESPACE)):
        # First call — populates cache with expiry = real_now + TTL.
        get_llm_api_key()
        first_count = core.read_namespaced_secret.call_count  # 1

        # Move time past the TTL.
        monkeypatch.setattr(_mod.time, "monotonic", lambda: real_now + _mod._TTL_SECONDS + 1.0)

        # Second call — cache expired; must re-read.
        get_llm_api_key()

    assert core.read_namespaced_secret.call_count > first_count, (
        "Cache entry must expire after TTL; expired entry must trigger a fresh Secret read"
    )


# ── 4. get — 404: secret absent → "" cached ───────────────────────────────────


def test_get_404_returns_empty_string_and_caches() -> None:
    """read_namespaced_secret raises 404 → get returns "" and caches it.

    A second call within TTL must NOT re-read (the "" result is cached).

    spec: BACKEND_LLM.md §LLM API key — Secret/key absent → treat as unset; cache empty.
    """
    core = _make_core(read_side_effect=_api_exception(404))

    with patch("src.backend.admin.llm_secret._require_client", return_value=(core, _NAMESPACE)):
        result = get_llm_api_key()
        assert result == "", "404 must yield empty string (secret unset)"

        # Second call — "" should be returned from cache.
        result2 = get_llm_api_key()
        assert result2 == ""

    assert core.read_namespaced_secret.call_count == 1, (
        "After a 404, the empty-string result must be cached so subsequent calls "
        "do not re-read within TTL"
    )


# ── 5. get — 403 fail-safe: returns "" but does NOT cache ────────────────────


def test_get_403_returns_empty_string_not_cached() -> None:
    """read_namespaced_secret raises 403 → returns "" WITHOUT caching.

    The next call must attempt to re-read (not return from cache).

    spec: BACKEND_LLM.md §LLM API key — RBAC 403 → fail safe, do not cache.
    """
    core = _make_core(read_side_effect=_api_exception(403))

    with patch("src.backend.admin.llm_secret._require_client", return_value=(core, _NAMESPACE)):
        result = get_llm_api_key()
        assert result == "", "403 must yield empty string (fail-safe)"

        # Second call — because 403 is not cached, Secret is re-read.
        result2 = get_llm_api_key()
        assert result2 == ""

    assert core.read_namespaced_secret.call_count == 2, (
        "403 result must NOT be cached — each subsequent call must re-attempt the read"
    )


def test_get_403_logs_warning_without_key_value(caplog) -> None:
    """The 403 warning log must NOT contain the key value.

    spec: BACKEND_LLM.md §LLM API key — plaintext key is NEVER logged.
    """
    core = _make_core(read_side_effect=_api_exception(403))

    with patch("src.backend.admin.llm_secret._require_client", return_value=(core, _NAMESPACE)):
        with caplog.at_level(logging.WARNING, logger="src.backend.admin.llm_secret"):
            get_llm_api_key()

    # There must be at least one warning record from the 403 path.
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warning_records, "A warning must be emitted on 403"

    # The sentinel key value "sk-secret-value" must never appear in any log text.
    # (We use a controlled value that is distinct from anything the impl would invent.)
    key_sentinel = "sk-secret-value"
    for record in warning_records:
        assert key_sentinel not in record.getMessage(), (
            "Plaintext key value must never appear in any log record message"
        )
        extra_str = str(getattr(record, "__dict__", {}))
        assert key_sentinel not in extra_str, (
            "Plaintext key value must not appear in log record extra fields"
        )


# ── 6. get — other k8s error raises SecretResolverUnavailable ────────────────


def test_get_500_raises_secret_resolver_unavailable() -> None:
    """read_namespaced_secret raises ApiException(500) → SecretResolverUnavailable propagated.

    spec: BACKEND_LLM.md §LLM API key — other k8s errors propagate as unavailable.
    """
    core = _make_core(read_side_effect=_api_exception(500))

    with patch("src.backend.admin.llm_secret._require_client", return_value=(core, _NAMESPACE)):
        with pytest.raises(SecretResolverUnavailable):
            get_llm_api_key()


# ── 7. get — secret exists but key absent ────────────────────────────────────


def test_get_returns_empty_string_when_data_is_none() -> None:
    """Secret exists with data=None → treated as unset; returns "".

    spec: BACKEND_LLM.md §LLM API key — key absent → treat as unset.
    """
    secret = _fake_secret(None)  # data=None
    core = _make_core(read_return=secret)

    with patch("src.backend.admin.llm_secret._require_client", return_value=(core, _NAMESPACE)):
        result = get_llm_api_key()

    assert result == "", "data=None must be treated as unset (returns '')"


def test_get_returns_empty_string_when_key_absent_from_data() -> None:
    """Secret exists with data={} (api_key missing) → treated as unset; returns "".

    spec: BACKEND_LLM.md §LLM API key — key absent → treat as unset.
    """
    secret = _fake_secret("")  # data={}
    core = _make_core(read_return=secret)

    with patch("src.backend.admin.llm_secret._require_client", return_value=(core, _NAMESPACE)):
        result = get_llm_api_key()

    assert result == "", "Empty data dict must be treated as unset (returns '')"


# ── 8. get — out-of-cluster fallback ─────────────────────────────────────────


def test_get_falls_back_to_settings_when_out_of_cluster(monkeypatch) -> None:
    """When _require_client raises SecretResolverUnavailable, returns settings.llm_api_key.

    spec: BACKEND_LLM.md §LLM API key — out-of-cluster fallback to env var.
    """
    monkeypatch.setattr("src.backend.admin.llm_secret.settings.llm_api_key", "env-fallback-key")

    with patch(
        "src.backend.admin.llm_secret._require_client",
        side_effect=SecretResolverUnavailable("out-of-cluster"),
    ):
        result = get_llm_api_key()

    assert result == "env-fallback-key", (
        "Out-of-cluster fallback must return settings.llm_api_key sentinel"
    )


def test_get_out_of_cluster_fallback_does_not_cache(monkeypatch) -> None:
    """Out-of-cluster fallback is NOT cached — a subsequent in-cluster call re-reads.

    spec: BACKEND_LLM.md §LLM API key — fallback does not cache (host-mode transient).
    """
    monkeypatch.setattr("src.backend.admin.llm_secret.settings.llm_api_key", "env-fallback-key")

    call_count = 0

    def _require_side_effect():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise SecretResolverUnavailable("out-of-cluster")
        # Second call: succeed with an in-cluster client.
        secret = _fake_secret("sk-incluster")
        core = _make_core(read_return=secret)
        return core, _NAMESPACE

    with patch("src.backend.admin.llm_secret._require_client", side_effect=_require_side_effect):
        first = get_llm_api_key()   # fallback → "env-fallback-key", not cached
        second = get_llm_api_key()  # now in-cluster → reads Secret

    assert first == "env-fallback-key"
    assert second == "sk-incluster", (
        "After an out-of-cluster call the next in-cluster call must read the Secret "
        "because the fallback result was not cached"
    )


# ── 9. set — create path (secret missing) ────────────────────────────────────


def test_set_create_path_calls_create_with_base64_value() -> None:
    """set_llm_api_key creates the Secret when it does not exist.

    create_namespaced_secret must be called with data={api_key: base64(value)}.
    patch_namespaced_secret must NOT be called.
    Cache must be invalidated (next get re-reads).

    spec: BACKEND_LLM.md §LLM API key — create-or-patch write semantics.
    """
    core = MagicMock()
    # read raises 404 → Secret does not exist.
    core.read_namespaced_secret.side_effect = _api_exception(404)

    with patch("src.backend.admin.llm_secret._require_client", return_value=(core, _NAMESPACE)):
        set_llm_api_key("sk-new")

    core.create_namespaced_secret.assert_called_once()
    core.patch_namespaced_secret.assert_not_called()

    # Verify the created secret body contains the base64-encoded key.
    created_body = core.create_namespaced_secret.call_args[1]["body"]
    assert created_body.data == {_SECRET_KEY: _b64("sk-new")}, (
        "create_namespaced_secret body.data must contain {api_key: base64('sk-new')}"
    )


def test_set_create_path_invalidates_cache() -> None:
    """set_llm_api_key (create path) invalidates the cache.

    Prime cache via get, then set (create), then verify next get re-reads.

    spec: BACKEND_LLM.md §LLM API key — PATCH invalidates cache so LLM call uses new key.
    """
    # Prime cache.
    secret_before = _fake_secret("sk-old")
    core_get = _make_core(read_return=secret_before)
    with patch("src.backend.admin.llm_secret._require_client", return_value=(core_get, _NAMESPACE)):
        assert get_llm_api_key() == "sk-old"

    # set (create path): 404 on read → create.
    core_set = MagicMock()
    core_set.read_namespaced_secret.side_effect = _api_exception(404)
    with patch("src.backend.admin.llm_secret._require_client", return_value=(core_set, _NAMESPACE)):
        set_llm_api_key("sk-new")

    # Next get must re-read, not return cached "sk-old".
    secret_after = _fake_secret("sk-new")
    core_after = _make_core(read_return=secret_after)
    with patch("src.backend.admin.llm_secret._require_client", return_value=(core_after, _NAMESPACE)):
        result = get_llm_api_key()

    assert result == "sk-new", (
        "Cache must be invalidated after set so next get reads the updated Secret"
    )
    assert core_after.read_namespaced_secret.call_count == 1, (
        "get after set must re-read — not return from cache"
    )


# ── 10. set — patch path (secret exists) ─────────────────────────────────────


def test_set_patch_path_calls_patch_with_correct_body() -> None:
    """set_llm_api_key patches the Secret when it already exists.

    patch_namespaced_secret must be called with body={"data": {api_key: base64(value)}}.
    create_namespaced_secret must NOT be called.

    spec: BACKEND_LLM.md §LLM API key — patch merges only the api_key field.
    """
    existing_secret = _fake_secret("sk-old")
    core = MagicMock()
    core.read_namespaced_secret.return_value = existing_secret

    with patch("src.backend.admin.llm_secret._require_client", return_value=(core, _NAMESPACE)):
        set_llm_api_key("sk-updated")

    core.patch_namespaced_secret.assert_called_once()
    core.create_namespaced_secret.assert_not_called()

    patch_call = core.patch_namespaced_secret.call_args
    patch_body = patch_call[1]["body"]
    assert patch_body == {"data": {_SECRET_KEY: _b64("sk-updated")}}, (
        "patch body must be {data: {api_key: base64('sk-updated')}} — "
        "merge patch preserves other Secret keys"
    )


def test_set_patch_path_does_not_touch_other_keys() -> None:
    """Patch body only sets data.api_key; it must not overwrite other Secret keys.

    The merge-patch body sent to Kubernetes should only contain the api_key entry
    so that any other keys in the same Secret are left intact by Kubernetes.

    spec: BACKEND_LLM.md §LLM API key — other keys untouched (merge-patch semantics).
    """
    existing_secret = MagicMock()
    existing_secret.data = {_SECRET_KEY: _b64("sk-old"), "other_key": _b64("some-value")}
    core = MagicMock()
    core.read_namespaced_secret.return_value = existing_secret

    with patch("src.backend.admin.llm_secret._require_client", return_value=(core, _NAMESPACE)):
        set_llm_api_key("sk-new")

    patch_body = core.patch_namespaced_secret.call_args[1]["body"]
    # The patch body must NOT include other_key.
    assert "other_key" not in patch_body.get("data", {}), (
        "Patch body must contain ONLY api_key — other keys must not be overwritten"
    )


# ── 11. set — clears with "" ──────────────────────────────────────────────────


def test_set_empty_string_clears_key() -> None:
    """set_llm_api_key("") writes base64("") to the Secret.

    A subsequent get (with mock reflecting the cleared state) must return "".

    spec: BACKEND_LLM.md §LLM API key — explicit "" clears the key.
    """
    core_set = MagicMock()
    # Secret exists — write takes the patch path.
    existing_secret = _fake_secret("sk-old")
    core_set.read_namespaced_secret.return_value = existing_secret

    with patch("src.backend.admin.llm_secret._require_client", return_value=(core_set, _NAMESPACE)):
        set_llm_api_key("")

    patch_body = core_set.patch_namespaced_secret.call_args[1]["body"]
    assert patch_body["data"][_SECRET_KEY] == _b64(""), (
        "set_llm_api_key('') must write base64('') to the Secret"
    )

    # Simulate next get: Secret now stores base64("") under api_key.
    # Reading base64("") back decodes to "".
    secret_cleared = MagicMock()
    secret_cleared.data = {_SECRET_KEY: _b64("")}
    core_get = _make_core(read_return=secret_cleared)
    with patch("src.backend.admin.llm_secret._require_client", return_value=(core_get, _NAMESPACE)):
        result = get_llm_api_key()

    assert result == "", "After clearing, get must return ''"


# ── 12. set — out-of-cluster raises SecretResolverUnavailable ────────────────


def test_set_out_of_cluster_raises() -> None:
    """set_llm_api_key propagates SecretResolverUnavailable when out-of-cluster.

    spec: BACKEND_LLM.md §LLM API key — PATCH cannot persist without the cluster.
    """
    with patch(
        "src.backend.admin.llm_secret._require_client",
        side_effect=SecretResolverUnavailable("out-of-cluster"),
    ):
        with pytest.raises(SecretResolverUnavailable):
            set_llm_api_key("sk-any")


# ── 13. set — invalidates cache ───────────────────────────────────────────────


def test_set_invalidates_cache_so_next_get_re_reads() -> None:
    """After set, the next get re-reads the Secret (call count increments).

    spec: BACKEND_LLM.md §LLM API key — invalidates cache on success.
    """
    # Prime cache via get.
    secret_v1 = _fake_secret("sk-v1")
    core_v1 = _make_core(read_return=secret_v1)
    with patch("src.backend.admin.llm_secret._require_client", return_value=(core_v1, _NAMESPACE)):
        get_llm_api_key()
    assert core_v1.read_namespaced_secret.call_count == 1

    # set (patch path).
    core_set = MagicMock()
    existing_secret = _fake_secret("sk-v1")
    core_set.read_namespaced_secret.return_value = existing_secret
    with patch("src.backend.admin.llm_secret._require_client", return_value=(core_set, _NAMESPACE)):
        set_llm_api_key("sk-v2")

    # Next get must re-read, not hit the old cache.
    secret_v2 = _fake_secret("sk-v2")
    core_v2 = _make_core(read_return=secret_v2)
    with patch("src.backend.admin.llm_secret._require_client", return_value=(core_v2, _NAMESPACE)):
        result = get_llm_api_key()

    assert core_v2.read_namespaced_secret.call_count == 1, (
        "After set, get must re-read the Secret (cache invalidated by set)"
    )
    assert result == "sk-v2"


# ── 14. llm_api_key_is_set ────────────────────────────────────────────────────


def test_llm_api_key_is_set_true_when_secret_has_key() -> None:
    """llm_api_key_is_set returns True when the Secret contains a non-empty api_key.

    Exercises the real get_llm_api_key → K8s read → base64-decode path.
    The K8s layer is mocked (CoreV1Api + _require_client); no cluster needed.

    spec: BACKEND_LLM.md §LLM API key — masked GET returns '********' when set.
    """
    secret = _fake_secret("sk-x")
    core = _make_core(read_return=secret)

    with patch("src.backend.admin.llm_secret._require_client", return_value=(core, _NAMESPACE)):
        result = llm_api_key_is_set()

    assert result is True, (
        "llm_api_key_is_set must return True when the Secret contains a non-empty api_key"
    )


def test_llm_api_key_is_set_false_when_secret_absent() -> None:
    """llm_api_key_is_set returns False when the Secret is absent (404).

    Exercises the real get_llm_api_key → K8s read → 404 → return "" path.
    The K8s layer is mocked; no cluster needed.

    spec: BACKEND_LLM.md §LLM API key — masked GET returns '' when unset.
    """
    core = _make_core(read_side_effect=_api_exception(404))

    with patch("src.backend.admin.llm_secret._require_client", return_value=(core, _NAMESPACE)):
        result = llm_api_key_is_set()

    assert result is False, (
        "llm_api_key_is_set must return False when the Secret is absent (404)"
    )


def test_llm_api_key_is_set_false_when_key_absent_from_data() -> None:
    """llm_api_key_is_set returns False when the Secret exists but api_key is missing.

    Exercises the real get_llm_api_key → K8s read → data={} → return "" path.

    spec: BACKEND_LLM.md §LLM API key — key absent → treat as unset.
    """
    secret = _fake_secret("")  # data={}; api_key key not present
    core = _make_core(read_return=secret)

    with patch("src.backend.admin.llm_secret._require_client", return_value=(core, _NAMESPACE)):
        result = llm_api_key_is_set()

    assert result is False, (
        "llm_api_key_is_set must return False when the Secret has no api_key entry"
    )


# ── 15. Plaintext never logged ────────────────────────────────────────────────


def test_403_warning_emitted_and_does_not_log_key_sentinel(caplog) -> None:
    """The 403 path emits a warning AND contains no key value in any captured log text.

    The 403 path never has the key value in scope, so this test confirms both that
    a warning IS emitted and that the sentinel is absent (defensive guard).

    spec: BACKEND_LLM.md §LLM API key — plaintext key is NEVER logged.
    """
    key_sentinel = "sk-ultra-secret-value"
    core = _make_core(read_side_effect=_api_exception(403))

    with patch("src.backend.admin.llm_secret._require_client", return_value=(core, _NAMESPACE)):
        with caplog.at_level(logging.WARNING, logger="src.backend.admin.llm_secret"):
            result = get_llm_api_key()

    assert result == ""

    # A warning must be emitted on the 403 path.
    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warning_records, "A warning must be emitted on 403 (RBAC denial)"

    # The sentinel must not appear in any log record.
    all_log_text = " ".join(r.getMessage() for r in caplog.records)
    assert key_sentinel not in all_log_text, (
        "Key sentinel must not appear in any log message on the 403 path"
    )
    for record in caplog.records:
        record_dict_str = str(vars(record))
        assert key_sentinel not in record_dict_str, (
            "Key sentinel must not appear in log record extra fields"
        )


def test_plaintext_not_logged_on_read_success_decode_path(caplog) -> None:
    """On the read-success/decode path the plaintext key must never appear in any log record.

    This is the real leak surface: an accidental logger.debug(f"...{decoded}") would
    expose the key. Drive the full read → base64-decode path with a controlled sentinel
    value and assert the sentinel is absent from all log output at DEBUG level.

    spec: BACKEND_LLM.md §LLM API key — plaintext key is NEVER logged.
    """
    key_sentinel = "sk-LEAK-SENTINEL-123"
    secret = _fake_secret(key_sentinel)
    core = _make_core(read_return=secret)

    with patch("src.backend.admin.llm_secret._require_client", return_value=(core, _NAMESPACE)):
        with caplog.at_level(logging.DEBUG, logger="src.backend.admin.llm_secret"):
            result = get_llm_api_key()

    # The accessor must have successfully decoded the sentinel value.
    assert result == key_sentinel, (
        "Sanity check: get_llm_api_key must return the decoded sentinel so we know "
        "the decode path ran"
    )

    # The decoded plaintext sentinel must NOT appear in any log record.
    for record in caplog.records:
        msg = record.getMessage()
        assert key_sentinel not in msg, (
            f"Plaintext key sentinel appeared in log message: {msg!r}. "
            "The decoded key must never be logged."
        )
        record_dict_str = str(vars(record))
        assert key_sentinel not in record_dict_str, (
            "Plaintext key sentinel must not appear in log record extra fields"
        )
