"""Unit tests for src/backend/metagen/service.py — MetagenService (conf collection).

Spec: spec/feature/BACKEND.md §Metadata Generation Service
      spec/USE_CASE_en.md §UC4
      spec/feature/BACKEND_SCHEMA.md §metagen_config / §metagen_candidates
      spec/DATAHUB_INTEGRATION.md §Editable vs Non-Editable Description Aspects

Groups:
  A – Conf collection CRUD (list/create/get/put/patch/delete; duplicate name → 409;
      missing → 404; URN validation)
  B – Boundary CRUD (opt-in semantics)
  C – list_items / get_item (cross-dataset; pagination; status/conf filters)
  D – run() guards (per-conf lock, disabled, missing conf)
  E – run() per-conf per-item budget (FIFO eviction; overwrite_pending=false skip)
  F – run() in-scope enumeration and rejected clearing (per-conf)
  G – run-complete event detail shape
  H – review_candidate (approve demotes sibling incl. cross-conf; reject guard; boundary guard)
  I – _fetch_evidence per-dataset ontology RAG (unchanged behaviour)
  J – list_uncovered (no_conf_match / boundary_blocked reasons)
"""

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.backend.metagen.service import (
    MetagenBoundaryDTO,
    MetagenConfDTO,
    MetagenService,
    RunResultDTO,
    UncoveredRowDTO,
)
from src.shared.exceptions import (
    ConflictError,
    EntityNotFoundError,
    InvalidDatasetUrnError,
    PreconditionFailedError,
)
from tests.unit.backend.conftest import mock_db_refresh

_VALID_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_VALID_URN2 = (
    "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.orders.daily_fulfillment_summary,DEV)"
)
_CONF_UUID = uuid.uuid4()
_CONF_UUID2 = uuid.uuid4()


# ── Factories ─────────────────────────────────────────────────────────────────


def _make_conf_row(
    *,
    conf_id: uuid.UUID | None = None,
    name: str = "catalog-docs",
    is_enabled: bool = True,
    schedule_tier: str | None = "daily",
    dataset_filter: dict[str, Any] | None = None,
    result_limit: int = 3,
    overwrite_pending: bool = True,
) -> MagicMock:
    row = MagicMock()
    row.id = conf_id or _CONF_UUID
    row.name = name
    row.is_enabled = is_enabled
    row.schedule_tier = schedule_tier
    row.dataset_filter = dataset_filter or {}
    row.result_limit = result_limit
    row.overwrite_pending = overwrite_pending
    row.created_at = datetime.now(tz=UTC)
    row.updated_at = datetime.now(tz=UTC)
    return row


def _make_conf_dto(
    *,
    conf_id: uuid.UUID | None = None,
    name: str = "catalog-docs",
    is_enabled: bool = True,
    schedule_tier: str | None = "daily",
    dataset_filter: dict[str, Any] | None = None,
    result_limit: int = 3,
    overwrite_pending: bool = True,
) -> MetagenConfDTO:
    return MetagenConfDTO(
        id=str(conf_id or _CONF_UUID),
        name=name,
        is_enabled=is_enabled,
        schedule_tier=schedule_tier,
        dataset_filter=dataset_filter or {},
        result_limit=result_limit,
        overwrite_pending=overwrite_pending,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )


def _make_boundary_row(
    *,
    dataset_urn: str = _VALID_URN,
    is_enabled: bool = True,
    allowed: list[str] | None = None,
) -> MagicMock:
    row = MagicMock()
    row.dataset_urn = dataset_urn
    row.is_enabled = is_enabled
    row.allowed = allowed or ["dataset.description", "column.description"]
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
    conf_id: uuid.UUID | None = None,
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
    row.conf_id = conf_id if conf_id is not None else _CONF_UUID
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
        # Some queries iterate the result directly via result.all() (no .scalars()).
        m.all.return_value = rows_or_none
    return m


# ── Fixture ───────────────────────────────────────────────────────────────────


@pytest.fixture
def svc(datahub, db, cache, llm, vector) -> MetagenService:
    return MetagenService(datahub=datahub, db=db, cache=cache, llm=llm, vector=vector)


# ═══════════════════════════════════════════════════════════════════════════════
# Group A: Conf collection CRUD
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_confs_returns_dtos_and_total(svc, db) -> None:
    """list_confs returns a list of MetagenConfDTO plus a total count.

    Spec: API.md §Metadata Generation — GET /metagen/conf lists confs (paginated).
    """
    id_a, id_b = uuid.uuid4(), uuid.uuid4()
    count_result = _make_result(scalar=2)
    rows_result = _make_result(
        [_make_conf_row(conf_id=id_a, name="a"), _make_conf_row(conf_id=id_b, name="b")]
    )
    # list_confs then issues two grouped rollup queries (dataset_affected_count,
    # last_run_at), each iterated via result.all(). Only conf "a" has candidates;
    # only conf "b" has a recorded run.
    last_run_ts = datetime.now(tz=UTC)
    affected_result = _make_result([(id_a, 5)])
    last_run_result = _make_result([(str(id_b), last_run_ts)])
    db.execute = AsyncMock(
        side_effect=[count_result, rows_result, affected_result, last_run_result]
    )

    confs, total = await svc.list_confs(offset=0, limit=20)

    assert total == 2
    assert [c.name for c in confs] == ["a", "b"]
    assert all(isinstance(c, MetagenConfDTO) for c in confs)
    by_name = {c.name: c for c in confs}
    # Rollups attach per-conf; confs without candidates/runs fall back to 0 / None.
    assert by_name["a"].dataset_affected_count == 5
    assert by_name["a"].last_run_at is None
    assert by_name["b"].dataset_affected_count == 0
    assert by_name["b"].last_run_at == last_run_ts


@pytest.mark.asyncio
async def test_get_conf_returns_dto_when_present(svc, db) -> None:
    """get_conf returns a MetagenConfDTO when the row exists.

    Spec: API.md §Metadata Generation — GET /metagen/conf/{conf_id}.
    """
    db.execute = AsyncMock(return_value=_make_result(scalar=_make_conf_row(is_enabled=True)))

    result = await svc.get_conf(str(_CONF_UUID))

    assert isinstance(result, MetagenConfDTO)
    assert result.is_enabled is True


@pytest.mark.asyncio
async def test_get_conf_raises_not_found_when_absent(svc, db) -> None:
    """get_conf raises EntityNotFoundError (→404 METAGEN_CONF_NOT_FOUND) when absent.

    Spec: API.md §Metadata Generation — 404 METAGEN_CONF_NOT_FOUND.
    """
    db.execute = AsyncMock(return_value=_make_result(scalar=None))

    with pytest.raises(EntityNotFoundError):
        await svc.get_conf(str(_CONF_UUID))


@pytest.mark.asyncio
async def test_get_conf_raises_not_found_for_malformed_uuid(svc, db) -> None:
    """get_conf raises EntityNotFoundError for a non-UUID conf_id.

    Spec: feature/BACKEND.md §Metadata Generation Service — id is a UUID; a malformed
    id resolves to not-found rather than a 500.
    """
    with pytest.raises(EntityNotFoundError):
        await svc.get_conf("not-a-uuid")


@pytest.mark.asyncio
async def test_create_conf_validates_malformed_urn(svc) -> None:
    """create_conf raises InvalidDatasetUrnError for malformed URNs in dataset_filter.

    Spec: feature/BACKEND.md §Metadata Generation Service — validates dataset_filter.dataset_urns.
    """
    with pytest.raises(InvalidDatasetUrnError):
        await svc.create_conf(
            {"name": "c", "dataset_filter": {"dataset_urns": ["not-a-valid-urn"]}}
        )


