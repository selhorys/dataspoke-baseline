"""Unit tests for src/backend/admin/peripheral_service.py.

Concerns covered:

1.  get — absent row returns None (unconfigured state).
2.  get — present datahub row returns DatahubConfigDTO with correct fields.
3.  get — present langfuse row returns LangfuseConfigDTO with correct fields.
4.  get — cache hit within TTL: second call does not re-query the DB.
5.  get — cache expiry forces a fresh DB read.
6.  invalidate — by name evicts one entry; other entries remain.
7.  invalidate — name=None clears all entries.
8.  patch — empty partial returns current config without creating a row.
9.  patch — creates a row on first non-empty call.
10. patch — merges subsequent partial updates (shallow merge, existing keys preserved).
11. patch — IntegrityError on INSERT → re-selects and merges (concurrent-PATCH race recovery).
12. invalidate_peripheral_config_cache called by patch to refresh the cache.

Spec traceability:
- plan/scalable-beaming-hamster.md §Backend — peripheral_service contracts.
- src/backend/admin/peripheral_service.py — public surface.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import IntegrityError

import src.backend.admin.peripheral_service as _svc_mod
from src.backend.admin.peripheral_service import (
    DatahubConfigDTO,
    LangfuseConfigDTO,
    get_peripheral_config,
    invalidate_peripheral_config_cache,
    patch_peripheral_config,
)
from src.shared.db.models import PeripheralConfig

# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_row(name: str, settings: dict) -> MagicMock:
    """Build a mock PeripheralConfig ORM row."""
    row = MagicMock(spec=PeripheralConfig)
    row.name = name
    row.settings = settings
    row.updated_at = datetime.now(tz=UTC)
    return row


def _db_with_row(row: MagicMock | None) -> AsyncMock:
    """Return a mock AsyncSession that yields ``row`` on the first execute."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    db.execute = AsyncMock(return_value=result)
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    async def _refresh(obj):
        pass

    db.refresh = AsyncMock(side_effect=_refresh)
    return db


# ── Fixture: flush cache before and after each test ───────────────────────────


@pytest.fixture(autouse=True)
def flush_cache():
    """Evict the peripheral config cache before and after every test.

    Prevents cache state from leaking across tests in the same process.
    """
    invalidate_peripheral_config_cache()
    yield
    invalidate_peripheral_config_cache()


# ── 1. get — absent row returns None ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_returns_none_when_row_absent() -> None:
    """get_peripheral_config returns None when no row exists in DB.

    spec: plan/scalable-beaming-hamster.md §Backend — absent row = unconfigured.
    """
    db = _db_with_row(None)
    result = await get_peripheral_config(db, "datahub")
    assert result is None, (
        "get_peripheral_config must return None when the row is absent (unconfigured)"
    )


# ── 2. get — datahub row returns DatahubConfigDTO ────────────────────────────


@pytest.mark.asyncio
async def test_get_datahub_returns_correct_dto() -> None:
    """get_peripheral_config('datahub') returns DatahubConfigDTO with correct fields.

    spec: plan/scalable-beaming-hamster.md §Backend — DatahubConfigDTO(gms_url, kafka_brokers).
    """
    row = _make_row("datahub", {"gms_url": "http://gms:8080", "kafka_brokers": "kafka:9092"})
    db = _db_with_row(row)

    result = await get_peripheral_config(db, "datahub")

    assert isinstance(result, DatahubConfigDTO), (
        "get_peripheral_config('datahub') must return DatahubConfigDTO"
    )
    assert result.gms_url == "http://gms:8080"
    assert result.kafka_brokers == "kafka:9092"


# ── 3. get — langfuse row returns LangfuseConfigDTO ──────────────────────────


@pytest.mark.asyncio
async def test_get_langfuse_returns_correct_dto() -> None:
    """get_peripheral_config('langfuse') returns LangfuseConfigDTO with correct fields.

    spec: plan/scalable-beaming-hamster.md §Backend — LangfuseConfigDTO(host, public_key).
    """
    row = _make_row("langfuse", {"host": "http://langfuse:3000", "public_key": "pk-test"})
    db = _db_with_row(row)

    result = await get_peripheral_config(db, "langfuse")

    assert isinstance(result, LangfuseConfigDTO), (
        "get_peripheral_config('langfuse') must return LangfuseConfigDTO"
    )
    assert result.host == "http://langfuse:3000"
    assert result.public_key == "pk-test"


