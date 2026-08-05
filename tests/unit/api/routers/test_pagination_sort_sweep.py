"""Unit tests for the app-wide pagination + sort standard.

These assert the shared list-endpoint contract introduced by the pagination/sort
sweep, anchored to spec/API_DESIGN_PRINCIPLE_en.md §5 and spec/API.md per-endpoint
"paginated; sortable by ..." notes:

  - ``?sort=<field>_asc`` reverses the default ordering (router parses it into the
    matching SQLAlchemy ``order_by`` clause and forwards it to the service).
  - Omitting ``sort`` preserves each endpoint's default ordering (router forwards
    ``order_by=None``; the service applies its own default).
  - ``limit`` caps at 1000 (``Query(..., le=1000)``): 1000 accepted, 1001 → 422.
  - The response carries the standard ``offset`` / ``limit`` / ``total_count`` envelope.

Coverage spans one representative endpoint per feature plus the three rebased
admin/auth/secrets list endpoints (UsersList, ApiTokenList, SecretRefList) that
now extend PaginatedResponse and expose ``total_count``.

The validation per-dataset result timeseries (``default 1000, le 10000``) is the
documented exception and is intentionally NOT covered here.
"""

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.dependencies import (
    get_ingestion_service,
    get_metagen_service,
    get_metrics_service,
    get_ontogen_service,
    get_validation_service,
)
from src.api.main import app
from tests.unit.api.conftest import TEST_SESSION_EPOCH, auth_headers

# ── helpers ───────────────────────────────────────────────────────────────────


def _order_str(call) -> str | None:
    """Render the ``order_by`` kwarg of a mocked-service call to a SQL string."""
    ob = call.kwargs.get("order_by")
    return None if ob is None else str(ob)


def _make_unmanaged_user() -> MagicMock:
    """A mock User row so require_authenticated resolves the JWT when the test
    supplies its own capturing get_db override (replacing the conftest session)."""
    u = MagicMock()
    u.id = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
    u.email = "unit-test@example.com"
    u.name = "Unit Test User"
    u.role = "Admin"
    u.google_sub = None
    # The bearer path compares the token's ``ses`` claim against this value
    # (spec/feature/AUTH.md §Session epoch), so it must match make_token()'s.
    u.session_epoch = TEST_SESSION_EPOCH
    return u


@pytest.fixture
def ontogen_svc() -> AsyncMock:
    svc = AsyncMock()
    svc.list_nodes = AsyncMock(return_value=([], 0))
    svc.list_edges = AsyncMock(return_value=([], 0))
    svc.list_triples = AsyncMock(return_value=([], 0))
    return svc


@pytest.fixture(autouse=True)
def _override_ontogen(ontogen_svc: AsyncMock):
    app.dependency_overrides[get_ontogen_service] = lambda: ontogen_svc
    yield
    app.dependency_overrides.pop(get_ontogen_service, None)


# ── OntoGen result endpoints — the three rebased panels ───────────────────────


