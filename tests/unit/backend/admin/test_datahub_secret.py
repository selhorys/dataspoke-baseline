"""Unit tests for src/backend/admin/datahub_secret.py.

Covers the DataHub token accessor — get, set, cache, and security invariants.
All Kubernetes API calls are mocked; no cluster is needed.

Concerns covered:

1.  get — in-cluster read: secret present with data.token → base64-decoded value returned.
2.  get — cache hit within TTL: second call does not re-read the secret.
3.  get — cache expiry: advancing monotonic clock past TTL causes a re-read.
4.  get — 404: secret absent → returns "" and caches "" (second call skips re-read).
5.  get — 403 fail-safe: RBAC denied → returns "", does NOT cache, re-reads on next call.
6.  get — other k8s error (500): raises SecretResolverUnavailable.
7.  get — secret exists but key absent (empty data dict or data=None) → returns "".
8.  set — create path: secret missing (404 on read) → create_namespaced_secret called
    with base64-encoded value; cache invalidated.
9.  set — patch path: secret exists → patch_namespaced_secret called with correct body.
10. set — k8s client init failure raises SecretResolverUnavailable.
11. set — invalidates cache: prime cache via get, then set, then next get re-reads.
12. datahub_token_is_set: True when non-empty, False when "".
13. Secret name and key constants: _SECRET_NAME="dataspoke-datahub-secret", _SECRET_KEY="token".

Spec traceability:
- plan/scalable-beaming-hamster.md §Backend — datahub_secret mirrors llm_secret pattern.
- src/backend/admin/datahub_secret.py — public surface.
"""

from __future__ import annotations

import base64
import logging
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.exceptions import ApiException

import src.backend.admin.datahub_secret as _mod
from src.backend.admin.datahub_secret import (
    datahub_token_is_set,
    get_datahub_token,
    invalidate_datahub_token_cache,
    set_datahub_token,
)
from src.backend.ingestion.secret_resolver import SecretResolverUnavailable

# ── Constants ─────────────────────────────────────────────────────────────────

_SECRET_NAME = "dataspoke-datahub-secret"
_SECRET_KEY = "token"
_NAMESPACE = "dataspoke"


# ── Helpers ───────────────────────────────────────────────────────────────────


def _b64(value: str) -> str:
    """Base64-encode a string the same way Kubernetes stores Secret data."""
    return base64.b64encode(value.encode()).decode()


def _fake_secret(token_value: str | None) -> MagicMock:
    """Build a mock secret object whose data dict reflects token_value.

    token_value=None simulates data=None (secret exists but no data).
    token_value="" simulates data={} (key absent).
    Any other string is base64-encoded and stored under _SECRET_KEY.
    """
    secret = MagicMock()
    if token_value is None:
        secret.data = None
    elif token_value == "":
        secret.data = {}
    else:
        secret.data = {_SECRET_KEY: _b64(token_value)}
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
    """Evict the module-level cache before and after every test.

    Prevents cache state from leaking across tests in the same process.
    """
    invalidate_datahub_token_cache()
    yield
    invalidate_datahub_token_cache()


# ── 13. Secret name/key constants ────────────────────────────────────────────


def test_secret_name_constant() -> None:
    """_SECRET_NAME must be 'dataspoke-datahub-secret'.

    spec: plan/scalable-beaming-hamster.md §Backend — hardcoded name guards security boundary.
    """
    assert _mod._SECRET_NAME == "dataspoke-datahub-secret", (
        "_SECRET_NAME must be 'dataspoke-datahub-secret' — cannot be parameterized."
    )


def test_secret_key_constant() -> None:
    """_SECRET_KEY must be 'token'.

    spec: plan/scalable-beaming-hamster.md §Backend — DataHub token stored under 'token' key.
    """
    assert _mod._SECRET_KEY == "token", (
        "_SECRET_KEY must be 'token' for DataHub secret."
    )


# ── 1. get — in-cluster read ──────────────────────────────────────────────────


