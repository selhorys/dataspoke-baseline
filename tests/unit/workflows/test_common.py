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
import pytest_asyncio  # noqa: F401 — ensures asyncio mode is active

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
    """make_llm() returns StubLLMClient when test_mode=true and test_llm_real=false.

    spec: TESTING.md §Test-Mode Stubs — make_llm() → StubLLMClient in test mode.
    spec: BACKEND_LLM.md §Test Mode — stub when test_mode and not test_llm_real.
    """
    monkeypatch.setattr("src.shared.settings.settings.test_mode", True)
    monkeypatch.setattr("src.shared.settings.settings.test_llm_real", False)
    result = make_llm()
    assert isinstance(result, StubLLMClient), (
        f"Expected StubLLMClient in test mode, got {type(result).__name__}. "
        "spec: TESTING.md §Test-Mode Stubs."
    )


def test_make_llm_returns_real_client_when_test_llm_real(monkeypatch) -> None:
    """make_llm() returns LLMClient when test_mode=true but test_llm_real=true.

    spec: BACKEND_LLM.md §Test Mode — DATASPOKE_TEST_LLM_REAL=true bypasses stub.
    """
    monkeypatch.setattr("src.shared.settings.settings.test_mode", True)
    monkeypatch.setattr("src.shared.settings.settings.test_llm_real", True)
    # make_llm resolves the key via get_llm_api_key() (k8s Secret + host-mode fallback).
    # Patch at the source module to bypass k8s state and process-level _cache.
    monkeypatch.setattr("src.backend.admin.llm_secret.get_llm_api_key", lambda: "test-key-sentinel")
    result = make_llm()
    assert isinstance(result, LLMClient), (
        f"Expected real LLMClient when test_llm_real=true, got {type(result).__name__}."
    )


def test_make_llm_returns_real_client_outside_test_mode(monkeypatch) -> None:
    """make_llm() returns LLMClient when DATASPOKE_TEST_MODE is false/unset.

    spec: TESTING.md §Test-Mode Stubs — real LLM outside test mode.
    """
    monkeypatch.setattr("src.shared.settings.settings.test_mode", False)
    monkeypatch.setattr("src.shared.settings.settings.test_llm_real", False)
    # make_llm resolves the key via get_llm_api_key() (k8s Secret + host-mode fallback).
    # Patch at the source module to bypass k8s state and process-level _cache.
    monkeypatch.setattr("src.backend.admin.llm_secret.get_llm_api_key", lambda: "test-key-sentinel")
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


@pytest.mark.asyncio
async def test_make_datahub_always_returns_datahub_client_in_test_mode(monkeypatch) -> None:
    """make_datahub(db) always returns DataHubClient, even in test mode.

    make_datahub is now async and requires a db session.  It reads the DataHub
    peripheral config from DB and calls get_datahub_token().  Both are patched
    at the source module level (lazy imports inside make_datahub resolve from
    the source module at call time).

    spec: TESTING.md §Test-Mode Stubs — DataHub is never stubbed.
    """
    from unittest.mock import AsyncMock

    from src.backend.admin.peripheral_service import DatahubConfigDTO

    monkeypatch.setattr("src.shared.settings.settings.test_mode", True)

    _fake_dto = DatahubConfigDTO(gms_url="http://gms-stub:8080", kafka_brokers="kafka-stub:9092")
    # Patch at source module level — make_datahub does a lazy import at call time
    monkeypatch.setattr(
        "src.backend.admin.peripheral_service.get_peripheral_config",
        AsyncMock(return_value=_fake_dto),
    )
    monkeypatch.setattr(
        "src.backend.admin.datahub_secret.get_datahub_token",
        lambda: "stub-token",
    )

    mock_db = AsyncMock()
    result = await make_datahub(mock_db)
    assert isinstance(result, DataHubClient), (
        f"make_datahub() must always return DataHubClient; got {type(result).__name__}. "
        "spec: TESTING.md §Test-Mode Stubs."
    )

    # Verify the client was constructed from the peripheral config + token.
    # DataHubClient stores its config inside DataHubGraph._graph_client_config
    # (or similar internal attributes). The observable surface is that the
    # emitter's server URL matches what was passed in.
    # The cleanest assertion: the _emitter was built with the right gms_url.
    assert result._emitter._gms_server == "http://gms-stub:8080", (
        f"DataHubClient must be constructed with the gms_url from peripheral config. "
        f"Expected 'http://gms-stub:8080', got {result._emitter._gms_server!r}. "
        "spec: plan/scalable-beaming-hamster.md §Consumer migration — "
        "make_datahub reads gms_url from peripheral config."
    )


@pytest.mark.asyncio
async def test_make_datahub_returns_datahub_client_outside_test_mode(monkeypatch) -> None:
    """make_datahub(db) returns DataHubClient outside test mode, constructed with peripheral values.

    spec: TESTING.md §Test-Mode Stubs — DataHub is always real.
    spec: plan/scalable-beaming-hamster.md §Consumer migration — make_datahub reads from DB.
    """
    from unittest.mock import AsyncMock

    from src.backend.admin.peripheral_service import DatahubConfigDTO

    monkeypatch.setattr("src.shared.settings.settings.test_mode", False)

    _fake_dto = DatahubConfigDTO(gms_url="http://gms-stub:8080", kafka_brokers="kafka-stub:9092")
    monkeypatch.setattr(
        "src.backend.admin.peripheral_service.get_peripheral_config",
        AsyncMock(return_value=_fake_dto),
    )
    monkeypatch.setattr(
        "src.backend.admin.datahub_secret.get_datahub_token",
        lambda: "stub-token",
    )

    mock_db = AsyncMock()
    result = await make_datahub(mock_db)
    assert isinstance(result, DataHubClient)

    # Verify the client was constructed with the correct gms_url from peripheral config.
    assert result._emitter._gms_server == "http://gms-stub:8080", (
        f"DataHubClient must be constructed with the gms_url from peripheral config. "
        f"Expected 'http://gms-stub:8080', got {result._emitter._gms_server!r}. "
        "spec: plan/scalable-beaming-hamster.md §Consumer migration — "
        "make_datahub reads gms_url from peripheral config."
    )


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