# ── 3b. get — new non-secret connection settings round-trip into the DTOs ─────


@pytest.mark.asyncio
async def test_get_datahub_dto_carries_service_corpuser_urn_and_default_env() -> None:
    """A datahub row with the new settings keys yields a DTO carrying them.

    The non-secret ``service_corpuser_urn`` + ``default_env`` keys stored in the
    settings JSONB must round-trip out of ``_row_to_dto`` into the DTO consumers read.

    spec: spec/feature/BACKEND_SCHEMA.md §peripheral_config — settings JSONB carries
        DataHub service_corpuser_urn / default_env.
    spec: spec/API.md §/admin/peripherals/datahub — non-secret connection settings.
    """
    row = _make_row(
        "datahub",
        {
            "gms_url": "http://gms:8080",
            "kafka_brokers": "kafka:9092",
            "service_corpuser_urn": "urn:li:corpuser:imazon-svc",
            "default_env": "PROD",
        },
    )
    db = _db_with_row(row)

    result = await get_peripheral_config(db, "datahub")

    assert isinstance(result, DatahubConfigDTO)
    assert result.service_corpuser_urn == "urn:li:corpuser:imazon-svc", (
        "DatahubConfigDTO must surface service_corpuser_urn from settings JSONB. "
        "spec: spec/feature/BACKEND_SCHEMA.md §peripheral_config."
    )
    assert result.default_env == "PROD", (
        "DatahubConfigDTO must surface default_env from settings JSONB. "
        "spec: spec/feature/BACKEND_SCHEMA.md §peripheral_config."
    )


@pytest.mark.asyncio
async def test_get_datahub_dto_new_fields_default_to_empty_when_absent() -> None:
    """A datahub row without the new keys yields empty DTO fields (no spurious values).

    The service layer returns "" for absent keys; the API layer is responsible for
    substituting the read-back factory defaults.

    spec: src/backend/admin/peripheral_service.py _row_to_dto — s.get(key, "").
    """
    row = _make_row("datahub", {"gms_url": "http://gms:8080", "kafka_brokers": "kafka:9092"})
    db = _db_with_row(row)

    result = await get_peripheral_config(db, "datahub")

    assert isinstance(result, DatahubConfigDTO)
    assert result.service_corpuser_urn == "", (
        "Absent service_corpuser_urn key must yield '' at the service layer."
    )
    assert result.default_env == "", "Absent default_env key must yield '' at the service layer."


@pytest.mark.asyncio
async def test_get_langfuse_dto_carries_project_id_and_environment_tag() -> None:
    """A langfuse row with the new settings keys yields a DTO carrying them.

    spec: spec/feature/BACKEND_SCHEMA.md §peripheral_config — settings JSONB carries
        Langfuse project_id / environment_tag.
    spec: spec/API.md §/admin/peripherals/langfuse — non-secret connection settings.
    """
    row = _make_row(
        "langfuse",
        {
            "host": "http://langfuse:3000",
            "public_key": "pk-test",
            "project_id": "imazon-metadata",
            "environment_tag": "production",
        },
    )
    db = _db_with_row(row)

    result = await get_peripheral_config(db, "langfuse")

    assert isinstance(result, LangfuseConfigDTO)
    assert result.project_id == "imazon-metadata", (
        "LangfuseConfigDTO must surface project_id from settings JSONB. "
        "spec: spec/feature/BACKEND_SCHEMA.md §peripheral_config."
    )
    assert result.environment_tag == "production", (
        "LangfuseConfigDTO must surface environment_tag from settings JSONB. "
        "spec: spec/feature/BACKEND_SCHEMA.md §peripheral_config."
    )


# ── 4. get — cache hit within TTL ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_cache_hit_does_not_re_query() -> None:
    """A second call within TTL returns the cached DTO without re-querying the DB.

    spec: plan/scalable-beaming-hamster.md §Backend — 30s TTL process-level cache.
    """
    row = _make_row("datahub", {"gms_url": "http://gms:8080", "kafka_brokers": "k:9092"})
    db = _db_with_row(row)

    first = await get_peripheral_config(db, "datahub")
    second = await get_peripheral_config(db, "datahub")

    assert first == second
    assert db.execute.call_count == 1, (
        "DB must be queried only once within TTL — second call must hit cache"
    )


# ── 5. get — cache expiry forces re-read ─────────────────────────────────────


