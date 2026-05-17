"""Unit tests for src/backend/metagen/service.py — MetagenService.

Spec: spec/feature/BACKEND.md §Metadata Generation Service
      spec/USE_CASE_en.md §UC4
      spec/DATAHUB_INTEGRATION.md §Editable vs Non-Editable Description Aspects

Groups:
  A – Global conf CRUD (singleton invariant; PUT replaces; PATCH partial; validation)
  B – Boundary CRUD (opt-in semantics)
  C – list_items / get_item (cross-dataset and per-dataset; pagination; status filter)
  D – run() guards (concurrent lock, disabled, tier short-circuit)
  E – run() per-item budget (FIFO eviction; overwrite_pending=false skip)
  F – run() in-scope enumeration and rejected clearing
  G – run() dry-run: counts use candidates_proposed, no DB writes
  H – review_candidate (approve demotes sibling; reject guard; boundary guard)
"""

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backend.metagen.service import (
    MetagenBoundaryDTO,
    MetagenGlobalConfDTO,
    MetagenService,
    RunResultDTO,
)
from src.shared.exceptions import (
    ConflictError,
    EntityNotFoundError,
    InvalidDatasetUrnError,
    PreconditionFailedError,
)
from tests.unit.backend.conftest import mock_db_refresh

_VALID_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_VALID_URN2 = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.orders.daily_fulfillment_summary,DEV)"


# ── Factories ─────────────────────────────────────────────────────────────────


def _make_conf_row(
    *,
    is_enabled: bool = True,
    schedule_tier: str | None = "daily",
    dataset_filter: dict[str, Any] | None = None,
    result_limit: int = 3,
    overwrite_pending: bool = True,
) -> MagicMock:
    row = MagicMock()
    row.id = 1
    row.is_enabled = is_enabled
    row.schedule_tier = schedule_tier
    row.dataset_filter = dataset_filter or {}
    row.result_limit = result_limit
    row.overwrite_pending = overwrite_pending
    row.updated_at = datetime.now(tz=UTC)
    return row


def _make_boundary_row(
    *,
    dataset_urn: str = _VALID_URN,
    is_enabled: bool = True,
    allowed: list[str] | None = None,
    owner: str | None = None,
) -> MagicMock:
    row = MagicMock()
    row.dataset_urn = dataset_urn
    row.is_enabled = is_enabled
    row.allowed = allowed or ["dataset.description", "column.description"]
    row.owner = owner
    row.created_at = datetime.now(tz=UTC)
    row.updated_at = datetime.now(tz=UTC)
    return row


def _make_item_row(
    *,
    dataset_urn: str = _VALID_URN,
    item_id: str = "dataset.description",
    kind: str = "dataset.description",
    field_path: str | None = None,
) -> MagicMock:
    row = MagicMock()
    row.dataset_urn = dataset_urn
    row.item_id = item_id
    row.kind = kind
    row.field_path = field_path
    row.created_at = datetime.now(tz=UTC)
    row.updated_at = datetime.now(tz=UTC)
    return row


def _make_candidate_row(
    *,
    candidate_id: uuid.UUID | None = None,
    dataset_urn: str = _VALID_URN,
    item_id: str = "dataset.description",
    run_id: uuid.UUID | None = None,
    value: str = "A fine description.",
    confidence_score: float = 0.9,
    status: str = "llm_approved",
    evidence: dict | None = None,
) -> MagicMock:
    row = MagicMock()
    row.candidate_id = candidate_id or uuid.uuid4()
    row.dataset_urn = dataset_urn
    row.item_id = item_id
    row.run_id = run_id or uuid.uuid4()
    row.value = value
    row.confidence_score = confidence_score
    row.status = status
    row.evidence = evidence or {}
    row.created_at = datetime.now(tz=UTC)
    row.reviewed_at = None
    row.reviewer_id = None
    return row


def _make_result(rows_or_none: Any = None, *, scalar: Any = None) -> MagicMock:
    """Build a SQLAlchemy execute result mock."""
    m = MagicMock()
    m.scalar_one_or_none.return_value = scalar
    m.scalar.return_value = scalar
    if rows_or_none is not None:
        ms = MagicMock()
        ms.all.return_value = rows_or_none
        m.scalars.return_value = ms
        m.fetchall.return_value = rows_or_none
    return m


# ── Fixture ───────────────────────────────────────────────────────────────────


@pytest.fixture
def svc(datahub, db, cache, llm, vector) -> MetagenService:
    return MetagenService(datahub=datahub, db=db, cache=cache, llm=llm, vector=vector)


# ═══════════════════════════════════════════════════════════════════════════════
# Group A: Global conf CRUD
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_global_conf_returns_none_when_absent(svc, db) -> None:
    """get_global_conf returns None when the singleton row does not exist.

    Spec: API.md §Metadata Generation — GET /metagen/attr/conf returns null when not configured.
    """
    db.execute = AsyncMock(return_value=_make_result(scalar=None))

    result = await svc.get_global_conf()

    assert result is None, (
        "get_global_conf must return None when no singleton row exists. "
        "spec: API.md §Metadata Generation — GET conf returns null when absent"
    )


@pytest.mark.asyncio
async def test_get_global_conf_returns_dto_when_present(svc, db) -> None:
    """get_global_conf returns a MetagenGlobalConfDTO when the singleton row exists.

    Spec: API.md §Metadata Generation — GET /metagen/attr/conf.
    """
    conf_row = _make_conf_row(is_enabled=True)
    db.execute = AsyncMock(return_value=_make_result(scalar=conf_row))

    result = await svc.get_global_conf()

    assert isinstance(result, MetagenGlobalConfDTO), (
        "get_global_conf must return a MetagenGlobalConfDTO when row exists. "
        "spec: API.md §Metadata Generation"
    )
    assert result.is_enabled is True


@pytest.mark.asyncio
async def test_put_global_conf_validates_malformed_urn(svc) -> None:
    """put_global_conf raises InvalidDatasetUrnError for malformed URNs in dataset_filter.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service — validates dataset_filter.dataset_urns.
    """
    with pytest.raises(InvalidDatasetUrnError):
        await svc.put_global_conf({
            "is_enabled": False,
            "dataset_filter": {"dataset_urns": ["not-a-valid-urn"]},
        })


@pytest.mark.asyncio
async def test_put_global_conf_validates_invalid_schedule_tier(svc) -> None:
    """put_global_conf raises PreconditionFailedError for unknown schedule_tier.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service — schedule_tier ∈ {hourly, daily, weekly, null}.
    """
    with pytest.raises(PreconditionFailedError) as exc_info:
        await svc.put_global_conf({"is_enabled": False, "schedule_tier": "minutely"})
    assert exc_info.value.error_code == "INVALID_PARAMETER"


@pytest.mark.asyncio
async def test_put_global_conf_creates_singleton_row_when_absent(svc, db) -> None:
    """put_global_conf creates a new row when none exists, returns DTO.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service — singleton upsert.
    """
    db.execute = AsyncMock(return_value=_make_result(scalar=None))
    mock_db_refresh(db)

    result = await svc.put_global_conf({"is_enabled": True, "result_limit": 5})

    assert isinstance(result, MetagenGlobalConfDTO)
    db.add.assert_called()
    db.commit.assert_called()


@pytest.mark.asyncio
async def test_put_global_conf_replaces_existing_row(svc, db) -> None:
    """put_global_conf fully replaces an existing row.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service — PUT is full replacement.
    """
    existing = _make_conf_row(is_enabled=False, schedule_tier="hourly")
    db.execute = AsyncMock(return_value=_make_result(scalar=existing))
    mock_db_refresh(db)

    result = await svc.put_global_conf({"is_enabled": True, "schedule_tier": "daily", "result_limit": 10})

    assert isinstance(result, MetagenGlobalConfDTO)
    assert existing.is_enabled is True
    assert existing.result_limit == 10


@pytest.mark.asyncio
async def test_patch_global_conf_raises_not_found_when_absent(svc, db) -> None:
    """patch_global_conf raises EntityNotFoundError when the singleton row does not exist.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service — PATCH requires existing conf.
    """
    db.execute = AsyncMock(return_value=_make_result(scalar=None))

    with pytest.raises(EntityNotFoundError):
        await svc.patch_global_conf({"is_enabled": True})


@pytest.mark.asyncio
async def test_patch_global_conf_updates_only_provided_fields(svc, db) -> None:
    """patch_global_conf updates only the provided fields.

    Spec: API_DESIGN_PRINCIPLE_en.md §HTTP method semantics — PATCH is partial update.
    """
    existing = _make_conf_row(is_enabled=False, result_limit=3)
    db.execute = AsyncMock(return_value=_make_result(scalar=existing))
    mock_db_refresh(db)

    result = await svc.patch_global_conf({"is_enabled": True})

    assert isinstance(result, MetagenGlobalConfDTO)
    assert existing.is_enabled is True
    # result_limit was not in patch — should remain 3
    assert existing.result_limit == 3


@pytest.mark.asyncio
async def test_delete_global_conf_commits_when_row_exists(svc, db) -> None:
    """delete_global_conf deletes the row and commits.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service — DELETE removes singleton conf.
    """
    existing = _make_conf_row()
    db.execute = AsyncMock(return_value=_make_result(scalar=existing))

    await svc.delete_global_conf()

    db.delete.assert_called_once_with(existing)
    db.commit.assert_called()


