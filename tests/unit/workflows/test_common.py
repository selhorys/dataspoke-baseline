"""Unit tests for workflow factory functions (_common.py).

Tests the factory stub behavior:
- With stub=True: make_llm_client() returns StubLLMClient,
  make_pgvector_manager() returns StubPgVectorManager, make_redis_client() returns StubRedisClient,
  make_notification_service() returns StubNotificationService.
- With stub=False (or default): factories return real clients.
- make_datahub() always returns a real DataHubClient regardless of stub flag.
- make_db_session() always returns a real session.
- urn_to_workflow_id produces a stable 12-char hex string from the same URN.

spec: src/workflows/_common.py — make_* factories accept stub= kwarg; DataHub and DB are always real.
"""

import pytest
import pytest_asyncio  # noqa: F401 — ensures asyncio mode is active

from src.workflows._common import (
    make_datahub,
    make_db_session,
    make_llm_client,
    make_notification_service,
    make_pgvector_manager,
    make_redis_client,
    urn_to_workflow_id,
)
from src.workflows._stubs import (
    StubLLMClient,
    StubNotificationService,
    StubPgVectorManager,
    StubRedisClient,
)
from src.shared.datahub.client import DataHubClient
from src.shared.cache.client import RedisClient
from src.shared.llm.client import LLMClient
from src.shared.vector.client import PgVectorManager


# ── make_llm_client with stub=True ────────────────────────────────────────────


def test_make_llm_client_returns_stub_when_stub_true() -> None:
    """make_llm_client() returns StubLLMClient when stub=True.

    spec: src/workflows/_common.py — make_llm_client(stub=True) → StubLLMClient.
    """
    result = make_llm_client(stub=True)
    assert isinstance(result, StubLLMClient), (
        f"Expected StubLLMClient when stub=True, got {type(result).__name__}. "
        "spec: src/workflows/_common.py — make_llm_client(stub=True) → StubLLMClient."
    )


def test_make_llm_client_returns_real_client_when_stub_false(monkeypatch) -> None:
    """make_llm_client() returns LLMClient when stub=False.

    spec: src/workflows/_common.py — make_llm_client(stub=False) → real LLMClient.
    """
    monkeypatch.setattr("src.backend.admin.llm_secret.get_llm_api_key", lambda: "test-key-sentinel")
    monkeypatch.setattr("src.backend.admin.langfuse_secret.get_langfuse_secret_key", lambda: None)
    result = make_llm_client(stub=False)
    assert isinstance(result, LLMClient), (
        f"Expected real LLMClient when stub=False, got {type(result).__name__}."
    )


def test_make_llm_client_defaults_to_real_client(monkeypatch) -> None:
    """make_llm_client() returns LLMClient when stub kwarg is omitted (default=False).

    spec: src/workflows/_common.py — Default False preserves prod-safe behavior.
    """
    monkeypatch.setattr("src.backend.admin.llm_secret.get_llm_api_key", lambda: "test-key-sentinel")
    monkeypatch.setattr("src.backend.admin.langfuse_secret.get_langfuse_secret_key", lambda: None)
    result = make_llm_client()
    assert isinstance(result, LLMClient), (
        f"Expected LLMClient by default, got {type(result).__name__}."
    )
    assert not isinstance(result, StubLLMClient), (
        "Default make_llm_client() must NOT return StubLLMClient; "
        "a StubLLMClient subclass refactor would silently break prod. "
        "spec: src/workflows/_common.py — Default False preserves prod-safe behavior."
    )


def test_make_redis_client_defaults_to_real_client() -> None:
    """make_redis_client() returns RedisClient when stub kwarg is omitted (default=False).

    Guards against a regression flipping the default stub=False → True, which would
    silently use StubRedisClient in production.

    spec: src/workflows/_common.py — Default False preserves prod-safe behavior.
    """
    result = make_redis_client()
    assert isinstance(result, RedisClient), (
        f"Expected RedisClient by default (stub=False), got {type(result).__name__}."
    )
    assert not isinstance(result, StubRedisClient), (
        "Default make_redis_client() must NOT return StubRedisClient; "
        "a StubRedisClient subclass refactor would silently break prod. "
        "spec: src/workflows/_common.py — Default False preserves prod-safe behavior."
    )


def test_make_pgvector_manager_defaults_to_real_client() -> None:
    """make_pgvector_manager() returns PgVectorManager when stub kwarg is omitted (default=False).

    Guards against a regression flipping the default stub=False → True, which would
    silently use StubPgVectorManager in production.

    spec: src/workflows/_common.py — Default False preserves prod-safe behavior.
    """
    result = make_pgvector_manager()
    assert isinstance(result, PgVectorManager), (
        f"Expected PgVectorManager by default (stub=False), got {type(result).__name__}."
    )
    assert not isinstance(result, StubPgVectorManager), (
        "Default make_pgvector_manager() must NOT return StubPgVectorManager; "
        "a StubPgVectorManager subclass refactor would silently break prod. "
        "spec: src/workflows/_common.py — Default False preserves prod-safe behavior."
    )