@pytest.mark.asyncio
async def test_create_conf_persists_and_returns_dto(svc, db) -> None:
    """create_conf inserts a new conf row and returns its DTO.

    Spec: API.md §Metadata Generation — POST /metagen/conf → 201 with the created conf.
    """
    # 1) name-collision check → none; subsequent commits/refresh; event row insert
    db.execute = AsyncMock(return_value=_make_result(scalar=None))
    mock_db_refresh(db)

    result = await svc.create_conf({"name": "orders-docs", "is_enabled": True, "result_limit": 5})

    assert isinstance(result, MetagenConfDTO)
    assert result.name == "orders-docs"
    db.add.assert_called()
    db.commit.assert_called()


@pytest.mark.asyncio
async def test_create_conf_duplicate_name_raises_conflict(svc, db) -> None:
    """create_conf raises ConflictError(METAGEN_CONF_EXISTS) on a duplicate name.

    Spec: API.md §Metadata Generation — name unique (409 METAGEN_CONF_EXISTS).
    """
    # name-collision check returns an existing id
    db.execute = AsyncMock(return_value=_make_result(scalar=uuid.uuid4()))

    with pytest.raises(ConflictError) as exc_info:
        await svc.create_conf({"name": "taken"})

    assert exc_info.value.error_code == "METAGEN_CONF_EXISTS"


@pytest.mark.asyncio
async def test_put_conf_replaces_existing_row(svc, db) -> None:
    """put_conf fully replaces an existing conf row.

    Spec: feature/BACKEND.md §Metadata Generation Service — PUT is full replacement.
    """
    existing = _make_conf_row(name="catalog-docs", is_enabled=False, schedule_tier="hourly")
    # load_conf_row → existing; name unchanged so no collision query; commit/refresh
    db.execute = AsyncMock(return_value=_make_result(scalar=existing))
    mock_db_refresh(db)

    result = await svc.put_conf(
        str(_CONF_UUID),
        {"name": "catalog-docs", "is_enabled": True, "schedule_tier": "daily", "result_limit": 10},
    )

    assert isinstance(result, MetagenConfDTO)
    assert existing.is_enabled is True
    assert existing.result_limit == 10


@pytest.mark.asyncio
async def test_put_conf_raises_not_found_when_absent(svc, db) -> None:
    """put_conf raises EntityNotFoundError when the conf does not exist.

    Spec: API.md §Metadata Generation — 404 METAGEN_CONF_NOT_FOUND.
    """
    db.execute = AsyncMock(return_value=_make_result(scalar=None))

    with pytest.raises(EntityNotFoundError):
        await svc.put_conf(str(_CONF_UUID), {"name": "c", "is_enabled": True})


@pytest.mark.asyncio
async def test_put_conf_name_collision_raises_conflict(svc, db) -> None:
    """put_conf renaming to a name owned by another conf raises METAGEN_CONF_EXISTS.

    Spec: API.md §Metadata Generation — name unique (409 METAGEN_CONF_EXISTS).
    """
    existing = _make_conf_row(name="old-name")
    # load_conf_row → existing; then collision-check finds another row's id
    db.execute = AsyncMock(
        side_effect=[_make_result(scalar=existing), _make_result(scalar=uuid.uuid4())]
    )

    with pytest.raises(ConflictError) as exc_info:
        await svc.put_conf(str(_CONF_UUID), {"name": "new-name", "is_enabled": True})

    assert exc_info.value.error_code == "METAGEN_CONF_EXISTS"


@pytest.mark.asyncio
async def test_patch_conf_updates_only_provided_fields(svc, db) -> None:
    """patch_conf updates only the provided fields.

    Spec: API_DESIGN_PRINCIPLE_en.md §HTTP method semantics — PATCH is partial update.
    """
    existing = _make_conf_row(is_enabled=False, result_limit=3)
    db.execute = AsyncMock(return_value=_make_result(scalar=existing))
    mock_db_refresh(db)

    result = await svc.patch_conf(str(_CONF_UUID), {"is_enabled": True})

    assert isinstance(result, MetagenConfDTO)
    assert existing.is_enabled is True
    assert existing.result_limit == 3  # untouched


@pytest.mark.asyncio
async def test_patch_conf_raises_not_found_when_absent(svc, db) -> None:
    """patch_conf raises EntityNotFoundError when the conf does not exist.

    Spec: API.md §Metadata Generation — 404 METAGEN_CONF_NOT_FOUND.
    """
    db.execute = AsyncMock(return_value=_make_result(scalar=None))

    with pytest.raises(EntityNotFoundError):
        await svc.patch_conf(str(_CONF_UUID), {"is_enabled": True})


@pytest.mark.asyncio
async def test_delete_conf_deletes_row_and_commits(svc, db) -> None:
    """delete_conf loads the row, deletes it, and commits.

    Spec: feature/BACKEND.md §Metadata Generation Service — DELETE removes the conf;
    the metagen_candidates.conf_id FK (ondelete=SET NULL) orphans all of its
    candidates regardless of status. Items, candidates, and embeddings are retained.
    """
    existing = _make_conf_row()
    # _load_conf_row → existing; then delete row (FK SET NULL orphans candidates)
    db.execute = AsyncMock(return_value=_make_result(scalar=existing))

    await svc.delete_conf(str(_CONF_UUID))

    db.delete.assert_called_once_with(existing)
    db.commit.assert_called()


@pytest.mark.asyncio
async def test_delete_conf_retains_candidates_no_manual_delete_statements(svc, db) -> None:
    """delete_conf RETAINS all candidates/items/embeddings — it issues NO DELETE
    statement of its own. Orphaning is the DB FK's job (conf_id ondelete=SET NULL).

    A manual ``delete(MetagenCandidate...)`` / ``delete(MetagenCandidateEmbedding...)``
    would strand zero-candidate items and destroy retained results, so the service
    must rely solely on the FK: the only ORM mutation is ``db.delete(conf_row)``, and
    no bulk DELETE statement is executed.

    Spec: feature/BACKEND.md §Metadata Generation Service — deleting a conf retains
      every item, candidate (pending/llm_approved/approved), and candidate embedding;
      they become parentless (conf_id=NULL) forever via the FK SET NULL.
    Spec: feature/BACKEND_SCHEMA.md §metagen_candidates — conf_id FK ON DELETE SET NULL.
    """
    from sqlalchemy.sql.dml import Delete

    existing = _make_conf_row()
    db.execute = AsyncMock(return_value=_make_result(scalar=existing))

    await svc.delete_conf(str(_CONF_UUID))

    # The only ORM delete is the conf row itself — never a candidate/embedding/item.
    db.delete.assert_called_once_with(existing)

    # No bulk DELETE statement was executed (the stale impl deleted non-approved
    # candidates + their embeddings and SET-NULLed approved ones; the new impl does
    # none of that — the FK orphans every candidate regardless of status).
    for call in db.execute.call_args_list:
        stmt = call.args[0] if call.args else None
        assert not isinstance(stmt, Delete), (
            "delete_conf must not issue any DELETE statement; candidates, embeddings, "
            "and items are retained and orphaned by the FK SET NULL. "
            "spec: feature/BACKEND.md §Metadata Generation Service — retention on conf delete"
        )


@pytest.mark.asyncio
async def test_delete_conf_raises_not_found_when_absent(svc, db) -> None:
    """delete_conf raises EntityNotFoundError when the conf does not exist.

    Spec: API.md §Metadata Generation — 404 METAGEN_CONF_NOT_FOUND.
    """
    db.execute = AsyncMock(return_value=_make_result(scalar=None))

    with pytest.raises(EntityNotFoundError):
        await svc.delete_conf(str(_CONF_UUID))


# ── _enumerate_in_scope_datasets — delegate to resolve_dataset_scope ─────────