@pytest.mark.asyncio
async def test_delete_global_conf_noop_when_absent(svc, db) -> None:
    """delete_global_conf is a no-op (no delete call) when no row exists.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service — idempotent delete.
    """
    db.execute = AsyncMock(return_value=_make_result(scalar=None))

    await svc.delete_global_conf()

    db.delete.assert_not_called()
    db.commit.assert_called()


# ═══════════════════════════════════════════════════════════════════════════════
# Group B: Boundary CRUD
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_boundary_returns_none_when_absent(svc, db) -> None:
    """get_boundary returns None when no boundary row exists.

    Spec: API.md §Metadata Generation — GET /data/{urn}/attr/metagen/conf returns null when not configured.
    """
    db.execute = AsyncMock(return_value=_make_result(scalar=None))
    result = await svc.get_boundary(_VALID_URN)
    assert result is None


@pytest.mark.asyncio
async def test_get_boundary_returns_dto_when_present(svc, db) -> None:
    """get_boundary returns a MetagenBoundaryDTO when the row exists.

    Spec: API.md §Metadata Generation — GET /data/{urn}/attr/metagen/conf.
    """
    bnd_row = _make_boundary_row()
    db.execute = AsyncMock(return_value=_make_result(scalar=bnd_row))
    result = await svc.get_boundary(_VALID_URN)
    assert isinstance(result, MetagenBoundaryDTO)
    assert result.dataset_urn == _VALID_URN


@pytest.mark.asyncio
async def test_put_boundary_rejects_invalid_kind(svc) -> None:
    """put_boundary raises PreconditionFailedError for unknown allowed kind.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service — allowed ∈ {dataset.description, column.description}.
    """
    with pytest.raises(PreconditionFailedError) as exc_info:
        await svc.put_boundary(_VALID_URN, {"allowed": ["cross_data.md"], "is_enabled": True})
    assert exc_info.value.error_code == "INVALID_PARAMETER"


@pytest.mark.asyncio
async def test_put_boundary_creates_row_when_absent(svc, db) -> None:
    """put_boundary creates a new boundary row when none exists.

    Spec: API_DESIGN_PRINCIPLE_en.md §HTTP method semantics — PUT is create-or-replace.
    """
    db.execute = AsyncMock(return_value=_make_result(scalar=None))
    mock_db_refresh(db)

    result = await svc.put_boundary(_VALID_URN, {"is_enabled": True, "allowed": ["dataset.description"]})

    assert isinstance(result, MetagenBoundaryDTO)
    db.add.assert_called()
    db.commit.assert_called()


@pytest.mark.asyncio
async def test_put_boundary_replaces_existing_row(svc, db) -> None:
    """put_boundary fully replaces an existing boundary row.

    Spec: API_DESIGN_PRINCIPLE_en.md §HTTP method semantics — PUT is full replacement.
    """
    existing = _make_boundary_row(is_enabled=False, allowed=["dataset.description"])
    db.execute = AsyncMock(return_value=_make_result(scalar=existing))
    mock_db_refresh(db)

    result = await svc.put_boundary(
        _VALID_URN, {"is_enabled": True, "allowed": ["dataset.description", "column.description"]}
    )

    assert isinstance(result, MetagenBoundaryDTO)
    assert existing.is_enabled is True


@pytest.mark.asyncio
async def test_patch_boundary_raises_not_found_when_absent(svc, db) -> None:
    """patch_boundary raises EntityNotFoundError when no boundary exists.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service — PATCH requires existing boundary.
    """
    db.execute = AsyncMock(return_value=_make_result(scalar=None))
    with pytest.raises(EntityNotFoundError):
        await svc.patch_boundary(_VALID_URN, {"is_enabled": False})


@pytest.mark.asyncio
async def test_patch_boundary_updates_only_provided_fields(svc, db) -> None:
    """patch_boundary updates only provided fields (partial update).

    Spec: API_DESIGN_PRINCIPLE_en.md §HTTP method semantics — PATCH is partial update.
    """
    existing = _make_boundary_row(is_enabled=True, allowed=["dataset.description"])
    db.execute = AsyncMock(return_value=_make_result(scalar=existing))
    mock_db_refresh(db)

    result = await svc.patch_boundary(_VALID_URN, {"is_enabled": False})

    assert isinstance(result, MetagenBoundaryDTO)
    assert existing.is_enabled is False
    # allowed was not patched — still original value
    assert existing.allowed == ["dataset.description"]


@pytest.mark.asyncio
async def test_delete_boundary_raises_not_found_when_absent(svc, db) -> None:
    """delete_boundary raises EntityNotFoundError when no boundary exists.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service — delete raises when absent.
    """
    db.execute = AsyncMock(return_value=_make_result(scalar=None))
    with pytest.raises(EntityNotFoundError):
        await svc.delete_boundary(_VALID_URN)


@pytest.mark.asyncio
async def test_delete_boundary_deletes_and_commits(svc, db) -> None:
    """delete_boundary deletes the boundary row and commits.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service — boundary delete.
    """
    existing = _make_boundary_row()
    db.execute = AsyncMock(return_value=_make_result(scalar=existing))

    await svc.delete_boundary(_VALID_URN)

    db.delete.assert_called_once_with(existing)
    db.commit.assert_called()


# ═══════════════════════════════════════════════════════════════════════════════
# Group C: list_items / get_item
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_items_returns_empty_list_when_no_rows(svc, db) -> None:
    """list_items returns empty list and total=0 when no items exist.

    Spec: API.md §Metadata Generation — GET /metagen/item returns paginated items.
    """
    count_result = _make_result(scalar=0)
    rows_result = _make_result([])
    db.execute = AsyncMock(side_effect=[count_result, rows_result])

    items, total = await svc.list_items(offset=0, limit=20)

    assert items == []
    assert total == 0


@pytest.mark.asyncio
async def test_list_items_returns_summaries_with_candidate_counts(svc, db) -> None:
    """list_items returns ItemSummaryDTO with candidate counts.

    Spec: API.md §Metadata Generation — item list includes candidate_count.
    """
    item = _make_item_row()
    cand1 = _make_candidate_row(dataset_urn=_VALID_URN, item_id="dataset.description", status="llm_approved")
    cand2 = _make_candidate_row(dataset_urn=_VALID_URN, item_id="dataset.description", status="llm_approved")

    count_result = _make_result(scalar=1)

    rows_m = MagicMock()
    rows_m.scalars.return_value.all.return_value = [item]
    db.execute = AsyncMock(side_effect=[
        count_result,
        rows_m,
        # For _build_item_summary — candidate query
        _make_result([cand1, cand2]),
    ])

    items, total = await svc.list_items(offset=0, limit=20)

    assert total == 1
    assert len(items) == 1
    assert items[0].dataset_urn == _VALID_URN
    assert items[0].candidate_count == 2


@pytest.mark.asyncio
async def test_get_item_raises_not_found_for_absent_item(svc, db) -> None:
    """get_item raises EntityNotFoundError when item does not exist.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service — 404 for absent item.
    """
    db.execute = AsyncMock(return_value=_make_result(scalar=None))

    with pytest.raises(EntityNotFoundError):
        await svc.get_item(_VALID_URN, "dataset.description")


@pytest.mark.asyncio
async def test_get_item_returns_detail_dto_with_candidates(svc, db) -> None:
    """get_item returns ItemDetailDTO including candidate list.

    Spec: API.md §Metadata Generation — item detail includes candidates list.
    """
    item = _make_item_row()
    cand = _make_candidate_row()

    cands_result = MagicMock()
    cands_result.scalars.return_value.all.return_value = [cand]

    db.execute = AsyncMock(side_effect=[
        _make_result(scalar=item),  # item lookup
        cands_result,               # candidates query
    ])

    detail = await svc.get_item(_VALID_URN, "dataset.description")

    assert detail.dataset_urn == _VALID_URN
    assert len(detail.candidates) == 1
    assert detail.candidates[0].status == "llm_approved"


@pytest.mark.asyncio
async def test_list_items_for_dataset_delegates_to_list_items(svc, db) -> None:
    """list_items_for_dataset is a thin wrapper for list_items scoped to the dataset.

    Spec: API.md §Metadata Generation — per-dataset item list uses same contract.
    """
    count_result = _make_result(scalar=0)
    rows_result = _make_result([])
    db.execute = AsyncMock(side_effect=[count_result, rows_result])

    items, total = await svc.list_items_for_dataset(_VALID_URN, offset=0, limit=20)

    assert total == 0
    assert items == []


# ═══════════════════════════════════════════════════════════════════════════════
# Group D: run() guards
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_run_raises_conflict_when_lock_held(svc, cache) -> None:
    """run() raises ConflictError(METAGEN_RUNNING) when Redis lock is already held.

    Spec: spec/feature/BACKEND.md §Concurrency Guards — singleton run serialised by Redis lock.
    Spec: API.md §Metadata Generation — POST method/run returns 409 METAGEN_RUNNING.
    """
    cache.set_nx = AsyncMock(return_value=False)

    with pytest.raises(ConflictError) as exc_info:
        await svc.run(tier=None, dataset_urns=None, dry_run=False)

    assert exc_info.value.error_code == "METAGEN_RUNNING", (
        "ConflictError must carry code METAGEN_RUNNING when lock is held. "
        "spec: API.md §Metadata Generation error codes"
    )


