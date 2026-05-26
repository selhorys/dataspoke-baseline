"""Unit tests for /spoke/common/ontogen routes.

Spec traceability:
- spec/API.md §Authentication & Authorization §Group-to-Route Access Control
- spec/API.md §Common (/spoke/common) §Ontology Generation
- spec/feature/BACKEND.md §Ontology Generation Service §Inference Pipeline
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.dependencies import get_ontogen_service
from src.api.main import app
from src.shared.exceptions import (
    EntityNotFoundError,
    PreconditionFailedError,
)

from tests.unit.api.conftest import auth_headers, make_token

_BASE = "/api/v1/spoke/common/ontogen"

# Named constants — cap values from the implementation
# impl-cap; spec gap surfaced 2026-05-01 (not defined in API_DESIGN_PRINCIPLE_en.md)
_REASON_MAX_LEN = 2000
_BODY_MAX_BYTES = 128 * 1024  # 128 KiB
# impl-cap; spec gap surfaced 2026-05-01 (not defined in API_DESIGN_PRINCIPLE_en.md)
_DATASET_FILTER_LIST_CAP = 1000

# Valid groups per spec/API.md §Group-to-Route Access Control table
_VALID_GROUPS = ("de", "da", "dg", "admin")


def _make_conf_row() -> MagicMock:
    row = MagicMock()
    row.is_enabled = False
    row.schedule_tier = None
    row.dataset_filter = {}
    row.default_run_prompt = None
    row.updated_at = datetime.now(tz=UTC)
    return row


def _make_seed_row() -> MagicMock:
    row = MagicMock()
    row.id = uuid.uuid4()
    row.body_md = "# Test seed"
    row.status = "active"
    row.updated_at = datetime.now(tz=UTC)
    return row


@pytest.fixture
def mock_svc() -> AsyncMock:
    return AsyncMock()


@pytest.fixture(autouse=True)
def override_service(mock_svc: AsyncMock):
    app.dependency_overrides[get_ontogen_service] = lambda: mock_svc
    yield
    app.dependency_overrides.pop(get_ontogen_service, None)


# ── Auth checks ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_conf_without_token_returns_401(client) -> None:
    """GET /ontogen/attr/conf without token returns 401."""
    resp = await client.get(f"{_BASE}/attr/conf")
    assert resp.status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("group", list(_VALID_GROUPS))
async def test_get_conf_with_valid_group_token_returns_200(
    client, mock_svc: AsyncMock, group: str
) -> None:
    """GET /ontogen/attr/conf with any valid group token returns 200.

    Spec: spec/API.md §Group-to-Route Access Control — /spoke/common/… requires
    any valid group; de/da/dg/admin all qualify.
    """
    mock_svc.get_conf = AsyncMock(return_value=_make_conf_row())
    resp = await client.get(f"{_BASE}/attr/conf", headers=auth_headers([group]))
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_get_conf_without_token_returns_401_not_403(client, mock_svc: AsyncMock) -> None:
    """GET /ontogen/attr/conf without a token returns 401.

    The old group-based access check ('ops' group → 403) is replaced by
    role-based auth. Any authenticated user may GET /spoke/common/* routes.
    Unauthenticated requests receive 401.

    Spec: API.md §Authentication — /spoke/common/* requires a valid token.
    """
    mock_svc.get_conf = AsyncMock(return_value=_make_conf_row())
    resp = await client.get(f"{_BASE}/attr/conf")  # no auth header
    assert resp.status_code == 401


# ── Conf round-trip ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_conf_returns_200(client, mock_svc: AsyncMock) -> None:
    """PUT /ontogen/attr/conf returns 200 with conf data."""
    conf_row = _make_conf_row()
    mock_svc.put_conf = AsyncMock(return_value=conf_row)

    resp = await client.put(
        f"{_BASE}/attr/conf",
        json={"is_enabled": False},
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_enabled"] is False


@pytest.mark.asyncio
async def test_put_conf_validates_dataset_filter_list_cap(client, mock_svc: AsyncMock) -> None:
    """PUT /ontogen/attr/conf with dataset_filter.dataset_urns > 1000 entries returns 422.

    Cap: _DATASET_FILTER_LIST_CAP (impl-cap; spec gap surfaced 2026-05-01).
    """
    # impl-cap; spec gap surfaced 2026-05-01
    too_many_urns = [f"urn:li:dataset:(urn:li:dataPlatform:postgres,t{i},PROD)" for i in range(_DATASET_FILTER_LIST_CAP + 1)]

    resp = await client.put(
        f"{_BASE}/attr/conf",
        json={
            "is_enabled": False,
            "dataset_filter": {"dataset_urns": too_many_urns},
        },
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 422


# ── Seeds ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_seed_returns_201(client, mock_svc: AsyncMock) -> None:
    """POST /ontogen/attr/seed returns 201 with seed_id."""
    seed = _make_seed_row()
    mock_svc.create_seed = AsyncMock(return_value=seed)

    resp = await client.post(
        f"{_BASE}/attr/seed",
        content=b"# My seed\n\nContent",
        headers={**auth_headers(["de"]), "Content-Type": "text/markdown"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert "seed_id" in data


@pytest.mark.asyncio
async def test_post_seed_body_too_large_returns_413(client, mock_svc: AsyncMock) -> None:
    """POST /ontogen/attr/seed with body > 128 KiB returns 413.

    Cap: _BODY_MAX_BYTES; spec API.md §Ontology Generation Payload caps.
    """
    big_body = b"x" * (_BODY_MAX_BYTES + 1)
    resp = await client.post(
        f"{_BASE}/attr/seed",
        content=big_body,
        headers={
            **auth_headers(["de"]),
            "Content-Type": "text/markdown",
            "Content-Length": str(len(big_body)),
        },
    )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_get_seed_malformed_uuid_returns_422(client, mock_svc: AsyncMock) -> None:
    """GET /ontogen/attr/seed/{bad_id} with non-UUID path segment returns 422."""
    resp = await client.get(
        f"{_BASE}/attr/seed/not-a-uuid-at-all",
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 422


# ── Run endpoint ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_run_returns_200_with_run_summary_body(client, mock_svc: AsyncMock) -> None:
    """POST /ontogen/method/run returns 200 with OntogenRunSummary body containing
    spec-mandated fields.

    Spec: spec/feature/BACKEND.md §Inference Pipeline L400-401 — ?dry_run=true evaluates
    steps 2-8 without persisting; run returns OntogenRunSummary.
    Spec: spec/feature/BACKEND.md L354 / L523 — unresolved_urns carries dataset URNs that
    did not resolve in DataHub at run time (ONTOGEN RUN_COMPLETE event payload).
    """
    from src.backend.ontogen.service import OntogenRunSummary

    mock_svc.run = AsyncMock(return_value=OntogenRunSummary(
        status="success",
        dry_run=False,
        unresolved_urns=["urn:li:dataset:(urn:li:dataPlatform:postgres,missing,PROD)"],
        counts={"nodes_added": 0, "edges_added": 0, "triples_added": 0},
    ))

    resp = await client.post(
        f"{_BASE}/method/run",
        content=b"",
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 200
    body = resp.json()
    # Spec-mandated: unresolved_urns must be a list (BACKEND.md L354/L523 — dataset_filter
    # URNs that don't resolve are skipped and reported here)
    assert "unresolved_urns" in body and isinstance(body["unresolved_urns"], list)
    # Spec-mandated: dry_run echoed in response body (BACKEND.md L400-401)
    assert "dry_run" in body and isinstance(body["dry_run"], bool)


@pytest.mark.asyncio
async def test_post_run_body_too_large_returns_413(client, mock_svc: AsyncMock) -> None:
    """POST /ontogen/method/run with body > 128 KiB returns 413.

    Cap: _BODY_MAX_BYTES; spec API.md §Ontology Generation Payload caps.
    """
    big_body = b"x" * (_BODY_MAX_BYTES + 1)
    resp = await client.post(
        f"{_BASE}/method/run",
        content=big_body,
        headers={
            **auth_headers(["de"]),
            "Content-Type": "text/markdown",
            "Content-Length": str(len(big_body)),
        },
    )
    assert resp.status_code == 413


# ── Triple review — dependency gate ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_triple_review_dependency_error_returns_422(client, mock_svc: AsyncMock) -> None:
    """POST .../result/triple/{id}/method/review with ONTOGEN_TRIPLE_DEPENDENCY_PENDING returns 422."""
    mock_svc.review_triple = AsyncMock(
        side_effect=PreconditionFailedError("ONTOGEN_TRIPLE_DEPENDENCY_PENDING", "not approved")
    )
    triple_id = "book__has_edition__edition"
    resp = await client.post(
        f"{_BASE}/result/triple/{triple_id}/method/review",
        json={"verdict": "approve"},
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error_code"] == "ONTOGEN_TRIPLE_DEPENDENCY_PENDING"


# ── Review request validation ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_node_review_reason_too_long_returns_422(client, mock_svc: AsyncMock) -> None:
    """POST .../result/node/{id}/method/review with reason > 2000 chars returns 422.

    Cap: _REASON_MAX_LEN (impl-cap; spec gap surfaced 2026-05-01).
    """
    resp = await client.post(
        f"{_BASE}/result/node/book/method/review",
        json={"verdict": "approve", "reason": "x" * (_REASON_MAX_LEN + 1)},  # impl-cap
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 422


# ── Schema structural absence tests ──────────────────────────────────────────


def test_node_response_schema_omits_glossary_term_urn() -> None:
    """NodeResponse must not have a glossary_term_urn field.

    Spec anchor: spec/feature/BACKEND_SCHEMA.md ontogen_nodes table (column
    removed in UC3/UC4 refactor) — the API response schema must not expose
    a field that no longer exists in the backing table.
    """
    from src.api.schemas.ontogen import NodeResponse

    assert "glossary_term_urn" not in NodeResponse.model_fields, (
        "NodeResponse must not expose glossary_term_urn (column removed per "
        "spec/feature/BACKEND_SCHEMA.md ontogen_nodes)"
    )


def test_ontogen_conf_schemas_omit_query_caps() -> None:
    """OntogenConfPutRequest and OntogenConfPatchRequest must not have
    max_manual_queries_per_dataset or max_system_queries_per_dataset fields.

    Spec anchor: spec/feature/BACKEND_SCHEMA.md ontogen_config (columns removed);
    spec/API.md §UC3 conf fields.
    """
    from src.api.schemas.ontogen import OntogenConfPutRequest, OntogenConfPatchRequest

    for schema_cls in (OntogenConfPutRequest, OntogenConfPatchRequest):
        for dropped_field in ("max_manual_queries_per_dataset", "max_system_queries_per_dataset"):
            assert dropped_field not in schema_cls.model_fields, (
                f"{schema_cls.__name__} must not expose {dropped_field!r} "
                f"(column removed per spec/feature/BACKEND_SCHEMA.md ontogen_config)"
            )


# ── dataset_filter origin — HTTP-level (router + schema layer) ────────────────


@pytest.mark.asyncio
async def test_put_conf_with_origin_and_tags_returns_200(
    client, mock_svc: AsyncMock
) -> None:
    """PUT /ontogen/attr/conf with dataset_filter={"origin": "DEV", "tags": [...]} returns 200.

    The schema layer accepts the four-dimension filter; the service layer is called
    with the validated dict. This exercises the unified dataset_filter shape at the
    HTTP layer for UC3.

    Spec: spec/API.md §UC3 Ontology Generation — dataset_filter unified four-dimension shape.
    """
    conf_row = _make_conf_row()
    conf_row.dataset_filter = {"origin": "DEV", "tags": ["urn:li:tag:area:fulfillment"]}
    mock_svc.put_conf = AsyncMock(return_value=conf_row)

    resp = await client.put(
        f"{_BASE}/attr/conf",
        json={
            "is_enabled": False,
            "dataset_filter": {"origin": "DEV", "tags": ["urn:li:tag:area:fulfillment"]},
        },
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 200, (
        f"PUT with origin+tags dataset_filter must return 200; "
        f"got {resp.status_code}: {resp.text}. "
        "spec: API.md §UC3 — dataset_filter unified four-dimension shape"
    )


@pytest.mark.asyncio
async def test_put_conf_with_malformed_urn_returns_422_invalid_dataset_urn(
    client, mock_svc: AsyncMock
) -> None:
    """PUT /ontogen/attr/conf with malformed dataset_urns returns 422 INVALID_DATASET_URN.

    UC3 now validates URN format at the schema layer (unified with UC5). The handler
    for InvalidDatasetUrnError maps it to 422 with error_code='INVALID_DATASET_URN'.

    Spec: spec/API.md §Error Catalogue — 422 INVALID_DATASET_URN for malformed URNs.
    Spec: spec/API.md §UC3 — URN format validated at schema layer.
    """
    resp = await client.put(
        f"{_BASE}/attr/conf",
        json={
            "is_enabled": False,
            "dataset_filter": {"dataset_urns": ["not-a-valid-urn"]},
        },
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 422, (
        f"PUT with malformed URN must return 422; got {resp.status_code}: {resp.text}. "
        "spec: API.md §Error Catalogue — INVALID_DATASET_URN"
    )
    body = resp.json()
    assert body.get("error_code") == "INVALID_DATASET_URN", (
        f"Response error_code must be INVALID_DATASET_URN; got {body.get('error_code')!r}. "
        "spec: API.md §Error Catalogue — INVALID_DATASET_URN"
    )


@pytest.mark.asyncio
async def test_patch_conf_with_malformed_urn_returns_422_invalid_dataset_urn(
    client, mock_svc: AsyncMock
) -> None:
    """PATCH /ontogen/attr/conf with malformed dataset_urns returns 422 INVALID_DATASET_URN.

    spec/API.md §Error Catalogue — INVALID_DATASET_URN validated at PUT/PATCH.
    """
    resp = await client.patch(
        f"{_BASE}/attr/conf",
        json={"dataset_filter": {"dataset_urns": ["not-a-valid-urn"]}},
        headers=auth_headers(["de"]),
    )
    assert resp.status_code == 422, (
        f"PATCH with malformed URN must return 422; got {resp.status_code}: {resp.text}. "
        "spec: API.md §Error Catalogue — INVALID_DATASET_URN"
    )
    body = resp.json()
    assert body.get("error_code") == "INVALID_DATASET_URN", (
        f"Response error_code must be INVALID_DATASET_URN; got {body.get('error_code')!r}. "
        "spec: API.md §Error Catalogue — INVALID_DATASET_URN"
    )