@pytest.mark.asyncio
async def test_enumerate_in_scope_calls_datahub_with_origin(svc, db, datahub) -> None:
    """_enumerate_in_scope_datasets passes origin from conf.dataset_filter to DataHub.

    Spec: feature/BACKEND.md §Metadata Generation Service — dataset_filter origin
    AND-ed with the OR-group when resolving scope; forwarded as-is to DataHub.
    """
    datahub.enumerate_datasets = AsyncMock(return_value=[_VALID_URN])

    bnd_result = MagicMock()
    bnd_result.fetchall.return_value = [MagicMock(dataset_urn=_VALID_URN)]
    db.execute = AsyncMock(return_value=bnd_result)

    conf = _make_conf_dto(
        dataset_filter={"origin": "DEV", "tags": ["urn:li:tag:area:fulfillment"]},
    )
    in_scope, _unresolved = await svc._enumerate_in_scope_datasets(conf, None)

    assert datahub.enumerate_datasets.called
    for call in datahub.enumerate_datasets.call_args_list:
        assert call.kwargs.get("origin") == "DEV"
    assert _VALID_URN in in_scope


@pytest.mark.asyncio
async def test_enumerate_in_scope_intersects_with_enabled_boundary(svc, db, datahub) -> None:
    """_enumerate_in_scope_datasets intersects matched URNs with is_enabled=true boundary rows.

    Spec: feature/BACKEND.md §Generation Pipeline step 1 — in-scope = dataset_filter match
    ∩ datasets with an is_enabled=true boundary; boundary-less datasets are excluded.
    """
    datahub.enumerate_datasets = AsyncMock(return_value=[_VALID_URN, _VALID_URN2])

    # Only _VALID_URN has an enabled boundary row
    bnd_result = MagicMock()
    bnd_result.fetchall.return_value = [MagicMock(dataset_urn=_VALID_URN)]
    db.execute = AsyncMock(return_value=bnd_result)

    conf = _make_conf_dto(dataset_filter={})
    in_scope, _unresolved = await svc._enumerate_in_scope_datasets(conf, None)

    assert in_scope == [_VALID_URN], (
        "Only datasets with an is_enabled=true boundary remain in scope. "
        "spec: feature/BACKEND.md §Generation Pipeline step 1"
    )


@pytest.mark.asyncio
async def test_enumerate_in_scope_empty_override_falls_through_to_conf_urns(
    svc, db, datahub
) -> None:
    """override_urns=[] falls through to conf.dataset_filter.dataset_urns.

    Spec: API.md §Metadata Generation (/spoke/metagen) — body dataset_urns narrows scope; empty
    list must not suppress the conf's own dataset_urns.
    """
    _CONF_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,db.t_conf,DEV)"

    from datahub.metadata.schema_classes import DatasetPropertiesClass

    datahub.get_aspect = AsyncMock(return_value=MagicMock(spec=DatasetPropertiesClass))

    bnd_result = MagicMock()
    bnd_result.fetchall.return_value = [MagicMock(dataset_urn=_CONF_URN)]
    db.execute = AsyncMock(return_value=bnd_result)

    conf = _make_conf_dto(dataset_filter={"dataset_urns": [_CONF_URN]})

    resolved_urns, _unresolved = await svc._enumerate_in_scope_datasets(conf, override_urns=[])

    assert _CONF_URN in resolved_urns


# ═══════════════════════════════════════════════════════════════════════════════
# Group B: Boundary CRUD
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_boundary_returns_none_when_absent(svc, db) -> None:
    """get_boundary returns None when no boundary row exists.

    Spec: API.md §Metadata Generation — GET /data/{urn}/attr/metagen/boundary returns null.
    """
    db.execute = AsyncMock(return_value=_make_result(scalar=None))
    assert await svc.get_boundary(_VALID_URN) is None


@pytest.mark.asyncio
async def test_get_boundary_returns_dto_when_present(svc, db) -> None:
    """get_boundary returns a MetagenBoundaryDTO when the row exists.

    Spec: API.md §Metadata Generation — GET /data/{urn}/attr/metagen/boundary.
    """
    db.execute = AsyncMock(return_value=_make_result(scalar=_make_boundary_row()))
    result = await svc.get_boundary(_VALID_URN)
    assert isinstance(result, MetagenBoundaryDTO)
    assert result.dataset_urn == _VALID_URN


@pytest.mark.asyncio
async def test_put_boundary_rejects_invalid_kind(svc) -> None:
    """put_boundary raises PreconditionFailedError(INVALID_PARAMETER) for unknown allowed kind.

    Spec: feature/BACKEND.md §Metadata Generation Service — allowed ∈
    {dataset.description, column.description}.
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

    result = await svc.put_boundary(
        _VALID_URN, {"is_enabled": True, "allowed": ["dataset.description"]}
    )

    assert isinstance(result, MetagenBoundaryDTO)
    db.add.assert_called()
    db.commit.assert_called()


@pytest.mark.asyncio
async def test_patch_boundary_raises_not_found_when_absent(svc, db) -> None:
    """patch_boundary raises EntityNotFoundError when no boundary exists.

    Spec: feature/BACKEND.md §Metadata Generation Service — PATCH requires existing boundary.
    """
    db.execute = AsyncMock(return_value=_make_result(scalar=None))
    with pytest.raises(EntityNotFoundError):
        await svc.patch_boundary(_VALID_URN, {"is_enabled": False})


@pytest.mark.asyncio
async def test_delete_boundary_raises_not_found_when_absent(svc, db) -> None:
    """delete_boundary raises EntityNotFoundError when no boundary exists.

    Spec: feature/BACKEND.md §Metadata Generation Service — delete raises when absent.
    """
    db.execute = AsyncMock(return_value=_make_result(scalar=None))
    with pytest.raises(EntityNotFoundError):
        await svc.delete_boundary(_VALID_URN)


@pytest.mark.asyncio
async def test_delete_boundary_deletes_and_commits(svc, db) -> None:
    """delete_boundary deletes the boundary row and commits.

    Spec: feature/BACKEND.md §Metadata Generation Service — boundary delete.
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
    db.execute = AsyncMock(side_effect=[_make_result(scalar=0), _make_result([])])

    items, total = await svc.list_items(offset=0, limit=20)

    assert items == []
    assert total == 0


@pytest.mark.asyncio
async def test_list_items_summary_has_candidate_and_non_rejected_counts(svc, db) -> None:
    """list_items returns ItemSummaryDTO carrying candidate_count and non_rejected_count.

    Spec: feature/BACKEND.md §Item status — status derived over non-rejected candidates;
    the summary DTO exposes both candidate_count and non_rejected_count.
    """
    item = _make_item_row()
    cand1 = _make_candidate_row(status="llm_approved")
    cand2 = _make_candidate_row(status="rejected")

    rows_m = MagicMock()
    rows_m.scalars.return_value.all.return_value = [item]
    db.execute = AsyncMock(
        side_effect=[_make_result(scalar=1), rows_m, _make_result([cand1, cand2])]
    )

    items, total = await svc.list_items(offset=0, limit=20)

    assert total == 1
    assert items[0].candidate_count == 2
    assert items[0].non_rejected_count == 1, (
        "non_rejected_count must exclude rejected candidates. "
        "spec: feature/BACKEND.md §Item status"
    )


@pytest.mark.asyncio
async def test_list_items_with_conf_filter_resolves_uuid(svc, db) -> None:
    """list_items(conf_id=...) accepts a valid conf UUID and queries without error.

    Spec: API.md §Metadata Generation — item list filterable by conf_id.
    """
    db.execute = AsyncMock(side_effect=[_make_result(scalar=0), _make_result([])])

    items, total = await svc.list_items(conf_id=str(_CONF_UUID), offset=0, limit=20)

    assert items == []
    assert total == 0


@pytest.mark.asyncio
async def test_list_items_with_malformed_conf_id_raises_not_found(svc, db) -> None:
    """list_items(conf_id='not-a-uuid') raises EntityNotFoundError.

    Spec: feature/BACKEND.md §Metadata Generation Service — conf_id is a UUID.
    """
    with pytest.raises(EntityNotFoundError):
        await svc.list_items(conf_id="not-a-uuid")


@pytest.mark.asyncio
async def test_get_item_raises_not_found_for_absent_item(svc, db) -> None:
    """get_item raises EntityNotFoundError when item does not exist.

    Spec: feature/BACKEND.md §Metadata Generation Service — 404 for absent item.
    """
    db.execute = AsyncMock(return_value=_make_result(scalar=None))

    with pytest.raises(EntityNotFoundError):
        await svc.get_item(_VALID_URN, "dataset.description")