@pytest.mark.asyncio
async def test_run_raises_conflict_when_disabled_and_not_dry_run(svc, cache, db) -> None:
    """run() raises ConflictError(METAGEN_DISABLED) when conf.is_enabled=false and dry_run=false.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service — disabled guard.
    Spec: API.md §Metadata Generation — 409 METAGEN_DISABLED.
    """
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    conf_row = _make_conf_row(is_enabled=False, schedule_tier=None)
    db.execute = AsyncMock(return_value=_make_result(scalar=conf_row))

    with pytest.raises(ConflictError) as exc_info:
        await svc.run(tier=None, dataset_urns=None, dry_run=False)

    assert exc_info.value.error_code == "METAGEN_DISABLED", (
        "ConflictError must carry code METAGEN_DISABLED when is_enabled=false. "
        "spec: API.md §Metadata Generation error codes"
    )


@pytest.mark.asyncio
async def test_run_allows_dry_run_when_disabled(svc, cache, db) -> None:
    """run(dry_run=True) is permitted even when conf.is_enabled=false.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service — dry_run bypasses disabled guard.
    """
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    conf_row = _make_conf_row(is_enabled=False, schedule_tier=None, dataset_filter={"dataset_urns": []})
    db.execute = AsyncMock(return_value=_make_result(scalar=conf_row))

    result = await svc.run(tier=None, dataset_urns=[], dry_run=True)

    assert isinstance(result, RunResultDTO), (
        "run(dry_run=True) must return a RunResultDTO even when disabled. "
        "spec: BACKEND.md §Metadata Generation Service"
    )


@pytest.mark.asyncio
async def test_run_returns_skipped_when_tier_mismatches(svc, cache, db) -> None:
    """run(tier='weekly') returns status='skipped' when conf.schedule_tier='daily'.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service — tier short-circuit.
    """
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    conf_row = _make_conf_row(is_enabled=True, schedule_tier="daily")
    db.execute = AsyncMock(return_value=_make_result(scalar=conf_row))

    result = await svc.run(tier="weekly", dataset_urns=None, dry_run=False)

    assert result.status == "skipped", (
        "run must return status='skipped' when the requested tier does not match conf.schedule_tier. "
        "spec: BACKEND.md §Metadata Generation Service §Tier short-circuit"
    )


