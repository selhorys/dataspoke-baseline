"""Unit tests for /spoke/ontogen routes.

Spec traceability:
- spec/API.md §Ontology Generation (/spoke/ontogen)
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
from tests.unit.api.conftest import auth_headers

_BASE = "/api/v1/spoke/ontogen"

# Named constants — payload caps mandated by API.md §Ontology Generation (Payload caps)
_REASON_MAX_LEN = 2000  # API.md §Ontology Generation — review.reason ≤ 2,000 chars
_BODY_MAX_BYTES = 128 * 1024  # API.md §Ontology Generation — seed/run Markdown ≤ 128 KiB
# API.md §`dataset_filter` grammar — Caps: ≤ 8,000 chars and ≤ 1,000 string literals
_DATASET_FILTER_LIST_CAP = 1000


def _make_conf_row() -> MagicMock:
    row = MagicMock()
    row.is_enabled = False
    row.schedule_tier = None
    row.dataset_filter = ""
    row.default_run_prompt = None
    row.updated_at = datetime.now(tz=UTC)
    return row


def _make_seed_row(is_enabled: bool = False) -> MagicMock:
    row = MagicMock()
    row.id = uuid.uuid4()
    row.body_md = "# Test seed"
    row.is_enabled = is_enabled
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
async def test_get_conf_with_valid_token_returns_200(
    client, mock_svc: AsyncMock
) -> None:
    """GET /ontogen/attr/conf with a valid token returns 200.

    Spec: spec/API.md §Authentication — /spoke/ontogen routes require a valid JWT.
    """
    mock_svc.get_conf = AsyncMock(return_value=_make_conf_row())
    resp = await client.get(f"{_BASE}/attr/conf", headers=auth_headers())
    assert resp.status_code == 200


# ── Conf round-trip ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_conf_returns_200(client, mock_svc: AsyncMock) -> None:
    """PUT /ontogen/attr/conf returns 200 with conf data."""
    conf_row = _make_conf_row()
    mock_svc.put_conf = AsyncMock(return_value=conf_row)

    resp = await client.put(
        f"{_BASE}/attr/conf",
        json={"is_enabled": False},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_enabled"] is False


@pytest.mark.asyncio
async def test_put_conf_validates_dataset_filter_literal_cap(client, mock_svc: AsyncMock) -> None:
    """PUT /ontogen/attr/conf with a filter over the 1,000-literal cap returns 422.

    Spec: API.md §`dataset_filter` grammar — Caps: "≤ 1,000 string literals";
    Spec: API.md §Error Catalogue — INVALID_DATASET_FILTER, 422, "exceeds a payload cap".
    """
    over_cap = "origin IN (" + ", ".join(
        f"'v{i}'" for i in range(_DATASET_FILTER_LIST_CAP + 1)
    ) + ")"

    resp = await client.put(
        f"{_BASE}/attr/conf",
        json={"is_enabled": False, "dataset_filter": over_cap},
        headers=auth_headers(),
    )
    assert resp.status_code == 422, resp.text
    assert resp.json().get("error_code") == "INVALID_DATASET_FILTER", resp.text


# ── Seeds ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_seed_returns_201(client, mock_svc: AsyncMock) -> None:
    """POST /ontogen/attr/seed returns 201 with seed_id."""
    seed = _make_seed_row()
    mock_svc.create_seed = AsyncMock(return_value=seed)

    resp = await client.post(
        f"{_BASE}/attr/seed",
        content=b"# My seed\n\nContent",
        headers={**auth_headers(), "Content-Type": "text/markdown"},
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
            **auth_headers(),
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
        headers=auth_headers(),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_patch_seed_enabled_returns_200_with_state(
    client, mock_svc: AsyncMock
) -> None:
    """PATCH /ontogen/attr/seed/{id}/attr/enabled flips and returns is_enabled.

    spec: API.md §PATCH attr/seed/{seed_id}/attr/enabled — JSON {is_enabled: bool},
    returns 200; a disabled seed stays visible but is excluded from inference.
    """
    seed = _make_seed_row(is_enabled=True)
    mock_svc.set_seed_enabled = AsyncMock(return_value=seed)

    resp = await client.patch(
        f"{_BASE}/attr/seed/{seed.id}/attr/enabled",
        json={"is_enabled": True},
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_enabled"] is True
    # The router delegates the toggle to set_seed_enabled with the requested flag.
    assert mock_svc.set_seed_enabled.await_args.args[1] is True


@pytest.mark.asyncio
async def test_patch_seed_enabled_requires_is_enabled_field(
    client, mock_svc: AsyncMock
) -> None:
    """PATCH attr/seed/{id}/attr/enabled with no is_enabled field is a 422 schema error.

    spec: API.md §PATCH attr/seed/{seed_id}/attr/enabled — body is JSON {is_enabled: bool}.
    """
    seed = _make_seed_row()
    resp = await client.patch(
        f"{_BASE}/attr/seed/{seed.id}/attr/enabled",
        json={},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


# ── Run endpoint ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_run_returns_200_with_run_summary_body(client, mock_svc: AsyncMock) -> None:
    """POST /ontogen/method/run returns 200 with OntogenRunSummary body containing
    spec-mandated fields.

    Spec: spec/feature/BACKEND.md §Ontology Generation Service — ?dry_run=true evaluates
    without persisting; run returns OntogenRunSummary.
    Spec: spec/feature/BACKEND.md §Ontology Generation Service — unresolved_urns carries
    dataset URNs that did not resolve in DataHub at run time (ONTOGEN RUN_COMPLETE payload).
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
        headers=auth_headers(),
    )
    assert resp.status_code == 200
    body = resp.json()
    # Spec-mandated: unresolved_urns must be a list (BACKEND.md §Ontology Generation Service
    # — dataset_filter URNs that don't resolve are skipped and reported here)
    assert "unresolved_urns" in body and isinstance(body["unresolved_urns"], list)
    # Spec-mandated: dry_run echoed in response body (BACKEND.md §Ontology Generation Service)
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
            **auth_headers(),
            "Content-Type": "text/markdown",
            "Content-Length": str(len(big_body)),
        },
    )
    assert resp.status_code == 413