@pytest.mark.asyncio
async def test_get_item_returns_detail_with_candidate_conf_name(svc, db) -> None:
    """get_item returns ItemDetailDTO; each candidate carries its conf_name.

    Spec: API.md §Metadata Generation — item detail includes candidates with conf_id/conf_name.
    """
    item = _make_item_row()
    cand = _make_candidate_row(conf_id=_CONF_UUID)

    cands_result = MagicMock()
    cands_result.scalars.return_value.all.return_value = [cand]
    # conf-name map query
    name_row = MagicMock()
    name_row.id = _CONF_UUID
    name_row.name = "catalog-docs"
    name_map_result = MagicMock()
    name_map_result.all.return_value = [name_row]

    db.execute = AsyncMock(
        side_effect=[_make_result(scalar=item), cands_result, name_map_result]
    )

    detail = await svc.get_item(_VALID_URN, "dataset.description")

    assert detail.dataset_urn == _VALID_URN
    assert len(detail.candidates) == 1
    assert detail.candidates[0].conf_id == str(_CONF_UUID)
    assert detail.candidates[0].conf_name == "catalog-docs"


# ═══════════════════════════════════════════════════════════════════════════════
# Group D: run() guards
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_run_raises_not_found_when_conf_absent(svc, db) -> None:
    """run(conf_id) raises EntityNotFoundError when the conf does not exist.

    Spec: API.md §Metadata Generation — 404 METAGEN_CONF_NOT_FOUND.
    """
    db.execute = AsyncMock(return_value=_make_result(scalar=None))

    with pytest.raises(EntityNotFoundError):
        await svc.run(str(_CONF_UUID), dataset_urns=None, dry_run=False)


@pytest.mark.asyncio
async def test_run_raises_conflict_when_lock_held(svc, cache, db) -> None:
    """run() raises ConflictError(METAGEN_RUNNING) when this conf's Redis lock is held.

    Spec: feature/BACKEND.md §Concurrency — runs serialised per conf by
    metagen:running:{conf_id}; a duplicate run returns 409 METAGEN_RUNNING.
    """
    db.execute = AsyncMock(return_value=_make_result(scalar=_make_conf_row()))
    cache.set_nx = AsyncMock(return_value=False)

    with pytest.raises(ConflictError) as exc_info:
        await svc.run(str(_CONF_UUID), dataset_urns=None, dry_run=False)

    assert exc_info.value.error_code == "METAGEN_RUNNING"


@pytest.mark.asyncio
async def test_run_uses_per_conf_lock_key(svc, cache, db) -> None:
    """run() acquires the per-conf lock metagen:running:{conf_id}.

    Spec: feature/BACKEND.md §Concurrency — distinct confs run concurrently; the lock
    key is scoped to the conf_id, not a global singleton.
    """
    db.execute = AsyncMock(return_value=_make_result(scalar=_make_conf_row()))
    cache.set_nx = AsyncMock(return_value=False)

    with pytest.raises(ConflictError):
        await svc.run(str(_CONF_UUID), dataset_urns=None, dry_run=False)

    lock_key = cache.set_nx.call_args.args[0]
    assert lock_key == f"metagen:running:{_CONF_UUID}", (
        "Run must use a per-conf lock key. "
        "spec: feature/BACKEND.md §Concurrency — metagen:running:{conf_id}"
    )


@pytest.mark.asyncio
async def test_run_raises_conflict_when_disabled_and_not_dry_run(svc, cache, db) -> None:
    """run() raises ConflictError(METAGEN_DISABLED) when conf.is_enabled=false and dry_run=false.

    Spec: API.md §Metadata Generation — 409 METAGEN_DISABLED.
    """
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    db.execute = AsyncMock(return_value=_make_result(scalar=_make_conf_row(is_enabled=False)))

    with pytest.raises(ConflictError) as exc_info:
        await svc.run(str(_CONF_UUID), dataset_urns=None, dry_run=False)

    assert exc_info.value.error_code == "METAGEN_DISABLED"


@pytest.mark.asyncio
async def test_run_allows_dry_run_when_disabled(svc, cache, db) -> None:
    """run(dry_run=True) is permitted even when conf.is_enabled=false.

    Spec: feature/BACKEND.md §Disabled-config rejection — dry-run is permitted regardless
    of is_enabled.
    """
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    conf_row = _make_conf_row(is_enabled=False, dataset_filter={"dataset_urns": []})
    db.execute = AsyncMock(return_value=_make_result(scalar=conf_row))

    with patch.object(
        svc, "_enumerate_in_scope_datasets", new=AsyncMock(return_value=([], []))
    ):
        result = await svc.run(str(_CONF_UUID), dataset_urns=[], dry_run=True)

    assert isinstance(result, RunResultDTO)
    assert result.conf_id == str(_CONF_UUID)


# ═══════════════════════════════════════════════════════════════════════════════
# Group E: per-(conf, item) budget (via _apply_per_item_budget)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_apply_per_item_budget_adds_when_under_limit(svc, db) -> None:
    """_apply_per_item_budget adds a new candidate when this conf's count < result_limit.

    Spec: feature/BACKEND.md §Per-item budget — budget counted per (conf, item).
    """
    conf = _make_conf_dto(result_limit=3, overwrite_pending=True)
    item_row = _make_item_row()

    db.execute = AsyncMock(side_effect=[_make_result(scalar=item_row), _make_result(scalar=1)])
    mock_db_refresh(db)
    svc._refresh_candidate_embedding = AsyncMock()

    added, evicted = await svc._apply_per_item_budget(
        urn=_VALID_URN,
        item_id="dataset.description",
        new_candidate_value="New description.",
        new_candidate_confidence=0.85,
        new_candidate_evidence={},
        run_id=uuid.uuid4(),
        conf_id=_CONF_UUID,
        conf=conf,
    )

    assert added is True
    assert evicted is False


@pytest.mark.asyncio
async def test_apply_per_item_budget_evicts_oldest_when_overwrite_pending_true(svc, db) -> None:
    """_apply_per_item_budget evicts this conf's oldest llm_approved when budget full
    and overwrite_pending=true.

    Spec: feature/BACKEND.md §Per-item budget — FIFO eviction of this conf's oldest
    llm_approved; other confs' candidates untouched.
    """
    conf = _make_conf_dto(result_limit=2, overwrite_pending=True)
    item_row = _make_item_row()
    oldest_llm = _make_candidate_row(status="llm_approved", conf_id=_CONF_UUID)

    db.execute = AsyncMock(
        side_effect=[
            _make_result(scalar=item_row),
            _make_result(scalar=2),  # budget full
            _make_result(scalar=oldest_llm),
        ]
    )
    mock_db_refresh(db)
    svc._refresh_candidate_embedding = AsyncMock()

    added, evicted = await svc._apply_per_item_budget(
        urn=_VALID_URN,
        item_id="dataset.description",
        new_candidate_value="Replacement description.",
        new_candidate_confidence=0.9,
        new_candidate_evidence={},
        run_id=uuid.uuid4(),
        conf_id=_CONF_UUID,
        conf=conf,
    )

    assert added is True
    assert evicted is True
    db.delete.assert_called_once_with(oldest_llm)


@pytest.mark.asyncio
async def test_apply_per_item_budget_skips_when_overwrite_pending_false(svc, db) -> None:
    """_apply_per_item_budget returns (False, False) when budget full and overwrite_pending=false.

    Spec: feature/BACKEND.md §Per-item budget — skip new candidate when full and
    overwrite_pending=false.
    """
    conf = _make_conf_dto(result_limit=2, overwrite_pending=False)
    item_row = _make_item_row()

    db.execute = AsyncMock(side_effect=[_make_result(scalar=item_row), _make_result(scalar=2)])

    added, evicted = await svc._apply_per_item_budget(
        urn=_VALID_URN,
        item_id="dataset.description",
        new_candidate_value="Skipped description.",
        new_candidate_confidence=0.9,
        new_candidate_evidence={},
        run_id=uuid.uuid4(),
        conf_id=_CONF_UUID,
        conf=conf,
    )

    assert added is False
    assert evicted is False