@pytest.mark.asyncio
async def test_get_re_reads_after_ttl_expires(monkeypatch) -> None:
    """After the TTL elapses the next call re-queries the DB.

    Technique: populate cache, then advance time.monotonic past _CACHE_TTL_SECONDS.
    """
    row = _make_row("datahub", {"gms_url": "http://gms:8080", "kafka_brokers": "k:9092"})
    db = _db_with_row(row)
    real_now = time.monotonic()

    await get_peripheral_config(db, "datahub")
    first_count = db.execute.call_count  # 1

    # Advance time past the TTL.
    monkeypatch.setattr(
        _svc_mod.time, "monotonic", lambda: real_now + _svc_mod._CACHE_TTL_SECONDS + 1.0
    )

    await get_peripheral_config(db, "datahub")

    assert db.execute.call_count > first_count, (
        "Cache entry must expire after TTL; expired entry must trigger a fresh DB query"
    )


# ── 6. invalidate — by name evicts only that entry ───────────────────────────


@pytest.mark.asyncio
async def test_invalidate_by_name_evicts_only_named_entry() -> None:
    """invalidate_peripheral_config_cache('datahub') evicts datahub but not langfuse.

    spec: plan/scalable-beaming-hamster.md §Backend — invalidate_peripheral_config_cache(name).
    """
    dh_row = _make_row("datahub", {"gms_url": "http://gms:8080", "kafka_brokers": "k:9092"})
    lf_row = _make_row("langfuse", {"host": "http://lf:3000", "public_key": "pk"})

    db_dh = _db_with_row(dh_row)
    db_lf = _db_with_row(lf_row)

    # Prime both entries.
    await get_peripheral_config(db_dh, "datahub")
    await get_peripheral_config(db_lf, "langfuse")
    assert db_dh.execute.call_count == 1
    assert db_lf.execute.call_count == 1

    # Invalidate only datahub.
    invalidate_peripheral_config_cache("datahub")

    # Datahub must re-query; langfuse must still hit cache.
    await get_peripheral_config(db_dh, "datahub")
    await get_peripheral_config(db_lf, "langfuse")

    assert db_dh.execute.call_count == 2, "datahub must be re-queried after targeted invalidation"
    assert db_lf.execute.call_count == 1, "langfuse must still be served from cache"


# ── 7. invalidate — name=None clears all entries ─────────────────────────────


@pytest.mark.asyncio
async def test_invalidate_none_clears_all_entries() -> None:
    """invalidate_peripheral_config_cache(name=None) evicts all peripheral caches.

    spec: plan/scalable-beaming-hamster.md §Backend — invalidate with name=None.
    """
    dh_row = _make_row("datahub", {"gms_url": "http://gms:8080", "kafka_brokers": "k:9092"})
    lf_row = _make_row("langfuse", {"host": "http://lf:3000", "public_key": "pk"})

    db_dh = _db_with_row(dh_row)
    db_lf = _db_with_row(lf_row)

    # Prime both.
    await get_peripheral_config(db_dh, "datahub")
    await get_peripheral_config(db_lf, "langfuse")

    # Clear all.
    invalidate_peripheral_config_cache(name=None)

    # Both must re-query.
    await get_peripheral_config(db_dh, "datahub")
    await get_peripheral_config(db_lf, "langfuse")

    assert db_dh.execute.call_count == 2, "datahub must re-query after full invalidation"
    assert db_lf.execute.call_count == 2, "langfuse must re-query after full invalidation"


# ── 8. patch — empty partial returns current config ──────────────────────────


@pytest.mark.asyncio
async def test_patch_empty_partial_does_not_create_row() -> None:
    """patch_peripheral_config with empty partial returns current config (or None).

    An empty PATCH must NOT create a spurious empty row.  This guards the case
    where a token-only PATCH routes the secret first, then calls patch with
    remaining (empty) DB fields.

    spec: plan/scalable-beaming-hamster.md §Backend — F6 fix: empty partial no-op.
    """
    db = _db_with_row(None)  # No row exists

    result = await patch_peripheral_config(db, "datahub")

    # No row should have been created.
    db.add.assert_not_called()
    db.flush.assert_not_called()
    assert result is None, (
        "Empty partial on unconfigured peripheral must return None, not create a row"
    )


# ── 9. patch — creates row on first non-empty call ───────────────────────────


