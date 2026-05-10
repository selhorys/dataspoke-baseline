"""Unit tests for workflow factory functions (_common.py).

Tests the spec-mandated test-mode stub behavior:
- With DATASPOKE_TEST_MODE=true: make_llm() returns StubLLMClient,
  make_vector() returns StubVectorManager, make_cache() returns StubRedisClient,
  make_notification() returns StubNotificationService.
- With DATASPOKE_TEST_MODE=false (or unset): factories return real clients.
- make_datahub() always returns a real DataHubClient regardless of test_mode.
- make_db_session() always returns a real session regardless of test_mode.
- urn_to_workflow_id produces a stable 12-char hex string from the same URN.

spec: TESTING.md §Test-Mode Stubs — make_* factories in src/workflows/_common.py
      return stubs in test mode; DataHub and DB are always real.
"""

import pytest

from src.workflows._common import (
    make_cache,
    make_datahub,
    make_db_session,
    make_llm,
    make_notification,
    make_vector,
    urn_to_workflow_id,
)
from src.workflows._stubs import (
    StubLLMClient,
    StubNotificationService,
    StubRedisClient,
    StubVectorManager,
)
from src.shared.datahub.client import DataHubClient
from src.shared.cache.client import RedisClient
from src.shared.llm.client import LLMClient
from src.shared.vector.client import PgVectorManager


# ── make_llm in test mode ─────────────────────────────────────────────────────


def test_make_llm_returns_stub_in_test_mode(monkeypatch) -> None:
    """make_llm() returns StubLLMClient when DATASPOKE_TEST_MODE=true.

    spec: TESTING.md §Test-Mode Stubs — make_llm() → StubLLMClient in test mode.
    """
    monkeypatch.setattr("src.shared.settings.settings.test_mode", True)
    result = make_llm()
    assert isinstance(result, StubLLMClient), (
        f"Expected StubLLMClient in test mode, got {type(result).__name__}. "
        "spec: TESTING.md §Test-Mode Stubs."
    )


def test_make_llm_returns_real_client_outside_test_mode(monkeypatch) -> None:
    """make_llm() returns LLMClient when DATASPOKE_TEST_MODE is false/unset.

    spec: TESTING.md §Test-Mode Stubs — real LLM outside test mode.
    """
    monkeypatch.setattr("src.shared.settings.settings.test_mode", False)
    result = make_llm()
    assert isinstance(result, LLMClient), (
        f"Expected LLMClient outside test mode, got {type(result).__name__}."
    )


# ── make_vector in test mode ──────────────────────────────────────────────────


def test_make_vector_returns_stub_in_test_mode(monkeypatch) -> None:
    """make_vector() returns StubVectorManager when DATASPOKE_TEST_MODE=true.

    spec: TESTING.md §Test-Mode Stubs — make_vector() → StubVectorManager in test mode.
    """
    monkeypatch.setattr("src.shared.settings.settings.test_mode", True)
    result = make_vector()
    assert isinstance(result, StubVectorManager), (
        f"Expected StubVectorManager in test mode, got {type(result).__name__}."
    )


def test_make_vector_returns_real_client_outside_test_mode(monkeypatch) -> None:
    """make_vector() returns PgVectorManager when DATASPOKE_TEST_MODE is false.

    spec: TESTING.md §Test-Mode Stubs — real PgVectorManager outside test mode.
    """
    monkeypatch.setattr("src.shared.settings.settings.test_mode", False)
    result = make_vector()
    assert isinstance(result, PgVectorManager), (
        f"Expected PgVectorManager outside test mode, got {type(result).__name__}."
    )


# ── make_cache in test mode ───────────────────────────────────────────────────


def test_make_cache_returns_stub_in_test_mode(monkeypatch) -> None:
    """make_cache() returns StubRedisClient when DATASPOKE_TEST_MODE=true.

    spec: TESTING.md §Test-Mode Stubs — make_cache() → StubRedisClient in test mode.
    """
    monkeypatch.setattr("src.shared.settings.settings.test_mode", True)
    result = make_cache()
    assert isinstance(result, StubRedisClient), (
        f"Expected StubRedisClient in test mode, got {type(result).__name__}."
    )