def test_get_returns_base64_decoded_value() -> None:
    """get_datahub_token() decodes the Kubernetes Secret's base64-encoded token.

    spec: plan/scalable-beaming-hamster.md §Backend — token stored as base64 in dataspoke-datahub-secret.
    """
    secret = _fake_secret("my-datahub-token")
    core = _make_core(read_return=secret)

    with patch("src.backend.admin.datahub_secret._require_client", return_value=(core, _NAMESPACE)):
        result = get_datahub_token()

    assert result == "my-datahub-token"
    core.read_namespaced_secret.assert_called_once_with(
        name=_SECRET_NAME, namespace=_NAMESPACE
    )


# ── 2. get — cache hit within TTL ─────────────────────────────────────────────


def test_get_cache_hit_does_not_re_read() -> None:
    """A second call within TTL returns the cached value without re-reading the Secret.

    spec: plan/scalable-beaming-hamster.md §Backend — short-TTL process cache.
    """
    secret = _fake_secret("cached-token")
    core = _make_core(read_return=secret)

    with patch("src.backend.admin.datahub_secret._require_client", return_value=(core, _NAMESPACE)):
        first = get_datahub_token()
        second = get_datahub_token()

    assert first == "cached-token"
    assert second == "cached-token"
    assert core.read_namespaced_secret.call_count == 1, (
        "read_namespaced_secret must be called exactly once within TTL"
    )


# ── 3. get — cache expiry forces re-read ─────────────────────────────────────


def test_get_re_reads_after_ttl_expires(monkeypatch) -> None:
    """Once the TTL has elapsed the next call re-reads the Secret.

    spec: plan/scalable-beaming-hamster.md §Backend — TTL-based cache; stale entry causes re-read.
    """
    import time as _time

    secret = _fake_secret("fresh-token")
    core = _make_core(read_return=secret)
    real_now = _time.monotonic()

    with patch("src.backend.admin.datahub_secret._require_client", return_value=(core, _NAMESPACE)):
        get_datahub_token()
        first_count = core.read_namespaced_secret.call_count

        monkeypatch.setattr(_mod.time, "monotonic", lambda: real_now + _mod._TTL_SECONDS + 1.0)
        get_datahub_token()

    assert core.read_namespaced_secret.call_count > first_count, (
        "Cache entry must expire after TTL; expired entry must trigger a fresh Secret read"
    )


# ── 4. get — 404 returns "" and caches ───────────────────────────────────────


def test_get_404_returns_empty_string_and_caches() -> None:
    """read_namespaced_secret raises 404 → get returns "" and caches it.

    spec: plan/scalable-beaming-hamster.md §Backend — Secret absent → treat as unset; cache empty.
    """
    core = _make_core(read_side_effect=_api_exception(404))

    with patch("src.backend.admin.datahub_secret._require_client", return_value=(core, _NAMESPACE)):
        result = get_datahub_token()
        assert result == "", "404 must yield empty string (secret unset)"

        result2 = get_datahub_token()
        assert result2 == ""

    assert core.read_namespaced_secret.call_count == 1, (
        "After a 404 the empty-string result must be cached so subsequent calls skip re-read"
    )


# ── 5. get — 403 fail-safe: returns "" but does NOT cache ────────────────────


def test_get_403_returns_empty_string_not_cached() -> None:
    """read_namespaced_secret raises 403 → returns "" WITHOUT caching.

    spec: plan/scalable-beaming-hamster.md §Backend — RBAC 403 → fail safe, do not cache.
    """
    core = _make_core(read_side_effect=_api_exception(403))

    with patch("src.backend.admin.datahub_secret._require_client", return_value=(core, _NAMESPACE)):
        result = get_datahub_token()
        assert result == "", "403 must yield empty string (fail-safe)"

        result2 = get_datahub_token()
        assert result2 == ""

    assert core.read_namespaced_secret.call_count == 2, (
        "403 result must NOT be cached — each subsequent call must re-attempt the read"
    )


