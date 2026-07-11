"""Unit tests for src/backend/admin/config_service.py.

Concerns covered:

1. Seed on empty DB:
   get_runtime_config on an empty DB inserts a row with EXACTLY the factory
   defaults and returns a DTO whose values match those defaults.

2. Partial patch:
   patch_runtime_config applies only the supplied fields, leaves all others at
   their prior values, and bumps updated_at.

3. Cache hit within TTL:
   A second call to get_runtime_config within the 30-second TTL does NOT
   re-query the DB (the cache is returned directly).

4. Cache invalidation:
   invalidate_runtime_config_cache() forces a fresh DB read on the next call.

5. Patch invalidates cache:
   After patch_runtime_config the cache reflects the patched values, not the
   pre-patch snapshot.

6. Defaults drift guard:
   RUNTIME_CONFIG_DEFAULTS values equal the documented factory defaults AND
   match the ORM column defaults on RuntimeConfig.

Spec traceability:
- BACKEND_LLM.md §Settings Reference — runtime_config singleton service contracts.
- src/backend/admin/config_service.py — lazy get-or-create, 30-second cache,
  invalidation on patch.
- src/shared/db/models.py RuntimeConfig — ORM column defaults.
"""

import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.backend.admin.config_service as _svc_mod
from src.backend.admin.config_service import (
    RUNTIME_CONFIG_DEFAULTS,
    get_runtime_config,
    invalidate_runtime_config_cache,
    patch_runtime_config,
)
from src.shared.db.models import RuntimeConfig
from tests.unit.conftest import route_db_execute

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_runtime_config_row(**overrides) -> MagicMock:
    """Build a mock RuntimeConfig ORM row from RUNTIME_CONFIG_DEFAULTS + overrides."""
    row = MagicMock(spec=RuntimeConfig)
    for field, value in {**RUNTIME_CONFIG_DEFAULTS, **overrides}.items():
        setattr(row, field, value)
    row.id = 1
    row.updated_at = datetime.now(tz=UTC)
    return row