# ── Node detail — member_datasets ────────────────────────────────────────────


def _make_node_row(*, node_id: str = "book", dataset_maps: list | None = None) -> MagicMock:
    row = MagicMock()
    row.id = node_id
    row.name = "Book"
    row.description = "A book entity"
    row.confidence_score = 0.9
    row.status = "approved"
    row.run_id = None
    row.created_at = datetime.now(tz=UTC)
    row.updated_at = datetime.now(tz=UTC)
    row.dataset_maps = dataset_maps if dataset_maps is not None else []
    return row


def _make_dataset_map(
    *,
    dataset_urn: str,
    confidence_score: float = 0.9,
    status: str = "approved",
    is_primary: bool = False,
) -> MagicMock:
    dm = MagicMock()
    dm.dataset_urn = dataset_urn
    dm.confidence_score = confidence_score
    dm.status = status
    dm.is_primary = is_primary
    return dm


@pytest.mark.asyncio
async def test_get_node_detail_returns_member_datasets(
    client, mock_svc: AsyncMock
) -> None:
    """GET /result/node/{node_id} returns member_datasets: every seeded
    dataset_node_map membership appears exactly once, each as a
    {dataset_urn, confidence_score, status, is_primary} object with the
    correct field values.

    Spec: spec/API.md §Ontology Generation — 'GET /spoke/ontogen/result/node/{node_id}
    — Get node detail (incl. member datasets)'. Spec defines the membership
    payload shape but not a specific ordering, so this test does not pin one.
    """
    dm_z = _make_dataset_map(
        dataset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,catalog.z_dataset,DEV)",
        confidence_score=0.7,
        status="approved",
        is_primary=False,
    )
    dm_primary = _make_dataset_map(
        dataset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,catalog.m_dataset,DEV)",
        confidence_score=0.95,
        status="approved",
        is_primary=True,
    )
    dm_a = _make_dataset_map(
        dataset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,catalog.a_dataset,DEV)",
        confidence_score=0.6,
        status="llm_pending",
        is_primary=False,
    )
    node = _make_node_row(node_id="book", dataset_maps=[dm_z, dm_primary, dm_a])
    mock_svc.get_node = AsyncMock(return_value=node)

    resp = await client.get(f"{_BASE}/result/node/book", headers=auth_headers())

    assert resp.status_code == 200, resp.text
    body = resp.json()
    expected = [
        {
            "dataset_urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,catalog.z_dataset,DEV)",
            "confidence_score": 0.7,
            "status": "approved",
            "is_primary": False,
        },
        {
            "dataset_urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,catalog.m_dataset,DEV)",
            "confidence_score": 0.95,
            "status": "approved",
            "is_primary": True,
        },
        {
            "dataset_urn": "urn:li:dataset:(urn:li:dataPlatform:postgres,catalog.a_dataset,DEV)",
            "confidence_score": 0.6,
            "status": "llm_pending",
            "is_primary": False,
        },
    ]
    actual = body["member_datasets"]
    assert len(actual) == len(expected), (
        f"expected exactly {len(expected)} member_datasets entries; got {actual!r}"
    )
    for entry in expected:
        assert entry in actual, (
            f"expected membership {entry!r} to appear in member_datasets; got {actual!r}"
        )


