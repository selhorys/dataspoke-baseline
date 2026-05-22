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
  URN-scoped    ``{type}-{md5(urn)[:12]}``     ``ingestion-6eb5d0afa434``
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


def make_llm(
    provider: str = "gemini",
    model: str = "gemini-3.5-flash",
    model_override: str | None = None,
) -> LLMClient:
    """Return an LLMClient honouring test-mode stubbing.

    ``provider`` and ``model`` come from the caller's ``RuntimeConfigDTO``.
    Defaults match the factory defaults so test-mode callers that don't have
    a DB session (e.g. unit tests running in stub mode) don't need to supply them.
    ``model_override`` replaces ``model`` while keeping the same provider and
    api_key — used by the Reviewer path when a separate reviewer model is
    configured.  Stubbing still applies when test_mode is active; provider and
    model are silently ignored because the stub is model-agnostic.
    """
    if settings.test_mode and not settings.test_llm_real:
        from src.workflows._stubs import StubLLMClient

        return StubLLMClient()  # type: ignore[return-value]
    from src.backend.admin.llm_secret import get_llm_api_key

    return LLMClient(
        provider=provider,
        api_key=get_llm_api_key(),
        model=model_override or model,
        langfuse_host=settings.langfuse_host,
        langfuse_public_key=settings.langfuse_public_key,
        langfuse_secret_key=settings.langfuse_secret_key,
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
    """Construct OntogenService dependencies (without LLM — caller loads RC from DB first).

    Spec: spec/feature/BACKEND.md §Feature Services — OntogenService requires
    datahub, db, cache, llm, vector.

    LLM is excluded from this tuple; callers must load RuntimeConfigDTO via
    ``get_runtime_config(db)`` and then call ``make_llm(provider=..., model=...)``.
    """
    from src.backend.ontogen.service import OntogenService

    datahub = make_datahub()
    cache = make_cache()
    vector = make_vector()
    # db session and llm are provided by callers via async context manager
    return OntogenService, datahub, cache, vector


def make_metagen() -> tuple:  # type: ignore[type-arg]
    """Construct MetagenService dependencies (without LLM — caller loads RC from DB first).

    Spec: spec/feature/BACKEND.md §Feature Services — MetagenService requires
    datahub, db, cache, llm, vector.

    LLM is excluded from this tuple; callers must load RuntimeConfigDTO via
    ``get_runtime_config(db)`` and then call ``make_llm(provider=..., model=...)``.
    """
    from src.backend.metagen.service import MetagenService

    datahub = make_datahub()
    cache = make_cache()
    vector = make_vector()
    # db session and llm are provided by callers via async context manager
    return MetagenService, datahub, cache, vector