def _db_with_row(row: MagicMock | None) -> AsyncMock:
    """Return a mock AsyncSession that yields ``row`` from scalar_one_or_none."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.add = MagicMock()

    async def _refresh(obj):
        # Simulate the server-side timestamp being populated after commit.
        if not hasattr(obj, "updated_at") or obj.updated_at is None:
            obj.updated_at = datetime.now(tz=UTC)

    db.refresh = AsyncMock(side_effect=_refresh)
    return db


# ── Fixture: always start each test with a clean cache ────────────────────────


@pytest.fixture(autouse=True)
def clear_cache():
    """Invalidate the process-level cache before (and after) every test.

    This prevents cache state from leaking across tests running in the same
    process.  The cache uses a module-level variable, so isolation requires
    an explicit eviction.
    """
    invalidate_runtime_config_cache()
    yield
    invalidate_runtime_config_cache()


# ── 1. Seed on empty DB ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_runtime_config_seeds_defaults_on_empty_db() -> None:
    """get_runtime_config on an empty DB inserts id=1 with factory defaults.

    Spec: BACKEND_SCHEMA §runtime_config / impl src/backend/admin/config_service.py —
    'get_runtime_config on empty DB seeds the id=1 row
    with EXACTLY the factory defaults; returns a DTO with those values.'

    Primary assertion: the RuntimeConfig object handed to db.add (i.e. the
    object the impl *constructed* before the INSERT) carries every one of the
    15 RUNTIME_CONFIG_DEFAULTS values.  This is the load-bearing assertion —
    it would catch a bug in the seed path such as a missing kwarg, a typo in
    a field name, or an off-by-one default value, none of which the old
    _refresh-overwrites-everything mock could catch.

    The _refresh side-effect is deliberately restricted to setting only id and
    updated_at (simulating what the DB does after RETURNING/refresh), leaving
    the 15 tunable fields exactly as the impl set them at construction time.
    The DTO assertions that follow are secondary checks confirming the full
    pipeline (construct → add → commit → refresh → DTO) produces the right
    output, but they stand on top of the primary insert-payload assertion.
    """
    db = _db_with_row(None)

    # Capture the exact object the impl passes to db.add.
    added_rows: list = []

    def _capture_add(obj):
        added_rows.append(obj)

    db.add = MagicMock(side_effect=_capture_add)

    # _refresh simulates only what the DB populates server-side: id and
    # updated_at.  It must NOT overwrite the 15 tunable fields — doing so
    # would let a broken seed construction (wrong/missing field) pass the DTO
    # assertions silently.
    async def _refresh(obj):
        obj.id = 1
        obj.updated_at = datetime.now(tz=UTC)

    db.refresh = AsyncMock(side_effect=_refresh)

    dto = await get_runtime_config(db)

    # ── Primary: verify the INSERT payload the impl constructed ───────────────
    # One db.add call, one db.commit call.
    assert db.add.call_count == 1
    assert db.commit.call_count == 1

    # The captured object must be a real RuntimeConfig instance, not a mock.
    inserted = added_rows[0]
    assert isinstance(inserted, RuntimeConfig), (
        "db.add must receive a RuntimeConfig ORM instance, not a mock or plain dict. "
        "spec: BACKEND_SCHEMA §runtime_config / impl src/backend/admin/config_service.py — impl "
        "constructs RuntimeConfig(id=1, **RUNTIME_CONFIG_DEFAULTS)."
    )

    # id=1 is required by the singleton contract.
    assert inserted.id == 1, (
        "impl must seed with id=1. "
        "spec: BACKEND_SCHEMA §runtime_config / impl src/backend/admin/config_service.py — "
        "'singleton RuntimeConfig row with id=1'."
    )

    # Every one of the 15 RUNTIME_CONFIG_DEFAULTS fields must be set on the
    # inserted object exactly as defined.  A missing or wrong value here is a
    # regression in the impl's seed-construction path.
    assert inserted.llm_provider == "gemini", (
        "inserted.llm_provider must equal RUNTIME_CONFIG_DEFAULTS['llm_provider']"
    )
    assert inserted.llm_model == "gemini-3.5-flash", (
        "inserted.llm_model must equal RUNTIME_CONFIG_DEFAULTS['llm_model']"
    )
    assert inserted.ontogen_llm_max_iterations == 3, (
        "inserted.ontogen_llm_max_iterations must equal RUNTIME_CONFIG_DEFAULTS value"
    )
    assert inserted.ontogen_debate_max_turns == 4, (
        "inserted.ontogen_debate_max_turns must equal RUNTIME_CONFIG_DEFAULTS value"
    )
    assert inserted.ontogen_debate_rag_k == 5, (
        "inserted.ontogen_debate_rag_k must equal RUNTIME_CONFIG_DEFAULTS value"
    )
    assert inserted.ontogen_debate_reviewer_model is None, (
        "inserted.ontogen_debate_reviewer_model must be None (nullable, no reviewer by default)"
    )
    assert inserted.metagen_llm_max_iterations == 3, (
        "inserted.metagen_llm_max_iterations must equal RUNTIME_CONFIG_DEFAULTS value"
    )
    assert inserted.metagen_debate_max_turns == 4, (
        "inserted.metagen_debate_max_turns must equal RUNTIME_CONFIG_DEFAULTS value"
    )
    assert inserted.metagen_debate_rag_k == 5, (
        "inserted.metagen_debate_rag_k must equal RUNTIME_CONFIG_DEFAULTS value"
    )
    assert inserted.metagen_debate_reviewer_model is None, (
        "inserted.metagen_debate_reviewer_model must be None (nullable, no reviewer by default)"
    )
    assert inserted.metagen_confidence_threshold == 0.7, (
        "inserted.metagen_confidence_threshold must equal RUNTIME_CONFIG_DEFAULTS value"
    )
    assert inserted.metagen_ontology_rag_node_k == 5, (
        "inserted.metagen_ontology_rag_node_k must equal RUNTIME_CONFIG_DEFAULTS value"
    )
    assert inserted.metagen_ontology_rag_edge_k == 5, (
        "inserted.metagen_ontology_rag_edge_k must equal RUNTIME_CONFIG_DEFAULTS value"
    )
    assert inserted.metagen_ontology_rag_triple_k == 5, (
        "inserted.metagen_ontology_rag_triple_k must equal RUNTIME_CONFIG_DEFAULTS value"
    )
    assert inserted.validation_score_n_intervals == 3, (
        "inserted.validation_score_n_intervals must equal RUNTIME_CONFIG_DEFAULTS value"
    )
    assert inserted.stub_redis_client is False, (
        "inserted.stub_redis_client must be False (prod-safe default)"
    )
    assert inserted.stub_llm_client is False, (
        "inserted.stub_llm_client must be False (prod-safe default)"
    )
    assert inserted.stub_pgvector_manager is False, (
        "inserted.stub_pgvector_manager must be False (prod-safe default)"
    )
    assert inserted.stub_notification_service is False, (
        "inserted.stub_notification_service must be False (prod-safe default)"
    )

    # ── Secondary: the full pipeline returns the correct DTO ──────────────────
    # These assertions confirm RuntimeConfigDTO.from_orm reads back the values
    # the impl set.  Because _refresh does NOT overwrite the tunable fields,
    # these pass only when the impl's construction AND from_orm both map fields
    # correctly.  (If _refresh had overwritten the fields, a from_orm bug that
    # reads the wrong attribute would still produce the right DTO value — that
    # blind spot is now closed.)
    assert dto.llm_provider == "gemini"
    assert dto.llm_model == "gemini-3.5-flash"
    assert dto.ontogen_llm_max_iterations == 3
    assert dto.ontogen_debate_max_turns == 4
    assert dto.ontogen_debate_rag_k == 5
    assert dto.ontogen_debate_reviewer_model is None
    assert dto.metagen_llm_max_iterations == 3
    assert dto.metagen_debate_max_turns == 4
    assert dto.metagen_debate_rag_k == 5
    assert dto.metagen_debate_reviewer_model is None
    assert dto.metagen_confidence_threshold == 0.7
    assert dto.metagen_ontology_rag_node_k == 5
    assert dto.metagen_ontology_rag_edge_k == 5
    assert dto.metagen_ontology_rag_triple_k == 5
    assert dto.validation_score_n_intervals == 3
    assert dto.stub_redis_client is False
    assert dto.stub_llm_client is False
    assert dto.stub_pgvector_manager is False
    assert dto.stub_notification_service is False


@pytest.mark.asyncio
async def test_get_runtime_config_returns_existing_row() -> None:
    """get_runtime_config with an existing DB row returns its values without inserting.

    Spec: BACKEND_SCHEMA §runtime_config / impl src/backend/admin/config_service.py — lazy
    get-or-create; does not re-create when row exists.
    """
    existing = _make_runtime_config_row(llm_model="gpt-4-turbo")
    db = _db_with_row(existing)

    dto = await get_runtime_config(db)

    assert db.add.call_count == 0, "Must not insert when row already exists"
    assert dto.llm_model == "gpt-4-turbo"


# ── 2. Partial patch ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_runtime_config_applies_only_provided_fields() -> None:
    """patch_runtime_config only modifies supplied fields; others stay unchanged.

    Spec: BACKEND_SCHEMA §runtime_config / impl src/backend/admin/config_service.py —
    'patch_runtime_config applies only provided fields,
    leaves others at prior values, and bumps updated_at.'
    """
    existing = _make_runtime_config_row()
    db = _db_with_row(existing)

    # Supply only two fields; all others must remain at their factory defaults.
    async def _refresh(obj):
        obj.updated_at = datetime.now(tz=UTC)

    db.refresh = AsyncMock(side_effect=_refresh)

    dto = await patch_runtime_config(
        db,
        llm_model="gpt-4o-mini",
        ontogen_debate_max_turns=6,
    )

    # Changed fields reflected.
    assert dto.llm_model == "gpt-4o-mini"
    assert dto.ontogen_debate_max_turns == 6

    # Unchanged fields keep prior (factory default) values.
    assert dto.llm_provider == "gemini"
    assert dto.ontogen_llm_max_iterations == 3
    assert dto.metagen_confidence_threshold == 0.7
    assert dto.validation_score_n_intervals == 3


@pytest.mark.asyncio
async def test_patch_runtime_config_commits_and_invalidates_cache() -> None:
    """patch_runtime_config commits the session and the subsequent get returns the new value.

    Spec: BACKEND_SCHEMA §runtime_config / impl src/backend/admin/config_service.py — 'patch
    invalidates the cache so a subsequent get
    reflects the patch.'

    To prove the returned value comes from the patch (not a coincidental DB
    re-read), the mock DB's fresh-query path returns a DIFFERENT stale value
    ("stale-model") than the patched one ("new-model").  A passing assertion on
    "new-model" from get_runtime_config proves the cache was populated by the
    patch path.
    """
    # Seed the cache via the public API so no private _cache write is needed.
    # The initial DB row returns "old-model"; this populates the cache.
    initial_row = _make_runtime_config_row(llm_model="old-model")
    db_seed = _db_with_row(initial_row)
    await get_runtime_config(db_seed)

    # Now build the patch DB.  The fresh-query path returns "stale-model" to
    # distinguish it from the patch result ("new-model").
    patch_row = _make_runtime_config_row(llm_model="new-model")
    db = _db_with_row(patch_row)

    async def _refresh(obj):
        obj.updated_at = datetime.now(tz=UTC)

    db.refresh = AsyncMock(side_effect=_refresh)

    # Wire a DIFFERENT stale value on re-query so we can distinguish cache vs DB.
    stale_result = MagicMock()
    stale_row = _make_runtime_config_row(llm_model="stale-model")
    stale_result.scalar_one_or_none.return_value = stale_row
    # Same runtime_config query re-fetched within one operation: patch's fetch
    # (patch_row) then, if get_runtime_config re-queries, the stale re-read.
    route_db_execute(
        db,
        [
            (
                "runtime_config",
                [
                    MagicMock(**{"scalar_one_or_none.return_value": patch_row}),
                    stale_result,
                ],
            )
        ],
    )

    await patch_runtime_config(db, llm_model="new-model")

    # Commit must have been called exactly once.
    assert db.commit.call_count == 1

    # The observable invariant: get_runtime_config must return the patched value.
    # If it returns "stale-model", the cache was cleared and re-queried from DB
    # rather than being populated with the patched DTO.
    dto = await get_runtime_config(db)
    assert dto.llm_model == "new-model", (
        "get_runtime_config after patch must return the patched value. "
        "spec: BACKEND_SCHEMA §runtime_config / impl src/backend/admin/config_service.py — "
        "'subsequent get reflects the patch'."
    )


# ── 3. Cache hit within TTL ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_runtime_config_cache_hit_does_not_requery() -> None:
    """A second get_runtime_config call within TTL does not issue a DB query.

    Spec: BACKEND_SCHEMA §runtime_config / impl src/backend/admin/config_service.py — '~30s process
    cache; second call within TTL does
    NOT re-query.'
    """
    existing = _make_runtime_config_row()
    db = _db_with_row(existing)

    # First call — populates cache.
    dto1 = await get_runtime_config(db)
    first_execute_count = db.execute.call_count  # 1

    # Second call — should hit cache, no new execute.
    dto2 = await get_runtime_config(db)

    assert db.execute.call_count == first_execute_count, (
        "Second get_runtime_config within TTL must not issue a new DB query "
        "(cache should be returned directly)"
    )
    assert dto1 == dto2


# ── 4. Cache invalidation ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invalidate_runtime_config_cache_forces_fresh_read() -> None:
    """invalidate_runtime_config_cache() causes the next call to re-query.

    Spec: BACKEND_SCHEMA §runtime_config / impl src/backend/admin/config_service.py —
    'invalidate_runtime_config_cache() forces a fresh
    read.'
    """
    existing = _make_runtime_config_row(llm_model="model-v1")
    db = _db_with_row(existing)

    # Populate cache via public API.
    await get_runtime_config(db)

    # Invalidate (observable: next get_runtime_config re-queries DB).
    invalidate_runtime_config_cache()

    # Mutate the "DB" to return a different value — simulating an out-of-band change.
    existing_v2 = _make_runtime_config_row(llm_model="model-v2")
    result_v2 = MagicMock()
    result_v2.scalar_one_or_none.return_value = existing_v2
    db.execute = AsyncMock(return_value=result_v2)

    dto = await get_runtime_config(db)
    assert dto.llm_model == "model-v2", (
        "After cache invalidation the fresh DB value must be returned"
    )


@pytest.mark.asyncio
async def test_cache_is_not_reused_after_expiry(monkeypatch) -> None:
    """A cache entry past its TTL triggers a fresh DB read.

    Spec: BACKEND_SCHEMA §runtime_config / impl src/backend/admin/config_service.py — TTL-based
    cache; stale entry causes re-query.

    Technique: populate the cache via get_runtime_config (cache stores
    expires_at = now + 30s), then monkeypatch time.monotonic in the service
    module to return a time 60 seconds in the future.  The cache entry is
    now expired; the second call must re-query the DB.
    """
    existing = _make_runtime_config_row()
    db = _db_with_row(existing)

    # First call — populates cache.  expires_at = real_now + 30s.
    real_now = time.monotonic()
    await get_runtime_config(db)
    first_count = db.execute.call_count  # should be 1

    # Move time 60 seconds into the future: the stored expires_at is now in the past.
    monkeypatch.setattr(_svc_mod.time, "monotonic", lambda: real_now + 60.0)

    # Second call — cache entry is expired; must re-query.
    await get_runtime_config(db)

    assert db.execute.call_count > first_count, (
        "Cache entry expired (time advanced 60s past TTL) must trigger a fresh DB query"
    )


# ── 5. Patch invalidates cache ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_after_patch_subsequent_get_reflects_updated_value() -> None:
    """After patch_runtime_config a subsequent get_runtime_config returns the new value.

    Spec: BACKEND_SCHEMA §runtime_config / impl src/backend/admin/config_service.py — 'patch
    invalidates the cache so a subsequent get
    reflects the patch.'

    The end-to-end invariant: patch → get must show the patched value.
    To prove the returned value came from the patch path and not a coincidental
    DB re-read, the mock DB's re-query path returns a DIFFERENT (stale) value
    (ontogen_debate_rag_k=5) than the patched one (=10).  A passing assertion
    on 10 proves the cache was populated by patch, not by a fresh query.
    """
    # The row the patch sees: ontogen_debate_rag_k will be set to 10.
    patch_row = _make_runtime_config_row(ontogen_debate_rag_k=5)
    db = _db_with_row(patch_row)

    async def _refresh(obj):
        obj.updated_at = datetime.now(tz=UTC)

    db.refresh = AsyncMock(side_effect=_refresh)

    # Wire a stale row on any subsequent DB re-query (k=5) so we can distinguish
    # cache-hit (k=10) from fresh-query (k=5).
    stale_result = MagicMock()
    stale_row = _make_runtime_config_row(ontogen_debate_rag_k=5)
    stale_result.scalar_one_or_none.return_value = stale_row
    # Same runtime_config query re-fetched within one operation: patch's fetch
    # (patch_row) then, if get_runtime_config re-queries after patch, the stale k=5 re-read.
    route_db_execute(
        db,
        [
            (
                "runtime_config",
                [
                    MagicMock(**{"scalar_one_or_none.return_value": patch_row}),
                    stale_result,
                ],
            )
        ],
    )

    # Patch: writes ontogen_debate_rag_k=10.
    await patch_runtime_config(db, ontogen_debate_rag_k=10)

    # Unconditionally call get_runtime_config — no branch on private _cache.
    dto = await get_runtime_config(db)
    assert dto.ontogen_debate_rag_k == 10, (
        "get_runtime_config after patch must return the patched value (k=10). "
        "If k=5 is returned the cache was not populated by the patch path. "
        "spec: BACKEND_SCHEMA §runtime_config / impl src/backend/admin/config_service.py — "
        "'subsequent get reflects the patch'."
    )


# ── 6. Defaults drift guard ───────────────────────────────────────────────────


def test_runtime_config_defaults_match_documented_factory_defaults() -> None:
    """RUNTIME_CONFIG_DEFAULTS values equal the documented factory defaults.

    Spec: BACKEND_SCHEMA §runtime_config / impl src/backend/admin/config_service.py — 'Defaults
    single-source: assert RUNTIME_CONFIG_DEFAULTS
    values equal the documented factory defaults AND match the ORM column defaults.'
    These values are the single source of truth — asserted here so any drift from
    the documented spec (BACKEND_LLM.md §Settings Reference §Runtime configuration)
    is caught immediately.
    """
    assert RUNTIME_CONFIG_DEFAULTS["llm_provider"] == "gemini"
    assert RUNTIME_CONFIG_DEFAULTS["llm_model"] == "gemini-3.5-flash"
    assert RUNTIME_CONFIG_DEFAULTS["ontogen_llm_max_iterations"] == 3
    assert RUNTIME_CONFIG_DEFAULTS["ontogen_debate_max_turns"] == 4
    assert RUNTIME_CONFIG_DEFAULTS["ontogen_debate_rag_k"] == 5
    assert RUNTIME_CONFIG_DEFAULTS["ontogen_debate_reviewer_model"] is None
    assert RUNTIME_CONFIG_DEFAULTS["metagen_llm_max_iterations"] == 3
    assert RUNTIME_CONFIG_DEFAULTS["metagen_debate_max_turns"] == 4
    assert RUNTIME_CONFIG_DEFAULTS["metagen_debate_rag_k"] == 5
    assert RUNTIME_CONFIG_DEFAULTS["metagen_debate_reviewer_model"] is None
    assert RUNTIME_CONFIG_DEFAULTS["metagen_confidence_threshold"] == 0.7
    assert RUNTIME_CONFIG_DEFAULTS["metagen_ontology_rag_node_k"] == 5
    assert RUNTIME_CONFIG_DEFAULTS["metagen_ontology_rag_edge_k"] == 5
    assert RUNTIME_CONFIG_DEFAULTS["metagen_ontology_rag_triple_k"] == 5
    assert RUNTIME_CONFIG_DEFAULTS["validation_score_n_intervals"] == 3
    assert RUNTIME_CONFIG_DEFAULTS["stub_redis_client"] is False
    assert RUNTIME_CONFIG_DEFAULTS["stub_llm_client"] is False
    assert RUNTIME_CONFIG_DEFAULTS["stub_pgvector_manager"] is False
    assert RUNTIME_CONFIG_DEFAULTS["stub_notification_service"] is False


def test_runtime_config_defaults_match_orm_column_defaults() -> None:
    """RUNTIME_CONFIG_DEFAULTS values match the ORM column defaults on RuntimeConfig.

    Spec: BACKEND_SCHEMA §runtime_config / impl src/backend/admin/config_service.py — drift guard
    ensures defaults dict and ORM column
    defaults stay in sync.  A mismatch here means the lazy-seed path would write
    a value different from what the DB column would produce on a raw INSERT.
    """
    # Map from RUNTIME_CONFIG_DEFAULTS keys to their ORM column default values.
    # Column defaults are extracted from the ORM column __init__ argument defaults,
    # which Python stores as the `default` kwarg on mapped_column().
    col_defaults: dict[str, object] = {
        "llm_provider": "gemini",
        "llm_model": "gemini-3.5-flash",
        "ontogen_llm_max_iterations": 3,
        "ontogen_debate_max_turns": 4,
        "ontogen_debate_rag_k": 5,
        "ontogen_debate_reviewer_model": None,
        "metagen_llm_max_iterations": 3,
        "metagen_debate_max_turns": 4,
        "metagen_debate_rag_k": 5,
        "metagen_debate_reviewer_model": None,
        "metagen_confidence_threshold": 0.7,
        "metagen_ontology_rag_node_k": 5,
        "metagen_ontology_rag_edge_k": 5,
        "metagen_ontology_rag_triple_k": 5,
        "validation_score_n_intervals": 3,
        "stub_redis_client": False,
        "stub_llm_client": False,
        "stub_pgvector_manager": False,
        "stub_notification_service": False,
    }

    # Columns with non-None ORM defaults: verify they match RUNTIME_CONFIG_DEFAULTS.
    # Columns with None ORM defaults (nullable): explicitly assert BOTH sides are None
    # so a stray ORM default is caught from both directions.
    # The two nullable reviewer_model columns have no ORM default (col.default is None)
    # and RUNTIME_CONFIG_DEFAULTS must also be None for them.
    NULLABLE_NO_ORM_DEFAULT = {
        "ontogen_debate_reviewer_model",
        "metagen_debate_reviewer_model",
    }

    table = RuntimeConfig.__table__
    for field, expected in col_defaults.items():
        col = table.c.get(field)
        assert col is not None, f"Column '{field}' missing from RuntimeConfig ORM table"

        if field in NULLABLE_NO_ORM_DEFAULT:
            # Explicit drift guard for nullable columns without ORM default.
            assert col.default is None, (
                f"ORM column '{field}' must have no default (nullable reviewer model); "
                "found an unexpected default. "
                "spec: BACKEND_SCHEMA.md §runtime_config — nullable columns must stay defaultless."
            )
            assert RUNTIME_CONFIG_DEFAULTS[field] is None, (
                f"RUNTIME_CONFIG_DEFAULTS['{field}'] must be None for nullable reviewer model; "
                f"got {RUNTIME_CONFIG_DEFAULTS[field]!r}. "
                "spec: BACKEND_SCHEMA.md §runtime_config."
            )
        elif col.default is not None:
            # SQLAlchemy ScalarElementColumnDefault stores the value in `.arg`.
            orm_default = col.default.arg
            assert orm_default == expected, (
                f"ORM column default for '{field}' ({orm_default!r}) does not "
                f"match RUNTIME_CONFIG_DEFAULTS ({expected!r}). "
                "spec: BACKEND_SCHEMA.md §runtime_config — ORM and DEFAULTS dict must agree."
            )

    # Also assert the RUNTIME_CONFIG_DEFAULTS dict exactly matches col_defaults.
    for field, expected in col_defaults.items():
        assert RUNTIME_CONFIG_DEFAULTS[field] == expected, (
            f"RUNTIME_CONFIG_DEFAULTS['{field}'] = {RUNTIME_CONFIG_DEFAULTS[field]!r} "
            f"differs from documented factory default {expected!r}."
        )


def test_runtime_config_defaults_covers_all_19_fields() -> None:
    """RUNTIME_CONFIG_DEFAULTS contains exactly the 20 documented fields.

    Spec: BACKEND_SCHEMA.md §runtime_config — 15 behavioral tunables + 4 stub toggle
    booleans + auth_datahub_corp_group.
    spec: src/backend/admin/config_service.py RUNTIME_CONFIG_DEFAULTS.
    """
    expected_fields = {
        "llm_provider",
        "llm_model",
        "ontogen_llm_max_iterations",
        "ontogen_debate_max_turns",
        "ontogen_debate_rag_k",
        "ontogen_debate_reviewer_model",
        "metagen_llm_max_iterations",
        "metagen_debate_max_turns",
        "metagen_debate_rag_k",
        "metagen_debate_reviewer_model",
        "metagen_confidence_threshold",
        "metagen_ontology_rag_node_k",
        "metagen_ontology_rag_edge_k",
        "metagen_ontology_rag_triple_k",
        "validation_score_n_intervals",
        "stub_redis_client",
        "stub_llm_client",
        "stub_pgvector_manager",
        "stub_notification_service",
        "auth_datahub_corp_group",
    }
    assert set(RUNTIME_CONFIG_DEFAULTS.keys()) == expected_fields, (
        f"RUNTIME_CONFIG_DEFAULTS must contain exactly 20 fields. "
        f"Extra: {set(RUNTIME_CONFIG_DEFAULTS) - expected_fields}, "
        f"missing: {expected_fields - set(RUNTIME_CONFIG_DEFAULTS)}"
    )