@pytest.mark.asyncio
async def test_get_node_detail_returns_empty_member_datasets_when_no_memberships(
    client, mock_svc: AsyncMock
) -> None:
    """GET /result/node/{node_id} for a node with no dataset_node_map rows
    serializes member_datasets as an empty list (NodeDetailResponse's default),
    not an omitted field or null.

    Spec: spec/API.md §Ontology Generation — 'GET /spoke/ontogen/result/node/{node_id}
    — Get node detail (incl. member datasets)'.
    """
    node = _make_node_row(node_id="book", dataset_maps=[])
    mock_svc.get_node = AsyncMock(return_value=node)

    resp = await client.get(f"{_BASE}/result/node/book", headers=auth_headers())

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["member_datasets"] == [], (
        "expected member_datasets == [] for a node with no memberships; "
        f"got {body['member_datasets']!r}"
    )


@pytest.mark.asyncio
async def test_get_node_detail_returns_404_when_node_missing(
    client, mock_svc: AsyncMock
) -> None:
    """GET /result/node/{node_id} returns 404 NODE_NOT_FOUND when the service
    raises EntityNotFoundError for an absent node id.

    Spec: spec/API.md §Error Catalogue — NODE_NOT_FOUND (404, 'Ontology node ID
    not found').
    """
    mock_svc.get_node = AsyncMock(side_effect=EntityNotFoundError("node", "missing"))

    resp = await client.get(f"{_BASE}/result/node/missing", headers=auth_headers())

    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body.get("error_code") == "NODE_NOT_FOUND", (
        f"Expected error_code='NODE_NOT_FOUND'; got {body.get('error_code')!r}. "
        "Spec: spec/API.md §Error Catalogue."
    )


@pytest.mark.asyncio
async def test_get_node_list_route_omits_member_datasets(client, mock_svc: AsyncMock) -> None:
    """GET /result/node (list) returns the plain NodeResponse shape — no
    member_datasets — so the list and detail routes don't get conflated.

    Spec: spec/API.md §Ontology Generation — 'GET /spoke/ontogen/result/node —
    List nodes' is a distinct, narrower shape than 'GET .../result/node/{node_id}
    — Get node detail (incl. member datasets)'.
    """
    node = _make_node_row(
        node_id="book",
        dataset_maps=[
            _make_dataset_map(
                dataset_urn="urn:li:dataset:(urn:li:dataPlatform:postgres,catalog.a,DEV)"
            )
        ],
    )
    mock_svc.list_nodes = AsyncMock(return_value=([node], 1))

    resp = await client.get(f"{_BASE}/result/node", headers=auth_headers())

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["nodes"], "expected at least one node row in the list response"
    for row in body["nodes"]:
        assert "member_datasets" not in row, (
            "the LIST route must return NodeResponse, not NodeDetailResponse — "
            f"got member_datasets on a list row: {row!r}"
        )
        assert {
            "id",
            "name",
            "description",
            "confidence_score",
            "status",
            "run_id",
            "created_at",
            "updated_at",
        } <= set(row.keys()), (
            f"expected the spec-defined NodeResponse content fields on a list row; got {row!r}"
        )