def test_get_403_logs_warning(caplog) -> None:
    """The 403 path emits a warning log and does NOT log any previously-set token.

    Procedure:
    1. Set token to a known sentinel via set_datahub_token (mocked k8s write).
    2. Prime the cache by calling get_datahub_token successfully.
    3. Invalidate the cache, then call get_datahub_token against a 403-returning k8s mock.
    4. Assert a warning was emitted AND the sentinel does NOT appear in any log record.

    spec: plan/scalable-beaming-hamster.md §Backend — plaintext token is NEVER logged.
    """
    _SENTINEL = "plaintext-sentinel-12345"

    # Step 1: seed the sentinel into the cache via a successful read.
    core_ok = _make_core(read_return=_fake_secret(_SENTINEL))
    with patch("src.backend.admin.datahub_secret._require_client", return_value=(core_ok, _NAMESPACE)):
        get_datahub_token()

    # Step 2: invalidate so we actually hit k8s on the next call.
    invalidate_datahub_token_cache()

    # Step 3: now call with a 403-returning core; capture warning logs.
    core_403 = _make_core(read_side_effect=_api_exception(403))
    with patch("src.backend.admin.datahub_secret._require_client", return_value=(core_403, _NAMESPACE)):
        with caplog.at_level(logging.WARNING, logger="src.backend.admin.datahub_secret"):
            get_datahub_token()

    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warning_records, "A warning must be emitted on 403 RBAC denial"

    # Step 4: assert the sentinel does NOT appear in any warning message.
    for record in warning_records:
        assert _SENTINEL not in record.getMessage(), (
            f"Plaintext token '{_SENTINEL}' must never appear in any log record. "
            f"Offending message: {record.getMessage()!r}. "
            "spec: plan/scalable-beaming-hamster.md §Backend — plaintext token never logged."
        )


# ── 6. get — other k8s error raises SecretResolverUnavailable ────────────────


def test_get_500_raises_secret_resolver_unavailable() -> None:
    """read_namespaced_secret raises ApiException(500) → SecretResolverUnavailable propagated.

    spec: plan/scalable-beaming-hamster.md §Backend — other k8s errors propagate as unavailable.
    """
    core = _make_core(read_side_effect=_api_exception(500))

    with patch("src.backend.admin.datahub_secret._require_client", return_value=(core, _NAMESPACE)):
        with pytest.raises(SecretResolverUnavailable):
            get_datahub_token()


# ── 7. get — secret exists but key absent ────────────────────────────────────


def test_get_returns_empty_string_when_data_is_none() -> None:
    """Secret exists with data=None → treated as unset; returns ""."""
    secret = _fake_secret(None)
    core = _make_core(read_return=secret)

    with patch("src.backend.admin.datahub_secret._require_client", return_value=(core, _NAMESPACE)):
        result = get_datahub_token()

    assert result == "", "data=None must be treated as unset (returns '')"


def test_get_returns_empty_string_when_key_absent_from_data() -> None:
    """Secret exists with data={} (token missing) → treated as unset; returns ""."""
    secret = _fake_secret("")  # data={}
    core = _make_core(read_return=secret)

    with patch("src.backend.admin.datahub_secret._require_client", return_value=(core, _NAMESPACE)):
        result = get_datahub_token()

    assert result == "", "Empty data dict must be treated as unset (returns '')"


# ── 8. set — create path ─────────────────────────────────────────────────────


def test_set_create_path_calls_create_with_base64_value() -> None:
    """set_datahub_token creates the Secret when it does not exist.

    spec: plan/scalable-beaming-hamster.md §Backend — create-or-patch write semantics.
    """
    core = MagicMock()
    core.read_namespaced_secret.side_effect = _api_exception(404)

    with patch("src.backend.admin.datahub_secret._require_client", return_value=(core, _NAMESPACE)):
        set_datahub_token("new-token")

    core.create_namespaced_secret.assert_called_once()
    core.patch_namespaced_secret.assert_not_called()

    created_body = core.create_namespaced_secret.call_args[1]["body"]
    # Verify the token value is correctly base64-encoded in the Secret data.
    # Use subset check to avoid pinning the body shape (metadata, kind, etc.).
    assert created_body.data[_SECRET_KEY] == _b64("new-token"), (
        f"create_namespaced_secret body.data[{_SECRET_KEY!r}] must be base64('new-token'). "
        "spec: plan/scalable-beaming-hamster.md §Backend — token stored as base64 in K8s Secret."
    )


# ── 9. set — patch path ──────────────────────────────────────────────────────