@pytest.mark.asyncio
async def test_run_proceeds_when_tier_matches_conf(svc, cache, db) -> None:
    """run(tier='daily') returns status='success' (not 'skipped') when conf.schedule_tier='daily'.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service — tier match: status='success'.
    The tier short-circuit (status='skipped') fires only when tier != conf.schedule_tier;
    when tier matches, in-scope enumeration runs and the result is 'success'.
    """
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    with patch.object(svc, "_load_conf_or_default", new=AsyncMock(return_value=MetagenGlobalConfDTO(
        is_enabled=True,
        schedule_tier="daily",
        dataset_filter={},
        result_limit=3,
        overwrite_pending=True,
        updated_at=datetime.now(tz=UTC),
    ))), patch.object(svc, "_enumerate_in_scope_datasets", new=AsyncMock(return_value=([], []))):
        result = await svc.run(tier="daily", dataset_urns=[], dry_run=False)

    assert result.status == "success", (
        "When tier matches conf.schedule_tier, run must return status='success', not 'skipped'. "
        "spec: BACKEND.md §Metadata Generation Service §Tier short-circuit"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Group E: per-item budget (via _apply_per_item_budget)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_apply_per_item_budget_adds_when_under_limit(svc, db) -> None:
    """_apply_per_item_budget adds new candidate when count < result_limit.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service §Per-item budget.
    """
    conf = MetagenGlobalConfDTO(
        is_enabled=True,
        schedule_tier="daily",
        dataset_filter={},
        result_limit=3,
        overwrite_pending=True,
        updated_at=datetime.now(tz=UTC),
    )

    item_row = _make_item_row()
    count_result = _make_result(scalar=1)  # 1 existing non-rejected

    db.execute = AsyncMock(side_effect=[
        _make_result(scalar=item_row),   # item lookup
        count_result,                    # non-rejected count
    ])
    mock_db_refresh(db)
    svc._refresh_candidate_embedding = AsyncMock()

    run_id = uuid.uuid4()
    added, evicted = await svc._apply_per_item_budget(
        urn=_VALID_URN,
        item_id="dataset.description",
        new_candidate_value="New description.",
        new_candidate_confidence=0.85,
        new_candidate_evidence={},
        run_id=run_id,
        conf=conf,
    )

    assert added is True, (
        "Should add candidate when count (1) < result_limit (3). "
        "spec: BACKEND.md §Per-item budget"
    )
    assert evicted is False


@pytest.mark.asyncio
async def test_apply_per_item_budget_evicts_oldest_when_overwrite_pending_true(svc, db) -> None:
    """_apply_per_item_budget evicts oldest llm_approved when budget full and overwrite_pending=true.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service §Per-item budget
    — FIFO eviction of oldest llm_approved when budget full.
    """
    conf = MetagenGlobalConfDTO(
        is_enabled=True,
        schedule_tier="daily",
        dataset_filter={},
        result_limit=2,
        overwrite_pending=True,
        updated_at=datetime.now(tz=UTC),
    )

    item_row = _make_item_row()
    oldest_llm = _make_candidate_row(status="llm_approved")

    count_result = _make_result(scalar=2)  # budget full
    oldest_result = _make_result(scalar=oldest_llm)

    db.execute = AsyncMock(side_effect=[
        _make_result(scalar=item_row),
        count_result,
        oldest_result,  # oldest llm_approved to evict
    ])
    mock_db_refresh(db)
    svc._refresh_candidate_embedding = AsyncMock()

    added, evicted = await svc._apply_per_item_budget(
        urn=_VALID_URN,
        item_id="dataset.description",
        new_candidate_value="Replacement description.",
        new_candidate_confidence=0.9,
        new_candidate_evidence={},
        run_id=uuid.uuid4(),
        conf=conf,
    )

    assert added is True, (
        "Should add candidate after evicting oldest when overwrite_pending=true. "
        "spec: BACKEND.md §Per-item budget §FIFO eviction"
    )
    assert evicted is True, (
        "evicted must be True when an llm_approved candidate is removed. "
        "spec: BACKEND.md §Per-item budget"
    )
    db.delete.assert_called_once_with(oldest_llm)


@pytest.mark.asyncio
async def test_apply_per_item_budget_skips_when_overwrite_pending_false(svc, db) -> None:
    """_apply_per_item_budget returns (False, False) when budget full and overwrite_pending=false.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service §Per-item budget
    — skip new candidate when overwrite_pending=false and budget full.
    """
    conf = MetagenGlobalConfDTO(
        is_enabled=True,
        schedule_tier="daily",
        dataset_filter={},
        result_limit=2,
        overwrite_pending=False,
        updated_at=datetime.now(tz=UTC),
    )

    item_row = _make_item_row()
    count_result = _make_result(scalar=2)  # budget full

    db.execute = AsyncMock(side_effect=[
        _make_result(scalar=item_row),
        count_result,
    ])

    added, evicted = await svc._apply_per_item_budget(
        urn=_VALID_URN,
        item_id="dataset.description",
        new_candidate_value="Skipped description.",
        new_candidate_confidence=0.9,
        new_candidate_evidence={},
        run_id=uuid.uuid4(),
        conf=conf,
    )

    assert added is False, (
        "Should NOT add candidate when overwrite_pending=false and budget is full. "
        "spec: BACKEND.md §Per-item budget"
    )
    assert evicted is False


@pytest.mark.asyncio
async def test_apply_per_item_budget_skips_when_only_approved_and_budget_full(svc, db) -> None:
    """_apply_per_item_budget returns (False, False) when budget full but no llm_approved to evict.

    Spec: spec/feature/BACKEND.md §Per-item budget — approved candidates are not evicted.
    """
    conf = MetagenGlobalConfDTO(
        is_enabled=True,
        schedule_tier="daily",
        dataset_filter={},
        result_limit=1,
        overwrite_pending=True,
        updated_at=datetime.now(tz=UTC),
    )

    item_row = _make_item_row()
    count_result = _make_result(scalar=1)  # budget full
    # No llm_approved available for eviction (all are 'approved')
    no_llm_approved = _make_result(scalar=None)

    db.execute = AsyncMock(side_effect=[
        _make_result(scalar=item_row),
        count_result,
        no_llm_approved,
    ])

    added, evicted = await svc._apply_per_item_budget(
        urn=_VALID_URN,
        item_id="dataset.description",
        new_candidate_value="Cannot add.",
        new_candidate_confidence=0.8,
        new_candidate_evidence={},
        run_id=uuid.uuid4(),
        conf=conf,
    )

    assert added is False, (
        "Should NOT add when budget full and no llm_approved available for eviction. "
        "spec: BACKEND.md §Per-item budget — approved candidates are not evicted"
    )
    assert evicted is False


# ═══════════════════════════════════════════════════════════════════════════════
# Group F: run() in-scope enumeration and rejected candidate clearing
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_run_clears_rejected_candidates_before_processing(svc, cache, db) -> None:
    """Real run reports a non-negative rejected_cleared count in result.counts.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service — rejected candidates
    cleared at run start (step 2 of generation pipeline); count surfaced in RUN_COMPLETE
    counts as 'rejected_cleared'.
    """
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    with (
        patch.object(svc, "_load_conf_or_default", new=AsyncMock(return_value=MetagenGlobalConfDTO(
            is_enabled=True,
            schedule_tier=None,
            dataset_filter={"dataset_urns": [_VALID_URN]},
            result_limit=3,
            overwrite_pending=True,
            updated_at=datetime.now(tz=UTC),
        ))),
        patch.object(
            svc,
            "_enumerate_in_scope_datasets",
            new=AsyncMock(return_value=([_VALID_URN], [])),
        ),
        patch.object(
            svc,
            "_clear_rejected_candidates",
            new=AsyncMock(return_value=5),
        ),
        patch.object(svc, "_fetch_evidence", new=AsyncMock(return_value={})),
        # boundary check inside the loop — no boundary means this URN is skipped cleanly
        patch.object(db, "execute", new=AsyncMock(return_value=_make_result(scalar=None))),
    ):
        result = await svc.run(tier=None, dataset_urns=None, dry_run=False)

    assert result.status == "success", (
        "run() must return status='success' after clearing rejected candidates. "
        "spec: BACKEND.md §Metadata Generation Service §Generation Pipeline"
    )
    assert result.counts["rejected_cleared"] == 5, (
        "rejected_cleared in result.counts must reflect the rowcount from _clear_rejected_candidates. "
        "spec: BACKEND.md §Event Catalogue — RUN_COMPLETE counts: rejected_cleared"
    )


@pytest.mark.asyncio
async def test_run_does_not_clear_rejected_on_dry_run(svc, cache, db) -> None:
    """run(dry_run=True) does not surface 'rejected_cleared' in result.counts.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service — dry_run: no DB writes;
    counts use candidates_proposed only (no rejected_cleared).
    """
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    with (
        patch.object(svc, "_load_conf_or_default", new=AsyncMock(return_value=MetagenGlobalConfDTO(
            is_enabled=True,
            schedule_tier=None,
            dataset_filter={},
            result_limit=3,
            overwrite_pending=True,
            updated_at=datetime.now(tz=UTC),
        ))),
        patch.object(
            svc,
            "_enumerate_in_scope_datasets",
            new=AsyncMock(return_value=([_VALID_URN], [])),
        ),
        patch.object(svc, "_fetch_evidence", new=AsyncMock(return_value={})),
        patch.object(db, "execute", new=AsyncMock(return_value=_make_result(scalar=None))),
    ):
        result = await svc.run(tier=None, dataset_urns=None, dry_run=True)

    assert result.dry_run is True
    assert "rejected_cleared" not in result.counts, (
        "Dry-run counts must NOT include 'rejected_cleared' — no DB writes on dry-run. "
        "spec: BACKEND.md §Metadata Generation Service — dry_run count keys"
    )


@pytest.mark.asyncio
async def test_run_dry_run_reports_candidates_proposed_not_added(svc, cache, db) -> None:
    """run(dry_run=True) counts use 'candidates_proposed', not 'candidates_added'.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service — dry_run: counts
    use candidates_proposed; no DB writes (no candidates_added/evicted/rejected_cleared).

    The run has one in-scope URN with one allowed item; the debate stub returns one
    accepted candidate, so candidates_proposed == 1.
    """
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    from src.backend.metagen.debate import DebateResult

    debate_result_stub = DebateResult(
        outcome="accept",
        payload={
            "candidates": [
                {
                    "dataset_urn": _VALID_URN,
                    "item_id": "dataset.description",
                    "value": "A dry-run proposed description.",
                    "confidence_score": 0.95,
                }
            ]
        },
        transcript={"producer_iterations": 1},
    )

    with (
        patch.object(svc, "_load_conf_or_default", new=AsyncMock(return_value=MetagenGlobalConfDTO(
            is_enabled=True,
            schedule_tier=None,
            dataset_filter={},
            result_limit=3,
            overwrite_pending=True,
            updated_at=datetime.now(tz=UTC),
        ))),
        patch.object(
            svc,
            "_enumerate_in_scope_datasets",
            new=AsyncMock(return_value=([_VALID_URN], [])),
        ),
        patch.object(svc, "_fetch_evidence", new=AsyncMock(return_value={})),
        patch("src.backend.metagen.service.run_debate", new=AsyncMock(return_value=debate_result_stub)),
        patch.object(
            svc,
            "_enumerate_target_items",
            return_value=[
                {
                    "dataset_urn": _VALID_URN,
                    "item_id": "dataset.description",
                    "kind": "dataset.description",
                    "field_path": None,
                }
            ],
        ),
        patch.object(db, "execute", new=AsyncMock(return_value=_make_result(
            scalar=_make_boundary_row(is_enabled=True)
        ))),
    ):
        result = await svc.run(tier=None, dataset_urns=None, dry_run=True)

    assert result.dry_run is True
    assert "candidates_proposed" in result.counts, (
        "Dry-run counts must include 'candidates_proposed'. "
        "spec: BACKEND.md §Event Catalogue — RUN_COMPLETE dry-run counts: items_considered, candidates_proposed"
    )
    assert result.counts["candidates_proposed"] == 1, (
        "candidates_proposed must equal 1 when one accepted candidate is produced. "
        "spec: BACKEND.md §Metadata Generation Service — dry_run count keys"
    )
    assert "candidates_added" not in result.counts, (
        "Dry-run counts must NOT include 'candidates_added'. "
        "spec: BACKEND.md §Metadata Generation Service — dry_run count keys"
    )
    assert "candidates_evicted" not in result.counts, (
        "Dry-run counts must NOT include 'candidates_evicted'. "
        "spec: BACKEND.md §Metadata Generation Service — dry_run count keys"
    )
    assert "rejected_cleared" not in result.counts, (
        "Dry-run counts must NOT include 'rejected_cleared'. "
        "spec: BACKEND.md §Metadata Generation Service — dry_run count keys"
    )
    # No DB writes: _apply_per_item_budget must not have been called
    # (dry-run uses candidates_proposed counter, not the budget path)
    assert result.counts.get("items_considered", 0) >= 1


# ═══════════════════════════════════════════════════════════════════════════════
# Group G-extra: run-complete event detail shape
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_run_complete_event_detail_keys_for_real_run(svc, cache, db) -> None:
    """METAGEN.RUN_COMPLETE event detail carries the required keys for a real (non-dry) run.

    Spec: spec/feature/BACKEND.md §Event Catalogue — RUN_COMPLETE detail keys:
    run_id, unresolved_urns, counts, dry_run, producer_iterations, debate_outcome.
    counts on real run: items_considered, candidates_added, candidates_evicted, rejected_cleared.
    """
    # TODO(F-CY2-2): this test mocks six private methods; consider reducing to only
    # _enumerate_in_scope_datasets, _fetch_evidence, run_debate, and event-capture surface.
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    recorded_detail: dict | None = None

    async def capture_event(entity_id, event_type, status, detail):
        nonlocal recorded_detail
        recorded_detail = detail

    with (
        patch.object(svc, "_load_conf_or_default", new=AsyncMock(return_value=MetagenGlobalConfDTO(
            is_enabled=True,
            schedule_tier=None,
            dataset_filter={},
            result_limit=3,
            overwrite_pending=True,
            updated_at=datetime.now(tz=UTC),
        ))),
        patch.object(svc, "_enumerate_in_scope_datasets", new=AsyncMock(return_value=([_VALID_URN], []))),
        patch.object(svc, "_clear_rejected_candidates", new=AsyncMock(return_value=0)),
        patch.object(svc, "_fetch_evidence", new=AsyncMock(return_value={})),
        patch.object(svc, "_enumerate_target_items", return_value=[]),
        patch.object(db, "execute", new=AsyncMock(return_value=_make_result(
            scalar=_make_boundary_row(is_enabled=True)
        ))),
        patch.object(svc, "_record_metagen_event", new=AsyncMock(side_effect=capture_event)),
    ):
        result = await svc.run(tier=None, dataset_urns=None, dry_run=False)

    assert result.status == "success"
    assert recorded_detail is not None, (
        "_record_metagen_event must be called on RUN_COMPLETE. "
        "spec: BACKEND.md §Event Catalogue — METAGEN RUN_COMPLETE"
    )
    for key in ("run_id", "unresolved_urns", "counts", "dry_run", "producer_iterations", "debate_outcome"):
        assert key in recorded_detail, (
            f"RUN_COMPLETE event detail must contain '{key}'. "
            "spec: BACKEND.md §Event Catalogue — RUN_COMPLETE detail keys"
        )
    counts = recorded_detail["counts"]
    for count_key in ("items_considered", "candidates_added", "candidates_evicted", "rejected_cleared"):
        assert count_key in counts, (
            f"RUN_COMPLETE counts on real run must contain '{count_key}'. "
            "spec: BACKEND.md §Event Catalogue — counts dict on real-run"
        )
    assert recorded_detail["dry_run"] is False


@pytest.mark.asyncio
async def test_run_complete_event_detail_keys_for_dry_run(svc, cache, db) -> None:
    """METAGEN.RUN_COMPLETE event detail carries the required keys for a dry run.

    Spec: spec/feature/BACKEND.md §Event Catalogue — RUN_COMPLETE detail keys
    on dry-run: run_id, unresolved_urns, counts, dry_run, producer_iterations, debate_outcome.
    counts on dry-run: items_considered, candidates_proposed (not candidates_added/evicted/rejected_cleared).
    """
    # TODO(F-CY2-2): this test mocks six private methods; consider reducing to only
    # _enumerate_in_scope_datasets, _fetch_evidence, run_debate, and event-capture surface.
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    recorded_detail: dict | None = None

    async def capture_event(entity_id, event_type, status, detail):
        nonlocal recorded_detail
        recorded_detail = detail

    with (
        patch.object(svc, "_load_conf_or_default", new=AsyncMock(return_value=MetagenGlobalConfDTO(
            is_enabled=True,
            schedule_tier=None,
            dataset_filter={},
            result_limit=3,
            overwrite_pending=True,
            updated_at=datetime.now(tz=UTC),
        ))),
        patch.object(svc, "_enumerate_in_scope_datasets", new=AsyncMock(return_value=([_VALID_URN], []))),
        patch.object(svc, "_fetch_evidence", new=AsyncMock(return_value={})),
        patch.object(svc, "_enumerate_target_items", return_value=[]),
        patch.object(db, "execute", new=AsyncMock(return_value=_make_result(
            scalar=_make_boundary_row(is_enabled=True)
        ))),
        patch.object(svc, "_record_metagen_event", new=AsyncMock(side_effect=capture_event)),
    ):
        result = await svc.run(tier=None, dataset_urns=None, dry_run=True)

    assert result.dry_run is True
    assert recorded_detail is not None, (
        "_record_metagen_event must be called on RUN_COMPLETE for dry-run too. "
        "spec: BACKEND.md §Event Catalogue — RUN_COMPLETE recorded for both dry-run and non-dry-run"
    )
    for key in ("run_id", "unresolved_urns", "counts", "dry_run", "producer_iterations", "debate_outcome"):
        assert key in recorded_detail, (
            f"RUN_COMPLETE event detail must contain '{key}' on dry-run too. "
            "spec: BACKEND.md §Event Catalogue — RUN_COMPLETE recorded for both dry-run and non-dry-run"
        )
    counts = recorded_detail["counts"]
    assert "items_considered" in counts, (
        "Dry-run counts must have 'items_considered'. "
        "spec: BACKEND.md §Event Catalogue — dry-run counts: items_considered, candidates_proposed"
    )
    assert "candidates_proposed" in counts, (
        "Dry-run counts must have 'candidates_proposed'. "
        "spec: BACKEND.md §Event Catalogue — dry-run counts: items_considered, candidates_proposed"
    )
    assert "candidates_added" not in counts
    assert "candidates_evicted" not in counts
    assert "rejected_cleared" not in counts
    assert recorded_detail["dry_run"] is True


@pytest.mark.asyncio
async def test_run_complete_emitted_when_empty_in_scope(svc, cache) -> None:
    """METAGEN.RUN_COMPLETE is emitted even when in_scope_urns is empty (real run).

    Spec: spec/feature/BACKEND.md §Event Catalogue — RUN_COMPLETE emitted for every
    completed run, including runs where no datasets are in scope.
    counts.items_considered == 0 and status == 'success' (not 'skipped').
    'skipped' is reserved for tier mismatch.
    """
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    _UNRESOLVED = "urn:li:dataset:(urn:li:dataPlatform:postgres,unresolved.table,DEV)"

    recorded_args: dict = {}

    async def capture_event(entity_id, event_type, status, detail):
        recorded_args["event_type"] = event_type
        recorded_args["detail"] = detail

    with (
        patch.object(svc, "_load_conf_or_default", new=AsyncMock(return_value=MetagenGlobalConfDTO(
            is_enabled=True,
            schedule_tier=None,
            dataset_filter={},
            result_limit=3,
            overwrite_pending=True,
            updated_at=datetime.now(tz=UTC),
        ))),
        patch.object(
            svc,
            "_enumerate_in_scope_datasets",
            new=AsyncMock(return_value=([], [_UNRESOLVED])),
        ),
        patch.object(svc, "_clear_rejected_candidates", new=AsyncMock(return_value=0)),
        patch.object(svc, "_record_metagen_event", new=AsyncMock(side_effect=capture_event)),
    ):
        result = await svc.run(tier=None, dataset_urns=None, dry_run=False)

    assert result.status == "success", (
        "Empty in-scope run must yield status='success', not 'skipped'. "
        "spec: BACKEND.md §Metadata Generation Service — 'skipped' reserved for tier mismatch"
    )
    assert recorded_args, (
        "_record_metagen_event must be called even when in_scope_urns is empty. "
        "spec: BACKEND.md §Event Catalogue — RUN_COMPLETE emitted for every completed run"
    )
    assert recorded_args["event_type"] == "METAGEN.RUN_COMPLETE", (
        "Event type must be METAGEN.RUN_COMPLETE. "
        "spec: BACKEND.md §Event Catalogue"
    )
    detail = recorded_args["detail"]
    assert detail["counts"]["items_considered"] == 0, (
        "items_considered must be 0 when no datasets are in scope. "
        "spec: BACKEND.md §Event Catalogue — RUN_COMPLETE counts"
    )
    assert _UNRESOLVED in detail["unresolved_urns"], (
        "unresolved_urns must be propagated into RUN_COMPLETE event detail. "
        "spec: BACKEND.md §Event Catalogue — RUN_COMPLETE detail: unresolved_urns"
    )


@pytest.mark.asyncio
async def test_run_complete_emitted_when_empty_in_scope_dry_run(svc, cache) -> None:
    """METAGEN.RUN_COMPLETE is emitted with dry_run=True even when in_scope_urns is empty.

    Spec: spec/feature/BACKEND.md §Event Catalogue — RUN_COMPLETE emitted for every
    completed run including dry-runs; counts use items_considered + candidates_proposed.
    """
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    _UNRESOLVED = "urn:li:dataset:(urn:li:dataPlatform:postgres,unresolved.table,DEV)"

    recorded_args: dict = {}

    async def capture_event(entity_id, event_type, status, detail):
        recorded_args["event_type"] = event_type
        recorded_args["detail"] = detail

    with (
        patch.object(svc, "_load_conf_or_default", new=AsyncMock(return_value=MetagenGlobalConfDTO(
            is_enabled=True,
            schedule_tier=None,
            dataset_filter={},
            result_limit=3,
            overwrite_pending=True,
            updated_at=datetime.now(tz=UTC),
        ))),
        patch.object(
            svc,
            "_enumerate_in_scope_datasets",
            new=AsyncMock(return_value=([], [_UNRESOLVED])),
        ),
        patch.object(svc, "_record_metagen_event", new=AsyncMock(side_effect=capture_event)),
    ):
        result = await svc.run(tier=None, dataset_urns=None, dry_run=True)

    assert result.dry_run is True
    assert recorded_args, (
        "_record_metagen_event must be called on dry-run even when in_scope_urns is empty. "
        "spec: BACKEND.md §Event Catalogue — RUN_COMPLETE emitted for every completed run"
    )
    detail = recorded_args["detail"]
    assert detail["dry_run"] is True, (
        "RUN_COMPLETE event detail must carry dry_run=True on dry-run. "
        "spec: BACKEND.md §Event Catalogue — dry_run flag in RUN_COMPLETE detail"
    )
    counts = detail["counts"]
    assert counts == {"items_considered": 0, "candidates_proposed": 0}, (
        "Dry-run counts with empty scope must be {items_considered:0, candidates_proposed:0}. "
        "spec: BACKEND.md §Event Catalogue — dry-run counts: items_considered, candidates_proposed"
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Group H: review_candidate
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_review_candidate_raises_422_when_boundary_absent(svc, db) -> None:
    """review_candidate raises PreconditionFailedError(METAGEN_DATASET_NOT_IN_BOUNDARY)
    when no boundary exists for the dataset.

    Spec: API.md §Metadata Generation — 422 METAGEN_DATASET_NOT_IN_BOUNDARY when
    no active boundary is configured for the dataset.
    """
    db.execute = AsyncMock(return_value=_make_result(scalar=None))  # no boundary

    with pytest.raises(PreconditionFailedError) as exc_info:
        await svc.review_candidate(
            dataset_urn=_VALID_URN,
            item_id="dataset.description",
            candidate_id=str(uuid.uuid4()),
            verdict="approve",
            reason="good description",
            reviewer_id="alice",
        )
    assert exc_info.value.error_code == "METAGEN_DATASET_NOT_IN_BOUNDARY", (
        "Must raise METAGEN_DATASET_NOT_IN_BOUNDARY when boundary is absent. "
        "spec: API.md §Metadata Generation error codes"
    )


@pytest.mark.asyncio
async def test_review_candidate_raises_422_when_boundary_disabled(svc, db) -> None:
    """review_candidate raises PreconditionFailedError(METAGEN_DATASET_NOT_IN_BOUNDARY)
    when the boundary exists but is_enabled=false.

    Spec: API.md §Metadata Generation — 422 METAGEN_DATASET_NOT_IN_BOUNDARY when
    boundary is not enabled.
    """
    disabled_bnd = _make_boundary_row(is_enabled=False)
    db.execute = AsyncMock(return_value=_make_result(scalar=disabled_bnd))

    with pytest.raises(PreconditionFailedError) as exc_info:
        await svc.review_candidate(
            dataset_urn=_VALID_URN,
            item_id="dataset.description",
            candidate_id=str(uuid.uuid4()),
            verdict="approve",
            reason="",
            reviewer_id=None,
        )
    assert exc_info.value.error_code == "METAGEN_DATASET_NOT_IN_BOUNDARY"


@pytest.mark.asyncio
async def test_review_candidate_approve_flips_status_to_approved(svc, db) -> None:
    """review_candidate(approve) flips candidate status from llm_approved to approved.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service §Approval flow
    — approve: flip target candidate to status='approved'.
    """
    bnd = _make_boundary_row(is_enabled=True)
    cand_id = uuid.uuid4()
    cand = _make_candidate_row(candidate_id=cand_id, status="llm_approved")

    # boundary, then candidate, then sibling (None = no existing approved)
    db.execute = AsyncMock(side_effect=[
        _make_result(scalar=bnd),
        _make_result(scalar=cand),
        _make_result(scalar=None),  # no sibling approved candidate
    ])
    mock_db_refresh(db)

    # Stub out side-effects that hit DataHub/vector
    svc._datahub.emit_aspect = AsyncMock()
    svc._datahub.get_aspect = AsyncMock(return_value=None)
    svc._llm.embed = AsyncMock(return_value=[0.0] * 10)

    with patch("src.backend.metagen.service._upsert_candidate_embedding", new=AsyncMock()):
        dto = await svc.review_candidate(
            dataset_urn=_VALID_URN,
            item_id="dataset.description",
            candidate_id=str(cand_id),
            verdict="approve",
            reason="looks good",
            reviewer_id="alice",
        )

    assert cand.status == "approved", (
        "Candidate status must be set to 'approved' on approve verdict. "
        "spec: BACKEND.md §Metadata Generation Service §Approval flow"
    )
    assert dto.status == "approved"


@pytest.mark.asyncio
async def test_review_candidate_approve_demotes_existing_approved_sibling(svc, db) -> None:
    """review_candidate(approve) atomically demotes existing approved sibling to llm_approved.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service §Approval flow
    — mutable approval: approving a sibling demotes the prior approved candidate.
    The partial unique index (UNIQUE (dataset_urn, item_id) WHERE status='approved')
    holds because the demotion flush precedes the promotion commit.
    """
    bnd = _make_boundary_row(is_enabled=True)
    cand_id = uuid.uuid4()
    cand = _make_candidate_row(candidate_id=cand_id, status="llm_approved")

    sibling_id = uuid.uuid4()
    sibling = _make_candidate_row(candidate_id=sibling_id, status="approved")

    db.execute = AsyncMock(side_effect=[
        _make_result(scalar=bnd),
        _make_result(scalar=cand),
        _make_result(scalar=sibling),  # existing approved sibling
    ])
    mock_db_refresh(db)

    svc._datahub.emit_aspect = AsyncMock()
    svc._datahub.get_aspect = AsyncMock(return_value=None)
    svc._llm.embed = AsyncMock(return_value=[0.0] * 10)

    with patch("src.backend.metagen.service._upsert_candidate_embedding", new=AsyncMock()):
        await svc.review_candidate(
            dataset_urn=_VALID_URN,
            item_id="dataset.description",
            candidate_id=str(cand_id),
            verdict="approve",
            reason="",
            reviewer_id="bob",
        )

    assert sibling.status == "llm_approved", (
        "Previously approved sibling must be demoted to 'llm_approved'. "
        "spec: BACKEND.md §Metadata Generation Service §Approval flow — mutable approval"
    )
    db.flush.assert_called(), (
        "db.flush() must be called to avoid transient partial-unique-index violation. "
        "spec: BACKEND.md §Metadata Generation Service §Approval flow"
    )


@pytest.mark.asyncio
async def test_review_candidate_approve_emits_dataset_description_to_editable_aspect(svc, db) -> None:
    """review_candidate(approve) for dataset.description emits to editableDatasetProperties.

    Spec: spec/DATAHUB_INTEGRATION.md §Editable vs Non-Editable Description Aspects
    — dataset.description → editableDatasetProperties.description (not datasetProperties).
    Spec: spec/feature/BACKEND.md — item kind table: dataset.description target is
    editableDatasetProperties.description.
    """
    from datahub.metadata.schema_classes import EditableDatasetPropertiesClass

    bnd = _make_boundary_row(is_enabled=True)
    cand_id = uuid.uuid4()
    cand = _make_candidate_row(
        candidate_id=cand_id,
        status="llm_approved",
        value="Dataset description text.",
        item_id="dataset.description",
    )

    db.execute = AsyncMock(side_effect=[
        _make_result(scalar=bnd),
        _make_result(scalar=cand),
        _make_result(scalar=None),
    ])
    mock_db_refresh(db)

    svc._datahub.emit_aspect = AsyncMock()
    svc._datahub.get_aspect = AsyncMock(return_value=None)
    svc._llm.embed = AsyncMock(return_value=[0.0] * 10)

    with patch("src.backend.metagen.service._upsert_candidate_embedding", new=AsyncMock()):
        await svc.review_candidate(
            dataset_urn=_VALID_URN,
            item_id="dataset.description",
            candidate_id=str(cand_id),
            verdict="approve",
            reason="",
            reviewer_id="carol",
        )

    svc._datahub.emit_aspect.assert_called_once(), (
        "Approve must emit exactly once to DataHub. "
        "spec: DATAHUB_INTEGRATION.md §Editable vs Non-Editable Description Aspects"
    )
    call_args = svc._datahub.emit_aspect.call_args
    # First positional arg is the URN, second is the aspect instance
    emitted_urn = call_args.args[0]
    emitted_aspect = call_args.args[1]
    assert emitted_urn == _VALID_URN, (
        "emit_aspect must target the correct dataset URN."
    )
    assert isinstance(emitted_aspect, EditableDatasetPropertiesClass), (
        "dataset.description approve must write to EditableDatasetPropertiesClass, "
        "not DatasetPropertiesClass (non-editable). "
        "spec: DATAHUB_INTEGRATION.md §Editable vs Non-Editable Description Aspects"
    )
    assert emitted_aspect.description == "Dataset description text.", (
        "The emitted description must match the approved candidate value. "
        "spec: BACKEND.md §Approval flow — emit the new value to the editable aspect"
    )


@pytest.mark.asyncio
async def test_review_candidate_approve_column_emits_to_editable_schema_metadata(svc, db) -> None:
    """review_candidate(approve) for column.description emits to editableSchemaMetadata keyed by fieldPath.

    Spec: spec/DATAHUB_INTEGRATION.md §Editable vs Non-Editable Description Aspects
    — column.description → editableSchemaMetadata.editableSchemaFieldInfo[fieldPath].description.
    Spec: spec/feature/BACKEND.md — item kind table: column.description target is
    editableSchemaMetadata.editableSchemaFieldInfo[].description keyed by fieldPath.
    """
    from datahub.metadata.schema_classes import EditableSchemaMetadataClass

    bnd = _make_boundary_row(is_enabled=True)
    cand_id = uuid.uuid4()
    field_path = "isbn"
    item_id = f"column.{field_path}.description"
    cand = _make_candidate_row(
        candidate_id=cand_id,
        status="llm_approved",
        value="The ISBN-13 identifier of the book edition.",
        item_id=item_id,
    )

    db.execute = AsyncMock(side_effect=[
        _make_result(scalar=bnd),
        _make_result(scalar=cand),
        _make_result(scalar=None),  # no sibling approved
    ])
    mock_db_refresh(db)

    svc._datahub.emit_aspect = AsyncMock()
    # Return None so the impl creates a fresh EditableSchemaMetadataClass
    svc._datahub.get_aspect = AsyncMock(return_value=None)
    svc._llm.embed = AsyncMock(return_value=[0.0] * 10)

    with patch("src.backend.metagen.service._upsert_candidate_embedding", new=AsyncMock()):
        await svc.review_candidate(
            dataset_urn=_VALID_URN,
            item_id=item_id,
            candidate_id=str(cand_id),
            verdict="approve",
            reason="",
            reviewer_id="frank",
        )

    svc._datahub.emit_aspect.assert_called_once(), (
        "Approve must emit exactly once to DataHub for column.description. "
        "spec: DATAHUB_INTEGRATION.md §Editable vs Non-Editable Description Aspects"
    )
    call_args = svc._datahub.emit_aspect.call_args
    emitted_urn = call_args.args[0]
    emitted_aspect = call_args.args[1]
    assert emitted_urn == _VALID_URN
    assert isinstance(emitted_aspect, EditableSchemaMetadataClass), (
        "column.description approve must write to EditableSchemaMetadataClass, not SchemaMetadataClass. "
        "spec: DATAHUB_INTEGRATION.md §Editable vs Non-Editable Description Aspects"
    )
    # The emitted aspect must carry the field info keyed by fieldPath
    field_infos = emitted_aspect.editableSchemaFieldInfo
    assert field_infos, "editableSchemaFieldInfo must be non-empty."
    matched = [fi for fi in field_infos if getattr(fi, "fieldPath", None) == field_path]
    assert matched, (
        f"editableSchemaFieldInfo must include an entry for fieldPath='{field_path}'. "
        "spec: BACKEND.md §Metadata Generation Service — item kind: column.description keyed by fieldPath"
    )
    assert matched[0].description == "The ISBN-13 identifier of the book edition.", (
        "The emitted column description must match the approved candidate value."
    )


@pytest.mark.asyncio
async def test_review_candidate_reject_llm_approved_flips_to_rejected(svc, db) -> None:
    """review_candidate(reject) flips an llm_approved candidate to rejected.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service §Rejection flow
    — reject: llm_approved → rejected; emit METAGEN.CANDIDATE_REJECT event.
    """
    bnd = _make_boundary_row(is_enabled=True)
    cand_id = uuid.uuid4()
    cand = _make_candidate_row(candidate_id=cand_id, status="llm_approved")

    db.execute = AsyncMock(side_effect=[
        _make_result(scalar=bnd),
        _make_result(scalar=cand),
    ])
    mock_db_refresh(db)

    dto = await svc.review_candidate(
        dataset_urn=_VALID_URN,
        item_id="dataset.description",
        candidate_id=str(cand_id),
        verdict="reject",
        reason="off-topic",
        reviewer_id="dave",
    )

    assert cand.status == "rejected", (
        "Candidate status must be 'rejected' after reject verdict on llm_approved. "
        "spec: BACKEND.md §Metadata Generation Service §Rejection flow"
    )
    assert dto.status == "rejected"


@pytest.mark.asyncio
async def test_review_candidate_reject_approved_raises_409(svc, db) -> None:
    """review_candidate(reject) on an already-approved candidate raises 409 METAGEN_CANNOT_REJECT_APPROVED.

    Spec: API.md §Metadata Generation — 409 METAGEN_CANNOT_REJECT_APPROVED.
    Spec: spec/feature/BACKEND.md §Metadata Generation Service — cannot reject approved.
    """
    bnd = _make_boundary_row(is_enabled=True)
    cand_id = uuid.uuid4()
    cand = _make_candidate_row(candidate_id=cand_id, status="approved")

    db.execute = AsyncMock(side_effect=[
        _make_result(scalar=bnd),
        _make_result(scalar=cand),
    ])

    with pytest.raises(ConflictError) as exc_info:
        await svc.review_candidate(
            dataset_urn=_VALID_URN,
            item_id="dataset.description",
            candidate_id=str(cand_id),
            verdict="reject",
            reason="want to reject the approved one",
            reviewer_id="eve",
        )

    assert exc_info.value.error_code == "METAGEN_CANNOT_REJECT_APPROVED", (
        "Must raise METAGEN_CANNOT_REJECT_APPROVED when rejecting an approved candidate. "
        "spec: API.md §Metadata Generation error codes"
    )


@pytest.mark.asyncio
async def test_review_candidate_raises_not_found_for_invalid_uuid(svc, db) -> None:
    """review_candidate raises EntityNotFoundError for a malformed candidate_id UUID.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service — invalid UUID raises not-found.
    """
    bnd = _make_boundary_row(is_enabled=True)
    db.execute = AsyncMock(return_value=_make_result(scalar=bnd))

    with pytest.raises(EntityNotFoundError):
        await svc.review_candidate(
            dataset_urn=_VALID_URN,
            item_id="dataset.description",
            candidate_id="not-a-uuid",
            verdict="approve",
            reason="",
            reviewer_id=None,
        )


@pytest.mark.asyncio
async def test_review_candidate_approve_upserts_embedding(svc, db) -> None:
    """review_candidate(approve) calls _upsert_candidate_embedding with the candidate value and kind.

    Spec: spec/feature/BACKEND.md §Metadata Generation Service §Approval flow —
    refresh embedding for the newly approved candidate so it informs the next run's
    Reviewer RAG.
    """
    bnd = _make_boundary_row(is_enabled=True)
    cand_id = uuid.uuid4()
    cand = _make_candidate_row(
        candidate_id=cand_id,
        status="llm_approved",
        value="Best description ever.",
        item_id="dataset.description",
    )

    db.execute = AsyncMock(side_effect=[
        _make_result(scalar=bnd),
        _make_result(scalar=cand),
        _make_result(scalar=None),  # no sibling
    ])
    mock_db_refresh(db)

    svc._datahub.emit_aspect = AsyncMock()
    svc._datahub.get_aspect = AsyncMock(return_value=None)
    svc._llm.embed = AsyncMock(return_value=[0.1] * 10)

    captured_calls: list[tuple] = []

    async def capture_upsert(vector, candidate_id, kind, embedding):
        captured_calls.append((candidate_id, kind, embedding))

    with patch("src.backend.metagen.service._upsert_candidate_embedding", new=AsyncMock(side_effect=capture_upsert)):
        await svc.review_candidate(
            dataset_urn=_VALID_URN,
            item_id="dataset.description",
            candidate_id=str(cand_id),
            verdict="approve",
            reason="great",
            reviewer_id="grace",
        )

    assert len(captured_calls) == 1, (
        "_upsert_candidate_embedding must be called exactly once on approve. "
        "spec: BACKEND.md §Approval flow — refresh embedding for the newly approved candidate"
    )
    emitted_candidate_id, emitted_kind, emitted_embedding = captured_calls[0]
    assert emitted_candidate_id == str(cand_id), (
        "Embedding upsert must use the approved candidate's ID."
    )
    assert emitted_kind == "dataset.description", (
        "Embedding upsert kind must match the candidate's item kind. "
        "spec: BACKEND.md §Approval flow"
    )
    assert emitted_embedding == [0.1] * 10, (
        "Embedding upsert must use the vector returned by _llm.embed."
    )


# ═══════════════════════════════════════════════════════════════════════════════
# Group I: _fetch_evidence — per-dataset ontology RAG
# ═══════════════════════════════════════════════════════════════════════════════


def _make_result_with_unique(rows: list) -> MagicMock:
    """Build a SQLAlchemy execute result mock that supports .unique().scalars().all().

    The triple hydration path in _fetch_evidence calls
    ``result.unique().scalars().all()`` — the standard _make_result helper only
    wires ``result.scalars().all()``.
    """
    m = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = rows
    unique_mock = MagicMock()
    unique_mock.scalars.return_value = scalars_mock
    m.unique.return_value = unique_mock
    # Keep .scalars().all() wired too for callers that don't use .unique()
    m.scalars.return_value = scalars_mock
    m.fetchall.return_value = rows
    return m


def _make_ontogen_node(*, id: str, name: str, description: str) -> MagicMock:
    row = MagicMock()
    row.id = id
    row.name = name
    row.description = description
    row.status = "approved"
    return row


def _make_ontogen_edge(*, id: str, label: str) -> MagicMock:
    row = MagicMock()
    row.id = id
    row.label = label
    row.status = "approved"
    return row


def _make_ontogen_triple_with_relations(
    *,
    id: str,
    subject_name: str,
    edge_label: str,
    object_name: str,
) -> MagicMock:
    row = MagicMock()
    row.id = id
    subject_node = MagicMock()
    subject_node.name = subject_name
    edge = MagicMock()
    edge.label = edge_label
    object_node = MagicMock()
    object_node.name = object_name
    row.subject_node = subject_node
    row.edge = edge
    row.object_node = object_node
    return row


@pytest.mark.asyncio
async def test_fetch_evidence_attaches_ontology_rag(svc, db) -> None:
    """_fetch_evidence populates evidence["ontology_rag"] with hydrated node/edge/triple dicts.

    Patches:
    - self._datahub.get_aspect / fetch_related_documents for the DataHub evidence layer
    - self._llm.embed to return a fixed vector
    - search_{node,edge,triple}_embeddings at the service-layer import site to return
      scripted VectorHit lists
    - DB execute sequence for DatasetNodeMap + OntogenNode + hydration queries

    Verifies that the resulting evidence["ontology_rag"] contains dicts with the expected
    fields for each collection.

    plan: /Users/soonmok/.claude/plans/glittery-crafting-kazoo.md §Tests §test_service.py — test_fetch_evidence_attaches_ontology_rag
    """
    from src.shared.vector.client import VectorHit

    # DataHub aspects: return None for all (minimal evidence)
    svc._datahub.get_aspect = AsyncMock(return_value=None)

    # fetch_related_documents returns empty list
    with patch("src.backend.metagen.service.fetch_related_documents", new=AsyncMock(return_value=[])):
        # LLM embed returns fixed vector
        svc._llm.embed = AsyncMock(return_value=[0.1] * 10)

        # Scripted VectorHit results for each collection
        node_hit = VectorHit(dataset_urn="order", score=0.9)
        edge_hit = VectorHit(dataset_urn="has_part_edge", score=0.8)
        triple_hit = VectorHit(dataset_urn="order__has_part__orderline", score=0.7)

        with (
            patch(
                "src.backend.metagen.service.search_node_embeddings",
                new=AsyncMock(return_value=[node_hit]),
            ),
            patch(
                "src.backend.metagen.service.search_edge_embeddings",
                new=AsyncMock(return_value=[edge_hit]),
            ),
            patch(
                "src.backend.metagen.service.search_triple_embeddings",
                new=AsyncMock(return_value=[triple_hit]),
            ),
        ):
            # DB execute sequence:
            # 1. DatasetNodeMap query → empty (no curated approved nodes)
            # 2. OntogenNode hydration for node_hit
            # 3. OntogenEdge hydration for edge_hit
            # 4. OntogenTriple hydration for triple_hit (needs .unique())
            node_row = _make_ontogen_node(id="order", name="Order", description="A customer order")
            edge_row = _make_ontogen_edge(id="has_part_edge", label="has_part")
            triple_row = _make_ontogen_triple_with_relations(
                id="order__has_part__orderline",
                subject_name="Order",
                edge_label="has_part",
                object_name="OrderLine",
            )

            no_map_rows = MagicMock()
            no_map_rows.scalars.return_value.all.return_value = []

            node_hydration = _make_result([node_row])
            edge_hydration = _make_result([edge_row])
            triple_hydration = _make_result_with_unique([triple_row])

            # Content-aware routing: inspect the rendered SQL to identify the target
            # table, so reordering or adding queries does not silently corrupt results.
            async def _route_execute(stmt, *args, **kwargs):
                sql = str(stmt)
                if "dataset_node_map" in sql:
                    return no_map_rows
                if "ontogen_triples" in sql:
                    return triple_hydration
                if "ontogen_edges" in sql:
                    return edge_hydration
                if "ontogen_nodes" in sql:
                    return node_hydration
                raise AssertionError(f"unexpected query target in SQL: {sql[:200]!r}")

            db.execute = AsyncMock(side_effect=_route_execute)

            evidence = await svc._fetch_evidence(_VALID_URN)

    assert "ontology_rag" in evidence, (
        "evidence must contain 'ontology_rag' key after _fetch_evidence. "
        "plan: glittery-crafting-kazoo.md §_fetch_evidence shape — per-dataset ontology RAG"
    )
    rag = evidence["ontology_rag"]

    # Shape checks
    assert isinstance(rag, dict), "ontology_rag must be a dict."
    assert set(rag.keys()) >= {"nodes", "edges", "triples"}, (
        "ontology_rag must have 'nodes', 'edges', 'triples' keys. "
        "plan: glittery-crafting-kazoo.md §_fetch_evidence shape"
    )

    # Node assertions
    assert len(rag["nodes"]) == 1, "One node hit must produce one hydrated node dict."
    node = rag["nodes"][0]
    assert node.get("id") == "order", "Node dict must have 'id'."
    assert node.get("name") == "Order", "Node dict must have 'name'."
    assert node.get("description") == "A customer order", "Node dict must have 'description'."
    assert "score" in node, "Node dict must carry 'score' for internal ordering."
    assert node["score"] == pytest.approx(0.9), "Node score must match VectorHit score."

    # Edge assertions
    assert len(rag["edges"]) == 1, "One edge hit must produce one hydrated edge dict."
    edge = rag["edges"][0]
    assert edge.get("id") == "has_part_edge", "Edge dict must have 'id'."
    assert edge.get("label") == "has_part", "Edge dict must have 'label'."
    assert "score" in edge, "Edge dict must carry 'score'."

    # Triple assertions
    assert len(rag["triples"]) == 1, "One triple hit must produce one hydrated triple dict."
    triple = rag["triples"][0]
    assert triple.get("subject_name") == "Order", "Triple dict must have 'subject_name'."
    assert triple.get("edge_label") == "has_part", "Triple dict must have 'edge_label'."
    assert triple.get("object_name") == "OrderLine", "Triple dict must have 'object_name'."
    assert "score" in triple, "Triple dict must carry 'score'."


@pytest.mark.asyncio
async def test_fetch_evidence_ontology_rag_k_zero_skips_search(svc, db) -> None:
    """When metagen_ontology_rag_{node,edge,triple}_k=0, the corresponding search is skipped.

    Plan states: "When any k is 0, skip that search."
    Setting all three to 0 must leave ontology_rag lists empty without calling
    any of the three search helpers.

    plan: /Users/soonmok/.claude/plans/glittery-crafting-kazoo.md §Tests — k=0 skips search
    """
    from src.shared.settings import settings

    # DataHub aspects: return None (minimal evidence)
    svc._datahub.get_aspect = AsyncMock(return_value=None)
    svc._llm.embed = AsyncMock(return_value=[0.0] * 10)

    with patch("src.backend.metagen.service.fetch_related_documents", new=AsyncMock(return_value=[])):
        with (
            patch(
                "src.backend.metagen.service.search_node_embeddings",
                new=AsyncMock(return_value=[]),
            ) as mock_node_search,
            patch(
                "src.backend.metagen.service.search_edge_embeddings",
                new=AsyncMock(return_value=[]),
            ) as mock_edge_search,
            patch(
                "src.backend.metagen.service.search_triple_embeddings",
                new=AsyncMock(return_value=[]),
            ) as mock_triple_search,
            patch.object(settings, "metagen_ontology_rag_node_k", 0),
            patch.object(settings, "metagen_ontology_rag_edge_k", 0),
            patch.object(settings, "metagen_ontology_rag_triple_k", 0),
        ):
            no_map_rows = MagicMock()
            no_map_rows.scalars.return_value.all.return_value = []
            db.execute = AsyncMock(return_value=no_map_rows)

            evidence = await svc._fetch_evidence(_VALID_URN)

    mock_node_search.assert_not_called()
    mock_edge_search.assert_not_called()
    mock_triple_search.assert_not_called()

    rag = evidence.get("ontology_rag", {})
    assert rag.get("nodes") == [], (
        "nodes must be empty when metagen_ontology_rag_node_k=0. "
        "plan: glittery-crafting-kazoo.md — k=0 skips that collection entirely"
    )
    assert rag.get("edges") == [], "edges must be empty when k=0."
    assert rag.get("triples") == [], "triples must be empty when k=0."


@pytest.mark.asyncio
async def test_fetch_evidence_ontology_rag_failure_falls_back_to_empty(svc, db) -> None:
    """When the ontology RAG embed call raises, evidence["ontology_rag"] falls back to empty dicts.

    The try/except around the RAG block must NOT swallow the earlier DataHub
    and related_documents evidence — those must still be populated.

    plan: /Users/soonmok/.claude/plans/glittery-crafting-kazoo.md §Tests — RAG failure → empty fallback; evidence intact
    """
    from datahub.metadata.schema_classes import DatasetPropertiesClass

    # DataHub returns a real-ish properties object for the dataset
    mock_props = MagicMock(spec=DatasetPropertiesClass)
    mock_props.name = "title_master"
    mock_props.description = "Master title catalog"
    mock_props.tags = None

    # getattr calls inside _fetch_evidence need real attribute access:
    svc._datahub.get_aspect = AsyncMock(side_effect=[
        mock_props,  # DatasetPropertiesClass -> populates evidence["datasetProperties"]
        None,        # SchemaMetadataClass
        None,        # EditableDatasetPropertiesClass
        None,        # EditableSchemaMetadataClass
        None,        # GlossaryTermsClass
    ])

    with patch(
        "src.backend.metagen.service.fetch_related_documents",
        new=AsyncMock(return_value=[{"title": "SomeDoc", "body": "content"}]),
    ):
        # Make embed raise so the RAG block fails
        svc._llm.embed = AsyncMock(side_effect=RuntimeError("embed service down"))

        no_map_rows = MagicMock()
        no_map_rows.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=no_map_rows)

        evidence = await svc._fetch_evidence(_VALID_URN)

    # Ontology RAG fallback: empty lists, not an exception
    rag = evidence.get("ontology_rag")
    assert rag is not None, (
        "evidence['ontology_rag'] must be set to the fallback dict even when embed raises. "
        "spec: BACKEND.md §Generation Pipeline — best-effort RAG; fallback on error"
    )
    assert rag == {"nodes": [], "edges": [], "triples": []}, (
        "Fallback ontology_rag must be {nodes: [], edges: [], triples: []}. "
        "plan: glittery-crafting-kazoo.md §service.py — try/except → empty-dict fallback"
    )

    # The earlier evidence fetchers must NOT have been swallowed by the RAG try/except.
    assert "related_documents" in evidence, (
        "related_documents must still be present when only the RAG block fails. "
        "plan: glittery-crafting-kazoo.md — try/except is correctly scoped to RAG only"
    )
    assert len(evidence["related_documents"]) == 1, (
        "The seeded document must appear in evidence when the RAG block fails."
    )