@pytest.mark.asyncio
async def test_apply_per_item_budget_skips_when_only_approved_and_budget_full(svc, db) -> None:
    """_apply_per_item_budget returns (False, False) when budget full but no llm_approved
    to evict (all approved).

    Spec: feature/BACKEND.md §Per-item budget — approved candidates are not evicted.
    """
    conf = _make_conf_dto(result_limit=1, overwrite_pending=True)
    item_row = _make_item_row()

    db.execute = AsyncMock(
        side_effect=[
            _make_result(scalar=item_row),
            _make_result(scalar=1),  # budget full
            _make_result(scalar=None),  # no llm_approved available
        ]
    )

    added, evicted = await svc._apply_per_item_budget(
        urn=_VALID_URN,
        item_id="dataset.description",
        new_candidate_value="Cannot add.",
        new_candidate_confidence=0.8,
        new_candidate_evidence={},
        run_id=uuid.uuid4(),
        conf_id=_CONF_UUID,
        conf=conf,
    )

    assert added is False
    assert evicted is False


# ═══════════════════════════════════════════════════════════════════════════════
# Group F: run() in-scope enumeration and rejected candidate clearing (per conf)
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_run_clears_rejected_candidates_before_processing(svc, cache, db) -> None:
    """A real run reports the rejected_cleared count from _clear_rejected_candidates.

    Spec: feature/BACKEND.md §Generation Pipeline step 2 — this conf's rejected candidates
    are cleared at run start; count surfaced in RUN_COMPLETE counts as rejected_cleared.
    """
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    with (
        patch.object(svc, "get_conf", new=AsyncMock(return_value=_make_conf_dto(is_enabled=True))),
        patch.object(
            svc, "_enumerate_in_scope_datasets", new=AsyncMock(return_value=([_VALID_URN], []))
        ),
        patch.object(svc, "_clear_rejected_candidates", new=AsyncMock(return_value=5)),
        patch.object(svc, "_fetch_evidence", new=AsyncMock(return_value={})),
        # inner boundary lookup returns None → URN skipped cleanly
        patch.object(db, "execute", new=AsyncMock(return_value=_make_result(scalar=None))),
    ):
        result = await svc.run(str(_CONF_UUID), dataset_urns=None, dry_run=False)

    assert result.status == "success"
    assert result.counts["rejected_cleared"] == 5
    assert result.conf_id == str(_CONF_UUID)


