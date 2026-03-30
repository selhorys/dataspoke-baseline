"""Shared constants and service factories for workflow activities.

Service Factories & Test Mode
─────────────────────────────
Each ``make_*()`` function returns either the real client or a stub,
depending on ``settings.test_mode`` (``DATASPOKE_TEST_MODE`` env var).

When ``test_mode`` is **True** (set by ``./dev_env/dataspoke-test-mode.sh``):

- ``make_llm()``          → ``StubLLMClient``          (canned responses)
- ``make_qdrant()``       → ``StubQdrantManager``      (empty searches)
- ``make_cache()``        → ``StubRedisClient``         (no-op ops)
- ``make_notification()`` → ``StubNotificationService`` (no-op alerts)

Always real regardless of test mode:

- ``make_datahub()``      → ``DataHubClient``   (dev-env GMS)
- ``make_db_session()``   → ``SessionLocal``    (dev-env PostgreSQL)

Stubs are defined in ``_stubs.py``.  To add a new stub, see the
"Adding a new stub" section in that module's docstring.

Workflow ID Convention
──────────────────────
All workflow IDs follow the pattern ``{type}-{identifier}``:

  ============  =============================  ==============================
  Scope         Format                         Example
  ============  =============================  ==============================
  URN-scoped    ``{type}-{md5(urn)[:12]}``     ``ingestion-6eb5d0afa434``
  Key-scoped    ``{type}-{key}``               ``metrics-imazon.freshness``
  Singleton     ``{type}``                     ``ontology-rebuild``
  Test          ``test-{type}-{short-label}``  ``test-ingestion-title-master``
  ============  =============================  ==============================

- ``type`` is a lowercase kebab-case workflow name matching the module
  (``ingestion``, ``validation``, ``generation``, ``metrics``,
  ``embedding-sync``, ``ontology-rebuild``).
- Test IDs always start with ``test-`` so they can be identified and
  cleaned up in the Kestra UI.  Use short, readable labels — never
  embed full URNs.
"""

import hashlib

from src.shared.settings import settings
from src.shared.cache.client import RedisClient
from src.shared.datahub.client import DataHubClient
from src.shared.db.session import SessionLocal
from src.shared.llm.client import LLMClient
from src.shared.vector.client import QdrantManager


def urn_to_workflow_id(urn: str) -> str:
    """Create a short, stable identifier from a URN for workflow IDs."""
    return hashlib.md5(urn.encode()).hexdigest()[:12]  # noqa: S324


def make_datahub() -> DataHubClient:
    return DataHubClient(settings.datahub_gms_url, settings.datahub_token)


def make_cache() -> RedisClient:
    if settings.test_mode:
        from src.workflows._stubs import StubRedisClient

        return StubRedisClient()  # type: ignore[return-value]
    return RedisClient(settings.redis_host, settings.redis_port, settings.redis_password)


def make_llm() -> LLMClient:
    if settings.test_mode:
        from src.workflows._stubs import StubLLMClient

        return StubLLMClient()  # type: ignore[return-value]
    return LLMClient(
        provider=settings.llm_provider,
        api_key=settings.llm_api_key,
        model=settings.llm_model,
    )


def make_qdrant() -> QdrantManager:
    if settings.test_mode:
        from src.workflows._stubs import StubQdrantManager

        return StubQdrantManager()  # type: ignore[return-value]
    return QdrantManager(
        host=settings.qdrant_host,
        port=settings.qdrant_http_port,
        api_key=settings.qdrant_api_key,
        grpc_port=settings.qdrant_grpc_port,
    )


def make_db_session():
    """Create a fresh AsyncSession for activity use.

    Returns an AsyncSession usable as ``async with make_db_session() as db:``.
    Patchable in tests to inject a test-scoped session.
    """
    return SessionLocal()


def make_notification():
    if settings.test_mode:
        from src.workflows._stubs import StubNotificationService

        return StubNotificationService()
    from src.shared.notifications.service import NotificationService

    return NotificationService()