def test_make_notification_service_defaults_to_real_client() -> None:
    """make_notification_service() returns NotificationService when stub kwarg is omitted (default=False).

    Guards against a regression flipping the default stub=False → True, which would
    silently use StubNotificationService in production.

    spec: src/workflows/_common.py — Default False preserves prod-safe behavior.
    """
    from src.shared.notifications.service import NotificationService

    result = make_notification_service()
    assert isinstance(result, NotificationService), (
        f"Expected NotificationService by default (stub=False), got {type(result).__name__}."
    )
    assert not isinstance(result, StubNotificationService), (
        "Default make_notification_service() must NOT return StubNotificationService; "
        "a StubNotificationService subclass refactor would silently break prod. "
        "spec: src/workflows/_common.py — Default False preserves prod-safe behavior."
    )


# ── make_pgvector_manager ─────────────────────────────────────────────────────


def test_make_pgvector_manager_returns_stub_when_stub_true() -> None:
    """make_pgvector_manager() returns StubPgVectorManager when stub=True.

    spec: src/workflows/_common.py — make_pgvector_manager(stub=True) → StubPgVectorManager.
    """
    result = make_pgvector_manager(stub=True)
    assert isinstance(result, StubPgVectorManager), (
        f"Expected StubPgVectorManager when stub=True, got {type(result).__name__}."
    )


def test_make_pgvector_manager_returns_real_client_when_stub_false() -> None:
    """make_pgvector_manager() returns PgVectorManager when stub=False.

    spec: src/workflows/_common.py — make_pgvector_manager(stub=False) → real PgVectorManager.
    """
    result = make_pgvector_manager(stub=False)
    assert isinstance(result, PgVectorManager), (
        f"Expected PgVectorManager when stub=False, got {type(result).__name__}."
    )


# ── make_redis_client ─────────────────────────────────────────────────────────


def test_make_redis_client_returns_stub_when_stub_true() -> None:
    """make_redis_client() returns StubRedisClient when stub=True.

    spec: src/workflows/_common.py — make_redis_client(stub=True) → StubRedisClient.
    """
    result = make_redis_client(stub=True)
    assert isinstance(result, StubRedisClient), (
        f"Expected StubRedisClient when stub=True, got {type(result).__name__}."
    )


def test_make_redis_client_returns_real_client_when_stub_false() -> None:
    """make_redis_client() returns RedisClient when stub=False.

    spec: src/workflows/_common.py — make_redis_client(stub=False) → real RedisClient.
    """
    result = make_redis_client(stub=False)
    assert isinstance(result, RedisClient), (
        f"Expected RedisClient when stub=False, got {type(result).__name__}."
    )


# ── make_notification_service ─────────────────────────────────────────────────


def test_make_notification_service_returns_stub_when_stub_true() -> None:
    """make_notification_service() returns StubNotificationService when stub=True.

    spec: src/workflows/_common.py — make_notification_service(stub=True) → StubNotificationService.
    """
    result = make_notification_service(stub=True)
    assert isinstance(result, StubNotificationService), (
        f"Expected StubNotificationService when stub=True, got {type(result).__name__}."
    )


def test_make_notification_service_returns_real_service_when_stub_false() -> None:
    """make_notification_service() returns NotificationService when stub=False.

    spec: src/workflows/_common.py — make_notification_service(stub=False) → real NotificationService.
    """
    from src.shared.notifications.service import NotificationService

    result = make_notification_service(stub=False)
    assert isinstance(result, NotificationService), (
        f"Expected NotificationService when stub=False, got {type(result).__name__}."
    )


# ── make_datahub: always real ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_make_datahub_always_returns_datahub_client(monkeypatch) -> None:
    """make_datahub(db) always returns DataHubClient — DataHub is never stubbed.

    make_datahub is async and requires a db session.  It reads the DataHub
    peripheral config from DB and calls get_datahub_token().  Both are patched
    at the source module level (lazy imports inside make_datahub resolve from
    the source module at call time).

    spec: src/workflows/_common.py — DataHub is never stubbed; make_datahub always returns DataHubClient.
    """
    from unittest.mock import AsyncMock

    from src.backend.admin.peripheral_service import DatahubConfigDTO

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
        "spec: src/workflows/_common.py — DataHub is never stubbed."
    )

    assert result._emitter._gms_server == "http://gms-stub:8080", (
        f"DataHubClient must be constructed with the gms_url from peripheral config. "
        f"Expected 'http://gms-stub:8080', got {result._emitter._gms_server!r}. "
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