@pytest.mark.asyncio
async def test_run_dry_run_reports_candidates_proposed_not_added(svc, cache, db) -> None:
    """run(dry_run=True) counts use 'candidates_proposed' (no DB-write count keys).

    Spec: feature/BACKEND.md §Generation Pipeline — dry_run: counts use candidates_proposed;
    no candidates_added/evicted/rejected_cleared.
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
        patch.object(svc, "get_conf", new=AsyncMock(return_value=_make_conf_dto(is_enabled=True))),
        patch.object(
            svc, "_enumerate_in_scope_datasets", new=AsyncMock(return_value=([_VALID_URN], []))
        ),
        patch.object(svc, "_fetch_evidence", new=AsyncMock(return_value={})),
        patch(
            "src.backend.metagen.service.run_debate",
            new=AsyncMock(return_value=debate_result_stub),
        ),
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
        patch.object(
            db,
            "execute",
            new=AsyncMock(return_value=_make_result(scalar=_make_boundary_row(is_enabled=True))),
        ),
    ):
        result = await svc.run(str(_CONF_UUID), dataset_urns=None, dry_run=True)

    assert result.dry_run is True
    assert result.counts["candidates_proposed"] == 1
    assert "candidates_added" not in result.counts
    assert "candidates_evicted" not in result.counts
    assert "rejected_cleared" not in result.counts


# ═══════════════════════════════════════════════════════════════════════════════
# Group G: run-complete event detail shape
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_run_complete_event_detail_keys_for_real_run(svc, cache, db) -> None:
    """RUN_COMPLETE detail carries the required keys (incl. conf_id/conf_name) on a real run.

    Spec: feature/BACKEND.md §Event Catalogue — RUN_COMPLETE detail keys: run_id, conf_id,
    conf_name, unresolved_urns, counts, dry_run, producer_iterations, debate_outcome.
    """
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    recorded_detail: dict | None = None

    async def capture_event(entity_id, event_type, status, detail):
        nonlocal recorded_detail
        recorded_detail = detail

    with (
        patch.object(svc, "get_conf", new=AsyncMock(return_value=_make_conf_dto(is_enabled=True))),
        patch.object(
            svc, "_enumerate_in_scope_datasets", new=AsyncMock(return_value=([_VALID_URN], []))
        ),
        patch.object(svc, "_clear_rejected_candidates", new=AsyncMock(return_value=0)),
        patch.object(svc, "_fetch_evidence", new=AsyncMock(return_value={})),
        patch.object(svc, "_enumerate_target_items", return_value=[]),
        patch.object(
            db,
            "execute",
            new=AsyncMock(return_value=_make_result(scalar=_make_boundary_row(is_enabled=True))),
        ),
        patch.object(svc, "_record_metagen_event", new=AsyncMock(side_effect=capture_event)),
    ):
        result = await svc.run(str(_CONF_UUID), dataset_urns=None, dry_run=False)

    assert result.status == "success"
    assert recorded_detail is not None
    for key in (
        "run_id",
        "conf_id",
        "conf_name",
        "unresolved_urns",
        "counts",
        "dry_run",
        "producer_iterations",
        "debate_outcome",
    ):
        assert key in recorded_detail, (
            f"RUN_COMPLETE detail must contain '{key}'. "
            "spec: feature/BACKEND.md §Event Catalogue — RUN_COMPLETE detail keys"
        )
    assert recorded_detail["conf_id"] == str(_CONF_UUID)
    for count_key in (
        "items_considered",
        "candidates_added",
        "candidates_evicted",
        "rejected_cleared",
    ):
        assert count_key in recorded_detail["counts"]
    assert recorded_detail["dry_run"] is False


@pytest.mark.asyncio
async def test_run_complete_emitted_when_empty_in_scope(svc, cache, db) -> None:
    """METAGEN.RUN_COMPLETE is emitted with items_considered=0 when in_scope is empty (real run).

    Spec: feature/BACKEND.md §Generation Pipeline step 1 — empty in-scope set still completes
    successfully and emits RUN_COMPLETE with all counts at zero.
    """
    cache.set_nx = AsyncMock(return_value=True)
    cache.delete_if_value = AsyncMock()

    _UNRESOLVED = "urn:li:dataset:(urn:li:dataPlatform:postgres,unresolved.table,DEV)"
    db.execute = AsyncMock(return_value=_make_result(scalar=_make_conf_row(is_enabled=True)))
    recorded_args: dict = {}

    async def capture_event(entity_id, event_type, status, detail):
        recorded_args["event_type"] = event_type
        recorded_args["detail"] = detail

    with (
        patch.object(
            svc, "_enumerate_in_scope_datasets", new=AsyncMock(return_value=([], [_UNRESOLVED]))
        ),
        patch.object(svc, "_clear_rejected_candidates", new=AsyncMock(return_value=0)),
        patch.object(svc, "_record_metagen_event", new=AsyncMock(side_effect=capture_event)),
    ):
        result = await svc.run(str(_CONF_UUID), dataset_urns=None, dry_run=False)

    assert result.status == "success"
    assert recorded_args["event_type"] == "METAGEN.RUN_COMPLETE"
    detail = recorded_args["detail"]
    assert detail["counts"]["items_considered"] == 0
    assert _UNRESOLVED in detail["unresolved_urns"]


# ═══════════════════════════════════════════════════════════════════════════════
# Group H: review_candidate
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_review_candidate_raises_422_when_boundary_absent(svc, db) -> None:
    """review_candidate raises METAGEN_DATASET_NOT_IN_BOUNDARY when no boundary exists.

    Spec: API.md §Metadata Generation — 422 METAGEN_DATASET_NOT_IN_BOUNDARY.
    """
    db.execute = AsyncMock(return_value=_make_result(scalar=None))

    with pytest.raises(PreconditionFailedError) as exc_info:
        await svc.review_candidate(
            dataset_urn=_VALID_URN,
            item_id="dataset.description",
            candidate_id=str(uuid.uuid4()),
            verdict="approve",
            reason="good description",
            reviewer_id="alice",
        )
    assert exc_info.value.error_code == "METAGEN_DATASET_NOT_IN_BOUNDARY"


@pytest.mark.asyncio
async def test_review_candidate_raises_422_when_boundary_disabled(svc, db) -> None:
    """review_candidate raises METAGEN_DATASET_NOT_IN_BOUNDARY when boundary is_enabled=false.

    Spec: API.md §Metadata Generation — 422 METAGEN_DATASET_NOT_IN_BOUNDARY.
    """
    db.execute = AsyncMock(return_value=_make_result(scalar=_make_boundary_row(is_enabled=False)))

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
    """review_candidate(approve) flips status from llm_approved to approved.

    Spec: feature/BACKEND.md §Approval flow — approve flips target to status='approved'.
    """
    bnd = _make_boundary_row(is_enabled=True)
    cand_id = uuid.uuid4()
    cand = _make_candidate_row(candidate_id=cand_id, status="llm_approved", conf_id=_CONF_UUID)

    name_row = MagicMock()
    name_row.id = _CONF_UUID
    name_row.name = "catalog-docs"
    name_map_result = MagicMock()
    name_map_result.all.return_value = [name_row]

    db.execute = AsyncMock(
        side_effect=[
            _make_result(scalar=bnd),
            _make_result(scalar=cand),
            _make_result(scalar=None),  # no sibling approved
            name_map_result,  # conf-name resolution
        ]
    )
    mock_db_refresh(db)

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

    assert cand.status == "approved"
    assert dto.status == "approved"
    assert dto.conf_name == "catalog-docs"


@pytest.mark.asyncio
async def test_review_candidate_approve_demotes_cross_conf_approved_sibling(svc, db) -> None:
    """review_candidate(approve) demotes an existing approved sibling, even one from a
    DIFFERENT conf, in the same transaction.

    Spec: feature/BACKEND.md §Approval flow — approving a candidate atomically demotes the
    approved sibling from any other conf; one-approved-per-item holds globally across confs.
    The demotion flush precedes the promotion to satisfy the partial unique index.
    """
    bnd = _make_boundary_row(is_enabled=True)
    cand_id = uuid.uuid4()
    cand = _make_candidate_row(candidate_id=cand_id, status="llm_approved", conf_id=_CONF_UUID)

    # Sibling belongs to a DIFFERENT conf
    sibling = _make_candidate_row(
        candidate_id=uuid.uuid4(), status="approved", conf_id=_CONF_UUID2
    )

    name_row = MagicMock()
    name_row.id = _CONF_UUID
    name_row.name = "catalog-docs"
    name_map_result = MagicMock()
    name_map_result.all.return_value = [name_row]

    db.execute = AsyncMock(
        side_effect=[
            _make_result(scalar=bnd),
            _make_result(scalar=cand),
            _make_result(scalar=sibling),  # cross-conf approved sibling
            name_map_result,
        ]
    )
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
        "A cross-conf approved sibling must be demoted to 'llm_approved'. "
        "spec: feature/BACKEND.md §Approval flow — cross-conf demotion"
    )
    assert cand.status == "approved"
    db.flush.assert_called()


@pytest.mark.asyncio
async def test_review_candidate_approve_emits_dataset_description_to_editable_aspect(
    svc, db
) -> None:
    """review_candidate(approve) for dataset.description emits to editableDatasetProperties.

    Spec: DATAHUB_INTEGRATION.md §Editable vs Non-Editable Description Aspects —
    dataset.description → editableDatasetProperties.description.
    """
    from datahub.metadata.schema_classes import EditableDatasetPropertiesClass

    bnd = _make_boundary_row(is_enabled=True)
    cand_id = uuid.uuid4()
    cand = _make_candidate_row(
        candidate_id=cand_id,
        status="llm_approved",
        value="Dataset description text.",
        item_id="dataset.description",
        conf_id=_CONF_UUID,
    )

    name_map_result = MagicMock()
    name_map_result.all.return_value = []

    db.execute = AsyncMock(
        side_effect=[
            _make_result(scalar=bnd),
            _make_result(scalar=cand),
            _make_result(scalar=None),
            name_map_result,
        ]
    )
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

    svc._datahub.emit_aspect.assert_called_once()
    emitted_urn = svc._datahub.emit_aspect.call_args.args[0]
    emitted_aspect = svc._datahub.emit_aspect.call_args.args[1]
    assert emitted_urn == _VALID_URN
    assert isinstance(emitted_aspect, EditableDatasetPropertiesClass)
    assert emitted_aspect.description == "Dataset description text."


@pytest.mark.asyncio
async def test_review_candidate_approve_column_emits_to_editable_schema_metadata(svc, db) -> None:
    """review_candidate(approve) for column.description emits to editableSchemaMetadata
    keyed by fieldPath.

    Spec: DATAHUB_INTEGRATION.md §Editable vs Non-Editable Description Aspects —
    column.description → editableSchemaMetadata.editableSchemaFieldInfo[fieldPath].description.
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
        conf_id=_CONF_UUID,
    )

    name_map_result = MagicMock()
    name_map_result.all.return_value = []

    db.execute = AsyncMock(
        side_effect=[
            _make_result(scalar=bnd),
            _make_result(scalar=cand),
            _make_result(scalar=None),
            name_map_result,
        ]
    )
    mock_db_refresh(db)

    svc._datahub.emit_aspect = AsyncMock()
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

    svc._datahub.emit_aspect.assert_called_once()
    emitted_aspect = svc._datahub.emit_aspect.call_args.args[1]
    assert isinstance(emitted_aspect, EditableSchemaMetadataClass)
    matched = [
        fi
        for fi in emitted_aspect.editableSchemaFieldInfo
        if getattr(fi, "fieldPath", None) == field_path
    ]
    assert matched
    assert matched[0].description == "The ISBN-13 identifier of the book edition."


@pytest.mark.asyncio
async def test_review_candidate_reject_llm_approved_flips_to_rejected(svc, db) -> None:
    """review_candidate(reject) flips an llm_approved candidate to rejected.

    Spec: feature/BACKEND.md §Approval flow — reject: llm_approved → rejected;
    emit METAGEN.CANDIDATE_REJECT.
    """
    bnd = _make_boundary_row(is_enabled=True)
    cand_id = uuid.uuid4()
    cand = _make_candidate_row(candidate_id=cand_id, status="llm_approved", conf_id=_CONF_UUID)

    name_map_result = MagicMock()
    name_map_result.all.return_value = []

    db.execute = AsyncMock(
        side_effect=[
            _make_result(scalar=bnd),
            _make_result(scalar=cand),
            name_map_result,
        ]
    )
    mock_db_refresh(db)

    # An llm_approved candidate was never written to DataHub, so rejecting it must
    # not touch DataHub (no editable aspect to clear).
    svc._datahub.emit_aspect = AsyncMock()

    dto = await svc.review_candidate(
        dataset_urn=_VALID_URN,
        item_id="dataset.description",
        candidate_id=str(cand_id),
        verdict="reject",
        reason="off-topic",
        reviewer_id="dave",
    )

    assert cand.status == "rejected"
    assert dto.status == "rejected"
    # No DataHub write: only an approved candidate had a live editable aspect to clear.
    svc._datahub.emit_aspect.assert_not_awaited()