@pytest.mark.asyncio
async def test_patch_creates_row_on_first_call() -> None:
    """patch_peripheral_config creates a new row when one does not yet exist.

    spec: plan/scalable-beaming-hamster.md §Backend — row created lazily on first PATCH.
    """
    db = _db_with_row(None)

    # Make db.refresh update the row's settings so _row_to_dto can succeed.
    async def _refresh(obj):
        if not hasattr(obj, "settings") or obj.settings is None:
            obj.settings = {"gms_url": "http://gms:8080", "kafka_brokers": "k:9092"}

    db.refresh = AsyncMock(side_effect=_refresh)

    result = await patch_peripheral_config(
        db, "datahub", gms_url="http://gms:8080", kafka_brokers="k:9092"
    )

    db.add.assert_called_once()
    assert isinstance(result, DatahubConfigDTO), (
        "patch_peripheral_config must return DatahubConfigDTO after creating a row"
    )


# ── 10. patch — merges partial updates ───────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_merges_partial_update() -> None:
    """patch_peripheral_config merges partial fields without clobbering existing ones.

    spec: plan/scalable-beaming-hamster.md §Backend — shallow merge of settings JSONB.
    """
    existing_settings = {"gms_url": "http://old:8080", "kafka_brokers": "old:9092"}
    row = _make_row("datahub", existing_settings)
    db = _db_with_row(row)

    async def _refresh(obj):
        # Simulate server-side refresh returning merged settings.
        pass

    db.refresh = AsyncMock(side_effect=_refresh)

    # Update only gms_url, kafka_brokers should be preserved.
    result = await patch_peripheral_config(db, "datahub", gms_url="http://new:8080")

    assert isinstance(result, DatahubConfigDTO)
    assert result.gms_url == "http://new:8080", "gms_url must be updated"
    # The row's settings should have been merged, not replaced.
    assert row.settings["kafka_brokers"] == "old:9092", (
        "kafka_brokers must be preserved after partial update"
    )


# ── 11. patch — IntegrityError race recovery ─────────────────────────────────


@pytest.mark.asyncio
async def test_patch_recovers_from_integrity_error_on_concurrent_insert() -> None:
    """When INSERT raises IntegrityError (concurrent PATCH), patch re-selects and merges.

    Fixture: existing row has {kafka_brokers: "k:9092"}, PATCH adds gms_url="http://new:8080".
    After IntegrityError recovery, both fields must be present in the merged result.

    spec: plan/scalable-beaming-hamster.md §Backend — concurrent-PATCH race recovery.
    """
    # Existing row has kafka_brokers already set; the new PATCH is only adding gms_url.
    existing_row = _make_row("datahub", {"kafka_brokers": "k:9092"})

    db = AsyncMock()
    # First execute returns no row (triggers INSERT path).
    result_empty = MagicMock()
    result_empty.scalar_one_or_none.return_value = None
    # Second execute (after rollback re-select) returns the existing row.
    result_with_row = MagicMock()
    result_with_row.scalar_one.return_value = existing_row

    db.execute = AsyncMock(side_effect=[result_empty, result_with_row])
    db.add = MagicMock()
    db.flush = AsyncMock(side_effect=IntegrityError(None, None, Exception("unique violation")))
    db.rollback = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    result = await patch_peripheral_config(db, "datahub", gms_url="http://new:8080")

    db.rollback.assert_called_once()
    assert isinstance(result, DatahubConfigDTO), (
        "patch_peripheral_config must recover from IntegrityError and return a DTO"
    )

    # The new gms_url must reach the existing row via the merge.
    assert existing_row.settings["gms_url"] == "http://new:8080", (
        "After IntegrityError recovery the new gms_url must be merged into the existing row. "
        "spec: plan/scalable-beaming-hamster.md §Backend — re-select and merge on race."
    )
    # The pre-existing kafka_brokers must be preserved (not clobbered).
    assert existing_row.settings["kafka_brokers"] == "k:9092", (
        "After IntegrityError recovery the pre-existing kafka_brokers must be preserved. "
        "spec: plan/scalable-beaming-hamster.md §Backend — shallow merge; existing keys preserved."
    )
    # The returned DTO must reflect the merged new value.
    assert result.gms_url == "http://new:8080", (
        "Returned DTO must reflect the merged gms_url value. "
        "spec: plan/scalable-beaming-hamster.md §Backend — DTO returned after race recovery."
    )
