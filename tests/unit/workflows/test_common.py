"""Unit tests for workflow factory functions (_common.py).

Tests the factory stub behavior:
- With stub=True: make_llm_client() returns StubLLMClient,
  make_pgvector_manager() returns StubPgVectorManager, make_redis_client() returns StubRedisClient,
  make_notification_service() returns StubNotificationService.
- With stub=False (or default): factories return real clients.
- make_datahub() always returns a real DataHubClient regardless of stub flag.
- make_db_session() always returns a real session.
- urn_to_workflow_id produces a stable 12-char hex string from the same URN.

spec: src/workflows/_common.py — make_* factories accept stub= kwarg;
DataHub and DB are always real.
"""

import pytest
import pytest_asyncio  # noqa: F401 — ensures asyncio mode is active

from src.shared.cache.client import RedisClient
from src.shared.datahub.client import DataHubClient
from src.shared.llm.client import LLMClient
from src.shared.vector.client import PgVectorManager
from src.workflows._common import (
    make_datahub,
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
    """make_notification_service() returns NotificationService when stub omitted (default=False).

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

    spec: src/workflows/_common.py — make_notification_service(stub=False) → real service.
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

    spec: src/workflows/_common.py — DataHub never stubbed; make_datahub returns DataHubClient.
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


# ── read_datahub_actor_urn: corpuser URN wiring ───────────────────────────────


@pytest.mark.asyncio
async def test_read_datahub_actor_urn_returns_configured_value(monkeypatch) -> None:
    """read_datahub_actor_urn returns the configured service_corpuser_urn when set.

    The DataHub peripheral's ``service_corpuser_urn`` is the corpuser actor
    DataSpoke writes as on emitted DataHub audit stamps (assertion ``lastUpdated``,
    ingestion run-event ``created``).  When configured, the reader must surface
    that exact URN.

    spec: spec/feature/BACKEND.md §Active-custom run pipeline — DataSpoke stamps
        the audit actor with the peripheral's configured ``service_corpuser_urn``
        (line ~276 / ~527).
    spec: spec/API.md §/admin/peripherals/datahub — ``service_corpuser_urn`` names
        the corpuser actor DataSpoke writes as.
    """
    from unittest.mock import AsyncMock

    from src.backend.admin.peripheral_service import DatahubConfigDTO
    from src.workflows._common import read_datahub_actor_urn

    configured = DatahubConfigDTO(
        gms_url="http://gms:8080",
        kafka_brokers="kafka:9092",
        service_corpuser_urn="urn:li:corpuser:imazon-svc",
        default_env="PROD",
    )
    monkeypatch.setattr(
        "src.backend.admin.peripheral_service.get_peripheral_config",
        AsyncMock(return_value=configured),
    )

    result = await read_datahub_actor_urn(AsyncMock())

    assert result == "urn:li:corpuser:imazon-svc", (
        "read_datahub_actor_urn must return the configured service_corpuser_urn. "
        "spec: spec/feature/BACKEND.md §Active-custom run pipeline."
    )


@pytest.mark.asyncio
async def test_read_datahub_actor_urn_falls_back_when_unset(monkeypatch) -> None:
    """read_datahub_actor_urn falls back to urn:li:corpuser:dataspoke when unset.

    An empty ``service_corpuser_urn`` (or absent peripheral row) must resolve to
    the documented default actor URN, not an empty string.

    spec: spec/API.md §/admin/peripherals/datahub — unset rows read back factory
        default ``service_corpuser_urn`` → ``urn:li:corpuser:dataspoke``.
    spec: spec/feature/BACKEND.md §Active-custom run pipeline — defaulting to
        ``urn:li:corpuser:dataspoke`` when unset (line ~276).
    """
    from unittest.mock import AsyncMock

    from src.backend.admin.peripheral_service import DatahubConfigDTO
    from src.workflows._common import DEFAULT_DATAHUB_ACTOR_URN, read_datahub_actor_urn

    # Empty service_corpuser_urn on a configured row.
    unset = DatahubConfigDTO(gms_url="http://gms:8080", kafka_brokers="kafka:9092")
    monkeypatch.setattr(
        "src.backend.admin.peripheral_service.get_peripheral_config",
        AsyncMock(return_value=unset),
    )
    result_unset = await read_datahub_actor_urn(AsyncMock())
    assert result_unset == DEFAULT_DATAHUB_ACTOR_URN == "urn:li:corpuser:dataspoke", (
        "Empty service_corpuser_urn must fall back to urn:li:corpuser:dataspoke. "
        "spec: spec/API.md §/admin/peripherals/datahub — factory default."
    )

    # No peripheral row at all → same fallback.
    monkeypatch.setattr(
        "src.backend.admin.peripheral_service.get_peripheral_config",
        AsyncMock(return_value=None),
    )
    result_none = await read_datahub_actor_urn(AsyncMock())
    assert result_none == "urn:li:corpuser:dataspoke", (
        "Absent peripheral row must fall back to urn:li:corpuser:dataspoke."
    )


# ── read_datahub_default_env: ingestion fabric/env wiring ──────────────────────


@pytest.mark.asyncio
async def test_read_datahub_default_env_returns_configured_value(monkeypatch) -> None:
    """read_datahub_default_env returns the configured default_env when set.

    spec: spec/API.md §/admin/peripherals/datahub — ``default_env`` is the
        fabric/env applied when an ingestion recipe omits ``env``.
    spec: spec/feature/BACKEND.md §ingestion — recipe omits ``env`` → extractor
        falls back to the peripheral's configured ``default_env`` (line ~273).
    """
    from unittest.mock import AsyncMock

    from src.backend.admin.peripheral_service import DatahubConfigDTO
    from src.workflows._common import read_datahub_default_env

    configured = DatahubConfigDTO(
        gms_url="http://gms:8080",
        kafka_brokers="kafka:9092",
        default_env="PROD",
    )
    monkeypatch.setattr(
        "src.backend.admin.peripheral_service.get_peripheral_config",
        AsyncMock(return_value=configured),
    )

    result = await read_datahub_default_env(AsyncMock())

    assert result == "PROD", (
        "read_datahub_default_env must return the configured default_env. "
        "spec: spec/feature/BACKEND.md §ingestion — configured default_env."
    )


@pytest.mark.asyncio
async def test_read_datahub_default_env_falls_back_to_dev_when_unset(monkeypatch) -> None:
    """read_datahub_default_env falls back to 'DEV' when unset or unconfigured.

    spec: spec/API.md §/admin/peripherals/datahub — unset rows read back factory
        default ``default_env`` → ``DEV``.
    """
    from unittest.mock import AsyncMock

    from src.backend.admin.peripheral_service import DatahubConfigDTO
    from src.workflows._common import DEFAULT_DATAHUB_DEFAULT_ENV, read_datahub_default_env

    unset = DatahubConfigDTO(gms_url="http://gms:8080", kafka_brokers="kafka:9092")
    monkeypatch.setattr(
        "src.backend.admin.peripheral_service.get_peripheral_config",
        AsyncMock(return_value=unset),
    )
    assert await read_datahub_default_env(AsyncMock()) == DEFAULT_DATAHUB_DEFAULT_ENV == "DEV", (
        "Empty default_env must fall back to 'DEV'. "
        "spec: spec/API.md §/admin/peripherals/datahub — factory default."
    )

    monkeypatch.setattr(
        "src.backend.admin.peripheral_service.get_peripheral_config",
        AsyncMock(return_value=None),
    )
    assert await read_datahub_default_env(AsyncMock()) == "DEV", (
        "Absent peripheral row must fall back to 'DEV'."
    )


# ── read_langfuse_config: env tag + project surfaced ───────────────────────────


@pytest.mark.asyncio
async def test_read_langfuse_config_surfaces_project_and_environment_tag(monkeypatch) -> None:
    """read_langfuse_config returns (host, public_key, project_id, environment_tag).

    ``environment_tag`` drives the Langfuse trace ``environment`` and ``project_id``
    is surfaced as trace metadata.  Both non-secret fields must round-trip out of
    the peripheral DTO into the tuple the LLM-client factory consumes.

    spec: spec/API.md §/admin/peripherals/langfuse — ``project_id`` /
        ``environment_tag`` surfaced to LLM tracing.
    spec: spec/feature/BACKEND_LLM.md §Observability — trace ``environment`` =
        configured ``environment_tag``; ``metadata.project_id`` = configured
        ``project_id`` (lines ~399-400).
    """
    from unittest.mock import AsyncMock

    from src.backend.admin.peripheral_service import LangfuseConfigDTO
    from src.workflows._common import read_langfuse_config

    configured = LangfuseConfigDTO(
        host="http://langfuse:3000",
        public_key="pk-imazon",
        project_id="imazon-metadata",
        environment_tag="production",
    )
    monkeypatch.setattr(
        "src.backend.admin.peripheral_service.get_peripheral_config",
        AsyncMock(return_value=configured),
    )

    host, public_key, project_id, environment_tag = await read_langfuse_config(AsyncMock())

    assert host == "http://langfuse:3000"
    assert public_key == "pk-imazon"
    assert project_id == "imazon-metadata", (
        "read_langfuse_config must surface project_id as the 3rd tuple element. "
        "spec: spec/feature/BACKEND_LLM.md §Observability."
    )
    assert environment_tag == "production", (
        "read_langfuse_config must surface environment_tag as the 4th tuple element. "
        "spec: spec/feature/BACKEND_LLM.md §Observability."
    )


@pytest.mark.asyncio
async def test_read_langfuse_config_omits_blank_project_and_environment_tag(monkeypatch) -> None:
    """read_langfuse_config maps blank project_id/environment_tag to None (omit).

    Absence must omit the optional fields (None) so tracing still works without
    them, per the spec's "Both are optional — absence omits them" rule.

    spec: spec/feature/BACKEND_LLM.md §Observability — both optional; absence omits.
    """
    from unittest.mock import AsyncMock

    from src.backend.admin.peripheral_service import LangfuseConfigDTO
    from src.workflows._common import read_langfuse_config

    blank = LangfuseConfigDTO(host="http://langfuse:3000", public_key="pk")
    monkeypatch.setattr(
        "src.backend.admin.peripheral_service.get_peripheral_config",
        AsyncMock(return_value=blank),
    )

    host, public_key, project_id, environment_tag = await read_langfuse_config(AsyncMock())

    assert host == "http://langfuse:3000"
    assert public_key == "pk"
    assert project_id is None, "blank project_id must map to None (omit). "
    assert environment_tag is None, "blank environment_tag must map to None (omit)."


def test_make_llm_client_threads_langfuse_project_id(monkeypatch) -> None:
    """make_llm_client threads langfuse_project_id into the constructed LLMClient.

    The non-secret Langfuse ``project_id`` must reach the real LLMClient so it can
    be surfaced as trace metadata.  ``secret_key`` is left unset here so no
    handler/network is constructed; ``_langfuse_project_id`` is stored regardless.

    The complementary ``environment_tag`` → Langfuse ``environment`` and
    ``project_id`` → trace ``metadata.project_id`` behaviors are asserted in
    tests/unit/shared/llm/test_langfuse_instrumentation.py.

    spec: spec/feature/BACKEND_LLM.md §Observability — project_id → trace metadata.
    """
    monkeypatch.setattr("src.backend.admin.llm_secret.get_llm_api_key", lambda: "test-key")
    monkeypatch.setattr(
        "src.backend.admin.langfuse_secret.get_langfuse_secret_key", lambda: None
    )

    client = make_llm_client(
        stub=False,
        langfuse_host="http://langfuse:3000",
        langfuse_public_key="pk-test",
        langfuse_environment="production",
        langfuse_project_id="imazon-metadata",
    )

    assert isinstance(client, LLMClient)
    assert client._langfuse_project_id == "imazon-metadata", (
        "make_llm_client must thread langfuse_project_id into the LLMClient. "
        "spec: spec/feature/BACKEND_LLM.md §Observability."
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