# ── Triple review — dependency gate ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_triple_review_dependency_error_returns_422(client, mock_svc: AsyncMock) -> None:
    """POST .../result/triple/{id}/method/review with ONTOGEN_TRIPLE_DEPENDENCY_PENDING
    returns 422."""
    mock_svc.review_triple = AsyncMock(
        side_effect=PreconditionFailedError("ONTOGEN_TRIPLE_DEPENDENCY_PENDING", "not approved")
    )
    triple_id = "book__has_edition__edition"
    resp = await client.post(
        f"{_BASE}/result/triple/{triple_id}/method/review",
        json={"verdict": "approve"},
        headers=auth_headers(),
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["error_code"] == "ONTOGEN_TRIPLE_DEPENDENCY_PENDING"


# ── Review request validation ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_node_review_reason_too_long_returns_422(client, mock_svc: AsyncMock) -> None:
    """POST .../result/node/{id}/method/review with reason > 2000 chars returns 422.

    Cap: _REASON_MAX_LEN — API.md §Ontology Generation (Payload caps), review.reason ≤ 2,000 chars.
    """
    resp = await client.post(
        f"{_BASE}/result/node/book/method/review",
        json={"verdict": "approve", "reason": "x" * (_REASON_MAX_LEN + 1)},
        headers=auth_headers(),
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
    spec/API.md §Ontology Generation conf fields.
    """
    from src.api.schemas.ontogen import OntogenConfPatchRequest, OntogenConfPutRequest

    for schema_cls in (OntogenConfPutRequest, OntogenConfPatchRequest):
        for dropped_field in ("max_manual_queries_per_dataset", "max_system_queries_per_dataset"):
            assert dropped_field not in schema_cls.model_fields, (
                f"{schema_cls.__name__} must not expose {dropped_field!r} "
                f"(column removed per spec/feature/BACKEND_SCHEMA.md ontogen_config)"
            )


# ── dataset_filter — HTTP-level (router + schema layer) ───────────────────────


@pytest.mark.asyncio
async def test_put_conf_with_a_composite_filter_returns_200(
    client, mock_svc: AsyncMock
) -> None:
    """A composite clause round-trips through the route and reaches the service verbatim.

    Spec: spec/API.md §`dataset_filter` grammar — "UC3's `ontogen/attr/conf.dataset_filter`
    […] use[s] this same grammar and validation"; `expr := term { (AND|OR) term }` composes
    an origin equality with tag membership.
    """
    clause = "origin = 'DEV' AND 'urn:li:tag:area:fulfillment' IN tag_urns"
    conf_row = _make_conf_row()
    conf_row.dataset_filter = clause
    mock_svc.put_conf = AsyncMock(return_value=conf_row)

    resp = await client.put(
        f"{_BASE}/attr/conf",
        json={"is_enabled": False, "dataset_filter": clause},
        headers=auth_headers(),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["dataset_filter"] == clause
    assert mock_svc.put_conf.await_args.args[0]["dataset_filter"] == clause, (
        "the clause must reach the service unmodified — the backend owns the grammar, "
        "so the route neither rewrites nor normalises it"
    )


@pytest.mark.asyncio
async def test_put_conf_with_a_malformed_filter_returns_422_invalid_dataset_filter(
    client, mock_svc: AsyncMock
) -> None:
    """Spec: spec/API.md §Error Catalogue — INVALID_DATASET_FILTER, 422, "`detail`
    carries the character position of the error"."""
    resp = await client.put(
        f"{_BASE}/attr/conf",
        json={"is_enabled": False, "dataset_filter": "owner = 'alice'"},
        headers=auth_headers(),
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body.get("error_code") == "INVALID_DATASET_FILTER", body
    assert "position" in (body.get("detail") or {}), (
        f"the 422 body must carry the character position; got {body!r}"
    )


@pytest.mark.asyncio
async def test_put_conf_with_malformed_urn_returns_422_invalid_dataset_urn(
    client, mock_svc: AsyncMock
) -> None:
    """PUT /ontogen/attr/conf with malformed dataset_urns returns 422 INVALID_DATASET_URN.

    UC3 now validates URN format at the schema layer (unified with UC5). The handler
    for InvalidDatasetUrnError maps it to 422 with error_code='INVALID_DATASET_URN'.

    Spec: spec/API.md §Error Catalogue — 422 INVALID_DATASET_URN for malformed URNs.
    Spec: spec/API.md §Ontology Generation — URN format validated at schema layer.
    """
    resp = await client.put(
        f"{_BASE}/attr/conf",
        json={
            "is_enabled": False,
            "dataset_filter": "dataset_urn = 'not-a-valid-urn'",
        },
        headers=auth_headers(),
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
        json={"dataset_filter": "dataset_urn = 'not-a-valid-urn'"},
        headers=auth_headers(),
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