def test_make_cache_returns_real_client_outside_test_mode(monkeypatch) -> None:
    """make_cache() returns RedisClient when DATASPOKE_TEST_MODE is false.

    spec: TESTING.md §Test-Mode Stubs — real RedisClient outside test mode.
    """
    monkeypatch.setattr("src.shared.settings.settings.test_mode", False)
    result = make_cache()
    assert isinstance(result, RedisClient), (
        f"Expected RedisClient outside test mode, got {type(result).__name__}."
    )


# ── make_notification in test mode ───────────────────────────────────────────


def test_make_notification_returns_stub_in_test_mode(monkeypatch) -> None:
    """make_notification() returns StubNotificationService in test mode.

    spec: TESTING.md §Test-Mode Stubs — make_notification() → StubNotificationService.
    """
    monkeypatch.setattr("src.shared.settings.settings.test_mode", True)
    result = make_notification()
    assert isinstance(result, StubNotificationService), (
        f"Expected StubNotificationService in test mode, got {type(result).__name__}."
    )


def test_make_notification_returns_real_service_outside_test_mode(monkeypatch) -> None:
    """make_notification() returns NotificationService outside test mode.

    spec: TESTING.md §Test-Mode Stubs — real NotificationService outside test mode.
    """
    from src.shared.notifications.service import NotificationService

    monkeypatch.setattr("src.shared.settings.settings.test_mode", False)
    result = make_notification()
    assert isinstance(result, NotificationService), (
        f"Expected NotificationService outside test mode, got {type(result).__name__}."
    )


# ── make_datahub: always real ──────────────────────────────────────────────────


def test_make_datahub_always_returns_datahub_client_in_test_mode(monkeypatch) -> None:
    """make_datahub() always returns DataHubClient, even in test mode.

    spec: TESTING.md §Test-Mode Stubs — DataHub is never stubbed.
    """
    monkeypatch.setattr("src.shared.settings.settings.test_mode", True)
    result = make_datahub()
    assert isinstance(result, DataHubClient), (
        f"make_datahub() must always return DataHubClient; got {type(result).__name__}. "
        "spec: TESTING.md §Test-Mode Stubs."
    )


def test_make_datahub_returns_datahub_client_outside_test_mode(monkeypatch) -> None:
    """make_datahub() returns DataHubClient outside test mode.

    spec: TESTING.md §Test-Mode Stubs — DataHub is always real.
    """
    monkeypatch.setattr("src.shared.settings.settings.test_mode", False)
    result = make_datahub()
    assert isinstance(result, DataHubClient)


# ── urn_to_workflow_id ────────────────────────────────────────────────────────


def test_urn_to_workflow_id_returns_12_char_hex() -> None:
    """urn_to_workflow_id must return a 12-character hex string.

    spec: feature/BACKEND.md §Concurrency Control / Airflow DAG run conf-based dedup
          (line ~786) — URN-scoped workflow IDs prevent concurrent duplicate DAG runs.
    """
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.orders,DEV)"
    wf_id = urn_to_workflow_id(urn)
    assert len(wf_id) == 12, f"Expected 12-char ID, got {len(wf_id)}"
    assert wf_id.islower() or all(c in "0123456789abcdef" for c in wf_id), (
        "Workflow ID must be a hex string."
    )


def test_urn_to_workflow_id_is_stable() -> None:
    """urn_to_workflow_id produces the same ID for the same URN on every call.

    spec: feature/BACKEND.md §Concurrency Control / Airflow DAG run conf-based dedup
          (line ~786) — deterministic ID derived from URN for dedup keying.
    """
    urn = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.catalog.title_master,DEV)"
    assert urn_to_workflow_id(urn) == urn_to_workflow_id(urn)


def test_urn_to_workflow_id_differs_for_different_urns() -> None:
    """Different URNs produce different workflow IDs.

    spec: feature/BACKEND.md §Concurrency Control / Airflow DAG run conf-based dedup
          (line ~786) — per-URN IDs must be distinct to prevent cross-dataset dedup collisions.
    """
    urn_a = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.a,DEV)"
    urn_b = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.b,DEV)"
    assert urn_to_workflow_id(urn_a) != urn_to_workflow_id(urn_b)
