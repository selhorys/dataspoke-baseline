"""Unit tests for the Kubernetes-Secret-backed accessor modules.

The three admin secret accessors —
``src/backend/admin/{llm,datahub,langfuse}_secret.py`` — share one get/set/is_set
shape: a base64-decoding read behind a monotonic-TTL process cache, create-or-patch
write, and an ``is_set`` predicate, all over a hardcoded ``dataspoke-*-secret`` name
with the plaintext value never logged. This suite parametrizes those shared
behaviors over the three modules in one place, so a change to the common contract is
verified for every accessor from a single test body.

All Kubernetes API calls are mocked; no cluster is needed.

Adding a fourth accessor (e.g. ``smtp_secret`` in G4): append ONE ``SecretModule``
row to ``SECRET_MODULES`` below — every shared behavior is then exercised against it
automatically. Only add a new standalone test if the fourth module has a code path
the other three lack.

Spec traceability:
- spec/feature/BACKEND_LLM.md §LLM API key — base64 decode, short-TTL process cache,
  403 fail-safe, 404/absent-key as unset, create-or-patch write, masked GET, plaintext
  never logged. ``llm_secret`` is the spec-named accessor; ``datahub_secret`` and
  ``langfuse_secret`` mirror the same pattern for DataSpoke-owned Secrets.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Callable
from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest
from kubernetes.client.exceptions import ApiException

import src.backend.admin.datahub_secret as _datahub_mod
import src.backend.admin.langfuse_secret as _langfuse_mod
import src.backend.admin.llm_secret as _llm_mod
from src.shared.secrets import SecretResolverUnavailable

_NAMESPACE = "dataspoke"


# ── Module descriptor + registry ─────────────────────────────────────────────


@dataclass(frozen=True)
class SecretModule:
    """One secret-accessor module and its public surface, for parametrization."""

    id: str
    module: object
    get: Callable[[], str]
    set: Callable[[str], None]
    is_set: Callable[[], bool]
    invalidate: Callable[[], None]
    require_client_target: str
    secret_name: str
    secret_key: str
    logger_name: str


SECRET_MODULES: list[SecretModule] = [
    SecretModule(
        id="llm",
        module=_llm_mod,
        get=_llm_mod.get_llm_api_key,
        set=_llm_mod.set_llm_api_key,
        is_set=_llm_mod.llm_api_key_is_set,
        invalidate=_llm_mod.invalidate_llm_api_key_cache,
        require_client_target="src.backend.admin.llm_secret.require_k8s_client",
        secret_name="dataspoke-llm-secret",
        secret_key="api_key",
        logger_name="src.backend.admin.llm_secret",
    ),
    SecretModule(
        id="datahub",
        module=_datahub_mod,
        get=_datahub_mod.get_datahub_token,
        set=_datahub_mod.set_datahub_token,
        is_set=_datahub_mod.datahub_token_is_set,
        invalidate=_datahub_mod.invalidate_datahub_token_cache,
        require_client_target="src.backend.admin.datahub_secret.require_k8s_client",
        secret_name="dataspoke-datahub-secret",
        secret_key="token",
        logger_name="src.backend.admin.datahub_secret",
    ),
    SecretModule(
        id="langfuse",
        module=_langfuse_mod,
        get=_langfuse_mod.get_langfuse_secret_key,
        set=_langfuse_mod.set_langfuse_secret_key,
        is_set=_langfuse_mod.langfuse_secret_key_is_set,
        invalidate=_langfuse_mod.invalidate_langfuse_secret_key_cache,
        require_client_target="src.backend.admin.langfuse_secret.require_k8s_client",
        secret_name="dataspoke-langfuse-secret",
        secret_key="secret_key",
        logger_name="src.backend.admin.langfuse_secret",
    ),
    # G4: append the smtp_secret row here — no other change is needed.
]

_over_modules = pytest.mark.parametrize(
    "sm", SECRET_MODULES, ids=[m.id for m in SECRET_MODULES]
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _b64(value: str) -> str:
    """Base64-encode a string the same way Kubernetes stores Secret data."""
    return base64.b64encode(value.encode()).decode()


def _fake_secret(secret_key: str, value: str | None) -> MagicMock:
    """Build a mock secret whose data dict reflects value under ``secret_key``.

    value=None  → data=None  (secret exists but no data)
    value=""    → data={}    (key absent)
    otherwise   → data={secret_key: base64(value)}
    """
    secret = MagicMock()
    if value is None:
        secret.data = None
    elif value == "":
        secret.data = {}
    else:
        secret.data = {secret_key: _b64(value)}
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


# ── Fixture: flush every module cache before and after each test ──────────────


@pytest.fixture(autouse=True)
def flush_all_caches():
    """Evict every module-level cache before and after each test.

    Parametrized tests touch different modules; flushing all keeps cache state
    from leaking across tests or across parametrization rows.
    spec: BACKEND_LLM.md §LLM API key — short-TTL process cache is invalidatable.
    """
    for sm in SECRET_MODULES:
        sm.invalidate()
    yield
    for sm in SECRET_MODULES:
        sm.invalidate()


# ── Secret name / key constants (security boundary) ──────────────────────────


@_over_modules
def test_secret_name_and_key_constants(sm: SecretModule) -> None:
    """The hardcoded Secret name and key match the module's contract.

    The fixed name is the security boundary — it must not be request-parameterizable.

    spec: BACKEND_LLM.md §LLM API key — accessor targets the fixed
    ``dataspoke-<x>-secret`` name; the fixed target plus admin auth are the controls.
    """
    assert sm.module._SECRET_NAME == sm.secret_name, (
        f"_SECRET_NAME must be {sm.secret_name!r} (hardcoded security boundary)"
    )
    assert sm.module._SECRET_KEY == sm.secret_key, (
        f"_SECRET_KEY must be {sm.secret_key!r} for the {sm.id} secret"
    )


# ── get — in-cluster read ─────────────────────────────────────────────────────


@_over_modules
def test_get_returns_base64_decoded_value(sm: SecretModule) -> None:
    """get() decodes the Kubernetes Secret's base64-encoded value.

    spec: BACKEND_LLM.md §LLM API key — value stored as base64 in the K8s Secret.
    """
    core = _make_core(read_return=_fake_secret(sm.secret_key, "decoded-value"))

    with patch(sm.require_client_target, return_value=(core, _NAMESPACE)):
        result = sm.get()

    assert result == "decoded-value", "get must return the base64-decoded Secret value"
    core.read_namespaced_secret.assert_called_once_with(
        name=sm.secret_name, namespace=_NAMESPACE
    )


# ── get — cache hit within TTL ────────────────────────────────────────────────


@_over_modules
def test_get_cache_hit_does_not_re_read(sm: SecretModule) -> None:
    """A second call within TTL returns the cached value without re-reading.

    spec: BACKEND_LLM.md §LLM API key — short-TTL process cache.
    """
    core = _make_core(read_return=_fake_secret(sm.secret_key, "cached-value"))

    with patch(sm.require_client_target, return_value=(core, _NAMESPACE)):
        first = sm.get()
        second = sm.get()

    assert first == "cached-value"
    assert second == "cached-value"
    assert core.read_namespaced_secret.call_count == 1, (
        "read_namespaced_secret must be called exactly once within TTL — "
        "second call must come from cache"
    )


# ── get — cache expiry forces re-read ─────────────────────────────────────────


@_over_modules
def test_get_re_reads_after_ttl_expires(sm: SecretModule, monkeypatch) -> None:
    """Once the TTL has elapsed the next call re-reads the Secret.

    spec: BACKEND_LLM.md §LLM API key — TTL-based cache; stale entry causes re-read.
    Technique: populate cache, then advance time.monotonic past _TTL_SECONDS.
    """
    import time as _time

    core = _make_core(read_return=_fake_secret(sm.secret_key, "fresh-value"))
    real_now = _time.monotonic()

    with patch(sm.require_client_target, return_value=(core, _NAMESPACE)):
        sm.get()
        first_count = core.read_namespaced_secret.call_count  # 1

        monkeypatch.setattr(
            sm.module.time,
            "monotonic",
            lambda: real_now + sm.module._TTL_SECONDS + 1.0,
        )

        sm.get()

    assert core.read_namespaced_secret.call_count > first_count, (
        "Cache entry must expire after TTL; expired entry must trigger a fresh read"
    )


# ── get — 404: secret absent → "" cached ──────────────────────────────────────


@_over_modules
def test_get_404_returns_empty_string_and_caches(sm: SecretModule) -> None:
    """read_namespaced_secret raises 404 → get returns "" and caches it.

    A second call within TTL must NOT re-read (the "" result is cached).

    spec: BACKEND_LLM.md §LLM API key — Secret/key absent → treat as unset; cache empty.
    """
    core = _make_core(read_side_effect=_api_exception(404))

    with patch(sm.require_client_target, return_value=(core, _NAMESPACE)):
        result = sm.get()
        assert result == "", "404 must yield empty string (secret unset)"

        result2 = sm.get()
        assert result2 == ""

    assert core.read_namespaced_secret.call_count == 1, (
        "After a 404, the empty-string result must be cached so subsequent calls "
        "do not re-read within TTL"
    )


# ── get — 403 fail-safe: returns "" but does NOT cache ────────────────────────


@_over_modules
def test_get_403_returns_empty_string_not_cached(sm: SecretModule) -> None:
    """read_namespaced_secret raises 403 → returns "" WITHOUT caching.

    The next call must attempt to re-read (not return from cache).

    spec: BACKEND_LLM.md §LLM API key — RBAC 403 → fail safe, do not cache.
    """
    core = _make_core(read_side_effect=_api_exception(403))

    with patch(sm.require_client_target, return_value=(core, _NAMESPACE)):
        result = sm.get()
        assert result == "", "403 must yield empty string (fail-safe)"

        result2 = sm.get()
        assert result2 == ""

    assert core.read_namespaced_secret.call_count == 2, (
        "403 result must NOT be cached — each subsequent call must re-attempt the read"
    )


@_over_modules
def test_get_403_logs_warning_without_plaintext(sm: SecretModule, caplog) -> None:
    """The 403 path emits a warning AND never leaks a previously-set plaintext value.

    Prime the cache with a sentinel via a successful read, invalidate, then hit a
    403-returning core and assert (a) a warning IS emitted and (b) the sentinel is
    absent from every captured log record's message and extra fields.

    spec: BACKEND_LLM.md §LLM API key — plaintext value is NEVER logged.
    """
    sentinel = f"plaintext-sentinel-{sm.id}-12345"

    # Prime the cache with the sentinel so it is known to the module.
    core_ok = _make_core(read_return=_fake_secret(sm.secret_key, sentinel))
    with patch(sm.require_client_target, return_value=(core_ok, _NAMESPACE)):
        assert sm.get() == sentinel

    # Invalidate so the next call actually hits k8s (and the 403 branch).
    sm.invalidate()

    core_403 = _make_core(read_side_effect=_api_exception(403))
    with patch(sm.require_client_target, return_value=(core_403, _NAMESPACE)):
        with caplog.at_level(logging.WARNING, logger=sm.logger_name):
            result = sm.get()

    assert result == "", "403 must yield empty string (fail-safe)"

    warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warning_records, "A warning must be emitted on 403 (RBAC denial)"

    for record in warning_records:
        assert sentinel not in record.getMessage(), (
            f"Plaintext value {sentinel!r} must never appear in a log message. "
            f"Offending: {record.getMessage()!r}"
        )
        assert sentinel not in str(vars(record)), (
            "Plaintext value must not appear in log record extra fields"
        )


@_over_modules
def test_plaintext_not_logged_on_read_success_decode_path(
    sm: SecretModule, caplog
) -> None:
    """On the read-success/decode path the plaintext value must never be logged.

    This is the real leak surface: an accidental logger.debug(f"...{decoded}") would
    expose the value. Drive the full read → base64-decode path with a controlled
    sentinel and assert it is absent from all log output at DEBUG level.

    spec: BACKEND_LLM.md §LLM API key — plaintext value is NEVER logged.
    """
    sentinel = f"LEAK-SENTINEL-{sm.id}-123"
    core = _make_core(read_return=_fake_secret(sm.secret_key, sentinel))

    with patch(sm.require_client_target, return_value=(core, _NAMESPACE)):
        with caplog.at_level(logging.DEBUG, logger=sm.logger_name):
            result = sm.get()

    # Backstop: the accessor must have decoded the sentinel, so the decode path ran.
    assert result == sentinel, (
        "Sanity check: get must return the decoded sentinel so we know decode ran"
    )

    for record in caplog.records:
        assert sentinel not in record.getMessage(), (
            f"Plaintext sentinel appeared in log message: {record.getMessage()!r}"
        )
        assert sentinel not in str(vars(record)), (
            "Plaintext sentinel must not appear in log record extra fields"
        )


# ── get — other k8s error raises SecretResolverUnavailable ────────────────────


@_over_modules
def test_get_500_raises_resolver_unavailable(sm: SecretModule) -> None:
    """read_namespaced_secret raises ApiException(500) → SecretResolverUnavailable.

    spec: BACKEND_LLM.md §LLM API key — other k8s errors propagate as unavailable.
    """
    core = _make_core(read_side_effect=_api_exception(500))

    with patch(sm.require_client_target, return_value=(core, _NAMESPACE)):
        with pytest.raises(SecretResolverUnavailable):
            sm.get()


# ── get — secret exists but key absent ────────────────────────────────────────


@_over_modules
def test_get_returns_empty_string_when_data_is_none(sm: SecretModule) -> None:
    """Secret exists with data=None → treated as unset; returns "".

    spec: BACKEND_LLM.md §LLM API key — key absent → treat as unset.
    """
    core = _make_core(read_return=_fake_secret(sm.secret_key, None))  # data=None

    with patch(sm.require_client_target, return_value=(core, _NAMESPACE)):
        result = sm.get()

    assert result == "", "data=None must be treated as unset (returns '')"


@_over_modules
def test_get_returns_empty_string_when_key_absent_from_data(sm: SecretModule) -> None:
    """Secret exists with data={} (key missing) → treated as unset; returns "".

    spec: BACKEND_LLM.md §LLM API key — key absent → treat as unset.
    """
    core = _make_core(read_return=_fake_secret(sm.secret_key, ""))  # data={}

    with patch(sm.require_client_target, return_value=(core, _NAMESPACE)):
        result = sm.get()

    assert result == "", "Empty data dict must be treated as unset (returns '')"


# ── set — create path (secret missing) ────────────────────────────────────────


@_over_modules
def test_set_create_path_calls_create_with_base64_value(sm: SecretModule) -> None:
    """set() creates the Secret when it does not exist.

    create_namespaced_secret must be called with data[secret_key]=base64(value);
    patch_namespaced_secret must NOT be called.

    spec: BACKEND_LLM.md §LLM API key — create-or-patch write semantics.
    """
    core = MagicMock()
    core.read_namespaced_secret.side_effect = _api_exception(404)  # missing → create

    with patch(sm.require_client_target, return_value=(core, _NAMESPACE)):
        sm.set("new-value")

    core.create_namespaced_secret.assert_called_once()
    core.patch_namespaced_secret.assert_not_called()

    created_body = core.create_namespaced_secret.call_args[1]["body"]
    assert created_body.data[sm.secret_key] == _b64("new-value"), (
        f"create body.data[{sm.secret_key!r}] must be base64('new-value')"
    )


@_over_modules
def test_set_create_path_invalidates_cache(sm: SecretModule) -> None:
    """set() (create path) invalidates the cache so the next get re-reads.

    spec: BACKEND_LLM.md §LLM API key — write invalidates the cache.
    """
    # Prime the cache.
    core_get = _make_core(read_return=_fake_secret(sm.secret_key, "old"))
    with patch(sm.require_client_target, return_value=(core_get, _NAMESPACE)):
        assert sm.get() == "old"

    # set (create path): 404 on read → create.
    core_set = MagicMock()
    core_set.read_namespaced_secret.side_effect = _api_exception(404)
    with patch(sm.require_client_target, return_value=(core_set, _NAMESPACE)):
        sm.set("new")

    # Next get must re-read, not return cached "old".
    core_after = _make_core(read_return=_fake_secret(sm.secret_key, "new"))
    with patch(sm.require_client_target, return_value=(core_after, _NAMESPACE)):
        result = sm.get()

    assert result == "new", "Cache must be invalidated after set so next get re-reads"
    assert core_after.read_namespaced_secret.call_count == 1, (
        "get after set must re-read — not return from cache"
    )


# ── set — patch path (secret exists) ──────────────────────────────────────────


@_over_modules
def test_set_patch_path_calls_patch_with_correct_body(sm: SecretModule) -> None:
    """set() patches the Secret when it already exists.

    patch_namespaced_secret must carry data[secret_key]=base64(value);
    create_namespaced_secret must NOT be called.

    spec: BACKEND_LLM.md §LLM API key — patch merges only the target field.
    """
    core = MagicMock()
    core.read_namespaced_secret.return_value = _fake_secret(sm.secret_key, "old")

    with patch(sm.require_client_target, return_value=(core, _NAMESPACE)):
        sm.set("updated")

    core.patch_namespaced_secret.assert_called_once()
    core.create_namespaced_secret.assert_not_called()

    patch_body = core.patch_namespaced_secret.call_args[1]["body"]
    assert patch_body["data"][sm.secret_key] == _b64("updated"), (
        f"patch body['data'][{sm.secret_key!r}] must be base64('updated')"
    )


@_over_modules
def test_set_patch_path_does_not_touch_other_keys(sm: SecretModule) -> None:
    """The merge-patch body sets ONLY the target key, leaving other Secret keys intact.

    spec: BACKEND_LLM.md §LLM API key — patch merges only the target field.
    """
    existing = MagicMock()
    existing.data = {sm.secret_key: _b64("old"), "other_key": _b64("some-value")}
    core = MagicMock()
    core.read_namespaced_secret.return_value = existing

    with patch(sm.require_client_target, return_value=(core, _NAMESPACE)):
        sm.set("new")

    patch_body = core.patch_namespaced_secret.call_args[1]["body"]
    assert "other_key" not in patch_body.get("data", {}), (
        "Patch body must contain ONLY the target key — other keys must not be overwritten"
    )


# ── set — clears with "" ──────────────────────────────────────────────────────


@_over_modules
def test_set_empty_string_clears_key(sm: SecretModule) -> None:
    """set("") writes base64("") to the Secret; a subsequent get returns "".

    spec: BACKEND_LLM.md §LLM API key — explicit "" clears the value.
    """
    core_set = MagicMock()
    core_set.read_namespaced_secret.return_value = _fake_secret(sm.secret_key, "old")

    with patch(sm.require_client_target, return_value=(core_set, _NAMESPACE)):
        sm.set("")

    patch_body = core_set.patch_namespaced_secret.call_args[1]["body"]
    assert patch_body["data"][sm.secret_key] == _b64(""), (
        "set('') must write base64('') to the Secret"
    )

    # Reading base64("") back decodes to "".
    cleared = MagicMock()
    cleared.data = {sm.secret_key: _b64("")}
    core_get = _make_core(read_return=cleared)
    with patch(sm.require_client_target, return_value=(core_get, _NAMESPACE)):
        assert sm.get() == "", "After clearing, get must return ''"


# ── set — k8s client init failure raises SecretResolverUnavailable ────────────


@_over_modules
def test_set_out_of_cluster_raises(sm: SecretModule) -> None:
    """set() propagates SecretResolverUnavailable on k8s client init failure.

    spec: BACKEND_LLM.md §LLM API key — PATCH cannot persist without the cluster.
    """
    with patch(
        sm.require_client_target,
        side_effect=SecretResolverUnavailable("out-of-cluster"),
    ):
        with pytest.raises(SecretResolverUnavailable):
            sm.set("any-value")


# ── set — invalidates cache (patch path) ──────────────────────────────────────


@_over_modules
def test_set_invalidates_cache_so_next_get_re_reads(sm: SecretModule) -> None:
    """After set (patch path), the next get re-reads the Secret (cache invalidated).

    spec: BACKEND_LLM.md §LLM API key — write invalidates the cache.
    """
    core_v1 = _make_core(read_return=_fake_secret(sm.secret_key, "v1"))
    with patch(sm.require_client_target, return_value=(core_v1, _NAMESPACE)):
        sm.get()
    assert core_v1.read_namespaced_secret.call_count == 1

    core_set = MagicMock()
    core_set.read_namespaced_secret.return_value = _fake_secret(sm.secret_key, "v1")
    with patch(sm.require_client_target, return_value=(core_set, _NAMESPACE)):
        sm.set("v2")

    core_v2 = _make_core(read_return=_fake_secret(sm.secret_key, "v2"))
    with patch(sm.require_client_target, return_value=(core_v2, _NAMESPACE)):
        result = sm.get()

    assert core_v2.read_namespaced_secret.call_count == 1, (
        "After set, get must re-read the Secret (cache invalidated by set)"
    )
    assert result == "v2"


# ── is_set predicate ──────────────────────────────────────────────────────────


@_over_modules
def test_is_set_true_when_present(sm: SecretModule) -> None:
    """is_set() returns True when the Secret contains a non-empty value.

    spec: BACKEND_LLM.md §LLM API key — masked GET returns '********' when set.
    """
    core = _make_core(read_return=_fake_secret(sm.secret_key, "live-value"))

    with patch(sm.require_client_target, return_value=(core, _NAMESPACE)):
        assert sm.is_set() is True


@_over_modules
def test_is_set_false_when_absent(sm: SecretModule) -> None:
    """is_set() returns False when the Secret is absent (404).

    spec: BACKEND_LLM.md §LLM API key — masked GET returns '' when unset.
    """
    core = _make_core(read_side_effect=_api_exception(404))

    with patch(sm.require_client_target, return_value=(core, _NAMESPACE)):
        assert sm.is_set() is False


@_over_modules
def test_is_set_false_when_key_absent(sm: SecretModule) -> None:
    """is_set() returns False when the Secret exists but the key is missing.

    spec: BACKEND_LLM.md §LLM API key — key absent → treat as unset.
    """
    core = _make_core(read_return=_fake_secret(sm.secret_key, ""))  # data={}

    with patch(sm.require_client_target, return_value=(core, _NAMESPACE)):
        assert sm.is_set() is False
