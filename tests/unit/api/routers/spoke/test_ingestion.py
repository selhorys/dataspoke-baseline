"""Unit tests for /spoke/ingestion/* routes (cross-dataset ingestion router).

Routes under test:
  GET    /spoke/ingestion/sources
  POST   /spoke/ingestion/sources
  GET    /spoke/ingestion/sources/{id}
  PUT    /spoke/ingestion/sources/{id}
  PATCH  /spoke/ingestion/sources/{id}
  DELETE /spoke/ingestion/sources/{id}
  POST   /spoke/ingestion/sources/{id}/method/run
  GET    /spoke/ingestion/sources/{id}/datasets
  GET    /spoke/ingestion/sources/{id}/event
  GET    /spoke/ingestion/unmanaged
  GET    /spoke/ingestion/secrets

Spec traceability:
  spec/API.md §Ingestion (/spoke/ingestion)
  spec/feature/BACKEND.md §Ingestion Control Service
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.api.dependencies import get_db, get_ingestion_service
from src.api.main import app
from src.backend.ingestion.service import (
    IngestionRunResult,
    IngestionService,
    IngestionSourceDatasetRecord,
    IngestionSourceRecord,
)
from src.shared.exceptions import (
    ConflictError,
    EntityNotFoundError,
    StorageUnavailableError,
)
from src.shared.secrets import SecretResolverUnavailable
from tests.unit.api.conftest import _make_mock_user, auth_headers
from tests.unit.conftest import route_db_execute

_BASE = "/api/v1/spoke/ingestion"
_SOURCE_ID = str(uuid.uuid4())
_URN = "urn:li:dataset:(urn:li:dataPlatform:postgres,example_db.catalog.title_master,DEV)"
_VALID_RECIPE = {"source": {"type": "postgres", "config": {"host_port": "db:5432"}}}


def _make_source_record(**over) -> IngestionSourceRecord:
    now = datetime.now(tz=UTC)
    defaults = dict(
        id=_SOURCE_ID,
        mode="ACTIVE_CUSTOM_MANAGED",
        name="catalog postgres",
        platform="postgres",
        recipe=_VALID_RECIPE,
        schedule="0 * * * *",
        schedule_tier="hourly",
        datahub_source_urn=None,
        parent_source_id=None,
        status="OK",
        created_at=now,
        updated_at=now,
    )
    defaults.update(over)
    return IngestionSourceRecord(**defaults)


def _make_dataset_record(**over) -> IngestionSourceDatasetRecord:
    now = datetime.now(tz=UTC)
    defaults = dict(
        source_id=_SOURCE_ID,
        dataset_urn=_URN,
        derivation="emitted",
        first_seen_at=now,
        last_seen_at=now,
    )
    defaults.update(over)
    return IngestionSourceDatasetRecord(**defaults)


def _make_run_result(**over) -> IngestionRunResult:
    defaults = dict(
        run_id=str(uuid.uuid4()),
        status="success",
        dry_run=False,
        discovered_urns=[_URN],
        emitted_urns=[_URN],
        errors=[],
        warnings=[],
    )
    defaults.update(over)
    return IngestionRunResult(**defaults)


@pytest.fixture
def mock_svc() -> AsyncMock:
    return AsyncMock(spec=IngestionService)


@pytest.fixture(autouse=True)
def override_service(mock_svc: AsyncMock):
    app.dependency_overrides[get_ingestion_service] = lambda: mock_svc
    yield
    app.dependency_overrides.pop(get_ingestion_service, None)


async def _reader_request(client, method: str, url: str, **kwargs):
    """Issue a request whose authenticated principal resolves to a Reader.

    Overrides ``get_db`` so the ``require_authenticated`` user-lookup returns a
    Reader row; the role-gate dependency then decides 403 vs pass.
    """
    reader = _make_mock_user(role="Reader")
    auth_m = MagicMock()
    auth_m.scalar_one_or_none.return_value = reader
    mock_db = AsyncMock()
    route_db_execute(mock_db, [("users", auth_m)], default=MagicMock())

    app.dependency_overrides[get_db] = lambda: mock_db
    try:
        return await client.request(method, url, headers=auth_headers(), **kwargs)
    finally:
        app.dependency_overrides.pop(get_db, None)


# ── 401 without token ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("GET", f"{_BASE}/sources"),
        ("POST", f"{_BASE}/sources"),
        ("GET", f"{_BASE}/sources/{_SOURCE_ID}"),
        ("POST", f"{_BASE}/sources/{_SOURCE_ID}/method/run"),
        ("GET", f"{_BASE}/unmanaged"),
        ("GET", f"{_BASE}/secrets"),
    ],
)
async def test_route_without_token_returns_401(client, method, url) -> None:
    """Every /ingestion route rejects an unauthenticated request.

    Spec: API.md §Authentication — all /spoke routes require a valid JWT.
    """
    resp = await client.request(method, url)
    assert resp.status_code == 401, (
        f"{method} {url} without a token must return 401, got {resp.status_code}"
    )


# ── GET /sources (list + mode filter) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_sources_returns_200_paginated_envelope(client, mock_svc: AsyncMock) -> None:
    """GET /ingestion/sources returns 200 with a paginated envelope keyed by 'sources'.

    Spec: API.md §Ingestion — GET /spoke/ingestion/sources lists sources (paginated).
    """
    mock_svc.list_sources = AsyncMock(return_value=([_make_source_record()], 1))

    resp = await client.get(f"{_BASE}/sources", headers=auth_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] == 1
    assert len(body["sources"]) == 1
    assert body["sources"][0]["id"] == _SOURCE_ID
    assert body["sources"][0]["mode"] == "ACTIVE_CUSTOM_MANAGED"


@pytest.mark.asyncio
async def test_get_sources_forwards_mode_filter(client, mock_svc: AsyncMock) -> None:
    """GET /ingestion/sources?mode=PASSIVE forwards the mode filter to the service.

    Spec: API.md §Ingestion — the source list is filterable by mode.
    """
    mock_svc.list_sources = AsyncMock(return_value=([], 0))

    resp = await client.get(f"{_BASE}/sources?mode=PASSIVE", headers=auth_headers())

    assert resp.status_code == 200
    assert mock_svc.list_sources.call_args.kwargs.get("mode_filter") == "PASSIVE"


@pytest.mark.asyncio
async def test_get_sources_invalid_mode_returns_422(client, mock_svc: AsyncMock) -> None:
    """GET /ingestion/sources?mode=BOGUS returns 422 (enum validation).

    Spec: API.md §Ingestion — mode is the Mode enum (DATAHUB_MANAGED | ACTIVE_CUSTOM_MANAGED
    | PASSIVE).
    """
    resp = await client.get(f"{_BASE}/sources?mode=BOGUS", headers=auth_headers())
    assert resp.status_code == 422


# ── POST /sources (create) ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_source_returns_201_with_body(client, mock_svc: AsyncMock) -> None:
    """POST /ingestion/sources returns 201 with the created source body.

    Spec: API.md §Ingestion — POST /spoke/ingestion/sources creates a source.
    """
    mock_svc.create_source = AsyncMock(return_value=_make_source_record(name="new src"))

    resp = await client.post(
        f"{_BASE}/sources",
        json={"mode": "ACTIVE_CUSTOM_MANAGED", "name": "new src", "recipe": _VALID_RECIPE},
        headers=auth_headers(),
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["name"] == "new src"
    assert "id" in body


@pytest.mark.asyncio
async def test_post_source_datahub_managed_returns_409_readonly(
    client, mock_svc: AsyncMock
) -> None:
    """POST /ingestion/sources with DATAHUB_MANAGED returns 409 INGESTION_SOURCE_READONLY.

    Spec: API.md §Ingestion — DATAHUB_MANAGED is synced, not created (read-only).
    """
    mock_svc.create_source = AsyncMock(
        side_effect=ConflictError("INGESTION_SOURCE_READONLY", "read-only")
    )

    resp = await client.post(
        f"{_BASE}/sources",
        json={"mode": "DATAHUB_MANAGED", "name": "x", "recipe": _VALID_RECIPE},
        headers=auth_headers(),
    )

    assert resp.status_code == 409
    assert resp.json()["error_code"] == "INGESTION_SOURCE_READONLY"


@pytest.mark.asyncio
async def test_post_source_missing_recipe_returns_422(client, mock_svc: AsyncMock) -> None:
    """POST /ingestion/sources without `recipe` returns 422 (schema validation).

    Spec: API.md §Ingestion — recipe is a required field of the create request.
    """
    resp = await client.post(
        f"{_BASE}/sources",
        json={"mode": "ACTIVE_CUSTOM_MANAGED", "name": "x"},
        headers=auth_headers(),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_source_reader_returns_403_read_only_role(client) -> None:
    """POST /ingestion/sources by a Reader returns 403 READ_ONLY_ROLE (write gate).

    Spec: API.md §Error Catalogue — READ_ONLY_ROLE 403 for Reader on any write method
    on /spoke/*.
    """
    resp = await _reader_request(
        client,
        "POST",
        f"{_BASE}/sources",
        json={"mode": "ACTIVE_CUSTOM_MANAGED", "name": "x", "recipe": _VALID_RECIPE},
    )
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "READ_ONLY_ROLE"


# ── GET /sources/{id} ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_source_returns_200(client, mock_svc: AsyncMock) -> None:
    """GET /ingestion/sources/{id} returns 200 with the source body.

    Spec: API.md §Ingestion — GET /spoke/ingestion/sources/{id}.
    """
    mock_svc.get_source = AsyncMock(return_value=_make_source_record())

    resp = await client.get(f"{_BASE}/sources/{_SOURCE_ID}", headers=auth_headers())

    assert resp.status_code == 200
    assert resp.json()["id"] == _SOURCE_ID


@pytest.mark.asyncio
async def test_get_source_not_found_returns_404(client, mock_svc: AsyncMock) -> None:
    """GET /ingestion/sources/{id} returns 404 INGESTION_SOURCE_NOT_FOUND when absent.

    Spec: API.md §Error Catalogue — 404 INGESTION_SOURCE_NOT_FOUND.
    """
    mock_svc.get_source = AsyncMock(
        side_effect=EntityNotFoundError("ingestion_source", _SOURCE_ID)
    )

    resp = await client.get(f"{_BASE}/sources/{_SOURCE_ID}", headers=auth_headers())

    assert resp.status_code == 404
    assert resp.json()["error_code"] == "INGESTION_SOURCE_NOT_FOUND"


# ── DELETE /sources/{id} ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_source_returns_204(client, mock_svc: AsyncMock) -> None:
    """DELETE /ingestion/sources/{id} returns 204 No Content.

    Spec: API_DESIGN_PRINCIPLE_en.md §HTTP method semantics — DELETE returns 204.
    """
    mock_svc.delete_source = AsyncMock(return_value=None)

    resp = await client.delete(f"{_BASE}/sources/{_SOURCE_ID}", headers=auth_headers())

    assert resp.status_code == 204


@pytest.mark.asyncio
async def test_delete_source_reader_returns_403(client) -> None:
    """DELETE /ingestion/sources/{id} by a Reader returns 403 READ_ONLY_ROLE.

    Spec: API.md §Error Catalogue — READ_ONLY_ROLE 403 for Reader on write methods.
    """
    resp = await _reader_request(client, "DELETE", f"{_BASE}/sources/{_SOURCE_ID}")
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "READ_ONLY_ROLE"


# ── POST /sources/{id}/method/run ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_run_returns_200_with_run_envelope(client, mock_svc: AsyncMock) -> None:
    """POST run returns 200 with run_id/status and a detail carrying discovered/emitted URNs.

    Spec: API.md §Ingestion — run response surfaces run_id + status; detail carries
    discovered_urns / emitted_urns (and counts).
    """
    mock_svc.run = AsyncMock(return_value=_make_run_result())

    resp = await client.post(
        f"{_BASE}/sources/{_SOURCE_ID}/method/run", json={}, headers=auth_headers()
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "success"
    assert "run_id" in body
    assert body["detail"]["discovered_urns"] == [_URN]
    assert body["detail"]["emitted_urns"] == [_URN]
    assert body["detail"]["dry_run"] is False


@pytest.mark.asyncio
async def test_post_run_dry_run_forwarded(client, mock_svc: AsyncMock) -> None:
    """POST run?dry_run=true forwards dry_run=True to the service.

    Spec: API.md §Ingestion — ?dry_run=true runs a connection check + discovery preview.
    """
    mock_svc.run = AsyncMock(return_value=_make_run_result(dry_run=True, emitted_urns=[]))

    resp = await client.post(
        f"{_BASE}/sources/{_SOURCE_ID}/method/run?dry_run=true", headers=auth_headers()
    )

    assert resp.status_code == 200
    assert mock_svc.run.call_args.kwargs.get("dry_run") is True
    assert resp.json()["detail"]["dry_run"] is True


@pytest.mark.asyncio
async def test_post_run_returns_409_when_running(client, mock_svc: AsyncMock) -> None:
    """POST run returns 409 INGESTION_RUNNING when a concurrent run is in progress.

    Spec: API.md §Ingestion — concurrent runs return 409 INGESTION_RUNNING.
    """
    mock_svc.run = AsyncMock(side_effect=ConflictError("INGESTION_RUNNING", "already running"))

    resp = await client.post(
        f"{_BASE}/sources/{_SOURCE_ID}/method/run", json={}, headers=auth_headers()
    )

    assert resp.status_code == 409
    assert resp.json()["error_code"] == "INGESTION_RUNNING"


@pytest.mark.asyncio
async def test_post_run_returns_409_not_applicable(client, mock_svc: AsyncMock) -> None:
    """POST run on a non-ACTIVE_CUSTOM_MANAGED source returns 409 INGESTION_RUN_NOT_APPLICABLE.

    Spec: API.md §Error Catalogue — 409 INGESTION_RUN_NOT_APPLICABLE.
    """
    mock_svc.run = AsyncMock(
        side_effect=ConflictError("INGESTION_RUN_NOT_APPLICABLE", "not applicable")
    )

    resp = await client.post(
        f"{_BASE}/sources/{_SOURCE_ID}/method/run", json={}, headers=auth_headers()
    )

    assert resp.status_code == 409
    assert resp.json()["error_code"] == "INGESTION_RUN_NOT_APPLICABLE"


# ── GET /sources/{id}/datasets ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_source_datasets_returns_200_with_authority(
    client, mock_svc: AsyncMock
) -> None:
    """GET /ingestion/sources/{id}/datasets returns 200 with rows carrying authority + derivation.

    Seeds an 'emitted' mapping and asserts its derived authority is 'high'.

    Spec: API.md §Ingestion — each mapping row carries authority + derivation.
    """
    mock_svc.list_datasets_for_source = AsyncMock(
        return_value=([_make_dataset_record(derivation="emitted")], 1)
    )

    resp = await client.get(f"{_BASE}/sources/{_SOURCE_ID}/datasets", headers=auth_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] == 1
    row = body["datasets"][0]
    assert row["dataset_urn"] == _URN
    assert row["derivation"] == "emitted"
    assert row["authority"] == "high"


# ── GET /sources/{id}/event ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_source_event_returns_200_envelope(client, mock_svc: AsyncMock) -> None:
    """GET /ingestion/sources/{id}/event returns 200 with a paginated events envelope.

    Seeds one event row and asserts it surfaces with its derived wrapper flag.

    Spec: API.md §Ingestion — run/event history per source; each row carries wrapper: bool.
    """
    now = datetime.now(tz=UTC)
    event = {
        "id": uuid.uuid4(),
        "entity_type": "ingestion",
        "entity_id": _SOURCE_ID,
        "event_type": "INGESTION.COMPLETE",
        "status": "success",
        "detail": {"emitted_urns_count": 1},
        "occurred_at": now,
        "wrapper": False,
    }
    mock_svc.get_events_for_source = AsyncMock(return_value=([event], 1))

    resp = await client.get(f"{_BASE}/sources/{_SOURCE_ID}/event", headers=auth_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] == 1
    assert body["events"][0]["event_type"] == "INGESTION.COMPLETE"
    assert body["events"][0]["wrapper"] is False


# ── GET /unmanaged (DB-direct route) ──────────────────────────────────────────


async def _run_unmanaged_route(client, url: str) -> tuple[int, dict]:
    """Drive the DB-direct /unmanaged route with a mocked db session.

    The route queries the DB directly (no service method). The auth middleware
    issues one user-lookup query before the route's count + rows queries.
    """
    auth_m = MagicMock()
    auth_m.scalar_one_or_none.return_value = _make_mock_user()
    count_m = MagicMock()
    count_m.scalar.return_value = 1
    rows_m = MagicMock()
    rows_m.all.return_value = [(_URN,)]

    mock_db_session = AsyncMock()
    route_db_execute(
        mock_db_session,
        [("users", auth_m), ("count(", count_m)],
        default=rows_m,
    )

    app.dependency_overrides[get_db] = lambda: mock_db_session
    try:
        resp = await client.get(url, headers=auth_headers())
    finally:
        app.dependency_overrides.pop(get_db, None)
    return resp.status_code, resp.json()


@pytest.mark.asyncio
async def test_get_unmanaged_returns_200_with_urns(client) -> None:
    """GET /ingestion/unmanaged returns 200 with the unmanaged dataset_urns envelope.

    Seeds one registered-but-unmapped URN and asserts it appears in the bucket.

    Spec: API.md §Ingestion — /spoke/ingestion/unmanaged lists DataHub datasets covered
    by no ingestion source.
    """
    status_code, body = await _run_unmanaged_route(client, f"{_BASE}/unmanaged")

    assert status_code == 200
    assert body["total_count"] == 1
    assert body["dataset_urns"] == [_URN]


# ── GET /secrets (Editor-gated) ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_secrets_returns_200_with_refs(client, monkeypatch) -> None:
    """GET /ingestion/secrets returns 200 with (ref, secret_name, key) rows, no values.

    Spec: API.md §Ingestion — one row per (secret, key) under the dataspoke-source-cred-
    prefix as {ref, secret_name, key}; values are never returned.
    """
    ref = MagicMock()
    ref.ref, ref.secret_name, ref.key = "pg__password", "dataspoke-source-cred-pg", "password"
    monkeypatch.setattr(
        "src.api.routers.spoke.ingestion.list_source_cred_refs", lambda: [ref]
    )

    resp = await client.get(f"{_BASE}/secrets", headers=auth_headers())

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_count"] == 1
    row = body["secrets"][0]
    assert row["ref"] == "pg__password"
    assert row["secret_name"] == "dataspoke-source-cred-pg"
    assert row["key"] == "password"
    # The value itself is never exposed on the secrets listing.
    assert "value" not in row


@pytest.mark.asyncio
async def test_get_secrets_resolver_unavailable_returns_503(client, monkeypatch) -> None:
    """GET /ingestion/secrets returns 503 STORAGE_UNAVAILABLE when the k8s resolver is down.

    Spec: API.md §Ingestion — 503 STORAGE_UNAVAILABLE when the in-cluster k8s config is
    not loadable or the k8s API is unreachable.
    """

    def _boom():
        raise SecretResolverUnavailable("no in-cluster config")

    monkeypatch.setattr("src.api.routers.spoke.ingestion.list_source_cred_refs", _boom)

    resp = await client.get(f"{_BASE}/secrets", headers=auth_headers())

    assert resp.status_code == 503
    assert resp.json()["error_code"] == StorageUnavailableError.error_code


@pytest.mark.asyncio
async def test_get_secrets_reader_returns_403(client) -> None:
    """GET /ingestion/secrets by a Reader returns 403 READ_ONLY_ROLE (Editor-gated read).

    Spec: API.md §Ingestion — GET /spoke/ingestion/secrets requires Editor or Admin
    (403 READ_ONLY_ROLE for Reader), the exception to the Reader-GET rule.
    """
    resp = await _reader_request(client, "GET", f"{_BASE}/secrets")
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "READ_ONLY_ROLE"