def test_set_patch_path_calls_patch_with_correct_body() -> None:
    """set_datahub_token patches the Secret when it already exists.

    spec: plan/scalable-beaming-hamster.md §Backend — patch merges only the token field.
    """
    existing_secret = _fake_secret("old-token")
    core = MagicMock()
    core.read_namespaced_secret.return_value = existing_secret

    with patch("src.backend.admin.datahub_secret._require_client", return_value=(core, _NAMESPACE)):
        set_datahub_token("updated-token")

    core.patch_namespaced_secret.assert_called_once()
    core.create_namespaced_secret.assert_not_called()

    patch_call = core.patch_namespaced_secret.call_args
    patch_body = patch_call[1]["body"]
    # Verify the token value is correctly base64-encoded in the patch body.
    # Use subset check: assert the key/value we care about, not the entire dict shape.
    assert patch_body["data"][_SECRET_KEY] == _b64("updated-token"), (
        f"patch body['data'][{_SECRET_KEY!r}] must be base64('updated-token'). "
        "spec: plan/scalable-beaming-hamster.md §Backend — patch merges only the token field."
    )


# ── 10. set — k8s client init failure raises SecretResolverUnavailable ───────


def test_set_out_of_cluster_raises() -> None:
    """set_datahub_token propagates SecretResolverUnavailable on k8s client init failure.

    spec: plan/scalable-beaming-hamster.md §Backend — PATCH cannot persist when k8s client is unavailable.
    """
    with patch(
        "src.backend.admin.datahub_secret._require_client",
        side_effect=SecretResolverUnavailable("out-of-cluster"),
    ):
        with pytest.raises(SecretResolverUnavailable):
            set_datahub_token("any-token")


# ── 11. set — invalidates cache ───────────────────────────────────────────────


def test_set_invalidates_cache_so_next_get_re_reads() -> None:
    """After set, the next get re-reads the Secret (cache was invalidated).

    spec: plan/scalable-beaming-hamster.md §Backend — invalidates cache on success.
    """
    # Prime cache.
    secret_v1 = _fake_secret("v1-token")
    core_v1 = _make_core(read_return=secret_v1)
    with patch("src.backend.admin.datahub_secret._require_client", return_value=(core_v1, _NAMESPACE)):
        get_datahub_token()
    assert core_v1.read_namespaced_secret.call_count == 1

    # set (patch path).
    core_set = MagicMock()
    core_set.read_namespaced_secret.return_value = _fake_secret("v1-token")
    with patch("src.backend.admin.datahub_secret._require_client", return_value=(core_set, _NAMESPACE)):
        set_datahub_token("v2-token")

    # Next get must re-read, not hit the old cache.
    secret_v2 = _fake_secret("v2-token")
    core_v2 = _make_core(read_return=secret_v2)
    with patch("src.backend.admin.datahub_secret._require_client", return_value=(core_v2, _NAMESPACE)):
        result = get_datahub_token()

    assert core_v2.read_namespaced_secret.call_count == 1, (
        "After set, get must re-read the Secret (cache invalidated)"
    )
    assert result == "v2-token"


# ── 12. datahub_token_is_set ─────────────────────────────────────────────────


def test_datahub_token_is_set_true_when_present() -> None:
    """datahub_token_is_set returns True when the Secret contains a non-empty token.

    spec: plan/scalable-beaming-hamster.md §Backend — is_set used for is_configured predicate.
    """
    secret = _fake_secret("live-token")
    core = _make_core(read_return=secret)

    with patch("src.backend.admin.datahub_secret._require_client", return_value=(core, _NAMESPACE)):
        result = datahub_token_is_set()

    assert result is True


def test_datahub_token_is_set_false_when_absent() -> None:
    """datahub_token_is_set returns False when the Secret is absent (404).

    spec: plan/scalable-beaming-hamster.md §Backend — is_set false when unset.
    """
    core = _make_core(read_side_effect=_api_exception(404))

    with patch("src.backend.admin.datahub_secret._require_client", return_value=(core, _NAMESPACE)):
        result = datahub_token_is_set()

    assert result is False


def test_datahub_token_is_set_false_when_key_absent() -> None:
    """datahub_token_is_set returns False when the Secret exists but token key is missing.

    spec: plan/scalable-beaming-hamster.md §Backend — is_set false when key absent.
    """
    secret = _fake_secret("")  # data={}
    core = _make_core(read_return=secret)

    with patch("src.backend.admin.datahub_secret._require_client", return_value=(core, _NAMESPACE)):
        result = datahub_token_is_set()

    assert result is False
