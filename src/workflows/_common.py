"""Shared constants and service factories for workflow activities.

Service Factories & Test Mode
─────────────────────────────
Each ``make_*()`` function returns either the real client or a stub,
depending on ``settings.test_mode`` (``DATASPOKE_TEST_MODE`` env var).

When ``test_mode`` is **True** (set by ``./dev_env/dataspoke-test-mode.sh``):

- ``make_llm()``          → ``StubLLMClient``          (canned responses)
- ``make_vector()``       → ``StubVectorManager``       (empty searches)
- ``make_cache()``        → ``StubRedisClient``         (no-op ops)
- ``make_notification()`` → ``StubNotificationService`` (no-op alerts)

Always real regardless of test mode:

- ``make_datahub()``      → ``DataHubClient``   (dev-env GMS)
- ``make_db_session()``   → ``SessionLocal``    (dev-env PostgreSQL)

Stubs are defined in ``_stubs.py``.  To add a new stub, see the
"Adding a new stub" section in that module's docstring.

LLM / pgvector / cache / notification backends are all stubbed via the
test-mode guard.  DataHub and PostgreSQL are never stubbed.

Workflow ID Convention
──────────────────────
All workflow IDs follow the pattern ``{type}-{identifier}``:

  ============  =============================  ==============================
  Scope         Format                         Example
  ============  =============================  ==============================
  URN-scoped    ``{type}-{md5(urn)[:12]}``     ``metagen-6eb5d0afa434``
  Key-scoped    ``{type}-{key}``               ``metrics-imazon.freshness``
  Singleton     ``{type}``                     ``ontogen-singleton``
  Test          ``test-{type}-{short-label}``  ``test-ingestion-title-master``
  ============  =============================  ==============================

- ``type`` is a lowercase kebab-case workflow name matching the module
  (``ingestion``, ``validation``, ``metagen``, ``metrics``, ``ontogen``,
  ``datahub-sync``).
- Test IDs always start with ``test-`` so they can be identified and
  cleaned up in the Airflow UI.  Use short, readable labels — never
  embed full URNs.
"""

import hashlib

from src.shared.cache.client import RedisClient
from src.shared.datahub.client import DataHubClient
from src.shared.db.session import SessionLocal
from src.shared.llm.client import LLMClient
from src.shared.settings import settings
from src.shared.vector.client import PgVectorManager


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


def make_llm(model_override: str | None = None) -> LLMClient:
    """Return an LLMClient honouring test-mode stubbing.

    ``model_override`` replaces ``settings.llm_model`` while keeping the
    same provider / api_key.  Stubbing still applies when test_mode is active
    — the override is silently ignored in stub mode because the stub is
    model-agnostic.
    """
    if settings.test_mode and not settings.test_llm_real:
        from src.workflows._stubs import StubLLMClient

        return StubLLMClient()  # type: ignore[return-value]
    return LLMClient(
        provider=settings.llm_provider,
        api_key=settings.llm_api_key,
        model=model_override or settings.llm_model,
    )


def make_vector() -> PgVectorManager:
    if settings.test_mode:
        from src.workflows._stubs import StubVectorManager

        return StubVectorManager()  # type: ignore[return-value]
    return PgVectorManager(session_factory=SessionLocal)


def make_db_session():  # type: ignore[no-untyped-def]
    """Create a fresh AsyncSession for activity use.

    Returns an AsyncSession usable as ``async with make_db_session() as db:``.
    Patchable in tests to inject a test-scoped session.
    """
    return SessionLocal()


def make_notification() -> object:
    if settings.test_mode:
        from src.workflows._stubs import StubNotificationService

        return StubNotificationService()
    from src.shared.notifications.service import NotificationService

    return NotificationService()


def make_ontogen() -> tuple:  # type: ignore[type-arg]
    """Construct OntogenService with all required dependencies.

    Spec: spec/feature/BACKEND.md §Feature Services — OntogenService requires
    datahub, db, cache, llm, vector.
    """
    from src.backend.ontogen.service import OntogenService

    datahub = make_datahub()
    cache = make_cache()
    llm = make_llm()
    vector = make_vector()
    # db session is provided by callers via async context manager
    return OntogenService, datahub, cache, llm, vector


def make_metagen() -> tuple:  # type: ignore[type-arg]
    """Construct MetagenService with all required dependencies.

    Spec: spec/feature/BACKEND.md §Feature Services — MetagenService requires
    datahub, db, cache, llm.
    """
    from src.backend.metagen.service import MetagenService

    datahub = make_datahub()
    llm = make_llm()
    # db session is provided by callers via async context manager
    return MetagenService, datahub, llm