@pytest.mark.asyncio
async def test_review_candidate_reject_approved_dataset_desc_clears_datahub(svc, db) -> None:
    """review_candidate(reject) on an APPROVED dataset.description flips to rejected
    AND clears the editableDatasetProperties description it had written.

    Reject is valid on an approved candidate (no error). The approved candidate had
    emitted an editableDatasetProperties.description; rejecting it removes that
    editable aspect (description=None) so the dataset falls back to its non-editable
    description. Mirrors the api-wired case A intent at the unit layer.

    Spec: API.md §Metadata Generation — reject valid on approved candidate; removes editable aspect.
    Spec: feature/BACKEND.md §Approval flow — rejecting an approved candidate flips it to
      rejected and removes the editable DataHub aspect (the editable dataset description
      is cleared while co-located editable fields like name are preserved). The exact
      clear-form (None vs "") and whether the impl reads-back-then-merges are impl
      tactics, not spec guarantees — assert the cleared invariant, not the tactic.
    """
    from datahub.metadata.schema_classes import EditableDatasetPropertiesClass

    bnd = _make_boundary_row(is_enabled=True)
    cand_id = uuid.uuid4()
    cand = _make_candidate_row(candidate_id=cand_id, status="approved", conf_id=_CONF_UUID)

    name_map_result = MagicMock()
    name_map_result.all.return_value = []

    db.execute = AsyncMock(
        side_effect=[
            _make_result(scalar=bnd),
            _make_result(scalar=cand),
            name_map_result,
        ]
    )
    mock_db_refresh(db)

    # The approved candidate had previously written an editable description, co-located
    # with an editable name. Rejecting must clear the description while preserving name.
    existing_props = EditableDatasetPropertiesClass(
        name="EU Customer Profile",
        description="Imazon EU customer profile dataset.",
    )
    svc._datahub.get_aspect = AsyncMock(return_value=existing_props)
    svc._datahub.emit_aspect = AsyncMock()

    dto = await svc.review_candidate(
        dataset_urn=_VALID_URN,
        item_id="dataset.description",
        candidate_id=str(cand_id),
        verdict="reject",
        reason="want to reject the approved one",
        reviewer_id="eve",
    )

    # Status flips to rejected (no error raised).
    assert cand.status == "rejected"
    assert dto.status == "rejected"

    # The editable description is cleared (None or "" — both are valid clear-forms).
    svc._datahub.emit_aspect.assert_awaited_once()
    cleared_urn = svc._datahub.emit_aspect.call_args.args[0]
    cleared_aspect = svc._datahub.emit_aspect.call_args.args[1]
    assert cleared_urn == _VALID_URN
    assert isinstance(cleared_aspect, EditableDatasetPropertiesClass)
    assert cleared_aspect.description in (None, "")
    # Co-located editable name is preserved (the load-bearing merge invariant).
    assert cleared_aspect.name == "EU Customer Profile"


@pytest.mark.asyncio
async def test_review_candidate_reject_approved_column_desc_clears_field_entry(svc, db) -> None:
    """review_candidate(reject) on an APPROVED column.description flips to rejected
    AND drops that field's editableSchemaFieldInfo description from editableSchemaMetadata,
    preserving sibling fields.

    Spec: feature/BACKEND.md §Approval flow — rejecting an approved column.description
      candidate clears that field's description while preserving sibling fields. Whether
      the target entry is dropped or retained-with-None is an impl tactic; the
      sibling-preservation is the load-bearing invariant.
    """
    from datahub.metadata.schema_classes import (
        EditableSchemaFieldInfoClass,
        EditableSchemaMetadataClass,
    )

    bnd = _make_boundary_row(is_enabled=True)
    cand_id = uuid.uuid4()
    field_path = "email"
    item_id = f"column.{field_path}.description"
    cand = _make_candidate_row(
        candidate_id=cand_id,
        status="approved",
        item_id=item_id,
        conf_id=_CONF_UUID,
    )

    name_map_result = MagicMock()
    name_map_result.all.return_value = []

    db.execute = AsyncMock(
        side_effect=[
            _make_result(scalar=bnd),
            _make_result(scalar=cand),
            name_map_result,
        ]
    )
    mock_db_refresh(db)

    # Existing editable schema with the target field + a sibling that must survive.
    existing_schema = EditableSchemaMetadataClass(
        editableSchemaFieldInfo=[
            EditableSchemaFieldInfoClass(fieldPath="email", description="Customer email."),
            EditableSchemaFieldInfoClass(fieldPath="user_id", description="Sibling — keep me."),
        ]
    )
    svc._datahub.get_aspect = AsyncMock(return_value=existing_schema)
    svc._datahub.emit_aspect = AsyncMock()

    dto = await svc.review_candidate(
        dataset_urn=_VALID_URN,
        item_id=item_id,
        candidate_id=str(cand_id),
        verdict="reject",
        reason="reject the approved column desc",
        reviewer_id="eve",
    )

    assert cand.status == "rejected"
    assert dto.status == "rejected"

    svc._datahub.emit_aspect.assert_awaited_once()
    emitted_aspect = svc._datahub.emit_aspect.call_args.args[1]
    assert isinstance(emitted_aspect, EditableSchemaMetadataClass)
    by_path = {
        fi.fieldPath: fi.description for fi in emitted_aspect.editableSchemaFieldInfo
    }
    # Target field's description cleared — tolerates the entry being dropped (absent)
    # OR retained with a None/"" description; all are valid clear-forms.
    assert by_path.get("email") in (None,)
    # Sibling field preserved unchanged (the load-bearing merge invariant).
    assert by_path["user_id"] == "Sibling — keep me."


@pytest.mark.asyncio
async def test_review_candidate_raises_not_found_for_invalid_uuid(svc, db) -> None:
    """review_candidate raises EntityNotFoundError for a malformed candidate_id.

    Spec: feature/BACKEND.md §Metadata Generation Service — invalid UUID → not-found.
    """
    db.execute = AsyncMock(return_value=_make_result(scalar=_make_boundary_row(is_enabled=True)))

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
    """review_candidate(approve) refreshes the candidate embedding with its value + kind.

    Spec: feature/BACKEND.md §Approval flow — refresh embedding for the newly approved
    candidate so it informs the next run's Reviewer RAG.
    """
    bnd = _make_boundary_row(is_enabled=True)
    cand_id = uuid.uuid4()
    cand = _make_candidate_row(
        candidate_id=cand_id,
        status="llm_approved",
        value="Best description ever.",
        item_id="dataset.description",
        conf_id=_CONF_UUID,
    )

    name_map_result = MagicMock()
    name_map_result.all.return_value = []

    db.execute = AsyncMock(
        side_effect=[
            _make_result(scalar=bnd),
            _make_result(scalar=cand),
            _make_result(scalar=None),
            name_map_result,
        ]
    )
    mock_db_refresh(db)

    svc._datahub.emit_aspect = AsyncMock()
    svc._datahub.get_aspect = AsyncMock(return_value=None)
    svc._llm.embed = AsyncMock(return_value=[0.1] * 10)

    captured: list[tuple] = []

    async def capture_upsert(vector, candidate_id, kind, embedding):
        captured.append((candidate_id, kind, embedding))

    with patch(
        "src.backend.metagen.service._upsert_candidate_embedding",
        new=AsyncMock(side_effect=capture_upsert),
    ):
        await svc.review_candidate(
            dataset_urn=_VALID_URN,
            item_id="dataset.description",
            candidate_id=str(cand_id),
            verdict="approve",
            reason="great",
            reviewer_id="grace",
        )

    assert len(captured) == 1
    candidate_id, kind, embedding = captured[0]
    assert candidate_id == str(cand_id)
    assert kind == "dataset.description"
    assert embedding == [0.1] * 10


# ═══════════════════════════════════════════════════════════════════════════════
# Group I: _fetch_evidence — per-dataset ontology RAG
# ═══════════════════════════════════════════════════════════════════════════════