@pytest.mark.parametrize(
    "kind, list_attr",
    [("node", "list_nodes"), ("edge", "list_edges"), ("triple", "list_triples")],
)
@pytest.mark.asyncio
async def test_ontogen_result_sort_asc_reverses_default(
    client, ontogen_svc: AsyncMock, kind: str, list_attr: str
) -> None:
    """GET /ontogen/result/{kind}?sort=created_at_asc forwards an ASC order_by.

    spec/API.md §Ontology Generation — result rows sortable by created_at (default created_at_desc).
    """
    resp = await client.get(
        f"/api/v1/spoke/ontogen/result/{kind}?sort=created_at_asc",
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    call = getattr(ontogen_svc, list_attr).await_args
    rendered = _order_str(call)
    assert rendered is not None and rendered.endswith("ASC"), (
        f"sort=created_at_asc must forward an ASC order_by; got {rendered!r}"
    )


@pytest.mark.parametrize(
    "kind, list_attr",
    [("node", "list_nodes"), ("edge", "list_edges"), ("triple", "list_triples")],
)
@pytest.mark.asyncio
async def test_ontogen_result_sort_desc_explicit(
    client, ontogen_svc: AsyncMock, kind: str, list_attr: str
) -> None:
    """GET /ontogen/result/{kind}?sort=created_at_desc forwards a DESC order_by."""
    resp = await client.get(
        f"/api/v1/spoke/ontogen/result/{kind}?sort=created_at_desc",
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    rendered = _order_str(getattr(ontogen_svc, list_attr).await_args)
    assert rendered is not None and rendered.endswith("DESC")


@pytest.mark.parametrize(
    "kind, list_attr",
    [("node", "list_nodes"), ("edge", "list_edges"), ("triple", "list_triples")],
)
@pytest.mark.asyncio
async def test_ontogen_result_default_sort_omitted_preserves_default(
    client, ontogen_svc: AsyncMock, kind: str, list_attr: str
) -> None:
    """GET /ontogen/result/{kind} with no sort forwards order_by=None.

    The router unit under test passes no order_by when sort is omitted, deferring
    to the service-layer default. (The service default itself — created_at_desc,
    src/backend/ontogen/service.py — is exercised in the ontogen service tests.)
    """
    resp = await client.get(
        f"/api/v1/spoke/ontogen/result/{kind}",
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    assert _order_str(getattr(ontogen_svc, list_attr).await_args) is None


@pytest.mark.asyncio
async def test_ontogen_result_envelope_and_cap(
    client, ontogen_svc: AsyncMock
) -> None:
    """GET /ontogen/result/node carries offset/limit/total_count; limit caps at 1000."""
    resp = await client.get(
        "/api/v1/spoke/ontogen/result/node?offset=5&limit=1000",
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    for key in ("offset", "limit", "total_count"):
        assert key in body, f"response must carry {key!r} envelope field"
    assert body["offset"] == 5
    assert body["limit"] == 1000

    over = await client.get(
        "/api/v1/spoke/ontogen/result/node?limit=1001",
        headers=auth_headers(),
    )
    assert over.status_code == 422, "limit > 1000 must be rejected (le=1000)"


# ── Ingestion sources — representative resource list ──────────────────────────


@pytest.mark.asyncio
async def test_ingestion_sources_sort_and_cap(client) -> None:
    """GET /ingestion/sources?sort=created_at_asc reverses default; limit caps at 1000.

    spec/API.md §Ingestion sources — paginated; sortable by created_at.
    """
    svc = AsyncMock()
    svc.list_sources = AsyncMock(return_value=([], 0))
    app.dependency_overrides[get_ingestion_service] = lambda: svc
    try:
        asc = await client.get(
            "/api/v1/spoke/ingestion/sources?sort=created_at_asc",
            headers=auth_headers(),
        )
        assert asc.status_code == 200
        assert _order_str(svc.list_sources.await_args).endswith("ASC")

        body = asc.json()
        for key in ("offset", "limit", "total_count"):
            assert key in body

        default = await client.get(
            "/api/v1/spoke/ingestion/sources", headers=auth_headers()
        )
        assert default.status_code == 200
        assert _order_str(svc.list_sources.await_args) is None

        over = await client.get(
            "/api/v1/spoke/ingestion/sources?limit=1001", headers=auth_headers()
        )
        assert over.status_code == 422
    finally:
        app.dependency_overrides.pop(get_ingestion_service, None)


@pytest.mark.asyncio
async def test_ingestion_source_datasets_sort_by_dataset_urn(client) -> None:
    """GET /ingestion/sources/{id}/datasets?sort=dataset_urn_desc forwards a DESC order_by.

    spec/API.md §Ingestion source datasets — sortable by dataset_urn.
    """
    svc = AsyncMock()
    svc.list_datasets_for_source = AsyncMock(return_value=([], 0))
    app.dependency_overrides[get_ingestion_service] = lambda: svc
    try:
        resp = await client.get(
            "/api/v1/spoke/ingestion/sources/src-1/datasets?sort=dataset_urn_desc",
            headers=auth_headers(),
        )
        assert resp.status_code == 200
        rendered = _order_str(svc.list_datasets_for_source.await_args)
        assert rendered is not None and rendered.endswith("DESC")
    finally:
        app.dependency_overrides.pop(get_ingestion_service, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sort, suffix",
    [("first_seen_at_desc", "DESC"), ("last_seen_at_asc", "ASC")],
)
async def test_ingestion_source_datasets_extra_sort_fields(client, sort, suffix) -> None:
    """GET /ingestion/sources/{id}/datasets honours first_seen_at / last_seen_at sorts.

    spec/API.md §Ingestion — sortable by dataset_urn / first_seen_at / last_seen_at.
    The router previously omitted the two timestamp fields, silently ignoring them.
    """
    svc = AsyncMock()
    svc.list_datasets_for_source = AsyncMock(return_value=([], 0))
    app.dependency_overrides[get_ingestion_service] = lambda: svc
    try:
        resp = await client.get(
            f"/api/v1/spoke/ingestion/sources/src-1/datasets?sort={sort}",
            headers=auth_headers(),
        )
        assert resp.status_code == 200
        rendered = _order_str(svc.list_datasets_for_source.await_args)
        assert rendered is not None and rendered.endswith(suffix), (
            f"sort={sort} must forward a {suffix} order_by; got {rendered!r}"
        )
        assert sort.rsplit("_", 1)[0] in rendered
    finally:
        app.dependency_overrides.pop(get_ingestion_service, None)


@pytest.mark.asyncio
async def test_ingestion_source_datasets_default_is_dataset_urn_asc(client) -> None:
    """GET /ingestion/sources/{id}/datasets with no sort forwards dataset_urn ASC.

    spec/API.md §Ingestion — default ``dataset_urn_asc``. The router pins this explicitly
    (the service's own None-fallback is last_seen_at desc), so the contract default
    must be visible at the router boundary.
    """
    svc = AsyncMock()
    svc.list_datasets_for_source = AsyncMock(return_value=([], 0))
    app.dependency_overrides[get_ingestion_service] = lambda: svc
    try:
        resp = await client.get(
            "/api/v1/spoke/ingestion/sources/src-1/datasets",
            headers=auth_headers(),
        )
        assert resp.status_code == 200
        rendered = _order_str(svc.list_datasets_for_source.await_args)
        assert rendered is not None and rendered.endswith("ASC")
        assert "dataset_urn" in rendered
    finally:
        app.dependency_overrides.pop(get_ingestion_service, None)


@pytest.mark.asyncio
async def test_ingestion_sources_sort_by_updated_at(client) -> None:
    """GET /ingestion/sources?sort=updated_at_desc forwards a DESC order_by.

    spec/API.md §Ingestion — sources sortable by created_at / updated_at; updated_at was missing.
    """
    svc = AsyncMock()
    svc.list_sources = AsyncMock(return_value=([], 0))
    app.dependency_overrides[get_ingestion_service] = lambda: svc
    try:
        resp = await client.get(
            "/api/v1/spoke/ingestion/sources?sort=updated_at_desc",
            headers=auth_headers(),
        )
        assert resp.status_code == 200
        rendered = _order_str(svc.list_sources.await_args)
        assert rendered is not None and rendered.endswith("DESC")
        assert "updated_at" in rendered
    finally:
        app.dependency_overrides.pop(get_ingestion_service, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sort, field, suffix",
    [
        ("created_at_desc", "created_at", "DESC"),
        ("created_at_asc", "created_at", "ASC"),
        ("updated_at_desc", "updated_at", "DESC"),
        ("updated_at_asc", "updated_at", "ASC"),
    ],
)
async def test_ingestion_unmanaged_extra_sort_fields(client, sort, field, suffix) -> None:
    """GET /ingestion/unmanaged honours created_at / updated_at sorts.

    spec/API.md §Ingestion — unmanaged sortable by dataset_urn / created_at / updated_at (default
    dataset_urn_asc). The handler builds the query inline (no mockable service), so
    we override get_db with a capturing session and render the ORDER BY clause of the
    rows query passed to db.execute. The router previously widened its sort dict but
    had no test exercising the two timestamp fields.
    """
    from sqlalchemy.dialects import postgresql

    from src.api.dependencies import get_db

    captured: list[str] = []

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = _make_unmanaged_user()
    mock_result.scalar.return_value = 0
    mock_result.all.return_value = []

    async def _capturing_execute(stmt, *args, **kwargs):
        captured.append(
            str(stmt.compile(dialect=postgresql.dialect()))
        )
        return mock_result

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=_capturing_execute)

    async def _mock_db():
        yield mock_session

    app.dependency_overrides[get_db] = _mock_db
    try:
        resp = await client.get(
            f"/api/v1/spoke/ingestion/unmanaged?sort={sort}",
            headers=auth_headers(),
        )
        assert resp.status_code == 200
        # The rows query is the only statement carrying an ORDER BY clause.
        order_stmts = [s for s in captured if "ORDER BY" in s]
        assert order_stmts, "expected a rows query with an ORDER BY clause"
        rendered = order_stmts[-1]
        order_clause = rendered.split("ORDER BY", 1)[1]
        assert field in order_clause, f"sort={sort} must order by {field}; got {order_clause!r}"
        assert suffix in order_clause, f"sort={sort} must order {suffix}; got {order_clause!r}"
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_ingestion_unmanaged_default_is_dataset_urn_asc(client) -> None:
    """GET /ingestion/unmanaged with no sort orders by dataset_urn ASC.

    spec/API.md §Ingestion — unmanaged default ``dataset_urn_asc``. The handler pins this at the
    parse_sort default, so the contract default is visible in the rows query.
    """
    from sqlalchemy.dialects import postgresql

    from src.api.dependencies import get_db

    captured: list[str] = []

    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = _make_unmanaged_user()
    mock_result.scalar.return_value = 0
    mock_result.all.return_value = []

    async def _capturing_execute(stmt, *args, **kwargs):
        captured.append(str(stmt.compile(dialect=postgresql.dialect())))
        return mock_result

    mock_session = AsyncMock()
    mock_session.execute = AsyncMock(side_effect=_capturing_execute)

    async def _mock_db():
        yield mock_session

    app.dependency_overrides[get_db] = _mock_db
    try:
        resp = await client.get(
            "/api/v1/spoke/ingestion/unmanaged", headers=auth_headers()
        )
        assert resp.status_code == 200
        order_stmts = [s for s in captured if "ORDER BY" in s]
        assert order_stmts, "expected a rows query with an ORDER BY clause"
        order_clause = order_stmts[-1].split("ORDER BY", 1)[1]
        # Ascending default: SQLAlchemy renders the bare column (no explicit ASC),
        # so the contract is dataset_urn ordering with no DESC.
        assert "dataset_urn" in order_clause
        assert "DESC" not in order_clause
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_validation_sort_by_dataset_urn(client) -> None:
    """GET /spoke/validation?sort=dataset_urn_asc forwards an ASC order_by.

    spec/API.md §Validation — list sortable by dataset_urn / updated_at; dataset_urn was missing.
    """
    svc = AsyncMock()
    svc.list_configs = AsyncMock(return_value=([], 0))
    app.dependency_overrides[get_validation_service] = lambda: svc
    try:
        resp = await client.get(
            "/api/v1/spoke/validation?sort=dataset_urn_asc",
            headers=auth_headers(),
        )
        assert resp.status_code == 200
        rendered = _order_str(svc.list_configs.await_args)
        assert rendered is not None and rendered.endswith("ASC")
        assert "dataset_urn" in rendered
    finally:
        app.dependency_overrides.pop(get_validation_service, None)


@pytest.mark.asyncio
async def test_metagen_conf_sort_by_name(client) -> None:
    """GET /spoke/metagen/conf?sort=name_asc forwards an ASC order_by.

    spec/API.md §Metadata Generation — sortable by created_at / updated_at / name; name was missing.
    """
    svc = AsyncMock()
    svc.list_confs = AsyncMock(return_value=([], 0))
    app.dependency_overrides[get_metagen_service] = lambda: svc
    try:
        resp = await client.get(
            "/api/v1/spoke/metagen/conf?sort=name_asc",
            headers=auth_headers(),
        )
        assert resp.status_code == 200
        rendered = _order_str(svc.list_confs.await_args)
        assert rendered is not None and rendered.endswith("ASC")
        assert "name" in rendered
    finally:
        app.dependency_overrides.pop(get_metagen_service, None)


@pytest.mark.asyncio
async def test_metagen_item_sort_by_dataset_urn(client) -> None:
    """GET /spoke/metagen/item?sort=dataset_urn_desc forwards a DESC order_by.

    spec/API.md §Metadata Generation — sortable by created_at / updated_at / dataset_urn;
    the latter was missing.
    """
    svc = AsyncMock()
    svc.list_items = AsyncMock(return_value=([], 0))
    app.dependency_overrides[get_metagen_service] = lambda: svc
    try:
        resp = await client.get(
            "/api/v1/spoke/metagen/item?sort=dataset_urn_desc",
            headers=auth_headers(),
        )
        assert resp.status_code == 200
        rendered = _order_str(svc.list_items.await_args)
        assert rendered is not None and rendered.endswith("DESC")
        assert "dataset_urn" in rendered
    finally:
        app.dependency_overrides.pop(get_metagen_service, None)


@pytest.mark.asyncio
async def test_admin_users_sort_by_updated_at(client) -> None:
    """GET /admin/users?sort=updated_at_desc forwards a DESC order_by.

    spec/API.md §Admin (/admin) — sortable by created_at / updated_at / email;
    updated_at was missing.
    """
    with patch(
        "src.backend.auth.users.list_users", new=AsyncMock(return_value=([], 0))
    ) as mock_list:
        resp = await client.get(
            "/api/v1/admin/users?sort=updated_at_desc", headers=auth_headers()
        )
        assert resp.status_code == 200
        rendered = _order_str(mock_list.await_args)
        assert rendered is not None and rendered.endswith("DESC")
        assert "updated_at" in rendered


# ── Governance metric list — representative resource list ─────────────────────


@pytest.mark.asyncio
async def test_governance_metric_list_sort_and_cap(client) -> None:
    """GET /governance/metric?sort=created_at_asc reverses default; limit caps at 1000.

    spec/API.md §Metric — "List all metrics (paginated, sortable by
    ``created_at``/``updated_at``/``title``/``description`` (default
    ``created_at_desc``) ...)".

    ``description_asc`` is the sort key the governance dashboard's description
    ordering is anchored on (spec/feature/FRONTEND_GOVERNANCE.md §Dashboard —
    "a **description sort**"), so it is exercised alongside ``created_at_asc``.
    """
    svc = AsyncMock()
    svc.list_metrics = AsyncMock(return_value=([], 0))
    app.dependency_overrides[get_metrics_service] = lambda: svc
    try:
        asc = await client.get(
            "/api/v1/spoke/governance/metric?sort=created_at_asc",
            headers=auth_headers(),
        )
        assert asc.status_code == 200
        assert _order_str(svc.list_metrics.await_args).endswith("ASC")

        by_description = await client.get(
            "/api/v1/spoke/governance/metric?sort=description_asc",
            headers=auth_headers(),
        )
        assert by_description.status_code == 200
        rendered = _order_str(svc.list_metrics.await_args)
        assert rendered is not None and rendered.endswith("ASC"), (
            f"sort=description_asc must forward an ASC order_by; got {rendered!r}"
        )
        assert "description" in rendered, (
            "sort=description_asc must order by the description column; "
            f"got {rendered!r}"
        )

        body = asc.json()
        for key in ("offset", "limit", "total_count"):
            assert key in body

        default = await client.get(
            "/api/v1/spoke/governance/metric", headers=auth_headers()
        )
        assert default.status_code == 200
        assert _order_str(svc.list_metrics.await_args) is None

        over = await client.get(
            "/api/v1/spoke/governance/metric?limit=1001", headers=auth_headers()
        )
        assert over.status_code == 422
    finally:
        app.dependency_overrides.pop(get_metrics_service, None)


# ── Admin users — rebased onto PaginatedResponse (now exposes total_count) ─────


@pytest.mark.asyncio
async def test_admin_users_envelope_total_count_and_sort(client) -> None:
    """GET /admin/users carries total_count; ?sort=email_asc forwards an ASC order_by.

    spec/API.md §admin/users — rebased onto the standard envelope (adds total_count);
    sortable by created_at/email. UsersListResponse now extends PaginatedResponse.
    """
    from src.api.schemas.admin import UsersListResponse
    from src.api.schemas.common import PaginatedResponse

    assert issubclass(UsersListResponse, PaginatedResponse), (
        "UsersListResponse must extend PaginatedResponse"
    )
    assert "total_count" in UsersListResponse.model_fields

    with patch(
        "src.backend.auth.users.list_users", new=AsyncMock(return_value=([], 7))
    ) as mock_list:
        asc = await client.get(
            "/api/v1/admin/users?sort=email_asc", headers=auth_headers()
        )
        assert asc.status_code == 200
        body = asc.json()
        assert body["total_count"] == 7
        for key in ("offset", "limit", "total_count"):
            assert key in body
        assert _order_str(mock_list.await_args).endswith("ASC")

        await client.get("/api/v1/admin/users", headers=auth_headers())
        assert _order_str(mock_list.await_args) is None

        over = await client.get(
            "/api/v1/admin/users?limit=1001", headers=auth_headers()
        )
        assert over.status_code == 422


# ── Auth api-tokens — rebased onto PaginatedResponse (in-memory slice) ─────────


def _token_row(created_at: datetime):
    """A transient ApiToken ORM instance (router asserts isinstance(t, ApiToken))."""
    from src.shared.db.models import ApiToken

    return ApiToken(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        name="tok",
        token_hash="0" * 64,
        role_snapshot="Editor",
        created_at=created_at,
        last_used_at=None,
        expires_at=None,
    )


@pytest.mark.asyncio
async def test_auth_api_tokens_envelope_and_default_sort(client) -> None:
    """GET /auth/api-tokens carries total_count; default order is created_at_desc
    (newest first) and an explicit ?sort=created_at_asc reverses to oldest first.

    spec/API.md §Auth — GET `/auth/api-tokens` is 'paginated with the
    standard offset/limit/total_count envelope, sortable by created_at, default
    created_at_desc'. The default (sort omitted) MUST surface the newest token
    first, regardless of the order the underlying query materialises rows in.
    """
    from src.api.schemas.auth import ApiTokenListResponse
    from src.api.schemas.common import PaginatedResponse

    assert issubclass(ApiTokenListResponse, PaginatedResponse)
    assert "total_count" in ApiTokenListResponse.model_fields

    now = datetime.now(tz=UTC)
    older = _token_row(now - timedelta(hours=2))
    newer = _token_row(now)
    # list_active materialises the tokens (order is an impl detail of the query);
    # the endpoint contract is that the *response* default is created_at_desc.
    active = [older, newer]

    with patch(
        "src.backend.auth.api_tokens.list_active",
        new=AsyncMock(return_value=list(active)),
    ):
        default = await client.get(
            "/api/v1/auth/api-tokens", headers=auth_headers()
        )
        assert default.status_code == 200
        body = default.json()
        assert body["total_count"] == 2
        for key in ("offset", "limit", "total_count"):
            assert key in body
        # spec default created_at_desc: newest first
        assert body["tokens"][0]["id"] == str(newer.id)

        asc = await client.get(
            "/api/v1/auth/api-tokens?sort=created_at_asc", headers=auth_headers()
        )
        assert asc.status_code == 200
        # explicit ascending: oldest first
        assert asc.json()["tokens"][0]["id"] == str(older.id)


@pytest.mark.asyncio
async def test_auth_api_tokens_limit_cap(client) -> None:
    """GET /auth/api-tokens?limit=1001 → 422 (le=1000); 1000 accepted."""
    with patch(
        "src.backend.auth.api_tokens.list_active", new=AsyncMock(return_value=[])
    ):
        ok = await client.get(
            "/api/v1/auth/api-tokens?limit=1000", headers=auth_headers()
        )
        assert ok.status_code == 200
        over = await client.get(
            "/api/v1/auth/api-tokens?limit=1001", headers=auth_headers()
        )
        assert over.status_code == 422


# ── Ingestion secrets — rebased onto PaginatedResponse (in-memory slice) ───────


def _secret_ref(ref: str) -> MagicMock:
    r = MagicMock()
    r.ref = ref
    r.secret_name = "dataspoke-source-cred-x"
    r.key = "password"
    return r


@pytest.mark.asyncio
async def test_ingestion_secrets_envelope_pagination_and_sort(client) -> None:
    """GET /ingestion/secrets paginates the k8s-enumerated refs (slice + count in
    router), carries total_count, and ?sort=ref_desc reverses the default ref-asc.

    spec/API.md §ingestion/secrets — paginated (extends PaginatedResponse), sortable
    by ref. Data source is the k8s API, sliced in the router.
    """
    from src.api.schemas.common import PaginatedResponse
    from src.api.schemas.ingestion import SecretRefListResponse

    assert issubclass(SecretRefListResponse, PaginatedResponse)
    assert "total_count" in SecretRefListResponse.model_fields

    # Unsorted source order from k8s; router sorts ref-asc by default.
    refs = [_secret_ref("c__k"), _secret_ref("a__k"), _secret_ref("b__k")]

    with patch(
        "src.api.routers.spoke.ingestion.list_source_cred_refs",
        return_value=list(refs),
    ):
        # default: ref ascending, paginate to 2 of 3
        default = await client.get(
            "/api/v1/spoke/ingestion/secrets?limit=2", headers=auth_headers()
        )
        assert default.status_code == 200
        body = default.json()
        assert body["total_count"] == 3
        assert body["limit"] == 2
        assert [s["ref"] for s in body["secrets"]] == ["a__k", "b__k"]

        # ref_desc reverses
        desc = await client.get(
            "/api/v1/spoke/ingestion/secrets?sort=ref_desc&limit=1",
            headers=auth_headers(),
        )
        assert desc.status_code == 200
        assert desc.json()["secrets"][0]["ref"] == "c__k"

        over = await client.get(
            "/api/v1/spoke/ingestion/secrets?limit=1001", headers=auth_headers()
        )
        assert over.status_code == 422