def _make_result_with_unique(rows: list) -> MagicMock:
    """Build a result mock supporting .unique().scalars().all() for triple hydration."""
    m = MagicMock()
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = rows
    unique_mock = MagicMock()
    unique_mock.scalars.return_value = scalars_mock
    m.unique.return_value = unique_mock
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
    *, id: str, subject_name: str, edge_label: str, object_name: str
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

    Spec: feature/BACKEND.md §Generation Pipeline step 3 — per-dataset ontology RAG over
    the three approved-ontology pgvector collections.
    """
    from src.shared.vector.client import VectorHit

    svc._datahub.get_aspect = AsyncMock(return_value=None)

    _no_docs = AsyncMock(return_value=[])
    with patch("src.backend.metagen.service.fetch_related_documents", new=_no_docs):
        svc._llm.embed = AsyncMock(return_value=[0.1] * 10)

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

            from src.backend.admin.config_service import RUNTIME_CONFIG_DEFAULTS, RuntimeConfigDTO

            fake_rc = RuntimeConfigDTO(**RUNTIME_CONFIG_DEFAULTS)
            evidence = await svc._fetch_evidence(_VALID_URN, rc=fake_rc)

    rag = evidence["ontology_rag"]
    assert set(rag.keys()) >= {"nodes", "edges", "triples"}
    assert rag["nodes"][0]["name"] == "Order"
    assert rag["edges"][0]["label"] == "has_part"
    assert rag["triples"][0]["subject_name"] == "Order"
    assert rag["triples"][0]["object_name"] == "OrderLine"


@pytest.mark.asyncio
async def test_fetch_evidence_ontology_rag_k_zero_skips_search(svc, db) -> None:
    """metagen_ontology_rag_{node,edge,triple}_k=0 skips the corresponding search.

    Spec: feature/BACKEND.md §Generation Pipeline step 3 — setting a collection's k to 0
    disables that contribution.
    """
    svc._datahub.get_aspect = AsyncMock(return_value=None)
    svc._llm.embed = AsyncMock(return_value=[0.0] * 10)

    from src.backend.admin.config_service import RUNTIME_CONFIG_DEFAULTS, RuntimeConfigDTO

    zero_k_rc = RuntimeConfigDTO(
        **{
            **RUNTIME_CONFIG_DEFAULTS,
            "metagen_ontology_rag_node_k": 0,
            "metagen_ontology_rag_edge_k": 0,
            "metagen_ontology_rag_triple_k": 0,
        }
    )

    _no_docs = AsyncMock(return_value=[])
    with patch("src.backend.metagen.service.fetch_related_documents", new=_no_docs):
        with (
            patch(
                "src.backend.metagen.service.search_node_embeddings", new=AsyncMock(return_value=[])
            ) as mock_node_search,
            patch(
                "src.backend.metagen.service.search_edge_embeddings", new=AsyncMock(return_value=[])
            ) as mock_edge_search,
            patch(
                "src.backend.metagen.service.search_triple_embeddings",
                new=AsyncMock(return_value=[]),
            ) as mock_triple_search,
        ):
            no_map_rows = MagicMock()
            no_map_rows.scalars.return_value.all.return_value = []
            db.execute = AsyncMock(return_value=no_map_rows)

            evidence = await svc._fetch_evidence(_VALID_URN, rc=zero_k_rc)

    mock_node_search.assert_not_called()
    mock_edge_search.assert_not_called()
    mock_triple_search.assert_not_called()

    rag = evidence.get("ontology_rag", {})
    assert rag.get("nodes") == []
    assert rag.get("edges") == []
    assert rag.get("triples") == []


@pytest.mark.asyncio
async def test_fetch_evidence_ontology_rag_failure_falls_back_to_empty(svc, db) -> None:
    """When the ontology RAG embed call raises, evidence["ontology_rag"] falls back to empty.

    Spec: feature/BACKEND.md §Generation Pipeline step 3 — RAG failure is best-effort;
    the evidence dict falls back to empty lists and the run proceeds.
    """
    from datahub.metadata.schema_classes import DatasetPropertiesClass

    mock_props = MagicMock(spec=DatasetPropertiesClass)
    mock_props.name = "title_master"
    mock_props.description = "Master title catalog"
    mock_props.tags = None

    svc._datahub.get_aspect = AsyncMock(side_effect=[mock_props, None, None, None, None])

    with patch(
        "src.backend.metagen.service.fetch_related_documents",
        new=AsyncMock(return_value=[{"title": "SomeDoc", "body": "content"}]),
    ):
        svc._llm.embed = AsyncMock(side_effect=RuntimeError("embed service down"))

        no_map_rows = MagicMock()
        no_map_rows.scalars.return_value.all.return_value = []
        db.execute = AsyncMock(return_value=no_map_rows)

        from src.backend.admin.config_service import RUNTIME_CONFIG_DEFAULTS, RuntimeConfigDTO

        fake_rc = RuntimeConfigDTO(**RUNTIME_CONFIG_DEFAULTS)
        evidence = await svc._fetch_evidence(_VALID_URN, rc=fake_rc)

    assert evidence.get("ontology_rag") == {"nodes": [], "edges": [], "triples": []}
    assert "related_documents" in evidence
    assert len(evidence["related_documents"]) == 1


# ═══════════════════════════════════════════════════════════════════════════════
# Group J: list_uncovered
# ═══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_uncovered_reports_no_conf_match_for_unmatched_registered_dataset(
    svc, db, datahub
) -> None:
    """A registered dataset matched by no enabled conf yields reason='no_conf_match'.

    Spec: API.md §Metadata Generation — uncovered default mode lists datasets reached by
    no enabled conf with reason=no_conf_match.
    """
    # registered datasets
    reg_result = _make_result([(_VALID_URN,)])
    # no enabled confs
    confs_result = _make_result([])
    # writable boundary set (empty)
    bnd_result = _make_result([])
    db.execute = AsyncMock(side_effect=[reg_result, confs_result, bnd_result])

    rows, total = await svc.list_uncovered(include_disallowed=False)

    assert total == 1
    assert isinstance(rows[0], UncoveredRowDTO)
    assert rows[0].dataset_urn == _VALID_URN
    assert rows[0].reason == "no_conf_match"


@pytest.mark.asyncio
async def test_list_uncovered_boundary_blocked_only_with_include_disallowed(
    svc, db, datahub
) -> None:
    """A dataset matched by an enabled conf but blocked by its boundary yields
    reason='boundary_blocked' only when include_disallowed=true.

    Spec: API.md §Metadata Generation — boundary_blocked surfaced only with
    include_disallowed=true.
    """
    enabled_conf = _make_conf_row(is_enabled=True, dataset_filter={})

    datahub.enumerate_datasets = AsyncMock(return_value=[_VALID_URN])

    def _three_query_sequence():
        # registered, enabled confs, writable boundary (none → blocked)
        return [
            _make_result([(_VALID_URN,)]),
            _make_result([enabled_conf]),
            _make_result([]),
        ]

    # include_disallowed=False → matched dataset is NOT listed (it is covered-ish:
    # matched by a conf, just blocked) → empty
    db.execute = AsyncMock(side_effect=_three_query_sequence())
    rows_default, total_default = await svc.list_uncovered(include_disallowed=False)
    assert total_default == 0, (
        "A conf-matched dataset must not appear under the default mode. "
        "spec: API.md §Metadata Generation — boundary_blocked needs include_disallowed=true"
    )

    # include_disallowed=True → surfaced with reason=boundary_blocked
    datahub.enumerate_datasets = AsyncMock(return_value=[_VALID_URN])
    db.execute = AsyncMock(side_effect=_three_query_sequence())
    rows_incl, total_incl = await svc.list_uncovered(include_disallowed=True)

    assert total_incl == 1
    assert rows_incl[0].dataset_urn == _VALID_URN
    assert rows_incl[0].reason == "boundary_blocked"
